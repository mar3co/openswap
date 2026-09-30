from __future__ import annotations
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from openswap.settings import configure_worker_service, update_worker_settings
from openswap.worker import remote as remote_mod
from openswap.worker.journal import StaleWriteError
from openswap.worker.leases import stable_account_identity
from openswap.worker.models import (JobSubmission, JobState, ProviderAvailability, ProviderRun,
                                     SafeEvent, SafeEventKind, InterruptResult)
from openswap.worker.protocol import Artifact, MAX_ARTIFACT, MAX_BODY, ProtocolError, Submission
from openswap.worker.refserver import ControlStore, make_server
from openswap.worker.remote import RemoteClient, Transport, NoRedirect
from openswap.worker.runtime import WorkerRuntime


class FakeAdapter:
    """Inert provider contract; no provider executable or auth state."""
    def __init__(self):
        self.starts = 0
        self.job_id = None
        self.entered = threading.Event()
        self.finish = threading.Event()
        self.finish.set()

    def probe(self):
        return ProviderAvailability(True, None, "synthetic")

    def start(self, job, workspace, *, worker_epoch):
        self.starts += 1
        self.job_id = job.job_id
        (workspace.output_root / "result.md").write_text("Synthetic cited result [source](https://example.org).")
        self.entered.set()
        return ProviderRun(None, None, worker_epoch, job.generation)

    def events(self, run, *, after_cursor):
        self.finish.wait(5)
        return (SafeEvent(self.job_id, 1, datetime.now(timezone.utc), SafeEventKind.PROVIDER_FINISHED,
                          JobState.SUCCEEDED, execution_stopped=True),)

    def interrupt(self, run):
        self.finish.set()
        return InterruptResult(True, True)


class StoreTransport:
    def __init__(self, store, key):
        self.store, self.key = store, key
        self.unreachable = False
        self.calls = []
        self.requests = []
        self.drop_response = None
        self.reject = {}  # operation -> ProtocolError raised once instead of serving it

    def request(self, operation, data):
        self.calls.append(operation)
        self.requests.append((operation, data))
        if self.unreachable:
            raise ProtocolError("service_unavailable", 503)
        if operation in self.reject:
            raise self.reject.pop(operation)
        value = self.store.request(operation, data, self.key)
        if self.drop_response == operation:
            self.drop_response = None
            raise ProtocolError("service_unavailable", 503)
        return value


@pytest.fixture
def remote_setup(tmp_path):
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(tmp_path / "service" / "db", clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    root = tmp_path / "local"
    update_worker_settings(root, enabled=True)
    configure_worker_service(root, "http://127.0.0.1:8765")
    adapter = FakeAdapter()
    runtime = WorkerRuntime(root, adapter=adapter, account_identity=stable_account_identity("codex", "synthetic"))
    transport = StoreTransport(store, paired["device_key"])
    remote = RemoteClient(runtime, "http://127.0.0.1:8765", paired["device_key"], transport=transport)
    remote.tick()
    return remote, runtime, adapter, store, ticks, paired, transport


def submit(setup, idem="one", seconds=100):
    remote, _, _, store, ticks, paired, _ = setup
    job = JobSubmission(idem, "codex", "Research", "research", "research",
                        datetime.fromtimestamp(ticks[0] + seconds, timezone.utc), 60)
    return store.request("submit", Submission(paired["worker_id"], job).to_dict(), paired["device_key"])["job_id"]


def test_remote_roundtrip_and_explicit_result(remote_setup):
    remote, runtime, adapter, store, _, paired, _ = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    assert runtime.reconcile_once().state == JobState.SUCCEEDED
    remote.tick()
    key = paired["device_key"]
    assert store.request("job", {"job_id": job_id}, key)["state"] == "succeeded"
    assert store.request("events", {"job_id": job_id, "after_cursor": 0}, key)["next_cursor"] >= 4
    artifact = store.request("artifacts", {"job_id": job_id, "name": "result.md"}, key)["artifact"]
    assert b"Synthetic cited result" in Artifact.from_dict(artifact).content
    assert adapter.starts == 1
    assert remote.journal.pending() == []


def test_unreachable_running_job_continues_and_reconciles_interrupted(remote_setup):
    remote, runtime, adapter, store, ticks, paired, transport = remote_setup
    job_id = submit(remote_setup)
    submit(remote_setup, "next")
    remote.tick()
    adapter.finish.clear()
    results = []
    runner = threading.Thread(target=lambda: results.append(runtime.reconcile_once()))
    runner.start()
    assert adapter.entered.wait(2)
    transport.unreachable = True
    remote.tick()
    assert remote.state == "offline"
    assert adapter.starts == 1 and runner.is_alive()
    ticks[0] += 25
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "interrupted"
    adapter.finish.set()
    runner.join(2)
    assert not runner.is_alive() and results[0].state == JobState.SUCCEEDED
    transport.unreachable = False
    remote.tick()
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "succeeded"
    # The next claim may be admitted only after the first result was reconciled.
    assert adapter.starts == 1


def test_lease_loss_blocks_new_claims_without_stopping_execution(remote_setup):
    remote, runtime, adapter, _, ticks, _, transport = remote_setup
    submit(remote_setup)
    submit(remote_setup, "next")
    remote.tick()
    adapter.finish.clear()
    runner = threading.Thread(target=runtime.reconcile_once)
    runner.start()
    assert adapter.entered.wait(2)
    ticks[0] += 21
    remote.tick()
    assert "poll" not in transport.calls[-3:]
    assert adapter.starts == 1 and runner.is_alive()
    adapter.finish.set()
    runner.join(2)
    assert not runner.is_alive()
    remote.tick()
    assert remote.state == "online"


def test_revocation_stops_new_claims_and_never_kills_running_job(remote_setup):
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    submit(remote_setup)
    remote.tick()
    adapter.finish.clear()
    runner = threading.Thread(target=runtime.reconcile_once)
    runner.start()
    assert adapter.entered.wait(2)
    store.revoke(paired["worker_id"])
    remote.tick()
    assert remote.state == "revoked" and runner.is_alive()
    before = len(transport.calls)
    remote.tick()
    assert len(transport.calls) == before
    adapter.finish.set()
    runner.join(2)
    assert not runner.is_alive() and adapter.starts == 1


@pytest.mark.parametrize("trigger", ["cancel", "expiry", "revoked", "offline"])
def test_final_launch_fence_refuses_stale_admission(remote_setup, trigger):
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    local = runtime.store.queue()[0]
    if trigger == "cancel":
        store.request("cancel", {"job_id": job_id}, paired["device_key"])
    elif trigger == "expiry":
        # Deterministic clock at the runtime fence; do not wait for a deadline.
        db = runtime.store._connect()
        try:
            db.execute("UPDATE jobs SET expires_at=? WHERE job_id=?",
                       ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), local.job_id))
        finally:
            db.close()
    elif trigger == "revoked":
        store.revoke(paired["worker_id"])
    else:
        transport.unreachable = True
    result = runtime.reconcile_once()
    assert result.state in {JobState.FAILED, JobState.EXPIRED}
    assert adapter.starts == 0


def test_cancel_reconnect_after_heartbeat_loss(remote_setup):
    remote, runtime, adapter, store, ticks, paired, transport = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    adapter.finish.clear()
    runner = threading.Thread(target=runtime.reconcile_once)
    runner.start()
    assert adapter.entered.wait(2)
    ticks[0] += 16
    store.request("cancel", {"job_id": job_id}, paired["device_key"])
    remote.tick()  # stop must propagate even though state is interrupted
    runner.join(2)
    assert not runner.is_alive()
    remote.tick()
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "cancelled"


@pytest.mark.parametrize("operation", ["events", "upload", "reconcile"])
def test_response_loss_replays_without_duplicate_job_or_result(remote_setup, operation):
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    runtime.reconcile_once()
    transport.drop_response = operation
    remote.tick()
    remote.tick()
    assert remote.journal.pending() == []
    assert adapter.starts == 1
    assert len(store.request("artifacts", {"job_id": job_id}, paired["device_key"])["artifacts"]) == 1


def test_restart_never_relaunches_recovered_claim(remote_setup):
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    # Registration interrupts the claim. Local queued journal survives restart.
    restarted = WorkerRuntime(runtime.backup_root, adapter=adapter, account_identity=runtime.account_identity)
    client = RemoteClient(restarted, remote.url, paired["device_key"], transport=transport)
    client.tick()
    assert restarted.reconcile_once() is None
    assert adapter.starts == 0
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "cancelled"


def test_url_change_blocks_old_claim_and_default_adapter_never_claims(remote_setup):
    remote, runtime, adapter, _, _, paired, transport = remote_setup
    submit(remote_setup)
    remote.tick()
    configure_worker_service(runtime.backup_root, "https://other.example")
    runtime.reconcile_once()
    assert adapter.starts == 0
    remote.tick()
    assert remote.state == "disabled"


def test_no_redirects_or_invalid_urls():
    with pytest.raises(ProtocolError, match="https_required"):
        Transport("http://example.org")
    with pytest.raises(ProtocolError, match="redirect_refused"):
        NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere")


@pytest.mark.parametrize("rejection", ["symlink", "oversized", "service_conflict"])
def test_rejected_artifact_is_skipped_and_the_claim_completes(remote_setup, tmp_path, rejection):
    """A refused result must never wedge the claim: it is skipped, journaled, and the next job runs."""
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    next_id = submit(remote_setup, "next")
    remote.tick()
    result = runtime.reconcile_once()
    path = runtime.backup_root / "worker" / "research" / result.job_id / "result.md"
    if rejection == "symlink":
        secret = tmp_path / "secret"
        secret.write_text("not a result")
        path.unlink()
        try:
            path.symlink_to(secret)
        except OSError:
            pytest.skip("symlinks unavailable for this Windows user")
    elif rejection == "oversized":
        path.write_bytes(b"x" * (MAX_ARTIFACT + 1))
    else:
        transport.reject["upload"] = ProtocolError("artifact_conflict", 409)
    remote.tick()
    key = paired["device_key"]
    assert [b["remote_id"] for b in remote.journal.pending()] == [next_id] and remote.state == "online"
    assert store.request("job", {"job_id": job_id}, key)["state"] == "succeeded"
    assert store.request("artifacts", {"job_id": job_id}, key)["artifacts"] == []
    events = store.request("events", {"job_id": job_id, "after_cursor": 0}, key)["events"]
    assert [e["kind"] for e in events if e["diagnostic_code"] == "artifact_rejected"] == ["diagnostic"]
    if rejection == "symlink":
        assert secret.read_text() not in json.dumps(transport.requests)  # the target was never read
    # The second claim was admitted in the same tick instead of blocking forever.
    assert len(runtime.store.queue()) == 1 and adapter.starts == 1


def test_cursor_conflict_ends_the_binding_instead_of_retrying_forever(remote_setup):
    """A divergent server history cannot be repaired by replay: diagnose, finish the binding, keep claiming."""
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    next_id = submit(remote_setup, "next")
    remote.tick()
    local = runtime.reconcile_once()
    assert local.state == JobState.SUCCEEDED
    transport.reject["events"] = ProtocolError("cursor_conflict", 409)
    remote.tick()
    assert remote.state == "online"
    assert [b["remote_id"] for b in remote.journal.pending()] == [next_id]
    assert remote.journal.binding(local.job_id)["done"] == 1
    page = runtime.events(local.job_id, after_cursor=0, limit=200)
    assert [e.diagnostic_code for e in page.events if e.diagnostic_code] == ["remote_sync_conflict"]
    # The outcome was reported as uncertain, never as the unproven local success; no artifact left.
    reconcile = [data for op, data in transport.requests if op == "reconcile" and data["job_id"] == job_id]
    assert [(r["state"], r["execution_stopped"], r["unlaunched"]) for r in reconcile] == [("interrupted", False, False)]
    assert "upload" not in transport.calls
    key = paired["device_key"]
    assert store.request("job", {"job_id": job_id}, key)["state"] == "interrupted"
    # The next claim was admitted in the same tick; the local outcome stands untouched.
    assert len(runtime.store.queue()) == 1 and adapter.starts == 1
    assert runtime.get(local.job_id).state == JobState.SUCCEEDED


def test_cursor_conflict_with_an_unreachable_service_is_retried_not_dropped(remote_setup):
    remote, runtime, _, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    local = runtime.reconcile_once()
    transport.reject["events"] = ProtocolError("cursor_conflict", 409)
    transport.reject["reconcile"] = ProtocolError("service_unavailable", 503)
    remote.tick()
    assert remote.state == "offline" and remote.journal.binding(local.job_id)["done"] == 0
    page = runtime.events(local.job_id, after_cursor=0, limit=200)
    assert not any(e.diagnostic_code == "remote_sync_conflict" for e in page.events)
    transport.reject["events"] = ProtocolError("cursor_conflict", 409)
    remote.tick()
    assert remote.state == "online" and remote.journal.binding(local.job_id)["done"] == 1
    page = runtime.events(local.job_id, after_cursor=0, limit=200)
    assert [e.diagnostic_code for e in page.events if e.diagnostic_code] == ["remote_sync_conflict"]
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "interrupted"


def test_transport_failure_during_upload_keeps_the_claim_pending(remote_setup):
    remote, runtime, _, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    runtime.reconcile_once()
    transport.reject["upload"] = ProtocolError("lease_lost", 409)
    remote.tick()
    assert remote.journal.pending()
    remote.tick()
    assert remote.journal.pending() == []
    assert len(store.request("artifacts", {"job_id": job_id}, paired["device_key"])["artifacts"]) == 1


def test_probe_failure_is_survived_and_claims_resume(remote_setup):
    remote, runtime, adapter, _, _, _, transport = remote_setup
    submit(remote_setup)

    def broken_probe():
        raise RuntimeError("provider binary misbehaved")

    adapter.probe = broken_probe
    before = len(transport.calls)
    remote.tick()
    assert remote.state == "online" and "poll" not in transport.calls[before:]
    del adapter.probe
    remote.tick()
    assert runtime.store.queue()


def test_run_loop_survives_a_journal_error_and_keeps_ticking(remote_setup, monkeypatch):
    remote, runtime, _, store, _, paired, _ = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    store.request("cancel", {"job_id": job_id}, paired["device_key"])
    original_cancel, failures = runtime.cancel, []

    def flaky_cancel(local_id):
        if not failures:
            failures.append(local_id)
            raise StaleWriteError("cancel lost its fence")  # RuntimeError: outside tick's own allowlist
        return original_cancel(local_id)

    monkeypatch.setattr(runtime, "cancel", flaky_cancel)
    monkeypatch.setattr(remote_mod, "HEARTBEAT_SECONDS", 0.01)
    ticks, enough = [], threading.Event()
    original_tick = remote.tick

    def counted_tick():
        entered = remote.state  # what run() left behind after the previous tick
        try:
            original_tick()
            ticks.append((entered, "returned"))
        except Exception:
            ticks.append((entered, "raised"))
            raise
        finally:
            if len(ticks) >= 3:
                enough.set()

    monkeypatch.setattr(remote, "tick", counted_tick)
    stop = threading.Event()
    thread = threading.Thread(target=remote.run, args=(stop,))
    thread.start()
    assert enough.wait(5), "the remote thread died"
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert failures and ticks[0] == ("online", "raised") and ticks[1] == ("offline", "returned")
    assert ticks[-1] == ("online", "returned")
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "cancelled"


def test_binding_is_published_before_local_admission(remote_setup, monkeypatch):
    """The reconcile loop may reach the launch fence the instant a job is queued."""
    remote, runtime, adapter, store, _, paired, _ = remote_setup
    job_id = submit(remote_setup)
    original_submit, raced = runtime.submit, []

    def racing_submit(submission, **kwargs):
        record = original_submit(submission, **kwargs)
        assert remote.launch_allowed(record.job_id) is True
        raced.append(runtime.reconcile_once())
        return record

    monkeypatch.setattr(runtime, "submit", racing_submit)
    remote.tick()
    assert raced[0].state == JobState.SUCCEEDED and raced[0].diagnostic_code != "worker_disabled"
    assert adapter.starts == 1
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "succeeded"
    assert remote.journal.pending() == []


def test_reserved_local_id_without_admission_is_admitted_once(remote_setup, monkeypatch):
    remote, runtime, adapter, store, _, paired, _ = remote_setup
    job_id = submit(remote_setup)
    original_submit = runtime.submit

    def crashing_submit(submission, **kwargs):
        monkeypatch.setattr(runtime, "submit", original_submit)
        raise OSError("journal unavailable")

    monkeypatch.setattr(runtime, "submit", crashing_submit)
    remote.tick()
    assert remote.state == "offline"
    binding = remote.journal.pending()[0]
    assert binding["local_id"] and not runtime.store.queue()
    remote.tick()
    assert [job.job_id for job in runtime.store.queue()] == [binding["local_id"]]
    assert runtime.reconcile_once().state == JobState.SUCCEEDED
    remote.tick()
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "succeeded"
    assert adapter.starts == 1


def test_long_remote_job_id_fits_the_idempotency_key(remote_setup):
    remote, runtime, adapter, store, _, paired, _ = remote_setup
    long_id = "j" * 200
    with closing(store.connect()) as db, db:
        db.execute("UPDATE jobs SET id=? WHERE id=?", (long_id, submit(remote_setup)))
    remote.tick()
    local = runtime.store.queue()[0]
    assert local.idempotency_key.startswith("remote:") and len(local.idempotency_key) < 200
    assert runtime.reconcile_once().state == JobState.SUCCEEDED
    remote.tick()
    assert store.request("job", {"job_id": long_id}, paired["device_key"])["state"] == "succeeded"
    assert adapter.starts == 1


@pytest.mark.parametrize("malformed", [{"cancel_requested": "false"}, {"cancel_requested": 1}, {"state": "bogus"}])
def test_malformed_job_response_never_cancels_or_launches(remote_setup, monkeypatch, malformed):
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    submit(remote_setup)
    remote.tick()
    local = runtime.store.queue()[0]
    original = transport.request
    monkeypatch.setattr(transport, "request", lambda op, data: {**original(op, data), **malformed}
                        if op in {"job", "renew"} else original(op, data))
    remote.tick()
    assert remote.state == "offline"
    assert runtime.store.get(local.job_id).state == JobState.QUEUED
    assert remote.launch_allowed(local.job_id) is False
    monkeypatch.setattr(transport, "request", original)
    remote.tick()
    assert remote.state == "online" and runtime.reconcile_once().state == JobState.SUCCEEDED


def test_stale_worker_epoch_reports_offline_and_reregisters(remote_setup):
    remote, _, _, store, _, paired, transport = remote_setup
    superseded = store.request("register", {}, paired["device_key"])["worker_epoch"]
    remote.tick()
    assert remote.state == "offline" and remote.worker_epoch is None
    remote.tick()
    assert remote.state == "online" and remote.worker_epoch == superseded + 1
    assert transport.calls[-3:-1] == ["register", "heartbeat"]


def test_lease_conflict_failure_reconciles_as_failed_unlaunched(remote_setup):
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    remote.tick()
    runtime.leases.acquire(job_id="f" * 32, account_identity=runtime.account_identity,
                           worker_pid=runtime.worker_pid, worker_epoch=runtime.worker_epoch, ttl_s=120)
    result = runtime.reconcile_once()
    assert (result.state, result.diagnostic_code) == (JobState.FAILED, "lease_conflict")
    remote.tick()
    reconcile = [data for op, data in transport.requests if op == "reconcile"][-1]
    assert (reconcile["state"], reconcile["unlaunched"], reconcile["execution_stopped"]) == ("failed", True, False)
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "failed"
    assert adapter.starts == 0


def test_remembered_but_never_admitted_claim_reconciles_unlaunched_after_restart(remote_setup, monkeypatch):
    remote, runtime, adapter, store, _, paired, transport = remote_setup
    job_id = submit(remote_setup)
    monkeypatch.setattr(runtime, "submit", lambda *a, **k: (_ for _ in ()).throw(OSError("crash before admission")))
    remote.tick()
    assert remote.journal.pending()
    restarted = WorkerRuntime(runtime.backup_root, adapter=adapter, account_identity=runtime.account_identity)
    client = RemoteClient(restarted, remote.url, paired["device_key"], transport=transport)
    client.tick()
    reconcile = [data for op, data in transport.requests if op == "reconcile"][-1]
    assert (reconcile["state"], reconcile["unlaunched"]) == ("interrupted", True)
    assert client.journal.pending() == [] and not restarted.store.queue() and adapter.starts == 0
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "interrupted"


@pytest.mark.parametrize("status, code", [
    (400, "invalid_request"), (400, "invalid_code"), (400, "hash_mismatch"), (400, "invalid_state"),
    (401, "unauthorized"), (401, "device_expired"), (403, "revoked"), (403, "forbidden"),
    (404, "not_found"), (404, "unsupported_version"), (409, "offline_worker"), (409, "idempotency_conflict"),
    (409, "stale_epoch"), (409, "lease_lost"), (409, "cursor_conflict"), (409, "artifact_conflict"),
    (409, "queue_full"), (413, "body_too_large"), (413, "artifact_too_large"), (413, "artifact_limit"),
    (503, "service_unavailable"),
])
def test_live_transport_surfaces_every_documented_error_code(status, code):
    """The documented codes pass through unchanged; anything else is still rewritten."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), CannedHandler)
    server.reply = (status, {}, json.dumps({"error": code}).encode())
    thread = serve(server)
    try:
        with pytest.raises(ProtocolError) as failure:
            Transport(f"http://127.0.0.1:{server.server_port}", "key").request("submit", {})
        assert (failure.value.code, failure.value.status) == (code, status)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(6)


class CannedHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        status, headers, body = self.server.reply
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    return thread


def test_live_transport_maps_reference_server_statuses(tmp_path):
    store = ControlStore(tmp_path / "service" / "db")
    with make_server(store, port=0) as server:
        thread = serve(server)
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            paired = Transport(url).request("pair", {"code": store.issue_code()})
            transport = Transport(url, paired["device_key"])
            assert transport.request("register", {})["worker_epoch"] == 1
            assert transport.request("poll", {"worker_epoch": 1}) == {"claim": None}
            with pytest.raises(ProtocolError) as failure:
                transport.request("heartbeat", {"worker_epoch": 7})
            assert (failure.value.code, failure.value.status) == ("stale_epoch", 409)
            with pytest.raises(ProtocolError) as failure:
                Transport(url, "not-the-key").request("register", {})
            assert (failure.value.code, failure.value.status) == ("unauthorized", 401)
            with pytest.raises(ProtocolError) as failure:
                transport.request("job", {"job_id": "missing"})
            assert (failure.value.code, failure.value.status) == ("not_found", 404)
        finally:
            server.shutdown()
            thread.join(2)


@pytest.mark.parametrize("reply, expected", [
    ((302, {"Location": "http://127.0.0.1:9/v1/register"}, b"{}"), ("redirect_refused", 400)),
    ((200, {}, b"x" * (MAX_BODY + 1)), ("body_too_large", 413)),
    ((200, {}, b"[]"), ("invalid_response", 400)),
    ((200, {}, b"not json"), ("invalid_response", 400)),
    ((409, {}, b'{"error":"vendor_specific_text"}'), ("service_unavailable", 409)),
    ((409, {}, b'{"error":"artifact_conflict"}'), ("artifact_conflict", 409)),
    ((503, {}, b""), ("invalid_response", 400)),
    ((400, {}, b'{"error":"invalid_request"}'), ("invalid_request", 400)),
])
def test_live_transport_refuses_unsafe_responses(reply, expected):
    server = ThreadingHTTPServer(("127.0.0.1", 0), CannedHandler)
    server.reply = reply
    thread = serve(server)
    try:
        with pytest.raises(ProtocolError) as failure:
            Transport(f"http://127.0.0.1:{server.server_port}", "key").request("register", {})
        assert (failure.value.code, failure.value.status) == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
