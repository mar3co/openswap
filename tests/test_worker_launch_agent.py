"""Unit tests for the opt-in worker LaunchAgent wrapper.

All launchctl calls are faked. These tests cover the plist and command shapes,
not launchd behavior or the user's login session.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from openswap.exceptions import ClaudeSwitchError
from openswap.worker import launch_agent as worker_launch_agent

PROGRAM = ["/Users/x/.local/bin/openswap"]
UID = 501


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["launchctl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture(autouse=True)
def _on_macos(monkeypatch):
    monkeypatch.setattr(worker_launch_agent.sys, "platform", "darwin")


def test_worker_plist_has_separate_label_and_headless_argv(tmp_path):
    parsed = plistlib.loads(
        worker_launch_agent.build_plist(PROGRAM, home=tmp_path)
    )

    assert parsed["Label"] == worker_launch_agent.LABEL
    assert parsed["Label"] != "com.opensoft.openswap.menubar"
    assert parsed["ProgramArguments"] == [*PROGRAM, "worker", "run"]
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert "ProcessType" not in parsed
    assert parsed["StandardOutPath"].endswith(f"{worker_launch_agent.LABEL}.log")


def test_worker_service_status_is_read_only_and_parses_job_fields(tmp_path, monkeypatch):
    calls = []

    def run(*args):
        calls.append(args)
        return _completed(
            stdout=(
                "gui/501/com.opensoft.openswap.worker = {\n"
                "\tstate = running\n"
                "\tpid = 4321\n"
                "\tsubservice = {\n"
                "\t\tstate = bogus\n"
                "\t\tpid = 9999\n"
                "\t}\n"
                "}\n"
            )
        )

    monkeypatch.setattr(worker_launch_agent, "_launchctl", run)
    result = worker_launch_agent.status(home=tmp_path, uid=UID)

    assert calls == [("print", f"gui/{UID}/{worker_launch_agent.LABEL}")]
    assert result["loaded"] is True
    assert result["installed"] is False
    assert result["state"] == "running"
    assert result["pid"] == 4321
    assert not worker_launch_agent.plist_path(tmp_path).exists()


def test_install_writes_worker_plist_and_bootstraps_only_worker_service(
    tmp_path, monkeypatch
):
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == "print":
            return _completed(3)
        return _completed()

    monkeypatch.setattr(worker_launch_agent, "_launchctl", run)
    result = worker_launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    plist = plistlib.loads(worker_launch_agent.plist_path(tmp_path).read_bytes())
    assert plist["ProgramArguments"] == [*PROGRAM, "worker", "run"]
    assert calls == [
        ("print", f"gui/{UID}/{worker_launch_agent.LABEL}"),
        (
            "bootstrap",
            f"gui/{UID}",
            str(worker_launch_agent.plist_path(tmp_path)),
        ),
    ]
    assert result["program"] == [*PROGRAM, "worker", "run"]
    assert result["already_loaded"] is False


def test_install_is_idempotent_for_loaded_worker_and_does_not_rewrite_plist(
    tmp_path, monkeypatch
):
    target = worker_launch_agent.plist_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing plist")
    calls = []

    def run(*args):
        calls.append(args)
        return _completed(stdout="\tstate = running\n")

    monkeypatch.setattr(worker_launch_agent, "_launchctl", run)
    result = worker_launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    assert result["already_loaded"] is True
    assert calls == [("print", f"gui/{UID}/{worker_launch_agent.LABEL}")]
    assert target.read_bytes() == b"existing plist"


def test_uninstall_boots_out_worker_and_removes_only_its_plist(tmp_path, monkeypatch):
    target = worker_launch_agent.plist_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"worker plist")
    calls = []

    print_count = 0

    def run(*args):
        nonlocal print_count
        calls.append(args)
        if args[0] == "print":
            print_count += 1
            return _completed(0 if print_count == 1 else 3)
        return _completed()

    monkeypatch.setattr(worker_launch_agent, "_launchctl", run)
    result = worker_launch_agent.uninstall(home=tmp_path, uid=UID)

    assert result == {
        "label": worker_launch_agent.LABEL,
        "was_loaded": True,
        "removed_plist": True,
    }
    assert calls == [
        ("print", f"gui/{UID}/{worker_launch_agent.LABEL}"),
        ("bootout", f"gui/{UID}/{worker_launch_agent.LABEL}"),
        ("print", f"gui/{UID}/{worker_launch_agent.LABEL}"),
    ]
    assert not target.exists()


def test_install_refuses_non_macos_without_calling_launchctl(monkeypatch):
    monkeypatch.setattr(worker_launch_agent.sys, "platform", "linux")
    with patch.object(worker_launch_agent, "_launchctl") as launchctl:
        with pytest.raises(ClaudeSwitchError, match="only available on macOS"):
            worker_launch_agent.install(program=PROGRAM)
    launchctl.assert_not_called()


def test_launchctl_calls_have_a_timeout_and_sanitize_timeout_errors(monkeypatch):
    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == worker_launch_agent.LAUNCHCTL_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], stderr="private details")

    monkeypatch.setattr(worker_launch_agent.subprocess, "run", timed_out)
    with pytest.raises(ClaudeSwitchError, match="launchctl command timed out") as exc:
        worker_launch_agent._launchctl("print", "gui/501/com.opensoft.openswap.worker")
    assert "private details" not in str(exc.value)
