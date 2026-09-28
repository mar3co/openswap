from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import threading
from dataclasses import replace

import pytest

from openswap import settings as shared_settings
from openswap.settings import (
    WorkerWorkspace,
    configure_worker_local_policy,
    load_worker_settings,
    update_worker_settings,
)
from openswap.worker.journal import AdmissionError, LocalJobStore, StaleWriteError
from openswap.worker.models import (
    InterruptResult,
    JobState,
    JobSubmission,
    ProviderAvailability,
    ProviderRun,
    SafeEvent,
    SafeEventKind,
)
from openswap.worker.runtime import WorkerRuntime, read_worker_snapshot, run_worker
from openswap.worker.leases import AccountLeaseStore, stable_account_identity


def _submission(key: str = "key-1", **changes) -> JobSubmission:
    values = {
        "idempotency_key": key,
        "provider": "codex",
        "task": "Research this topic",
        "capability_profile": "research",
        "workspace_id": "research",
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "runtime_limit_s": 600,
    }
    values.update(changes)
    return JobSubmission(**values)


def test_worker_settings_default_off_and_preserve_other_sections(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"autoswitch": {"threshold": 80}, "other": {"x": 1}}))

    assert load_worker_settings(tmp_path).enabled is False
    updated = update_worker_settings(tmp_path, enabled=True, paused=True)
    raw = json.loads(settings.read_text())
    assert updated.enabled is True and updated.paused is True
    assert raw["autoswitch"] == {"threshold": 80}
    assert raw["other"] == {"x": 1}
    assert (settings.stat().st_mode & 0o777) == 0o600


def test_malformed_worker_policy_fails_closed(tmp_path):
    (tmp_path / "settings.json").write_text(
        '{"worker":{"enabled":true,"paused":"no"}}', encoding="utf-8"
    )

    policy = load_worker_settings(tmp_path)
    assert (policy.enabled, policy.paused) == (False, False)


def test_concurrent_worker_policy_updates_do_not_lose_disable(tmp_path, monkeypatch):
    update_worker_settings(tmp_path, enabled=True, paused=False)
    read_pause = threading.Event()
    allow_pause_write = threading.Event()
    disable_done = threading.Event()
    original_read = shared_settings._read_raw_for_write

    def delayed_read(path):
        raw = original_read(path)
        if threading.current_thread().name == "pause-writer":
            read_pause.set()
            assert allow_pause_write.wait(3)
        return raw

    monkeypatch.setattr(shared_settings, "_read_raw_for_write", delayed_read)

    def pause():
        update_worker_settings(tmp_path, paused=True)

    def disable():
        update_worker_settings(tmp_path, enabled=False)
        disable_done.set()

    first = threading.Thread(target=pause, name="pause-writer")
    second = threading.Thread(target=disable, name="disable-writer")
    first.start()
    assert read_pause.wait(3)
    second.start()
    try:
        assert not disable_done.wait(0.1)
    finally:
        allow_pause_write.set()
    first.join(3)
    second.join(3)
    assert not first.is_alive() and not second.is_alive()
    settings = load_worker_settings(tmp_path)
    assert settings.enabled is False and settings.paused is True


def test_read_only_snapshot_does_not_create_state_and_real_adapter_is_disabled(tmp_path):
    snapshot = read_worker_snapshot(tmp_path)

    assert snapshot.process_state.value == "stopped"
    assert snapshot.enabled is False
    assert snapshot.remote_connectivity.value == "disabled"
    assert snapshot.provider.available is False
    assert snapshot.provider.diagnostic_code == "live_adapter_disabled"
    assert not (tmp_path / "worker").exists()


def test_read_only_snapshot_quarantines_incomplete_released_lease(tmp_path):
    lease_dir = tmp_path / "worker" / "leases"
    lease_dir.mkdir(mode=0o700, parents=True)
    (lease_dir / "codex.json").write_text(
        json.dumps({"schema_version": 1, "lease_generation": 1, "lease": {"state": "released"}}),
        encoding="utf-8",
    )

    snapshot = read_worker_snapshot(tmp_path)

    assert snapshot.lease_quarantined is True


@pytest.mark.parametrize(
    ("stopped", "expected_state", "expected_lease"),
    [
        (True, JobState.CANCELLED, "released"),
        (False, JobState.INTERRUPTED, "uncertain"),
    ],
)
def test_stale_event_after_stop_interrupts_owned_provider_run(
    tmp_path, stopped, expected_state, expected_lease,
):
    update_worker_settings(tmp_path, enabled=True)

    class CancelDuringEvents(_FakeAdapter):
        runtime = None
        job_id = None

        def events(self, run, *, after_cursor):
            self.runtime.stop(self.job_id)
            return (SafeEvent(
                job_id=self.job_id,
                cursor=21,
                timestamp=datetime.now(timezone.utc),
                kind=SafeEventKind.DIAGNOSTIC,
                diagnostic_code="provider_unavailable",
            ),)

    adapter = CancelDuringEvents(stopped=stopped)
    identity = stable_account_identity("codex", "cancel-during-events-test")
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    adapter.runtime = runtime
    adapter.job_id = job.job_id

    result = runtime.reconcile_once()

    assert result.state == expected_state
    assert adapter.interrupt_count == 1
    assert AccountLeaseStore(tmp_path).current().state == expected_lease


@pytest.mark.parametrize(
    ("stopped", "expected_state", "expected_lease"),
    [
        (True, JobState.CANCELLED, "released"),
        (False, JobState.INTERRUPTED, "uncertain"),
    ],
)
def test_stop_during_interrupt_uses_latest_generation_without_retry(
    tmp_path, stopped, expected_state, expected_lease,
):
    update_worker_settings(tmp_path, enabled=True)

    class StopDuringInterrupt(_FakeAdapter):
        runtime = None
        job_id = None

        def interrupt(self, run):
            self.interrupt_count += 1
            result = self.runtime.stop(self.job_id)
            assert result.accepted is True
            return InterruptResult(True, stopped, None)

    adapter = StopDuringInterrupt(stopped=stopped)
    identity = stable_account_identity("codex", "stop-during-interrupt-test")
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    adapter.runtime = runtime
    adapter.job_id = job.job_id
    shutdown = threading.Event()
    shutdown.set()

    result = runtime.reconcile_once(shutdown_event=shutdown)

    assert result.state == expected_state
    assert adapter.interrupt_count == 1
    assert AccountLeaseStore(tmp_path).current().state == expected_lease


@pytest.mark.parametrize("race_point", ["append", "terminal_transition"])
def test_provider_finished_proof_survives_concurrent_stop(tmp_path, race_point):
    update_worker_settings(tmp_path, enabled=True)

    class FinishedAdapter(_FakeAdapter):
        interrupt_count = 0

        def events(self, run, *, after_cursor):
            self.event_cursors.append(after_cursor)
            return (_finished_event(
                run_job_id, state=JobState.SUCCEEDED, stopped=True, cursor=21,
            ),)

        def interrupt(self, run):
            self.interrupt_count += 1
            return InterruptResult(True, False, "provider_unavailable")

    identity = stable_account_identity("codex", "finished-stop-race-test")
    run_job_id = ""
    adapter = FinishedAdapter()
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    run_job_id = job.job_id
    original_transition = runtime.store.transition
    original_append_event = runtime.store.append_event
    injected_stop = False

    def stop_before_terminal_transition(job_id, **kwargs):
        nonlocal injected_stop
        if not injected_stop and kwargs.get("new_state") == JobState.SUCCEEDED:
            injected_stop = True
            result = runtime.stop(job_id)
            assert result.accepted is True
        return original_transition(job_id, **kwargs)

    def stop_before_event_append(job_id, **kwargs):
        nonlocal injected_stop
        if (
            not injected_stop
            and kwargs.get("kind") == SafeEventKind.PROVIDER_FINISHED
            and kwargs.get("execution_stopped") is True
        ):
            injected_stop = True
            result = runtime.stop(job_id)
            assert result.accepted is True
        return original_append_event(job_id, **kwargs)

    if race_point == "append":
        runtime.store.append_event = stop_before_event_append
    else:
        runtime.store.transition = stop_before_terminal_transition

    result = runtime.reconcile_once()

    assert injected_stop is True
    assert result.state == JobState.CANCELLED
    assert adapter.interrupt_count == 0
    assert AccountLeaseStore(tmp_path).current().state == "released"


def test_malformed_provider_stop_proof_is_not_preserved(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    adapter = _FakeAdapter(stopped=False)
    identity = stable_account_identity("codex", "malformed-provider-proof-test")
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    adapter.events_out = (
        _finished_event(job.job_id, stopped="true"),
    )

    result = runtime.reconcile_once()

    assert result.state == JobState.INTERRUPTED
    assert AccountLeaseStore(tmp_path).current().state == "uncertain"


@pytest.mark.parametrize("failure_point", ["running_transition", "running_get"])
@pytest.mark.parametrize(
    ("stopped", "expected_state", "expected_lease"),
    [
        (True, JobState.FAILED, "released"),
        (False, JobState.INTERRUPTED, "uncertain"),
    ],
)
def test_post_start_journal_failure_interrupts_owned_run(
    tmp_path, failure_point, stopped, expected_state, expected_lease,
):
    update_worker_settings(tmp_path, enabled=True)
    adapter = _FakeAdapter(stopped=stopped)
    identity = stable_account_identity("codex", "post-start-journal-failure-test")
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    original_transition = runtime.store.transition
    original_get = runtime.store.get
    failed = False

    def fail_running_transition(job_id, **kwargs):
        nonlocal failed
        if not failed and kwargs.get("new_state") == JobState.RUNNING:
            failed = True
            raise RuntimeError("synthetic_journal_failure")
        return original_transition(job_id, **kwargs)

    def fail_running_get(job_id):
        nonlocal failed
        current = original_get(job_id)
        if not failed and current.state == JobState.RUNNING:
            failed = True
            raise RuntimeError("synthetic_journal_failure")
        return current

    if failure_point == "running_transition":
        runtime.store.transition = fail_running_transition
    else:
        runtime.store.get = fail_running_get

    result = runtime.reconcile_once()

    assert failed is True
    assert adapter.start_count == 1
    assert adapter.interrupt_count == 1
    assert result.state == expected_state
    assert AccountLeaseStore(tmp_path).current().state == expected_lease


def test_local_workspace_registry_is_opaque_disjoint_and_persisted(tmp_path):
    output = tmp_path / "approved-output"
    source = tmp_path / "approved-source"
    account_ref = stable_account_identity("codex", "locally-pinned-reference")
    policy = configure_worker_local_policy(
        tmp_path,
        pinned_account_ref=account_ref,
        workspaces=(WorkerWorkspace("research", output, (source,)),),
    )

    assert policy.pinned_account_ref == account_ref
    assert policy.workspaces[0].output_root == output.resolve()
    assert policy.workspaces[0].readonly_roots == (source.resolve(),)
    with pytest.raises(ValueError):
        configure_worker_local_policy(
            tmp_path,
            pinned_account_ref=account_ref,
            workspaces=(WorkerWorkspace("research", output, (output / "source",)),),
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_group_or_world_writable_readonly_root_fails_the_job(tmp_path):
    """The output root already refuses group/world-writable permissions;
    an approved read-only source must be held to the same standard, since a
    concurrently-writable "read-only" source is not actually read-only."""
    output = tmp_path / "approved-output"
    source = tmp_path / "approved-source"
    source.mkdir()
    os.chmod(source, 0o777)
    account_ref = stable_account_identity("codex", "locally-pinned-reference")
    configure_worker_local_policy(
        tmp_path,
        pinned_account_ref=account_ref,
        workspaces=(WorkerWorkspace("research", output, (source,)),),
    )
    update_worker_settings(tmp_path, enabled=True)
    adapter = _FakeAdapter()
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=account_ref)
    # A short deadline bounds this test even if workspace resolution someday
    # stops refusing the run before adapter.start(): it would then just poll
    # to a fast timeout instead of hanging on the fake adapter's empty events.
    runtime.submit(_submission(runtime_limit_s=1))

    result = runtime.reconcile_once()

    assert result.state == JobState.FAILED
    assert result.diagnostic_code == "provider_unavailable"
    assert adapter.start_count == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
@pytest.mark.parametrize("mode, accepted", [(0o755, True), (0o750, True), (0o775, False), (0o757, False)])
def test_readonly_root_may_be_readable_but_not_writable_by_others(tmp_path, mode, accepted):
    output = tmp_path / "approved-output"
    source = tmp_path / "approved-source"
    source.mkdir()
    os.chmod(source, mode)
    account_ref = stable_account_identity("codex", "locally-pinned-reference")
    configure_worker_local_policy(
        tmp_path,
        pinned_account_ref=account_ref,
        workspaces=(WorkerWorkspace("research", output, (source,)),),
    )
    runtime = WorkerRuntime(tmp_path, adapter=_FakeAdapter(), account_identity=account_ref)
    if accepted:
        resolved = runtime._resolve_workspace("research", "a" * 32)
        assert resolved.readonly_sources == (source,)
    else:
        with pytest.raises(ValueError, match="permissions are unsafe"):
            runtime._resolve_workspace("research", "a" * 32)


def test_runtime_admission_is_internal_and_disabled_adapter_never_executes(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    runtime = WorkerRuntime(tmp_path)
    job = runtime.submit(_submission())

    assert runtime.get(job.job_id).state == JobState.QUEUED
    terminal = runtime.reconcile_once()
    assert terminal is not None
    assert terminal.state == JobState.FAILED
    assert terminal.diagnostic_code == "live_adapter_disabled"
    assert runtime.status().provider.available is False


def test_expired_queued_job_becomes_expired_without_adapter_start(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    adapter = _FakeAdapter()
    runtime = WorkerRuntime(tmp_path, adapter=adapter)
    expired = _submission(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    runtime.store.create(
        expired, owner_ref="local-user", worker_epoch=runtime.worker_epoch,
    )

    result = runtime.reconcile_once()

    assert result.state == JobState.EXPIRED
    assert adapter.start_count == 0


class _FakeAdapter:
    def __init__(self, events=(), *, stopped=True, block_start=False):
        self.events_out = tuple(events)
        self.stopped = stopped
        self.block_start = block_start
        self.start_entered = threading.Event()
        self.release_start = threading.Event()
        self.start_count = 0
        self.interrupt_count = 0
        self.event_cursors = []
        self.workspace = None

    def probe(self):
        return ProviderAvailability(True, None, "fake-test")

    def start(self, job, workspace, *, worker_epoch):
        self.start_count += 1
        self.workspace = workspace
        self.start_entered.set()
        if self.block_start:
            assert self.release_start.wait(3)
        return ProviderRun(None, "synthetic-session", worker_epoch, job.generation, 20)

    def events(self, run, *, after_cursor):
        self.event_cursors.append(after_cursor)
        result, self.events_out = self.events_out, ()
        return result

    def interrupt(self, run):
        self.interrupt_count += 1
        return InterruptResult(True, self.stopped, None)


def _finished_event(job_id, *, state=JobState.SUCCEEDED, stopped=True, cursor=21, diagnostic=None):
    return SafeEvent(
        job_id=job_id,
        cursor=cursor,
        timestamp=datetime.now(timezone.utc),
        kind=SafeEventKind.PROVIDER_FINISHED,
        state=state,
        diagnostic_code=diagnostic,
        execution_stopped=stopped,
    )


def test_fake_adapter_execution_uses_provider_cursor_proof_and_releases_lease(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter()
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    adapter.events_out = (_finished_event(job.job_id, state=JobState.FAILED, diagnostic="provider_rate_limited"),)

    finished = runtime.reconcile_once()

    assert finished.state == JobState.FAILED
    assert finished.diagnostic_code == "provider_rate_limited"
    assert adapter.event_cursors == [20]
    assert adapter.workspace.workspace_id == "research"
    assert adapter.workspace.output_root.name == job.job_id
    assert AccountLeaseStore(tmp_path).current().state == "released"


def test_provider_terminal_without_stop_proof_quarantines_lease(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter()
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    adapter.events_out = (_finished_event(job.job_id, stopped=False),)

    result = runtime.reconcile_once()

    assert result.state == JobState.INTERRUPTED
    assert result.diagnostic_code == "execution_uncertain"
    assert AccountLeaseStore(tmp_path).current().state == "uncertain"


def test_worker_restart_quarantines_an_active_lease_left_by_a_crashed_process(tmp_path):
    """A worker that crashes mid-job must not leave its lease ``active`` forever.

    ``start_epoch`` already interrupts the orphaned job; a fresh runtime must
    also stop trusting the lease it left behind, since nothing else will
    (``LeaseMutationGuard`` refuses ``active``/``uncertain`` leases forever
    and there is no other supported release path but the owner-invoked one).
    """
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    crashed = WorkerRuntime(tmp_path, adapter=_FakeAdapter(), account_identity=identity)
    job = crashed.submit(_submission())
    claimed = crashed.store.claim(
        job.job_id, worker_epoch=crashed.worker_epoch, expected_generation=job.generation,
    )
    running, _run, _token = crashed._prepare_run(claimed)
    assert running.state == JobState.RUNNING
    assert AccountLeaseStore(tmp_path).current().state == "active"

    # The process is gone without ever releasing the lease or stopping the
    # job (no mark_stopped, no interrupt). A fresh worker starts in its place.
    restarted = WorkerRuntime(tmp_path, adapter=_FakeAdapter(), account_identity=identity)

    assert job.job_id in restarted.recovered_job_ids
    assert restarted.store.get(job.job_id).state == JobState.INTERRUPTED
    lease = AccountLeaseStore(tmp_path).current()
    assert lease.state == "uncertain"
    assert lease.reason == "worker_restarted"
    assert lease.job_id == job.job_id


def test_worker_restart_leaves_an_unrelated_released_lease_alone(tmp_path):
    """Only a lease still ``active`` for a recovered job is ever touched."""
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter()
    first = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = first.submit(_submission())
    adapter.events_out = (_finished_event(job.job_id),)
    finished = first.reconcile_once()
    assert finished.state == JobState.SUCCEEDED
    released = AccountLeaseStore(tmp_path).current()
    assert released.state == "released"

    restarted = WorkerRuntime(tmp_path, adapter=_FakeAdapter(), account_identity=identity)

    assert restarted.recovered_job_ids == ()
    assert AccountLeaseStore(tmp_path).current().token() == released.token()
    assert AccountLeaseStore(tmp_path).current().state == "released"


def test_stop_cannot_race_provider_start_and_ack_is_not_stop_proof(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter(block_start=True, stopped=True)
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    results = {}
    runner = threading.Thread(target=lambda: results.setdefault("run", runtime.reconcile_once()))
    stopper = threading.Thread(target=lambda: results.setdefault("stop", runtime.stop(job.job_id)))

    runner.start()
    assert adapter.start_entered.wait(3)
    stopper.start()
    # The stop write is serialized behind the provider-start decision. It is
    # not acknowledged until start returns and the active generation is known.
    assert "stop" not in results
    adapter.release_start.set()
    runner.join(3)
    stopper.join(3)

    assert not runner.is_alive() and not stopper.is_alive()
    assert results["stop"].accepted is True
    assert results["stop"].diagnostic_code == "stop_requested"
    assert results["run"].state == JobState.CANCELLED
    assert adapter.start_count == 1
    assert AccountLeaseStore(tmp_path).current().state == "released"


@pytest.mark.parametrize(
    ("reason", "stopped", "expected_state", "expected_lease"),
    [
        ("deadline", True, JobState.FAILED, "released"),
        ("deadline", False, JobState.INTERRUPTED, "uncertain"),
        ("deadline", "false", JobState.INTERRUPTED, "uncertain"),
        ("shutdown", True, JobState.CANCELLED, "released"),
        ("shutdown", False, JobState.INTERRUPTED, "uncertain"),
    ],
)
def test_deadline_and_shutdown_require_explicit_stop_proof(
    tmp_path, reason, stopped, expected_state, expected_lease,
):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter(stopped=stopped)
    clock = [0.0]
    runtime = WorkerRuntime(
        tmp_path, adapter=adapter, account_identity=identity,
        monotonic=lambda: clock[0], sleeper=lambda _: clock.__setitem__(0, 11.0),
    )
    runtime.submit(_submission(runtime_limit_s=10))
    shutdown = threading.Event()
    if reason == "shutdown":
        shutdown.set()

    result = runtime.reconcile_once(shutdown_event=shutdown)

    assert result.state == expected_state
    assert AccountLeaseStore(tmp_path).current().state == expected_lease


@pytest.mark.parametrize(
    ("trigger", "expected_state"),
    [
        ("stop", JobState.CANCELLED),
        ("shutdown", JobState.CANCELLED),
        ("deadline", JobState.FAILED),
    ],
)
def test_blocked_event_reader_is_interruptible_and_blocks_next_job(
    tmp_path, trigger, expected_state,
):
    update_worker_settings(tmp_path, enabled=True)

    class BlockingEventsAdapter(_FakeAdapter):
        def __init__(self):
            super().__init__(stopped=True)
            self.events_entered = threading.Event()
            self.release_events = threading.Event()

        def events(self, run, *, after_cursor):
            self.event_cursors.append(after_cursor)
            self.events_entered.set()
            assert self.release_events.wait(5)
            # This stale completion must be discarded after interruption.
            return (_finished_event(run_job_id, cursor=21),)

    identity = stable_account_identity("codex", f"blocked-events-{trigger}")
    adapter = BlockingEventsAdapter()
    clock = [0.0]
    runtime = WorkerRuntime(
        tmp_path, adapter=adapter, account_identity=identity,
        monotonic=lambda: clock[0],
    )
    first = runtime.submit(_submission(f"blocked-{trigger}"))
    runtime.submit(_submission(f"queued-{trigger}"))
    run_job_id = first.job_id
    shutdown = threading.Event()
    results = []
    runner = threading.Thread(
        target=lambda: results.append(runtime.reconcile_once(shutdown_event=shutdown)),
    )
    runner.start()
    assert adapter.events_entered.wait(3)

    if trigger == "stop":
        assert runtime.stop(first.job_id).accepted is True
    elif trigger == "shutdown":
        shutdown.set()
    else:
        clock[0] = 601.0

    runner.join(3)
    try:
        assert not runner.is_alive()
        assert len(results) == 1
        assert results[0].state == expected_state
        assert adapter.interrupt_count == 1
        assert adapter.start_count == 1
        # The old events() call is still blocked, so no second job/read starts.
        assert runtime.reconcile_once() is None
        queued = runtime.store.queue(limit=1)
        assert len(queued) == 1 and queued[0].state == JobState.QUEUED
        assert not any(
            event.kind == SafeEventKind.PROVIDER_FINISHED
            for event in runtime.events(first.job_id).events
        )
    finally:
        adapter.release_events.set()
    reader = runtime._event_reader
    if reader is not None:
        reader.join(3)
        assert not reader.is_alive()
    assert not any(
        event.kind == SafeEventKind.PROVIDER_FINISHED
        for event in runtime.events(first.job_id).events
    )


@pytest.mark.parametrize(
    ("stopped", "expected_state", "expected_lease"),
    [
        (True, JobState.CANCELLED, "released"),
        (False, JobState.INTERRUPTED, "uncertain"),
    ],
)
def test_manual_worker_exits_when_policy_is_disabled(
    tmp_path, monkeypatch, stopped, expected_state, expected_lease,
):
    update_worker_settings(tmp_path, enabled=True)
    adapter = _FakeAdapter(stopped=stopped)
    identity = stable_account_identity("codex", "policy-disable-test")
    runtime_ready = threading.Event()
    captured_runtime = []
    results = []

    def runtime_factory(root):
        runtime = WorkerRuntime(root, adapter=adapter, account_identity=identity)
        captured_runtime.append(runtime)
        runtime_ready.set()
        return runtime

    def fake_serve(_path, _control, stop_event, *, ready_event=None):
        if ready_event is not None:
            ready_event.set()
        stop_event.wait(5)

    monkeypatch.setattr("openswap.worker.ipc.serve", fake_serve)
    worker = threading.Thread(
        target=lambda: results.append(run_worker(tmp_path, runtime_factory=runtime_factory)),
        daemon=True,
    )
    worker.start()
    assert runtime_ready.wait(2)
    runtime = captured_runtime[0]
    job = runtime.submit(_submission())
    assert adapter.start_entered.wait(2)

    # Simulate a local settings change while this manually started worker is
    # running, without relying on the LaunchAgent lifecycle.
    update_worker_settings(tmp_path, enabled=False)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert results == [0]
    assert LocalJobStore(tmp_path).get(job.job_id).state == expected_state
    assert AccountLeaseStore(tmp_path).current().state == expected_lease
    snapshot = read_worker_snapshot(tmp_path)
    assert snapshot.process_state.value == "stopped"
    assert snapshot.active_job is None


def test_journal_recovery_adopts_queue_and_interrupts_active_without_relaunch(tmp_path):
    store = LocalJobStore(tmp_path)
    first_epoch, _ = store.start_epoch(os.getpid())
    queued = store.create(_submission(), owner_ref="local-user", worker_epoch=first_epoch)
    waiting = store.create(_submission("key-waiting"), owner_ref="local-user", worker_epoch=first_epoch)
    claimed = store.claim(
        queued.job_id, worker_epoch=first_epoch, expected_generation=queued.generation,
    )
    starting = store.transition(
        claimed.job_id, expected_states=(JobState.CLAIMED,), new_state=JobState.STARTING,
        worker_epoch=first_epoch, expected_generation=claimed.generation,
    )
    running = store.transition(
        starting.job_id, expected_states=(JobState.STARTING,), new_state=JobState.RUNNING,
        worker_epoch=first_epoch, expected_generation=starting.generation,
    )

    second_epoch, recovered = store.start_epoch(os.getpid())
    assert second_epoch == first_epoch + 1
    assert recovered == (running.job_id,)
    assert store.get(running.job_id).state == JobState.INTERRUPTED
    adopted = store.get(waiting.job_id)
    assert adopted.worker_epoch == second_epoch
    assert adopted.generation == waiting.generation + 1
    assert store.claim(
        adopted.job_id, worker_epoch=second_epoch, expected_generation=adopted.generation,
    ).state == JobState.CLAIMED
    assert store.active().job_id == adopted.job_id


def test_journal_enforces_one_active_job_and_valid_state_graph(tmp_path):
    store = LocalJobStore(tmp_path)
    epoch, _ = store.start_epoch(os.getpid())
    first = store.create(_submission(), owner_ref="local-user", worker_epoch=epoch)
    second = store.create(_submission("key-2"), owner_ref="local-user", worker_epoch=epoch)
    claimed = store.claim(first.job_id, worker_epoch=epoch, expected_generation=first.generation)

    with pytest.raises(AdmissionError):
        store.claim(second.job_id, worker_epoch=epoch, expected_generation=second.generation)
    with pytest.raises(StaleWriteError):
        store.transition(
            claimed.job_id, expected_states=(JobState.CLAIMED,), new_state=JobState.QUEUED,
            worker_epoch=epoch, expected_generation=claimed.generation,
        )


def test_journal_rejects_submission_past_bounded_queue_capacity(tmp_path):
    store = LocalJobStore(tmp_path, max_pending=1)
    epoch, _ = store.start_epoch(os.getpid())
    store.create(_submission(), owner_ref="local-user", worker_epoch=epoch)

    with pytest.raises(AdmissionError):
        store.create(_submission("key-2"), owner_ref="local-user", worker_epoch=epoch)


def test_idempotency_key_cannot_alias_different_private_payload(tmp_path):
    store = LocalJobStore(tmp_path)
    epoch, _ = store.start_epoch(os.getpid())
    original = _submission()
    created = store.create(original, owner_ref="local-user", worker_epoch=epoch)
    assert store.create(original, owner_ref="local-user", worker_epoch=epoch).job_id == created.job_id

    with pytest.raises(AdmissionError):
        store.create(
            replace(original, task="Different request"), owner_ref="local-user",
            worker_epoch=epoch,
        )
    assert store.get(created.job_id).task == "Research this topic"


def test_repeated_cancel_is_idempotent_and_snapshot_is_redacted(tmp_path):
    store = LocalJobStore(tmp_path)
    epoch, _ = store.start_epoch(os.getpid())
    created = store.create(_submission(), owner_ref="private-owner", worker_epoch=epoch)
    claimed = store.claim(created.job_id, worker_epoch=epoch, expected_generation=created.generation)
    started = store.transition(
        claimed.job_id, expected_states=(JobState.CLAIMED,), new_state=JobState.STARTING,
        worker_epoch=epoch, expected_generation=claimed.generation,
    )
    requested = store.cancel(
        started.job_id, worker_epoch=epoch, expected_generation=started.generation,
    )
    repeated = store.cancel(
        requested.job_id, worker_epoch=epoch, expected_generation=requested.generation,
    )

    assert requested.generation == repeated.generation
    assert repeated.state == JobState.CANCEL_REQUESTED
    public = repeated.snapshot().to_dict()
    assert "task" not in public and "owner_ref" not in public
    assert "Research this topic" not in json.dumps(public)
    assert store.list_events(created.job_id).next_cursor == repeated.event_cursor


def test_phase_two_never_emits_approval_state_or_arbitrary_events(tmp_path):
    store = LocalJobStore(tmp_path)
    epoch, _ = store.start_epoch(os.getpid())
    created = store.create(_submission(), owner_ref="local-user", worker_epoch=epoch)

    with pytest.raises(ValueError):
        store.transition(
            created.job_id, expected_states=(JobState.QUEUED,),
            new_state=JobState.WAITING_FOR_APPROVAL, worker_epoch=epoch,
            expected_generation=created.generation,
        )
    with pytest.raises(ValueError):
        store.append_event(
            created.job_id, kind=SafeEventKind.DIAGNOSTIC,
            worker_epoch=epoch, expected_generation=created.generation,
            diagnostic_code="raw provider text",
        )
