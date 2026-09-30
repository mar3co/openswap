"""Focused CLI lifecycle safety tests for the opt-in local worker."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import os
import time
import stat
import subprocess
import sys

import pytest

from openswap.worker import cli
from openswap.worker.journal import LocalJobStore
from openswap.worker.leases import AccountLeaseStore, ReleaseEvidence, stable_account_identity
from openswap.worker.models import JobState
from openswap.settings import load_worker_settings, update_worker_settings
from tests.test_worker_core import _submission


def _running_job(store: LocalJobStore, *, epoch: int) -> str:
    """Drive a freshly created job to RUNNING, as the runtime would in flight; returns its id."""
    created = store.create(_submission(), owner_ref="local-user", worker_epoch=epoch)
    claimed = store.claim(created.job_id, worker_epoch=epoch, expected_generation=created.generation)
    starting = store.transition(
        claimed.job_id, expected_states=(JobState.CLAIMED,), new_state=JobState.STARTING,
        worker_epoch=epoch, expected_generation=claimed.generation,
    )
    store.transition(
        starting.job_id, expected_states=(JobState.STARTING,), new_state=JobState.RUNNING,
        worker_epoch=epoch, expected_generation=starting.generation,
    )
    return starting.job_id


def _dead_pid() -> int:
    """A pid guaranteed to no longer belong to any process."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class _Client:
    def __init__(self, _path):
        self.stop_ids = []
        self.paused = []

    def set_paused(self, paused):
        self.paused.append(paused)
        return {"accepted": True}

    def stop(self, job_id):
        self.stop_ids.append(job_id)
        return {"accepted": True, "job_id": job_id}


def test_disable_pauses_targets_the_snapshot_job_and_waits_for_terminal_state(
    tmp_path: Path, monkeypatch
):
    writes = []
    was_enabled = True
    snapshots = iter(
        [
            {
                "process_state": "running",
                "enabled": True,
                "paused": True,
                "lease_quarantined": False,
                "active_job": {"job_id": "job-safe-1", "state": "running"},
            },
            {
                "process_state": "running",
                "enabled": True,
                "paused": True,
                "lease_quarantined": False,
                "active_job": None,
            },
        ]
    )
    client = _Client(None)
    monkeypatch.setattr(cli, "WorkerClient", lambda _path: client)
    monkeypatch.setattr(cli, "_snapshot", lambda _root: next(snapshots))
    monkeypatch.setattr(
        cli,
        "update_worker_settings",
        lambda _root, **values: writes.append(values) or SimpleNamespace(enabled=was_enabled),
    )
    monkeypatch.setattr(cli, "uninstall", lambda **_kwargs: {"unloaded": True})
    monkeypatch.setattr(cli, "_DISABLE_POLL_SECONDS", 0)

    ok, result, diagnostic = cli.disable_worker(tmp_path)

    assert ok is True
    assert diagnostic is None
    assert client.paused == [True]
    assert client.stop_ids == ["job-safe-1"]
    assert writes == [
        {"paused": True},
        {"enabled": False, "paused": True},
    ]
    assert result["snapshot"]["active_job"] is None


@pytest.mark.parametrize("was_enabled", [True, False])
def test_disable_keeps_prior_opt_in_paused_when_lease_state_is_unknown(
    tmp_path: Path, monkeypatch, was_enabled: bool
):
    """A blocked disable pauses without changing the opt-in, so retrying it on
    an already-disabled worker never re-enables Remote tasks."""
    writes = []
    client = _Client(None)
    snapshot = {
        "process_state": "running",
        "enabled": True,
        "paused": True,
        "lease_quarantined": True,
        "active_job": None,
    }
    monkeypatch.setattr(cli, "WorkerClient", lambda _path: client)
    monkeypatch.setattr(cli, "_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(
        cli,
        "update_worker_settings",
        lambda _root, **values: writes.append(values) or SimpleNamespace(enabled=was_enabled),
    )
    monkeypatch.setattr(
        cli,
        "uninstall",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unsafe unload")),
    )

    ok, _result, diagnostic = cli.disable_worker(tmp_path)

    assert ok is False
    assert diagnostic == "lease_state_unknown"
    assert client.paused == [True]
    assert writes[0] == {"paused": True}
    assert writes[-1] == {"enabled": was_enabled, "paused": True}


@pytest.mark.parametrize("was_enabled", [True, False])
def test_disable_keeps_prior_opt_in_paused_when_unload_fails(
    tmp_path: Path, monkeypatch, was_enabled: bool
):
    writes = []
    client = _Client(None)
    snapshot = {
        "process_state": "stopped",
        "enabled": was_enabled,
        "paused": True,
        "lease_quarantined": False,
        "active_job": None,
    }
    monkeypatch.setattr(cli, "WorkerClient", lambda _path: client)
    monkeypatch.setattr(cli, "_snapshot", lambda _root: snapshot)
    monkeypatch.setattr(
        cli,
        "update_worker_settings",
        lambda _root, **values: writes.append(values) or SimpleNamespace(enabled=was_enabled),
    )
    monkeypatch.setattr(
        cli,
        "uninstall",
        lambda **_kwargs: (_ for _ in ()).throw(cli.ClaudeSwitchError("bootout failed")),
    )

    ok, _result, diagnostic = cli.disable_worker(tmp_path)

    assert ok is False
    assert diagnostic == "worker_unload_failed"
    assert writes[-1] == {"enabled": was_enabled, "paused": True}


def test_disable_waits_for_manual_worker_and_enable_refuses_held_instance_lock(
    tmp_path: Path, monkeypatch, capsys
):
    from openswap.exceptions import ClaudeSwitchError
    from openswap.locking import FileLock

    update_worker_settings(tmp_path, enabled=True)
    LocalJobStore(tmp_path)._ensure_private_dir()
    instance_lock = FileLock(tmp_path / "worker" / "instance.lock", timeout=0)
    assert instance_lock.acquire(timeout=0)
    monkeypatch.setattr(cli, "WorkerClient", lambda _path: _Client(None))
    monkeypatch.setattr(
        cli,
        "_snapshot",
        lambda _root: {
            "process_state": "running",
            "enabled": True,
            "paused": True,
            "lease_quarantined": False,
            "active_job": None,
        },
    )
    monkeypatch.setattr(cli, "uninstall", lambda **_kwargs: {"unloaded": False})
    monkeypatch.setattr(cli, "_DISABLE_EXIT_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(cli, "_DISABLE_POLL_SECONDS", 0.001)
    installs = []
    monkeypatch.setattr(cli, "install", lambda: installs.append(True))

    try:
        exit_code = cli.main(["disable"], backup_root=tmp_path)

        assert exit_code == 1
        assert capsys.readouterr().err == (
            "Worker stop is not confirmed; it remains disabled and "
            "admission-paused. Wait for it to exit before enabling.\n"
        )
        policy = load_worker_settings(tmp_path)
        assert policy.enabled is False
        assert policy.paused is True

        with pytest.raises(ClaudeSwitchError, match="worker_stop_unconfirmed"):
            cli.enable_worker(tmp_path)
        assert load_worker_settings(tmp_path).enabled is False
        assert installs == []
    finally:
        instance_lock.release()

    assert cli._worker_instance_lock_is_free(tmp_path) is True
    assert cli.enable_worker(tmp_path)["enabled"] is True
    assert load_worker_settings(tmp_path).enabled is True
    assert installs == [True]


def test_enable_creates_private_worker_root_before_lifecycle_lock(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(cli, "install", lambda: {"already_loaded": False})

    result = cli.enable_worker(tmp_path)

    worker_dir = tmp_path / "worker"
    assert result["enabled"] is True
    assert load_worker_settings(tmp_path).enabled is True
    info = worker_dir.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert not stat.S_ISLNK(info.st_mode)
    if os.name == "posix":  # Windows has no POSIX owner or mode bits
        assert info.st_uid == os.getuid()
        assert stat.S_IMODE(info.st_mode) == 0o700
    LocalJobStore(tmp_path)._ensure_private_dir()


def test_worker_enable_migrates_legacy_backup_before_creating_worker_root(
    temp_home: Path, monkeypatch, capsys
):
    legacy = temp_home / ".claude-swap-backup"
    legacy.mkdir()
    (legacy / "accounts.json").write_text('{"kept":true}', encoding="utf-8")
    target = temp_home / "Library" / "Application Support" / "OpenSwap"
    monkeypatch.setattr(cli.paths, "get_legacy_backup_root", lambda: legacy)
    monkeypatch.setattr(cli, "install", lambda: {"already_loaded": False})

    result = cli.enable_worker(target)

    assert result["enabled"] is True
    assert not legacy.exists()
    assert (target / "accounts.json").read_text(encoding="utf-8") == '{"kept":true}'
    assert (target / "worker").is_dir()
    assert cli.paths.migrate_legacy_backup_dir(target) is False
    assert capsys.readouterr().err == (
        f"openswap: migrated data from {legacy} to {target}\n"
    )


def test_status_reports_a_stopped_worker_on_hosts_without_unix_sockets(
    tmp_path: Path, monkeypatch
):
    """Windows has no AF_UNIX: status (and purge, which reads it) must fall
    back to the read-only snapshot instead of raising."""
    import socket

    monkeypatch.delattr(socket, "AF_UNIX", raising=False)

    assert cli.read_status(tmp_path)["process_state"] == "stopped"


@pytest.mark.parametrize("enabled", [True, False])
def test_blocked_disable_reports_the_persisted_opt_in(
    tmp_path: Path, monkeypatch, capsys, enabled: bool
):
    update_worker_settings(tmp_path, enabled=enabled)
    monkeypatch.setattr(cli, "disable_worker", lambda _root: (False, {}, "lease_state_unknown"))

    assert cli.main(["disable"], backup_root=tmp_path) == 1

    state = "enabled" if enabled else "disabled"
    assert capsys.readouterr().err == (
        f"Worker remains {state} and admission-paused (lease_state_unknown).\n"
    )


def test_worker_status_does_not_migrate_legacy_backup(
    temp_home: Path, monkeypatch, capsys
):
    legacy = temp_home / ".claude-swap-backup"
    legacy.mkdir()
    (legacy / "accounts.json").write_text('{"keep":true}', encoding="utf-8")
    target = temp_home / "Library" / "Application Support" / "OpenSwap"
    monkeypatch.setattr(cli.paths, "get_legacy_backup_root", lambda: legacy)

    result = cli.main(["status", "--json"], backup_root=target)

    assert result == 0
    assert legacy.exists()
    assert (legacy / "accounts.json").exists()
    assert not target.exists()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("service_loaded, refused", [(False, True), (True, False)])
def test_enable_refuses_while_a_manual_worker_holds_the_instance_lock(
    tmp_path: Path, monkeypatch, service_loaded, refused
):
    """An enabled policy with a manual `worker run` daemon must not install a
    second, lock-starved managed worker; a loaded LaunchAgent stays idempotent."""
    update_worker_settings(tmp_path, enabled=True)
    installs = []
    monkeypatch.setattr(cli, "_worker_instance_lock_is_free", lambda root: False)
    monkeypatch.setattr(cli, "worker_service_status", lambda: {"loaded": service_loaded})
    monkeypatch.setattr(cli, "install", lambda: installs.append(1) or {"already_loaded": service_loaded})
    if refused:
        with pytest.raises(cli.ClaudeSwitchError, match="worker_running_unmanaged"):
            cli.enable_worker(tmp_path)
        assert installs == []
    else:
        assert cli.enable_worker(tmp_path)["enabled"] is True
        assert installs == [1]


def test_manual_run_refuses_while_the_launch_agent_is_loaded(tmp_path: Path, monkeypatch, capsys):
    from openswap.worker import runtime as worker_runtime

    update_worker_settings(tmp_path, enabled=True)
    monkeypatch.setattr(cli, "_managed_worker_loaded", lambda: True)
    started = []

    def factory(root):
        started.append(root)
        raise RuntimeError("must not start")

    real_run_worker = worker_runtime.run_worker
    monkeypatch.setattr(
        worker_runtime, "run_worker",
        lambda root, **kwargs: real_run_worker(root, runtime_factory=factory, **kwargs),
    )
    assert cli.main(["run"], backup_root=tmp_path) == 1
    assert "LaunchAgent is running the worker" in capsys.readouterr().err
    assert started == []

    # The LaunchAgent's own start passes --managed and is not refused.
    assert cli.main(["run", "--managed"], backup_root=tmp_path) == 1  # factory raised
    assert started == [tmp_path]


def test_enable_refuses_invalid_pinned_account_without_installing(
    tmp_path: Path, monkeypatch, capsys
):
    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"worker":{"enabled":false,"paused":false,'
        '"pinnedAccountRef":"invalid reference"}}',
        encoding="utf-8",
    )
    installs = []
    monkeypatch.setattr(cli, "install", lambda: installs.append(True))

    exit_code = cli.main(["enable"], backup_root=tmp_path)

    assert exit_code == 1
    assert capsys.readouterr().err == (
        "Worker configuration is invalid; fix local worker settings before enabling.\n"
    )
    assert installs == []
    assert load_worker_settings(tmp_path).enabled is False


def test_rejected_pause_restores_previous_paused_policy(tmp_path: Path, monkeypatch):
    class RefusingClient:
        def __init__(self, _path):
            pass

        def set_paused(self, paused):
            assert paused is True
            return {"accepted": False, "diagnostic_code": "pause_refused"}

    update_worker_settings(tmp_path, enabled=True, paused=True)
    monkeypatch.setattr(cli, "WorkerClient", RefusingClient)

    result = cli.request_pause(tmp_path, True)

    assert result == {
        "accepted": False,
        "paused": True,
        "diagnostic_code": "pause_refused",
    }
    assert load_worker_settings(tmp_path).paused is True


def test_release_lease_refuses_while_recording_worker_pid_is_alive(tmp_path: Path):
    """A live worker (same epoch, live pid) must keep owning its lease."""
    job_store = LocalJobStore(tmp_path)
    job_store._ensure_private_dir()
    epoch = job_store.current_epoch()
    lease_store = AccountLeaseStore(tmp_path, "codex")
    token = lease_store.acquire(
        job_id="job-nonexistent",
        account_identity=stable_account_identity("codex", "acct-a"),
        worker_pid=os.getpid(),
        worker_epoch=epoch,
        ttl_s=60,
    )

    ok, result, diagnostic = cli.release_lease(tmp_path)

    assert ok is False
    assert result == {}
    assert diagnostic == "worker_owner_may_be_alive"
    assert AccountLeaseStore(tmp_path, "codex").current().token() == token


def test_release_lease_refuses_while_its_job_is_not_terminal(tmp_path: Path):
    """The recorded worker being gone is not enough while its job may still run."""
    job_store = LocalJobStore(tmp_path)
    epoch = job_store.current_epoch()
    job_id = _running_job(job_store, epoch=epoch)
    lease_store = AccountLeaseStore(tmp_path, "codex")
    token = lease_store.acquire(
        job_id=job_id,
        account_identity=stable_account_identity("codex", "acct-a"),
        worker_pid=_dead_pid(),
        worker_epoch=epoch,
        ttl_s=60,
    )

    ok, result, diagnostic = cli.release_lease(tmp_path)

    assert ok is False
    assert result == {}
    assert diagnostic == "job_not_terminal"
    assert AccountLeaseStore(tmp_path, "codex").current().token() == token


def test_release_lease_succeeds_after_worker_gone_and_job_terminal_then_switch_works(
    tmp_path: Path,
):
    """Once a crash-restart proves the owner gone, release unblocks account mutations."""
    from openswap.worker.models import ProviderAvailability, ProviderRun
    from openswap.worker.runtime import WorkerRuntime

    class _StartsThenHangsAdapter:
        """Starts a run and never reports it finished: models a crashed worker."""

        def probe(self):
            return ProviderAvailability(True, None, "fake-test")

        def start(self, job, workspace, *, worker_epoch):
            return ProviderRun(None, "synthetic-session", worker_epoch, job.generation, 20)

    identity = stable_account_identity("codex", "acct-a")
    update_worker_settings(tmp_path, enabled=True)
    runtime = WorkerRuntime(
        tmp_path, adapter=_StartsThenHangsAdapter(), account_identity=identity,
    )
    job = runtime.submit(_submission())
    claimed = runtime.store.claim(
        job.job_id, worker_epoch=runtime.worker_epoch, expected_generation=job.generation,
    )
    running, _run, _token, _deadline = runtime._prepare_run(claimed)
    assert running.state == JobState.RUNNING
    stuck_lease = AccountLeaseStore(tmp_path, "codex").current()
    assert stuck_lease.state == "active"

    # The worker crashes without ever releasing the lease or stopping the
    # job. A fresh worker instance starts in its place; start_epoch already
    # interrupts the orphaned job, and __init__ quarantines its lease.
    WorkerRuntime(tmp_path, adapter=_StartsThenHangsAdapter(), account_identity=identity)
    assert runtime.store.get(job.job_id).state == JobState.INTERRUPTED
    assert AccountLeaseStore(tmp_path, "codex").current().state == "uncertain"

    # INTERRUPTED records uncertainty, not a stop: the owner must confirm.
    assert cli.release_lease(tmp_path) == (False, {}, "stop_unproven_confirm_required")
    ok, result, diagnostic = cli.release_lease(tmp_path, confirm_stopped=True)

    assert ok is True
    assert diagnostic is None
    assert result == {"lease_state": "released"}
    released = AccountLeaseStore(tmp_path, "codex").current()
    assert released.state == "released"
    assert released.reason == ReleaseEvidence.OWNER_RELEASED.value

    # Switching the account is the real-world proof the account is usable again.
    with AccountLeaseStore(tmp_path, "codex").mutation_guard() as guard:
        guard.assert_available()  # does not raise


def _stranded_kickoff_lease(root: Path, provider: str, *, ttl_s: float) -> None:
    AccountLeaseStore(root, provider).acquire(
        job_id="kickoff-" + "b" * 32,
        account_identity=stable_account_identity(provider, "acct-a"),
        worker_pid=_dead_pid(),
        worker_epoch=time.time_ns(),
        ttl_s=ttl_s,
    )


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_release_lease_needs_expiry_and_confirmation_for_a_stranded_kickoff_lease(
    tmp_path: Path, provider: str
):
    """A kickoff child can outlive its killed menu process and is never
    journaled, so a missing job row is not stop evidence."""
    _stranded_kickoff_lease(tmp_path, provider, ttl_s=0.05)
    time.sleep(0.1)

    assert cli.release_lease(tmp_path, provider) == (False, {}, "stop_unproven_confirm_required")
    assert AccountLeaseStore(tmp_path, provider).current().state != "released"

    ok, result, diagnostic = cli.release_lease(tmp_path, provider, confirm_stopped=True)
    assert (ok, diagnostic) == (True, None)
    lease = AccountLeaseStore(tmp_path, provider).current()
    assert lease.state == "released" and lease.reason == "owner_released"


def test_release_lease_refuses_an_unexpired_kickoff_lease_even_when_confirmed(tmp_path: Path):
    _stranded_kickoff_lease(tmp_path, "claude", ttl_s=600)

    assert cli.release_lease(tmp_path, "claude", confirm_stopped=True) == (
        False, {}, "lease_not_expired",
    )


def test_release_lease_treats_a_missing_worker_job_row_as_unproven(tmp_path: Path):
    job_store = LocalJobStore(tmp_path)
    job_store._ensure_private_dir()
    AccountLeaseStore(tmp_path, "codex").acquire(
        job_id="c" * 32,
        account_identity=stable_account_identity("codex", "acct-a"),
        worker_pid=_dead_pid(),
        worker_epoch=job_store.current_epoch(),
        ttl_s=60,
    )

    assert cli.release_lease(tmp_path) == (False, {}, "stop_unproven_confirm_required")
    assert cli.release_lease(tmp_path, confirm_stopped=True)[0] is True


def test_release_lease_needs_confirmation_for_an_interrupted_job(tmp_path: Path):
    """INTERRUPTED records uncertainty, not a stop, even under a newer epoch."""
    job_store = LocalJobStore(tmp_path)
    epoch = job_store.current_epoch()
    job_id = _running_job(job_store, epoch=epoch)
    AccountLeaseStore(tmp_path, "codex").acquire(
        job_id=job_id,
        account_identity=stable_account_identity("codex", "acct-a"),
        worker_pid=_dead_pid(),
        worker_epoch=epoch,
        ttl_s=60,
    )
    job_store.start_epoch(os.getpid())  # a replacement worker interrupts the row
    assert job_store.get(job_id).state == JobState.INTERRUPTED

    assert cli.release_lease(tmp_path) == (False, {}, "stop_unproven_confirm_required")
    assert cli.release_lease(tmp_path, confirm_stopped=True) == (True, {"lease_state": "released"}, None)


def test_release_lease_from_a_live_worker_needs_a_terminal_job_and_confirmation(tmp_path: Path):
    """The recording worker is still running (same epoch, live pid): an
    unproven interrupt quarantined the lease, and the owner's confirmation is
    the only way back without killing the worker."""
    job_store = LocalJobStore(tmp_path)
    epoch = job_store.current_epoch()
    job_id = _running_job(job_store, epoch=epoch)
    lease_store = AccountLeaseStore(tmp_path, "codex")
    token = lease_store.acquire(
        job_id=job_id,
        account_identity=stable_account_identity("codex", "acct-a"),
        worker_pid=os.getpid(),
        worker_epoch=epoch,
        ttl_s=60,
    )

    # While the job is still running, the live worker keeps its lease.
    assert cli.release_lease(tmp_path, confirm_stopped=True) == (
        False, {}, "worker_owner_may_be_alive"
    )

    running = job_store.get(job_id)
    job_store.transition(
        job_id, expected_states=(JobState.RUNNING,), new_state=JobState.INTERRUPTED,
        worker_epoch=epoch, expected_generation=running.generation,
        diagnostic_code="execution_uncertain",
    )
    lease_store.mark_uncertain(token, "execution_uncertain")

    assert cli.release_lease(tmp_path) == (False, {}, "stop_unproven_confirm_required")
    assert cli.release_lease(tmp_path, confirm_stopped=True) == (
        True, {"lease_state": "released"}, None
    )


def test_release_lease_recovers_an_uncertain_usage_lease_held_by_a_live_menu(tmp_path: Path):
    """A usage read's lease is a short-lived probe: its long-lived holder (the
    menu process) stays alive, so expiry plus confirmation must suffice."""
    store = AccountLeaseStore(tmp_path, "codex")
    token = store.acquire(
        job_id="usage-" + "d" * 32,
        account_identity=stable_account_identity("codex", "acct-a"),
        worker_pid=os.getpid(),  # the menu process is still running
        worker_epoch=time.time_ns(),
        ttl_s=0.05,
    )
    store.mark_uncertain(token, "usage_stop_unconfirmed")
    time.sleep(0.1)

    assert cli.release_lease(tmp_path) == (False, {}, "stop_unproven_confirm_required")
    assert cli.release_lease(tmp_path, confirm_stopped=True)[0] is True
    assert AccountLeaseStore(tmp_path, "codex").current().state == "released"


def test_lease_release_cli_selects_the_claude_store(tmp_path: Path, capsys):
    _stranded_kickoff_lease(tmp_path, "claude", ttl_s=0.05)
    time.sleep(0.1)

    assert cli.main(["lease", "release", "--provider", "claude"], backup_root=tmp_path) == 1
    assert "--confirm-stopped" in capsys.readouterr().err
    assert cli.main(
        ["lease", "release", "--provider", "claude", "--confirm-stopped"], backup_root=tmp_path
    ) == 0
    assert AccountLeaseStore(tmp_path, "claude").current().state == "released"
