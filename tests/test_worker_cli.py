"""Focused CLI lifecycle safety tests for the opt-in local worker."""

from __future__ import annotations

from pathlib import Path
import os
import stat

from openswap.worker import cli
from openswap.worker.journal import LocalJobStore
from openswap.settings import load_worker_settings, update_worker_settings


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
        lambda _root, **values: writes.append(values),
    )
    monkeypatch.setattr(cli, "uninstall", lambda **_kwargs: {"unloaded": True})
    monkeypatch.setattr(cli, "_DISABLE_POLL_SECONDS", 0)

    ok, result, diagnostic = cli.disable_worker(tmp_path)

    assert ok is True
    assert diagnostic is None
    assert client.paused == [True]
    assert client.stop_ids == ["job-safe-1"]
    assert writes == [
        {"enabled": True, "paused": True},
        {"enabled": False, "paused": True},
    ]
    assert result["snapshot"]["active_job"] is None


def test_disable_keeps_worker_enabled_paused_when_lease_state_is_unknown(
    tmp_path: Path, monkeypatch
):
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
        lambda _root, **values: writes.append(values),
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
    assert writes[0] == {"enabled": True, "paused": True}
    assert writes[-1] == {"enabled": True, "paused": True}


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
