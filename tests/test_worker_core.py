from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import threading
import time
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
from openswap.worker import cli as worker_cli
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
    if os.name == "posix":  # Windows has no POSIX file modes
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


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
def test_read_only_snapshot_creates_no_journal_sidecars_even_in_a_read_only_dir(tmp_path):
    import sqlite3

    store = LocalJobStore(tmp_path)
    store.create(_submission(), owner_ref="local-user", worker_epoch=store.current_epoch())
    db_path = tmp_path / "worker" / "jobs.sqlite3"
    wal = db_path.with_name(db_path.name + "-wal")
    shm = db_path.with_name(db_path.name + "-shm")
    # A stopped worker's fully checkpointed journal, without sidecars.
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    for sidecar in (wal, shm):
        if sidecar.exists():
            sidecar.unlink()
    worker_dir = db_path.parent
    worker_dir.chmod(0o500)
    try:
        snapshot = read_worker_snapshot(tmp_path)
    finally:
        worker_dir.chmod(0o700)

    assert snapshot.process_state.value != "unavailable"
    assert snapshot.queue_depth == 1
    assert not wal.exists() and not shm.exists()


def test_read_only_snapshot_still_reads_uncheckpointed_wal_content(tmp_path):
    import sqlite3

    store = LocalJobStore(tmp_path)
    store.create(_submission(), owner_ref="local-user", worker_epoch=store.current_epoch())
    db_path = tmp_path / "worker" / "jobs.sqlite3"
    # A live reader keeps the WAL from being checkpointed on close.
    keeper = sqlite3.connect(db_path)
    keeper.execute("SELECT COUNT(*) FROM jobs").fetchone()
    try:
        store.create(_submission("key-2"), owner_ref="local-user", worker_epoch=store.current_epoch())
        assert db_path.with_name(db_path.name + "-wal").stat().st_size > 0
        snapshot = read_worker_snapshot(tmp_path)
    finally:
        keeper.close()

    assert snapshot.queue_depth == 2


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions")
def test_read_only_snapshot_reads_wal_without_shm_and_creates_nothing(tmp_path):
    import shutil
    import sqlite3

    source = tmp_path / "source"
    store = LocalJobStore(source)
    store.create(_submission(), owner_ref="local-user", worker_epoch=store.current_epoch())
    source_db = source / "worker" / "jobs.sqlite3"
    keeper = sqlite3.connect(source_db)  # keeps the WAL uncheckpointed
    keeper.execute("SELECT COUNT(*) FROM jobs").fetchone()
    try:
        store.create(_submission("key-2"), owner_ref="local-user", worker_epoch=store.current_epoch())
        # A restored or partly cleaned journal: WAL content, but no -shm.
        restored = tmp_path / "restored" / "worker"
        restored.mkdir(mode=0o700, parents=True)
        shutil.copyfile(source_db, restored / "jobs.sqlite3")
        shutil.copyfile(source_db.with_name("jobs.sqlite3-wal"), restored / "jobs.sqlite3-wal")
    finally:
        keeper.close()
    assert (restored / "jobs.sqlite3-wal").stat().st_size > 0
    before = sorted(entry.name for entry in restored.iterdir())
    restored.chmod(0o500)
    try:
        snapshot = read_worker_snapshot(tmp_path / "restored")
    finally:
        restored.chmod(0o700)

    assert snapshot.process_state.value != "unavailable"
    assert snapshot.queue_depth == 2
    assert sorted(entry.name for entry in restored.iterdir()) == before


def test_pid_exists_reports_this_process_alive_and_a_reaped_child_gone():
    import subprocess
    import sys as _sys

    from openswap.worker.runtime import _pid_exists

    child = subprocess.Popen([_sys.executable, "-c", "pass"])
    child.wait()

    assert _pid_exists(os.getpid()) is True
    assert _pid_exists(child.pid) is False


def test_long_socket_path_works_without_a_posix_uid(tmp_path, monkeypatch):
    """Windows has no os.getuid; status and purge build this path there."""
    from openswap.worker import ipc

    monkeypatch.delattr(ipc.os, "getuid", raising=False)
    root = tmp_path / ("x" * 120)

    first = ipc.socket_path(root)
    assert first == ipc.socket_path(root)
    assert first.name == "control.sock"
    assert len(os.fsencode(first)) <= ipc.MAX_SOCKET_PATH_BYTES


def test_pid_exists_never_signals_on_windows(monkeypatch):
    """Signal 0 is CTRL_C_EVENT on Windows: probing must not use os.kill."""
    import openswap.worker.runtime as runtime_module

    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    monkeypatch.setattr(
        runtime_module.os, "kill", lambda *_a: pytest.fail("os.kill used as a Windows probe")
    )
    monkeypatch.setattr(runtime_module, "_pid_exists_windows", lambda pid: pid == 4242)

    assert runtime_module._pid_exists(4242) is True
    assert runtime_module._pid_exists(4243) is False


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
    shutdown = _ShutdownAfterLaunch(adapter)

    result = runtime.reconcile_once(shutdown_event=shutdown)

    assert result.state == expected_state
    assert adapter.interrupt_count == 1
    assert AccountLeaseStore(tmp_path).current().state == expected_lease


def test_journal_failure_while_recording_an_interrupt_is_not_swallowed(tmp_path):
    """A non-stale journal failure must surface (so a restart can recover the
    row) rather than return the stale in-flight record."""
    update_worker_settings(tmp_path, enabled=True)
    adapter = _FakeAdapter(stopped=False)
    identity = stable_account_identity("codex", "journal-failure-test")
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    # A short runtime limit bounds this test if the failure is ever swallowed again.
    runtime.submit(_submission(runtime_limit_s=1))
    real_transition = runtime.store.transition

    def failing_transition(job_id, **kwargs):
        if kwargs.get("new_state") == JobState.INTERRUPTED:
            raise OSError("disk full")
        return real_transition(job_id, **kwargs)

    runtime.store.transition = failing_transition
    shutdown = _ShutdownAfterLaunch(adapter)

    with pytest.raises(OSError, match="disk full"):
        runtime.reconcile_once(shutdown_event=shutdown)
    assert adapter.interrupt_count == 1
    # The run was never relaunched and its lease stays quarantined for recovery.
    assert adapter.start_count == 1
    assert AccountLeaseStore(tmp_path).current().state == "uncertain"


def test_journal_reload_failure_after_an_interrupt_is_not_swallowed(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    adapter = _FakeAdapter(stopped=False)
    identity = stable_account_identity("codex", "journal-reload-test")
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    # A short runtime limit bounds this test if the failure is ever swallowed again.
    runtime.submit(_submission(runtime_limit_s=1))
    real_get = runtime.store.get

    def failing_get(job_id):
        if adapter.interrupt_count:
            raise OSError("journal unreadable")
        return real_get(job_id)

    runtime.store.get = failing_get
    shutdown = _ShutdownAfterLaunch(adapter)

    with pytest.raises(OSError, match="journal unreadable"):
        runtime.reconcile_once(shutdown_event=shutdown)
    assert adapter.interrupt_count == 1 and adapter.start_count == 1
    assert AccountLeaseStore(tmp_path).current().state == "uncertain"


def test_job_that_expires_during_launch_preparation_never_starts(tmp_path):
    update_worker_settings(tmp_path, enabled=True)

    class ExpiringProbe(_FakeAdapter):
        def probe(self):
            # The deadline passes while the probe runs, after the queue's expiry
            # check: moved deterministically, never raced against a wall clock.
            db = runtime.store._connect()
            try:
                db.execute("UPDATE jobs SET expires_at=? WHERE job_id=?",
                           ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), job.job_id))
                db.commit()
            finally:
                db.close()
            return super().probe()

    adapter = ExpiringProbe()
    identity = stable_account_identity("codex", "expiry-during-launch-test")
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission(expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))

    result = runtime.reconcile_once()

    assert result.state == JobState.EXPIRED
    assert result.diagnostic_code == "job_expired"
    assert adapter.start_count == 0
    lease = AccountLeaseStore(tmp_path).current()
    assert lease.state == "released" and lease.reason == "unlaunched"


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


def test_launch_resolves_the_folder_under_the_lifecycle_lock(tmp_path, monkeypatch):
    """A folder change holds the same lock, so it either sees the STARTING job
    as in use or finishes first and this job resolves the new folder."""
    from openswap.locking import FileLock

    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "lifecycle-lock-test")
    runtime = WorkerRuntime(tmp_path, adapter=_FakeAdapter(), account_identity=identity)
    original = runtime._resolve_workspace
    held = []

    def probe(workspace_id, job_id):
        other = FileLock(tmp_path / "worker" / "lifecycle.lock")
        acquired = other.acquire(timeout=0)
        if acquired:
            other.release()
        held.append(not acquired)
        original(workspace_id, job_id)
        raise ValueError("stop before launch")  # keep the test from running the job

    monkeypatch.setattr(runtime, "_resolve_workspace", probe)
    runtime.submit(_submission())
    result = runtime.reconcile_once()
    assert held == [True]
    assert result.state == JobState.FAILED and result.diagnostic_code == "provider_unavailable"


def test_ipc_startup_failure_clears_the_worker_health_record(tmp_path, monkeypatch):
    from openswap.worker import ipc

    update_worker_settings(tmp_path, enabled=True)
    runtimes = []

    def factory(root):
        runtime = WorkerRuntime(root)
        runtimes.append(runtime)
        return runtime

    def failing_serve(path, runtime, stop_event, *, ready_event):
        return None  # could not bind the control socket; never signals ready

    monkeypatch.setattr(ipc, "serve", failing_serve)
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: self.is_set())

    assert run_worker(tmp_path, runtime_factory=factory) == 1
    assert len(runtimes) == 1
    assert read_worker_snapshot(tmp_path).to_dict()["process_state"] == "stopped"


def test_status_reports_a_claude_kickoff_lease_as_quarantined(tmp_path):
    assert read_worker_snapshot(tmp_path).lease_quarantined is False
    store = AccountLeaseStore(tmp_path, "claude")
    token = store.acquire(
        job_id="kickoff-" + "e" * 32,
        account_identity=stable_account_identity("claude", "a@example.test", ""),
        worker_pid=os.getpid(), worker_epoch=1, ttl_s=60,
    )
    store.mark_uncertain(token, "kickoff_timeout")
    assert read_worker_snapshot(tmp_path).lease_quarantined is True


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


class _ShutdownAfterLaunch:
    """A shutdown signal that arrives once the provider has launched.

    A shutdown already set before launch never starts the provider, so tests
    of interrupting a running provider must deliver it afterwards.
    """

    def __init__(self, adapter):
        self._adapter = adapter

    def is_set(self) -> bool:
        return self._adapter.start_count > 0


class _FakeAdapter:
    def __init__(self, events=(), *, stopped=True, block_start=False, block_probe=False):
        self.events_out = tuple(events)
        self.stopped = stopped
        self.block_start = block_start
        self.block_probe = block_probe
        self.probe_entered = threading.Event()
        self.release_probe = threading.Event()
        self.start_entered = threading.Event()
        self.release_start = threading.Event()
        self.start_count = 0
        self.interrupt_count = 0
        self.event_cursors = []
        self.workspace = None

    def probe(self):
        self.probe_entered.set()
        if self.block_probe:
            assert self.release_probe.wait(3)
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
    running, _run, _token, _deadline = crashed._prepare_run(claimed)
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


def test_stop_during_slow_provider_start_is_acknowledged_and_enforced(tmp_path):
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
    # start() runs without the control lock: the stop is journaled while the
    # provider is still starting, then enforced once start returns.
    stopper.join(3)
    assert not stopper.is_alive()
    assert runtime.get(job.job_id).state == JobState.CANCEL_REQUESTED
    adapter.release_start.set()
    runner.join(3)

    assert not runner.is_alive()
    assert results["stop"].accepted is True
    # start() was already committed: the reply says so truthfully.
    assert results["stop"].diagnostic_code == "stop_after_launch_committed"
    assert results["run"].state == JobState.CANCELLED
    assert adapter.start_count == 1
    assert adapter.interrupt_count == 1
    assert AccountLeaseStore(tmp_path).current().state == "released"


@pytest.mark.parametrize("trigger", ["stop", "shutdown"])
def test_hung_provider_start_is_abandoned_as_uncertain_and_blocks_admission(
    tmp_path, monkeypatch, trigger
):
    import openswap.worker.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "START_CANCEL_GRACE_SECONDS", 0.1)
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter(block_start=True)
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    shutdown = threading.Event()
    results = {}
    runner = threading.Thread(
        target=lambda: results.setdefault("run", runtime.reconcile_once(shutdown_event=shutdown))
    )

    runner.start()
    assert adapter.start_entered.wait(3)
    if trigger == "stop":
        assert runtime.stop(job.job_id).accepted is True
    else:
        shutdown.set()
    runner.join(3)

    # start() never returned a handle: nothing could be interrupted, so the
    # job is interrupted and the lease quarantined, not released.
    assert not runner.is_alive()
    assert results["run"].state == JobState.INTERRUPTED
    assert results["run"].diagnostic_code == "execution_uncertain"
    assert AccountLeaseStore(tmp_path).current().state == "uncertain"
    runtime.submit(_submission("key-2"))
    assert runtime.reconcile_once() is None  # the hung start still blocks admission
    assert adapter.start_count == 1
    # The start may still launch, so even a confirmed release must wait.
    assert worker_cli.release_lease(tmp_path, confirm_stopped=True) == (
        False, {}, "provider_start_pending"
    )

    adapter.release_start.set()
    deadline = time.monotonic() + 3
    while (
        AccountLeaseStore(tmp_path).current().reason != "launch_uncertain"
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert adapter.interrupt_count == 1  # the late handle is interrupted
    assert worker_cli.release_lease(tmp_path, confirm_stopped=True) == (
        True, {"lease_state": "released"}, None
    )


@pytest.mark.parametrize("trigger", ["shutdown", "opt_out"])
def test_shutdown_during_slow_probe_never_launches(tmp_path, trigger):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter(block_probe=True)
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    runtime.submit(_submission())
    shutdown = threading.Event()
    results = {}
    runner = threading.Thread(
        target=lambda: results.setdefault("run", runtime.reconcile_once(shutdown_event=shutdown))
    )

    runner.start()
    assert adapter.probe_entered.wait(3)
    if trigger == "shutdown":
        shutdown.set()
    else:
        update_worker_settings(tmp_path, enabled=False)
    adapter.release_probe.set()
    runner.join(3)

    assert not runner.is_alive()
    assert results["run"].state == JobState.FAILED
    assert results["run"].diagnostic_code == "worker_disabled"
    assert adapter.start_count == 0
    assert AccountLeaseStore(tmp_path).current() is None


@pytest.mark.parametrize(
    ("trigger", "expected_state"),
    [("stop", JobState.CANCELLED), ("shutdown", JobState.FAILED)],
)
def test_stop_or_shutdown_just_before_start_never_launches(tmp_path, trigger, expected_state):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter()
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    shutdown = threading.Event()
    real_fence = runtime._launch_fence

    def late_signal_fence(job_id, shutdown_event, *authorization):
        # Lands after the preparation step released the lock, just before
        # the start thread's own fence read.
        if trigger == "stop":
            assert runtime.stop(job.job_id).accepted is True
        else:
            shutdown.set()
        return real_fence(job_id, shutdown_event, *authorization)

    runtime._launch_fence = late_signal_fence
    result = runtime.reconcile_once(shutdown_event=shutdown)

    assert result.state == expected_state
    assert adapter.start_count == 0
    assert AccountLeaseStore(tmp_path).current().state == "released"


def test_stop_right_after_the_launch_fence_reports_the_committed_launch(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    stops = []

    class StopAtStartAdapter(_FakeAdapter):
        def start(self, job, workspace, *, worker_epoch):
            # The fence passed and released the lock; a stop lands before
            # the provider actually launches.
            stops.append(runtime.stop(job.job_id))
            return super().start(job, workspace, worker_epoch=worker_epoch)

    adapter = StopAtStartAdapter(stopped=True)
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    runtime.submit(_submission())

    result = runtime.reconcile_once()

    assert stops[0].accepted is True
    assert stops[0].diagnostic_code == "stop_after_launch_committed"
    assert result.state == JobState.CANCELLED
    assert adapter.start_count == 1 and adapter.interrupt_count == 1
    assert AccountLeaseStore(tmp_path).current().state == "released"


def test_stop_during_slow_probe_cancels_before_launch(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    adapter = _FakeAdapter(block_probe=True)
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=identity)
    job = runtime.submit(_submission())
    results = {}
    runner = threading.Thread(target=lambda: results.setdefault("run", runtime.reconcile_once()))

    runner.start()
    assert adapter.probe_entered.wait(3)
    stopped = runtime.stop(job.job_id)
    adapter.release_probe.set()
    runner.join(3)

    assert not runner.is_alive()
    assert stopped.accepted is True
    assert results["run"].state == JobState.CANCELLED
    assert adapter.start_count == 0
    assert AccountLeaseStore(tmp_path).current() is None


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
    shutdown = _ShutdownAfterLaunch(adapter) if reason == "shutdown" else threading.Event()

    result = runtime.reconcile_once(shutdown_event=shutdown)

    assert result.state == expected_state
    assert AccountLeaseStore(tmp_path).current().state == expected_lease


def test_runtime_limit_counts_time_spent_in_provider_start(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    identity = stable_account_identity("codex", "synthetic-worker-test")
    clock = [0.0]

    class SlowStartAdapter(_FakeAdapter):
        def start(self, job, workspace, *, worker_epoch):
            clock[0] += 9.0  # startup spends most of the 10 s limit
            return super().start(job, workspace, worker_epoch=worker_epoch)

        def events(self, run, *, after_cursor):
            clock[0] += 2.0
            return super().events(run, after_cursor=after_cursor)

    adapter = SlowStartAdapter(stopped=True)
    runtime = WorkerRuntime(
        tmp_path, adapter=adapter, account_identity=identity, monotonic=lambda: clock[0],
    )
    runtime.submit(_submission(runtime_limit_s=10))

    result = runtime.reconcile_once()

    # Without the startup time the loop would have granted a fresh 10 s.
    assert result.state == JobState.FAILED
    assert result.diagnostic_code == "runtime_limit_reached"
    assert len(adapter.event_cursors) == 1


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
    assert adapter.events_entered.wait(15)

    if trigger == "stop":
        assert runtime.stop(first.job_id).accepted is True
    elif trigger == "shutdown":
        shutdown.set()
    else:
        clock[0] = 601.0

    runner.join(15)
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
        reader.join(15)
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
        stop_event.wait(30)

    monkeypatch.setattr("openswap.worker.ipc.serve", fake_serve)
    worker = threading.Thread(
        target=lambda: results.append(run_worker(tmp_path, runtime_factory=runtime_factory)),
        daemon=True,
    )
    worker.start()
    assert runtime_ready.wait(15)
    runtime = captured_runtime[0]
    job = runtime.submit(_submission())
    assert adapter.start_entered.wait(15)

    # Simulate a local settings change while this manually started worker is
    # running, without relying on the LaunchAgent lifecycle.
    update_worker_settings(tmp_path, enabled=False)
    worker.join(timeout=15)

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
