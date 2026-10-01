from datetime import datetime, timedelta, timezone
from dataclasses import replace
from contextlib import closing
import http.client
import json
import os
import socket
import sqlite3
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from openswap.worker.protocol import Artifact, ProtocolError, Submission
from openswap.worker.models import JobSubmission, JobState, SafeEvent, SafeEventKind
from openswap.worker.refserver import ControlStore, make_server
from openswap.worker.refserver import cli as refserver_cli
from openswap.worker import cli


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "private" / "server.sqlite3"
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(path, clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    key = paired["device_key"]
    epoch = store.request("register", {}, key)["worker_epoch"]
    store.request("heartbeat", {"worker_epoch": epoch}, key)
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
    with pytest.raises(ProtocolError, match="forbidden"):
        store.request("submit", payload, other["device_key"])
    # Another owner's job is indistinguishable from an unknown ID: no existence oracle.
    for operation, data in [("job", {"job_id": result["job_id"]}), ("cancel", {"job_id": result["job_id"]}),
                            ("artifacts", {"job_id": result["job_id"]}), ("job", {"job_id": "missing"})]:
        with pytest.raises(ProtocolError, match="not_found"):
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


def test_heartbeat_lists_only_cancellations_still_needing_enforcement(service):
    store, ticks, _, key, epoch = service
    queued, _ = submit(service, "queued")
    assert store.request("cancel", {"job_id": queued["job_id"]}, key)["state"] == "cancelled"
    finished, _ = submit(service, "finished")
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert claim["job_id"] == finished["job_id"]
    store.request("cancel", {"job_id": finished["job_id"]}, key)
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == [finished["job_id"]]
    store.request("reconcile", {**fence(service, claim), "state": "cancelled", "execution_stopped": True,
                                "unlaunched": False}, key)
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == []
    active, _ = submit(service, "active")
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    store.request("cancel", {"job_id": active["job_id"]}, key)
    ticks[0] += 16  # heartbeat loss interrupts the job; the flag must still be enforced on reconnect
    assert store.request("job", {"job_id": active["job_id"]}, key)["state"] == "interrupted"
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == [active["job_id"]]


def test_event_driven_cancel_requested_sets_the_flag(service):
    store, ticks, _, key, epoch = service
    submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert store.request("renew", fence(service, claim), key)["cancel_requested"] is False
    event = SafeEvent(claim["job_id"], 1, datetime.fromtimestamp(ticks[0], timezone.utc),
                      SafeEventKind.STATE_CHANGED, JobState.CANCEL_REQUESTED).to_dict()
    store.request("events", {**fence(service, claim), "after_cursor": 0, "events": [event]}, key)
    assert store.request("job", {"job_id": claim["job_id"]}, key) == {
        **store.request("job", {"job_id": claim["job_id"]}, key), "state": "cancel_requested", "cancel_requested": True}
    assert store.request("renew", fence(service, claim), key)["cancel_requested"] is True
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == [claim["job_id"]]


def test_upload_before_success_is_invalid_state(service):
    store, _, _, key, epoch = service
    submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    artifact = Artifact("result.md", b"early").to_dict()
    with pytest.raises(ProtocolError, match="invalid_state"):
        store.request("upload", {**fence(service, claim), "artifact": artifact}, key)
    store.request("reconcile", {**fence(service, claim), "state": "failed", "execution_stopped": True,
                                "unlaunched": False}, key)
    with pytest.raises(ProtocolError, match="invalid_state"):
        store.request("upload", {**fence(service, claim), "artifact": artifact}, key)
    assert store.request("artifacts", {"job_id": claim["job_id"]}, key)["artifacts"] == []


def test_register_bumps_epoch_and_interrupts_the_active_claim(service):
    store, _, _, key, epoch = service
    submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    registered = store.request("register", {}, key)
    assert registered["worker_epoch"] == epoch + 1
    assert store.request("job", {"job_id": claim["job_id"]}, key)["state"] == "interrupted"
    with pytest.raises(ProtocolError, match="stale_epoch"):
        store.request("renew", fence(service, claim), key)
    with pytest.raises(ProtocolError, match="stale_epoch"):
        store.request("poll", {"worker_epoch": epoch}, key)
    store.request("heartbeat", {"worker_epoch": registered["worker_epoch"]}, key)
    assert store.request("poll", {"worker_epoch": registered["worker_epoch"]}, key)["claim"] is None


def test_concurrent_polls_grant_exactly_one_claim(service):
    store, _, _, key, epoch = service
    submit(service)
    barrier, results, errors = threading.Barrier(2), [], []

    def poll():
        try:
            barrier.wait(2)
            results.append(store.request("poll", {"worker_epoch": epoch}, key)["claim"])
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=poll) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors and len(results) == 2 and results[0] == results[1]
    assert results[0]["epoch"] == 1
    assert store.request("job", {"job_id": results[0]["job_id"]}, key)["epoch"] == 1


def test_cli_reports_sqlite_errors_and_restores_umask(tmp_path, capsys, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "not-a-database.sqlite3").mkdir(mode=0o700)
    before = os.umask(0)
    os.umask(before)
    assert refserver_cli.main(["pair-code", "--database", str(private / "not-a-database.sqlite3")]) == 1
    assert "unavailable" in capsys.readouterr().err
    seen = {}

    class Recording:
        def __init__(self, path):
            seen["umask"] = os.umask(0)
            os.umask(seen["umask"])

        def issue_code(self, worker_id=None):
            return "code"

    monkeypatch.setattr(refserver_cli, "ControlStore", Recording)
    assert refserver_cli.main(["pair-code", "--database", str(private / "service.sqlite3")]) == 0
    assert seen["umask"] == 0o077
    after = os.umask(0)
    os.umask(after)
    assert after == before


def _raw_request(port, request_bytes, timeout=2):
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request_bytes)
        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status, dict(response.getheaders()), json.loads(response.read())


def test_http_auth_is_checked_before_the_body_is_read(service):
    store = service[0]
    with make_server(store, port=0) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        port = server.server_port
        try:
            # Content-Length promises 40 bytes that never arrive; a body read would stall for the
            # 5 s socket timeout, so a prompt 401 proves the credential check came first.
            for auth in ("", "Authorization: Basic abc\r\n", "Authorization: Bearer \r\n",
                         "Authorization: Bearer a\r\nAuthorization: Bearer b\r\n",
                         "Authorization: Bearer " + "k" * 201 + "\r\n"):
                request = (f"POST /v1/register HTTP/1.1\r\nHost: x\r\n{auth}Content-Type: application/json\r\n"
                           "Content-Length: 40\r\n\r\n").encode()
                status, headers, body = _raw_request(port, request)
                assert (status, body) == (401, {"error": "unauthorized"})
                assert headers["Server"] == "OpenSwapReference/1"
            status, _, body = _raw_request(port, b"POST /v1/pair HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                                                b"Content-Length: 40\r\n\r\n", timeout=8)
            assert (status, body) == (503, {"error": "service_unavailable"})
            for method in ("GET", "HEAD", "PUT", "DELETE"):
                status, headers, body = _raw_request(port, f"{method} /v1/register HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                assert (status, headers["Allow"], body) == (405, "POST", {"error": "invalid_request"})
            key = store.request("pair", {"code": store.issue_code()})["device_key"]
            headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
            with urlopen(Request(f"http://127.0.0.1:{port}/v1/register", b"{}", headers), timeout=2) as response:
                assert json.load(response)["worker_epoch"] == 1

            def explode(*_):
                raise RuntimeError("bug")

            server.store = type("Broken", (), {"request": staticmethod(explode)})()
            with pytest.raises(HTTPError) as failure:
                urlopen(Request(f"http://127.0.0.1:{port}/v1/register", b"{}", headers), timeout=2)
            assert failure.value.code == 500 and json.load(failure.value) == {"error": "service_unavailable"}
            failure.value.close()
        finally:
            server.shutdown()
            thread.join(2)
            assert not thread.is_alive()


def test_register_alone_leaves_the_worker_offline_until_the_first_heartbeat(tmp_path):
    path = tmp_path / "private" / "server.sqlite3"
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(path, clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    key = paired["device_key"]
    epoch = store.request("register", {}, key)["worker_epoch"]
    service = store, ticks, paired["worker_id"], key, epoch
    with pytest.raises(ProtocolError, match="offline_worker"):
        submit(service)
    with pytest.raises(ProtocolError, match="offline_worker"):
        store.request("poll", {"worker_epoch": epoch}, key)
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    submit(service)
    assert store.request("poll", {"worker_epoch": epoch}, key)["claim"] is not None
    ticks[0] += 16  # a re-registration after heartbeat loss must not revive liveness either
    epoch = store.request("register", {}, key)["worker_epoch"]
    with pytest.raises(ProtocolError, match="offline_worker"):
        submit((store, ticks, paired["worker_id"], key, epoch), "second")
    with pytest.raises(ProtocolError, match="offline_worker"):
        store.request("poll", {"worker_epoch": epoch}, key)
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    submit((store, ticks, paired["worker_id"], key, epoch), "second")


def test_confirmed_interruption_is_terminal_and_leaves_the_cancel_list(service):
    store, ticks, _, key, epoch = service
    result, _ = submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    store.request("cancel", {"job_id": result["job_id"]}, key)
    ticks[0] += 16  # heartbeat loss: a provisional interruption
    assert store.request("job", {"job_id": result["job_id"]}, key)["state"] == "interrupted"
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == [result["job_id"]]
    uncertain = {**fence(service, claim), "state": "interrupted", "execution_stopped": False, "unlaunched": False}
    assert store.request("reconcile", uncertain, key)["state"] == "interrupted"
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == [result["job_id"]]
    confirmed = {**uncertain, "execution_stopped": True}
    assert store.request("reconcile", confirmed, key)["state"] == "interrupted"
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == []
    assert store.request("reconcile", confirmed, key)["state"] == "interrupted"
    assert store.request("reconcile", uncertain, key)["state"] == "interrupted"
    for state in ("succeeded", "failed", "cancelled"):
        with pytest.raises(ProtocolError, match="invalid_state"):
            store.request("reconcile", {**confirmed, "state": state}, key)
    assert store.request("job", {"job_id": result["job_id"]}, key)["state"] == "interrupted"
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == []


def test_provisional_interruption_still_yields_to_the_true_outcome(service):
    store, ticks, _, key, epoch = service
    result, _ = submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    store.request("cancel", {"job_id": result["job_id"]}, key)
    ticks[0] += 16
    assert store.request("job", {"job_id": result["job_id"]}, key)["state"] == "interrupted"
    uncertain = {**fence(service, claim), "state": "interrupted", "execution_stopped": False, "unlaunched": False}
    store.request("reconcile", uncertain, key)
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == [result["job_id"]]
    assert store.request("reconcile", {**uncertain, "state": "cancelled", "execution_stopped": True}, key)["state"] == "cancelled"
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == []
    with pytest.raises(ProtocolError, match="invalid_state"):
        store.request("reconcile", {**uncertain, "state": "interrupted", "unlaunched": True}, key)
    # An unlaunched confirmation is as final as a stopped one.
    second, _ = submit(service, "second")
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    ticks[0] += 16
    store.request("reconcile", {**fence(service, claim), "state": "interrupted", "execution_stopped": False,
                                "unlaunched": True}, key)
    with pytest.raises(ProtocolError, match="invalid_state"):
        store.request("reconcile", {**fence(service, claim), "state": "expired", "execution_stopped": False,
                                    "unlaunched": True}, key)


def test_existing_database_gains_the_confirmed_column(tmp_path):
    path = tmp_path / "private" / "server.sqlite3"
    path.parent.mkdir(mode=0o700)
    with closing(sqlite3.connect(path)) as db, db:  # a jobs table written before the column existed
        db.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, device TEXT NOT NULL, idem TEXT NOT NULL, "
                   "payload TEXT NOT NULL, state TEXT NOT NULL, expiry REAL NOT NULL, epoch INTEGER NOT NULL DEFAULT 0, "
                   "lease REAL, cursor INTEGER NOT NULL DEFAULT 0, cancel INTEGER NOT NULL DEFAULT 0, UNIQUE(device,idem))")
    os.chmod(path, 0o600)
    store = ControlStore(path)
    with closing(store.connect()) as db:
        assert "confirmed" in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    ControlStore(path)  # reopening is idempotent


def test_register_clears_the_previous_incarnations_liveness(service):
    store, ticks, _, key, epoch = service
    ticks[0] += 1  # the old heartbeat is still fresh
    epoch = store.request("register", {}, key)["worker_epoch"]
    with pytest.raises(ProtocolError, match="offline_worker"):
        store.request("poll", {"worker_epoch": epoch}, key)
    with pytest.raises(ProtocolError, match="offline_worker"):
        submit(service)
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    assert store.request("poll", {"worker_epoch": epoch}, key) == {"claim": None}


def test_late_cancel_leaves_a_confirmed_interruption_alone(service):
    store, _, _, key, epoch = service
    submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    store.request("reconcile", {**fence(service, claim), "state": "interrupted", "execution_stopped": True,
                                "unlaunched": False}, key)
    assert store.request("cancel", {"job_id": claim["job_id"]}, key)["state"] == "interrupted"
    assert store.request("job", {"job_id": claim["job_id"]}, key)["cancel_requested"] is False
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == []


@pytest.mark.parametrize("kind", [SafeEventKind.PROVIDER_STARTED, SafeEventKind.STOP_REQUESTED,
                                  SafeEventKind.DIAGNOSTIC])
def test_only_state_changed_events_move_the_job(service, kind):
    store, ticks, _, key, epoch = service
    submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    event = SafeEvent(claim["job_id"], 1, datetime.fromtimestamp(ticks[0], timezone.utc),
                      kind, JobState.CANCEL_REQUESTED).to_dict()
    store.request("events", {**fence(service, claim), "after_cursor": 0, "events": [event]}, key)
    job = store.request("job", {"job_id": claim["job_id"]}, key)
    assert (job["state"], job["cancel_requested"]) == ("claimed", False)


def test_heartbeat_cancel_list_is_bounded_newest_first(service, monkeypatch):
    from openswap.worker.refserver import store as store_module
    monkeypatch.setattr(store_module, "MAX_CANCEL_IDS", 3)
    store, _, _, key, epoch = service
    ids = []
    for n in range(5):  # cancel -> register cycles leave unconfirmed cancelled interruptions behind
        submit(service, f"k{n}")
        claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
        store.request("cancel", {"job_id": claim["job_id"]}, key)
        ids.append(claim["job_id"])
        epoch = store.request("register", {}, key)["worker_epoch"]
        store.request("heartbeat", {"worker_epoch": epoch}, key)
    submit(service, "live")
    live = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    store.request("cancel", {"job_id": live["job_id"]}, key)
    listed = store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"]
    assert listed == [live["job_id"], ids[4], ids[3]]


def test_renewal_code_rotates_the_key_and_keeps_the_worker(service):
    store, ticks, worker_id, key, epoch = service
    job, _ = submit(service)
    ticks[0] += 29 * 86400
    renewed = store.request("pair", {"code": store.issue_code(worker_id)})
    assert renewed["worker_id"] == worker_id and renewed["device_key"] != key
    with pytest.raises(ProtocolError, match="unauthorized"):
        store.request("register", {}, key)  # the old key stops working at once
    ticks[0] += 2 * 86400  # past the original expiry, inside the renewed one
    new_key = renewed["device_key"]
    assert store.request("job", {"job_id": job["job_id"]}, new_key)["job_id"] == job["job_id"]
    epoch = store.request("register", {}, new_key)["worker_epoch"]
    store.request("heartbeat", {"worker_epoch": epoch}, new_key)


def test_renewal_code_refuses_unknown_and_revoked_workers(service):
    store, _, worker_id, _, _ = service
    with pytest.raises(ProtocolError, match="not_found"):
        store.issue_code("nobody")
    code = store.issue_code(worker_id)
    store.revoke(worker_id)
    with pytest.raises(ProtocolError, match="invalid_code"):
        store.request("pair", {"code": code})  # revoked after the code was issued
    with pytest.raises(ProtocolError, match="revoked"):
        store.issue_code(worker_id)


def test_refserver_cli_issues_a_renewal_code(tmp_path, capsys):
    path = tmp_path / "private" / "server.sqlite3"
    store = ControlStore(path)
    worker_id = store.request("pair", {"code": store.issue_code()})["worker_id"]
    assert refserver_cli.main(["pair-code", "--database", str(path), "--renew", worker_id]) == 0
    code = capsys.readouterr().out.strip()
    assert store.request("pair", {"code": code})["worker_id"] == worker_id


def test_key_rotation_clears_the_previous_keys_liveness(service):
    store, ticks, worker_id, key, epoch = service
    ticks[0] += 1  # the old key's heartbeat is still fresh
    renewed = store.request("pair", {"code": store.issue_code(worker_id)})
    with pytest.raises(ProtocolError, match="offline_worker"):
        submit(service[:3] + (renewed["device_key"], epoch))


def test_cancel_requested_event_after_heartbeat_loss_still_sets_the_flag(service):
    store, ticks, _, key, epoch = service
    submit(service)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    ticks[0] += 16  # heartbeat loss: the job becomes interrupted
    assert store.request("job", {"job_id": claim["job_id"]}, key)["state"] == "interrupted"
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    event = SafeEvent(claim["job_id"], 1, datetime.fromtimestamp(ticks[0], timezone.utc),
                      SafeEventKind.STATE_CHANGED, JobState.CANCEL_REQUESTED).to_dict()
    store.request("events", {**fence(service, claim), "after_cursor": 0, "events": [event]}, key)
    job = store.request("job", {"job_id": claim["job_id"]}, key)
    assert (job["state"], job["cancel_requested"]) == ("interrupted", True)
    assert store.request("heartbeat", {"worker_epoch": epoch}, key)["cancel_job_ids"] == [claim["job_id"]]
