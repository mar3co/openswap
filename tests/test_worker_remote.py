from __future__ import annotations
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import threading

import pytest

from openswap.settings import configure_worker_service, update_worker_settings
from openswap.worker.leases import stable_account_identity
from openswap.worker.models import (JobSubmission, JobState, ProviderAvailability, ProviderRun,
                                     SafeEvent, SafeEventKind, InterruptResult)
from openswap.worker.protocol import Artifact, ProtocolError, Submission
from openswap.worker.refserver import ControlStore
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
        self.drop_response = None

    def request(self, operation, data):
        self.calls.append(operation)
        if self.unreachable:
            raise ProtocolError("service_unavailable", 503)
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


def test_artifact_export_refuses_symlink(remote_setup, tmp_path):
    remote, runtime, _, _, _, _, _ = remote_setup
    submit(remote_setup)
    remote.tick()
    result = runtime.reconcile_once()
    directory = runtime.backup_root / "worker" / "research" / result.job_id
    secret = tmp_path / "secret"
    secret.write_text("not a result")
    path = directory / "result.md"
    path.unlink()
    try:
        path.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks unavailable for this Windows user")
    remote.tick()
    assert remote.journal.pending()  # no artifact acknowledgement
    assert remote.state == "offline"
