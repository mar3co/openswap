from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from openswap.worker.protocol import Artifact, ProtocolError, Submission
from openswap.worker.models import JobSubmission, JobState, SafeEvent, SafeEventKind
from openswap.worker.refserver import ControlStore, make_server
from openswap.worker import cli


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "private" / "server.sqlite3"
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(path, clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    key = paired["device_key"]
    epoch = store.request("register", {}, key)["worker_epoch"]
    return store, ticks, paired["worker_id"], key, epoch


def submit(service, key_id="test", expiry=None):
    store, ticks, worker_id, key, _ = service
    job = JobSubmission(key_id, "codex", "Research", "research", "research",
                        datetime.fromtimestamp(expiry or ticks[0] + 100, timezone.utc), 60)
    payload = Submission(worker_id, job).to_dict()
    return store.request("submit", payload, key), payload


def fence(service, claim):
    return {"worker_epoch": service[4], "job_id": claim["job_id"], "epoch": claim["epoch"]}


def test_pair_code_one_use_expiry_and_durable_enrollment(service):
    store, ticks, worker_id, key, epoch = service
    code = store.issue_code()
    store.request("pair", {"code": code})
    with pytest.raises(ProtocolError, match="invalid_code"):
        store.request("pair", {"code": code})
    expired = store.issue_code()
    ticks[0] += 601
    with pytest.raises(ProtocolError, match="invalid_code"):
        store.request("pair", {"code": expired})
    reopened = ControlStore(store.path, clock=lambda: ticks[0])
    assert reopened.request("register", {}, key)["worker_id"] == worker_id
    ticks[0] += 30 * 86400
    with pytest.raises(ProtocolError, match="device_expired"):
        reopened.request("register", {}, key)


def test_offline_rejection_and_idempotency(service):
    store, ticks, _, key, _ = service
    result, payload = submit(service)
    ticks[0] += 16
    assert store.request("submit", payload, key)["job_id"] == result["job_id"]
    with pytest.raises(ProtocolError, match="offline_worker"):
        submit(service, "new")
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        store.request("submit", {**payload, "task": "changed"}, key)


def test_owner_scope_revocation_and_auth(service):
    store, _, worker_id, key, _ = service
    result, payload = submit(service)
    other = store.request("pair", {"code": store.issue_code()})
    for operation, data in [("job", {"job_id": result["job_id"]}), ("submit", payload),
                            ("cancel", {"job_id": result["job_id"]}),
                            ("artifacts", {"job_id": result["job_id"]})]:
        with pytest.raises(ProtocolError, match="forbidden"):
            store.request(operation, data, other["device_key"])
    store.revoke(worker_id)
    for operation, data in [("job", {"job_id": result["job_id"]}), ("submit", payload),
                            ("poll", {"worker_epoch": service[4]})]:
        with pytest.raises(ProtocolError, match="revoked"):
            store.request(operation, data, key)
    with pytest.raises(ProtocolError, match="unauthorized"):
        store.request("register", {}, "unknown")


def test_claim_fences_heartbeat_interruption_and_reconciliation(service):
    store, ticks, _, key, epoch = service
    result, _ = submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert store.request("poll", {"worker_epoch": epoch}, key)["claim"] == claim
    with pytest.raises(ProtocolError, match="stale_epoch"):
        store.request("renew", {**fence(service, claim), "epoch": 2}, key)
    ticks[0] += 16
    assert store.request("job", {"job_id": result["job_id"]}, key)["state"] == "interrupted"
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    with pytest.raises(ProtocolError, match="lease_lost"):
        store.request("renew", fence(service, claim), key)
    report = {**fence(service, claim), "state": "succeeded", "execution_stopped": True, "unlaunched": False}
    assert store.request("reconcile", report, key)["state"] == "succeeded"
    assert store.request("reconcile", report, key)["state"] == "succeeded"
    with pytest.raises(ProtocolError, match="invalid_state"):
        store.request("reconcile", {**report, "state": "failed"}, key)
    store.request("register", {}, key)
    with pytest.raises(ProtocolError, match="stale_epoch"):
        store.request("reconcile", report, key)


def test_expiry_cancel_and_lost_lease(service):
    store, ticks, _, key, epoch = service
    result, _ = submit(service, expiry=ticks[0] + 1)
    ticks[0] += 2
    assert store.request("poll", {"worker_epoch": epoch}, key)["claim"] is None
    assert store.request("job", {"job_id": result["job_id"]}, key)["state"] == "expired"
    result, _ = submit(service, "second")
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert store.request("cancel", {"job_id": result["job_id"]}, key)["state"] == "cancel_requested"
    assert store.request("cancel", {"job_id": result["job_id"]}, key)["state"] == "cancel_requested"
    heartbeat = store.request("heartbeat", {"worker_epoch": epoch}, key)
    assert heartbeat["cancel_job_ids"] == [result["job_id"]]
    ticks[0] += 21
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    with pytest.raises(ProtocolError, match="lease_lost"):
        store.request("renew", fence(service, claim), key)


def test_events_replay_gaps_and_artifacts(service):
    store, ticks, _, key, epoch = service
    submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    event = SafeEvent(claim["job_id"], 1, datetime.fromtimestamp(ticks[0], timezone.utc),
                      SafeEventKind.PROVIDER_FINISHED, JobState.SUCCEEDED, execution_stopped=True).to_dict()
    body = {**fence(service, claim), "after_cursor": 0, "events": [event]}
    assert store.request("events", body, key)["next_cursor"] == 1
    assert store.request("events", body, key)["events"] == [event]
    with pytest.raises(ProtocolError, match="cursor_conflict"):
        store.request("events", {**body, "events": [{**event, "cursor": 3}]}, key)
    store.request("reconcile", {**fence(service, claim), "state": "succeeded", "execution_stopped": True, "unlaunched": False}, key)
    artifact = Artifact("result.md", b"cited result").to_dict()
    assert store.request("upload", {**fence(service, claim), "artifact": artifact}, key)["size"] == 12
    store.request("upload", {**fence(service, claim), "artifact": artifact}, key)
    with pytest.raises(ProtocolError, match="artifact_conflict"):
        store.request("upload", {**fence(service, claim), "artifact": Artifact("result.md", b"other").to_dict()}, key)
    reopened = ControlStore(store.path)
    assert reopened.request("artifacts", {"job_id": claim["job_id"], "name": "result.md"}, key)["artifact"] == artifact
    assert len(reopened.request("artifacts", {"job_id": claim["job_id"]}, key)["artifacts"]) == 1


def test_cli_operator_commands(tmp_path, capsys):
    path = tmp_path / "private" / "service.sqlite3"
    assert cli.main(["refserver", "pair-code", "--database", str(path)], backup_root=tmp_path) == 0
    code = capsys.readouterr().out.strip()
    assert code.startswith("pair_")  # always a positional CLI argument, never an option
    store = ControlStore(path)
    paired = store.request("pair", {"code": code})
    assert cli.main(["refserver", "revoke", "--database", str(path), paired["worker_id"]], backup_root=tmp_path) == 0
    with pytest.raises(ProtocolError, match="revoked"):
        store.request("register", {}, paired["device_key"])


def test_bind_policy(service):
    with pytest.raises(ValueError, match="TLS"):
        make_server(service[0], "0.0.0.0", 0)


def test_http_strict_parsing_and_pairing(service):
    store = service[0]
    with make_server(store, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/v1/pair"
        try:
            for body in (b'{"code":"x","code":"y"}', b'{"code":NaN}', b'[]'):
                with pytest.raises(HTTPError) as failure:
                    urlopen(Request(url, body, {"Content-Type": "application/json"}), timeout=2)
                assert failure.value.code == 400
                failure.value.close()
            body = json.dumps({"code": store.issue_code()}).encode()
            with urlopen(Request(url, body, {"Content-Type": "application/json"}), timeout=2) as response:
                assert json.load(response)["worker_id"]
        finally:
            server.shutdown()
            thread.join(2)
            assert not thread.is_alive()
