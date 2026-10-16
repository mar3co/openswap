"""Tests for the launchd LaunchAgent that keeps ``openswap menubar`` alive.

``subprocess.run`` is patched throughout so the real ``launchctl`` is never
driven: these assert the argv this module *shapes* and how it reads launchctl's
replies, not launchd's behaviour. Every test pins ``home`` to a tmp path and
passes an explicit ``uid``, so nothing here depends on the machine it runs on
or writes outside the temp directory.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from openswap import launch_agent
from openswap.exceptions import ClaudeSwitchError

PROGRAM = ["/Users/x/.local/bin/openswap"]
UID = 501
_RUNS_FROM_TEMPORARY_LOCATION = launch_agent.bundle.runs_from_temporary_location


@pytest.fixture(autouse=True)
def _on_macos():
    """The module refuses off-darwin; these tests exercise the darwin path."""
    with patch.object(launch_agent.sys, "platform", "darwin"):
        yield


def _completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["launchctl"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _router(responses: dict[str, subprocess.CompletedProcess]):
    """Answer per launchctl subcommand, defaulting to success."""

    def run(argv, **kwargs):
        return responses.get(argv[1], _completed(0))

    return run


# --- plist shape -----------------------------------------------------------


def test_build_plist_is_parseable_and_runs_the_menubar_subcommand(tmp_path):
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["Label"] == launch_agent.LABEL
    assert parsed["ProgramArguments"] == [*PROGRAM, "menubar"]
    assert parsed["RunAtLoad"] is True


def test_build_plist_keepalive_restarts_a_crash_but_respects_a_quit(tmp_path):
    """A bare `KeepAlive: True` would make the menu bar's Quit item a no-op.

    menubar.py's quit handler calls rumps.quit_application() — a clean exit(0)
    — so unconditional KeepAlive relaunches it immediately and the user has no
    way to stop the menu bar short of `launchctl bootout`.
    """
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}


def test_build_plist_marks_the_agent_interactive_not_background(tmp_path):
    # Background would have launchd throttle a process that owns UI.
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["ProcessType"] == "Interactive"


def test_build_plist_survives_paths_that_would_break_hand_written_xml(tmp_path):
    odd = tmp_path / "home & <co>"
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=odd))
    assert parsed["StandardErrorPath"] == str(odd / "Library/Logs" / f"{launch_agent.LABEL}.err")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX path shapes; the agent only ever runs on macOS",
)
def test_build_plist_path_env_leads_with_the_programs_own_directory(tmp_path):
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    assert parsed["EnvironmentVariables"]["PATH"].split(":")[0] == "/Users/x/.local/bin"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX path shapes; the agent only ever runs on macOS",
)
def test_build_plist_path_env_includes_the_user_bin_dir(tmp_path):
    # A uv tool install puts openswap's siblings in ~/.local/bin; launchd's own
    # default PATH does not include it.
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    entries = parsed["EnvironmentVariables"]["PATH"].split(":")
    assert os.path.expanduser("~/.local/bin") in entries


def test_build_plist_path_env_keeps_the_launchd_defaults(tmp_path):
    parsed = plistlib.loads(launch_agent.build_plist(PROGRAM, home=tmp_path))
    entries = parsed["EnvironmentVariables"]["PATH"].split(":")
    assert {"/usr/bin", "/bin", "/usr/sbin", "/sbin"} <= set(entries)


# --- program resolution ----------------------------------------------------


def test_resolve_program_prefers_the_console_script(tmp_path):
    script = tmp_path / "openswap"
    script.write_text("#!/bin/sh\n")
    with patch.object(launch_agent.sys, "argv", [str(script)]):
        assert launch_agent.resolve_program() == [str(script)]


def test_resolve_program_keeps_the_symlink_and_does_not_follow_it(tmp_path):
    """`uv tool install` links ~/.local/bin/openswap into the tool's virtualenv.

    Resolving that symlink would pin the virtualenv-internal path, which a
    reinstall recreates — exactly the path this module exists to avoid.
    """
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    real = venv_bin / "openswap"
    real.write_text("#!/bin/sh\n")
    link_dir = tmp_path / "local" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "openswap"
    link.symlink_to(real)

    with patch.object(launch_agent.sys, "argv", [str(link)]):
        assert launch_agent.resolve_program() == [str(link)]


def test_resolve_program_makes_a_relative_argv0_absolute(tmp_path, monkeypatch):
    script = tmp_path / "openswap"
    script.write_text("#!/bin/sh\n")
    monkeypatch.chdir(tmp_path)
    with patch.object(launch_agent.sys, "argv", ["./openswap"]):
        result = launch_agent.resolve_program()
    assert result == [str(script)]
    assert Path(result[0]).is_absolute()


def test_resolve_program_falls_back_to_the_interpreter_without_a_script(tmp_path):
    with patch.object(launch_agent.sys, "argv", [str(tmp_path / "gone")]):
        with patch.object(launch_agent.shutil, "which", return_value=None):
            assert launch_agent.resolve_program() == [sys.executable, "-m", "openswap"]


def test_resolve_program_ignores_an_argv0_that_is_not_openswap(tmp_path):
    # Running through pytest, argv[0] is the test runner — not a thing launchd
    # should be pointed at.
    other = tmp_path / "pytest"
    other.write_text("#!/bin/sh\n")
    found = tmp_path / "openswap"
    found.write_text("#!/bin/sh\n")
    with patch.object(launch_agent.sys, "argv", [str(other)]):
        with patch.object(launch_agent.shutil, "which", return_value=str(found)):
            assert launch_agent.resolve_program() == [str(found)]


def test_resolve_program_pins_the_in_bundle_executable_when_frozen(frozen_app, tmp_path):
    """From a terminal argv[0] is the cask's symlink; the plist must not keep it.

    The bundle path is stable across `brew upgrade`, and launchd starting the
    bundle binary keeps the app's LaunchServices identity.
    """
    link = tmp_path / "bin" / "openswap"
    link.parent.mkdir()
    link.symlink_to(frozen_app / "Contents" / "MacOS" / "OpenSwap")
    with patch.object(launch_agent.sys, "argv", [str(link)]):
        with patch.object(launch_agent.shutil, "which", return_value=str(link)):
            assert launch_agent.resolve_program() == [
                str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
            ]


def test_build_plist_runs_the_bundle_when_frozen(frozen_app, tmp_path):
    parsed = plistlib.loads(launch_agent.build_plist(home=tmp_path))
    assert parsed["ProgramArguments"] == [
        str(frozen_app / "Contents" / "MacOS" / "OpenSwap"),
        "menubar",
    ]


# --- install ---------------------------------------------------------------


def test_install_writes_the_plist_and_bootstraps_it(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    written = Path(result["plist"])
    assert written.exists()
    calls = [c.args[0] for c in run.call_args_list]
    assert ["launchctl", "bootstrap", f"gui/{UID}", str(written)] in calls


def test_install_creates_the_log_directory(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)
    assert Path(result["stderr_log"]).parent.is_dir()


def test_install_boots_out_first_when_already_loaded(tmp_path):
    # Without this, launchd refuses a reinstall with "service already loaded".
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0)})
        launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    subcommands = [c.args[0][1] for c in run.call_args_list]
    assert subcommands.index("bootout") < subcommands.index("bootstrap")


def test_install_does_not_boot_out_when_nothing_is_loaded(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    assert "bootout" not in [c.args[0][1] for c in run.call_args_list]


def test_install_waits_for_the_old_job_to_go_away_before_bootstrapping(tmp_path):
    """`bootout` can return before launchd has finished the teardown.

    A `bootstrap` inside that window fails with "Operation already in
    progress", so install polls until the job is actually gone.
    """
    prints = {"n": 0}

    def run(argv, **kwargs):
        if argv[1] == "print":
            prints["n"] += 1
            return _completed(0 if prints["n"] <= 3 else 1)
        return _completed(0)

    with patch.object(launch_agent.time, "sleep") as slept:
        with patch.object(launch_agent.subprocess, "run", side_effect=run) as ran:
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)

    assert slept.called, "did not wait for the old job at all"
    subcommands = [c.args[0][1] for c in ran.call_args_list]
    # every liveness check sits between the bootout and the bootstrap
    assert subcommands.index("bootout") < subcommands.index("bootstrap")
    assert subcommands.count("print") > 2


def test_wait_until_unloaded_gives_up_after_the_timeout(tmp_path):
    with patch.object(launch_agent.time, "sleep"):
        with patch.object(launch_agent.subprocess, "run") as run:
            run.side_effect = _router({"print": _completed(0)})
            assert launch_agent._wait_until_unloaded(uid=UID, timeout=0.0) is False


def test_install_names_the_lingering_predecessor_when_bootstrap_fails(tmp_path):
    with patch.object(launch_agent, "_wait_until_unloaded", return_value=False):
        with patch.object(launch_agent.subprocess, "run") as run:
            run.side_effect = _router(
                {"print": _completed(0), "bootstrap": _completed(5, stderr="Operation already in progress")}
            )
            with pytest.raises(ClaudeSwitchError, match="still shutting down"):
                launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)


def test_install_raises_with_launchctl_detail_when_bootstrap_fails(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router(
            {"print": _completed(1), "bootstrap": _completed(5, stderr="Input/output error")}
        )
        with pytest.raises(ClaudeSwitchError, match="Input/output error"):
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)


@pytest.mark.parametrize("op", ["mkdir", "write_bytes"])
def test_install_reports_an_unwritable_launch_agents_directory(tmp_path, op):
    with patch.object(launch_agent.subprocess, "run") as run, patch.object(
        Path, op, side_effect=PermissionError(13, "Permission denied")
    ):
        run.side_effect = _router({"print": _completed(1)})
        with pytest.raises(ClaudeSwitchError, match="Could not write the launch agent.*Permission denied"):
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)


def test_install_refuses_off_macos(tmp_path):
    with patch.object(launch_agent.sys, "platform", "linux"):
        with pytest.raises(ClaudeSwitchError, match="only available on macOS"):
            launch_agent.install(home=tmp_path, program=PROGRAM, uid=UID)


# --- uninstall -------------------------------------------------------------


def test_uninstall_boots_out_and_removes_the_plist(tmp_path):
    target = launch_agent.plist_path(home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")

    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID)

    assert result == {"label": launch_agent.LABEL, "was_loaded": True, "removed_plist": True}
    assert not target.exists()
    assert ["launchctl", "bootout", f"gui/{UID}/{launch_agent.LABEL}"] in [
        c.args[0] for c in run.call_args_list
    ]


def test_uninstall_is_quiet_when_nothing_is_installed(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID)
    assert result["was_loaded"] is False and result["removed_plist"] is False


def test_uninstall_removes_a_plist_that_was_never_bootstrapped(tmp_path):
    target = launch_agent.plist_path(home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID)
    assert result["removed_plist"] is True and not target.exists()


def test_uninstall_reports_a_plist_it_cannot_remove(tmp_path):
    plist = launch_agent.plist_path(home=tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"")
    with patch.object(launch_agent.subprocess, "run") as run, patch.object(
        Path, "unlink", side_effect=PermissionError(13, "Permission denied")
    ):
        run.side_effect = _router({"print": _completed(1)})
        with pytest.raises(ClaudeSwitchError, match="Could not remove the launch agent.*Permission denied"):
            launch_agent.uninstall(home=tmp_path, uid=UID)


def test_uninstall_tolerates_a_plist_that_vanished_after_the_existence_check(tmp_path):
    plist = launch_agent.plist_path(home=tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"")
    real_exists = Path.exists

    def exists_then_vanish(self, *a, **k):
        present = real_exists(self, *a, **k)
        if self == plist and present:
            plist.unlink()
        return present

    with patch.object(launch_agent.subprocess, "run") as run, patch.object(
        Path, "exists", exists_then_vanish
    ):
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.uninstall(home=tmp_path, uid=UID)
    assert result["removed_plist"] is True


def test_uninstall_raises_when_bootout_fails_and_the_service_stays_loaded(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0), "bootout": _completed(3, stderr="in use")})
        with pytest.raises(ClaudeSwitchError, match="in use"):
            launch_agent.uninstall(home=tmp_path, uid=UID)


def test_uninstall_tolerates_a_bootout_race_that_already_unloaded_it(tmp_path):
    # bootout returns non-zero because the job went away underneath it; the
    # follow-up print shows it gone, which is the outcome uninstall wants.
    calls = {"print": 0}

    def run(argv, **kwargs):
        if argv[1] == "print":
            calls["print"] += 1
            return _completed(0) if calls["print"] == 1 else _completed(1)
        if argv[1] == "bootout":
            return _completed(3, stderr="No such process")
        return _completed(0)

    with patch.object(launch_agent.subprocess, "run", side_effect=run):
        result = launch_agent.uninstall(home=tmp_path, uid=UID)
    assert result["was_loaded"] is True


# --- status ----------------------------------------------------------------


def test_status_reads_state_and_pid_from_launchctl_print(tmp_path):
    printed = "\tstate = running\n\tpid = 25026\n\tlast exit code = (never exited)\n"
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout=printed)})
        result = launch_agent.status(home=tmp_path, uid=UID)

    assert result["loaded"] is True
    assert result["state"] == "running"
    assert result["pid"] == 25026


def test_status_reports_not_loaded_without_inventing_a_pid(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["loaded"] is False
    assert result["pid"] is None and result["state"] is None


def test_status_separates_installed_from_loaded(tmp_path):
    # A plist on disk that launchd has not been given is a real state, and the
    # difference is what tells a user to run --install-service again.
    target = launch_agent.plist_path(home=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"x")
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(1)})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["installed"] is True and result["loaded"] is False


def test_status_reads_the_jobs_own_state_not_a_nested_blocks(tmp_path):
    """Regression: real `launchctl print` repeats `state` inside sub-dicts.

    Observed on macOS 25.5.0 — the job prints `state = running`, then a
    `pid-local endpoints` block prints `state = active`. Taking the last match
    reported the endpoint's state as the service's.
    """
    printed = (
        "gui/501/com.opensoft.openswap.menubar = {\n"
        "\tactive count = 1\n"
        "\tstate = running\n"
        "\tpid = 25026\n"
        "\tpid-local endpoints = {\n"
        "\t\tstate = active\n"
        "\t\tpid = 999\n"
        "\t}\n"
        "}\n"
    )
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout=printed)})
        result = launch_agent.status(home=tmp_path, uid=UID)

    assert result["state"] == "running"
    assert result["pid"] == 25026


def test_status_keeps_multi_word_launchd_states(tmp_path):
    # Before the process spawns, launchd reports "spawn scheduled".
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout="\tstate = spawn scheduled\n")})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["state"] == "spawn scheduled"


def test_status_reads_the_loaded_programs_arguments(tmp_path):
    printed = (
        "gui/501/com.opensoft.openswap.menubar = {\n"
        "\tactive count = 0\n"
        "\tpath = /Users/x/Library/LaunchAgents/com.opensoft.openswap.menubar.plist\n"
        "\tstate = not running\n"
        "\n"
        "\tprogram = /Applications/OpenSwap.app/Contents/MacOS/OpenSwap\n"
        "\targuments = {\n"
        "\t\t/Applications/OpenSwap.app/Contents/MacOS/OpenSwap\n"
        "\t\tmenubar\n"
        "\t}\n"
        "\n"
        "\tenvironment = {\n"
        "\t\tPATH => /usr/bin:/bin\n"
        "\t}\n"
        "}\n"
    )
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout=printed)})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["program"] == ["/Applications/OpenSwap.app/Contents/MacOS/OpenSwap", "menubar"]
    assert result["state"] == "not running"


def test_status_leaves_the_program_unknown_without_an_arguments_block(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout="\tstate = running\n\tpid = 12\n")})
        assert launch_agent.status(home=tmp_path, uid=UID)["program"] is None
        run.side_effect = _router({"print": _completed(1)})
        assert launch_agent.status(home=tmp_path, uid=UID)["program"] is None


def test_status_ignores_a_non_numeric_pid_line(tmp_path):
    with patch.object(launch_agent.subprocess, "run") as run:
        run.side_effect = _router({"print": _completed(0, stdout="\tpid = (none)\n")})
        result = launch_agent.status(home=tmp_path, uid=UID)
    assert result["pid"] is None


# --- waiting for launchd's pid ---------------------------------------------


def test_wait_for_pid_returns_pid_once_launchd_spawned_it(tmp_path):
    answers = iter([None, None, 77])
    with patch.object(launch_agent, "status", lambda label, uid=None: {"pid": next(answers)}):
        with patch.object(launch_agent.time, "sleep"):
            assert launch_agent._wait_for_pid("x", UID, timeout=5.0) == 77


def test_wait_for_pid_gives_up_at_the_deadline(tmp_path):
    with patch.object(launch_agent, "status", lambda label, uid=None: {"pid": None}):
        assert launch_agent._wait_for_pid("x", UID, timeout=0.0) is None


# --- Open at login from a running extra (plan 020, Step 2) -----------------


class FakeLaunchd:
    """Answers launchctl for the one menu bar job the way launchd would, and
    records each call's arguments."""

    def __init__(self, *, loaded=False, pid=None, bootstrap_rc=0, spawns=True):
        self.loaded = loaded
        self.pid = pid
        self.bootstrap_rc = bootstrap_rc
        self.kickstart_rc = 0
        self.spawns = spawns
        # How many more `print`s still see the job after a bootout (launchd
        # finishing the teardown).
        self.unloading = 0
        # The argv launchd loaded, as `launchctl print` reports it; set from
        # the plist at bootstrap. None prints no `arguments` block.
        self.loaded_program: list[str] | None = None
        self.calls: list[list[str]] = []
        self.data = Path()

    def __call__(self, argv, **kwargs):
        assert argv[0] == "launchctl"
        self.calls.append(argv[1:])
        sub = argv[1]
        if sub == "print":
            if self.unloading:
                self.unloading -= 1
                return _completed(0, stdout="\tstate = exited\n")
            if not self.loaded:
                return _completed(113, stderr="Could not find service")
            pid_line = f"\tpid = {self.pid}\n" if self.pid else ""
            program = ""
            if self.loaded_program:
                program = (
                    f"\tprogram = {self.loaded_program[0]}\n\targuments = {{\n"
                    + "".join(f"\t\t{arg}\n" for arg in self.loaded_program)
                    + "\t}\n"
                )
            return _completed(0, stdout=f"\tstate = running\n{program}{pid_line}")
        if sub == "bootout":
            self.loaded, self.pid, self.loaded_program = False, None, None
        elif sub == "bootstrap":
            if self.bootstrap_rc:
                return _completed(self.bootstrap_rc, stderr="Bootstrap failed: 5: Input/output error")
            self.loaded = True
            self.loaded_program = plistlib.loads(Path(argv[3]).read_bytes())["ProgramArguments"]
            self.pid = 4242 if self.spawns else None
        elif sub == "kickstart":
            if not self.loaded or self.kickstart_rc:
                return _completed(self.kickstart_rc or 113, stderr="Could not find service")
            self.pid = self.pid or (4242 if self.spawns else None)
        return _completed(0)

    @property
    def subcommands(self) -> list[str]:
        return [call[0] for call in self.calls]


@pytest.fixture
def launchd(monkeypatch, tmp_path):
    """A Finder-opened copy of a uv install, no cask, launchd faked."""
    fake = FakeLaunchd()
    fake.data = tmp_path / "data"
    monkeypatch.setattr("openswap.paths.get_backup_root", lambda: fake.data)
    monkeypatch.setattr(launch_agent.subprocess, "run", fake)
    monkeypatch.setattr(launch_agent, "resolve_program", lambda: list(PROGRAM))
    monkeypatch.setattr(launch_agent.bundle, "runs_from_temporary_location", lambda app=None: False)
    monkeypatch.setattr(launch_agent.bundle, "cask_prefix", lambda: None)
    # A clock that moves only when the code sleeps: every wait is instant
    # and still reaches its deadline.
    clock = [1000.0]
    monkeypatch.setattr(launch_agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launch_agent.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    return fake


def _as_the_login_item(monkeypatch):
    monkeypatch.setenv("XPC_SERVICE_NAME", launch_agent.LABEL)


def _write_plist(home: Path, program: list[str]) -> Path:
    path = launch_agent.plist_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launch_agent.build_plist(program, home=home))
    return path


def _plist_program(home: Path) -> list[str]:
    return plistlib.loads(launch_agent.plist_path(home=home).read_bytes())["ProgramArguments"]


def test_login_item_enabled_is_whether_the_plist_exists(tmp_path):
    assert launch_agent.login_item_enabled(home=tmp_path) is False
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.login_item_enabled(home=tmp_path) is True


def test_runs_as_login_item_reads_launchds_service_name(monkeypatch):
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert launch_agent.runs_as_login_item() is False
    monkeypatch.setenv("XPC_SERVICE_NAME", "application.com.apple.Terminal.1234")
    assert launch_agent.runs_as_login_item() is False
    monkeypatch.setenv("XPC_SERVICE_NAME", launch_agent.LABEL)
    assert launch_agent.runs_as_login_item() is True


def test_job_turning_on_writes_a_missing_plist_without_launchctl(launchd, monkeypatch, tmp_path):
    _as_the_login_item(monkeypatch)
    assert launch_agent.set_login_item(True, home=tmp_path, uid=UID) == "enabled"
    assert _plist_program(tmp_path) == [*PROGRAM, "menubar"]
    assert launchd.calls == []


def test_job_turning_on_repoints_a_plist_that_runs_another_program(launchd, monkeypatch, tmp_path):
    _as_the_login_item(monkeypatch)
    _write_plist(tmp_path, ["/Users/x/old/openswap"])
    assert launch_agent.set_login_item(True, home=tmp_path, uid=UID) == "enabled"
    assert _plist_program(tmp_path) == [*PROGRAM, "menubar"]
    assert launchd.calls == []


def test_job_turning_on_leaves_a_plist_that_already_runs_it(launchd, monkeypatch, tmp_path):
    _as_the_login_item(monkeypatch)
    path = _write_plist(tmp_path, PROGRAM)
    before = path.stat().st_mtime_ns
    assert launch_agent.set_login_item(True, home=tmp_path, uid=UID) == "unchanged"
    assert path.stat().st_mtime_ns == before
    assert launchd.calls == []


def test_job_turning_off_deletes_the_plist_and_keeps_running(launchd, monkeypatch, tmp_path):
    """Booting out the job would SIGTERM this very process (a STOP condition)."""
    _as_the_login_item(monkeypatch)
    launchd.loaded, launchd.pid = True, os.getpid()
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.set_login_item(False, home=tmp_path, uid=UID) == "disabled_after_quit"
    assert not launch_agent.plist_path(home=tmp_path).exists()
    assert launchd.calls == []


def test_job_turning_off_without_a_plist_is_unchanged(launchd, monkeypatch, tmp_path):
    _as_the_login_item(monkeypatch)
    launchd.loaded = True
    assert launch_agent.set_login_item(False, home=tmp_path, uid=UID) == "unchanged"
    assert launchd.calls == []


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("plist_program", [None, PROGRAM, ["/Users/x/old/openswap"]])
def test_the_job_never_boots_itself_out(launchd, monkeypatch, tmp_path, enabled, plist_program):
    _as_the_login_item(monkeypatch)
    launchd.loaded, launchd.pid = True, os.getpid()
    if plist_program is not None:
        _write_plist(tmp_path, plist_program)
    launch_agent.set_login_item(enabled, home=tmp_path, uid=UID)
    assert "bootout" not in launchd.subcommands
    assert "bootstrap" not in launchd.subcommands


def test_other_copy_turning_on_hands_over_to_launchd(launchd, tmp_path):
    assert launch_agent.set_login_item(True, home=tmp_path, uid=UID) == "handoff"
    plist = launch_agent.plist_path(home=tmp_path)
    assert _plist_program(tmp_path) == [*PROGRAM, "menubar"]
    assert ["bootstrap", f"gui/{UID}", str(plist)] in launchd.calls
    assert "bootout" not in launchd.subcommands
    # It returned only once launchd had a pid, so the caller can quit.
    assert launchd.subcommands[-1] == "print"
    assert launchd.pid == 4242


def test_other_copy_turning_on_restarts_a_loaded_job_from_the_new_plist(launchd, tmp_path):
    # Loaded from the old plist, a kickstart would start the old program.
    launchd.loaded = True
    _write_plist(tmp_path, ["/Users/x/old/openswap"])
    assert launch_agent.set_login_item(True, home=tmp_path, uid=UID) == "handoff"
    assert _plist_program(tmp_path) == [*PROGRAM, "menubar"]
    assert launchd.subcommands.index("bootout") < launchd.subcommands.index("bootstrap")
    assert "kickstart" not in launchd.subcommands


def test_other_copy_turning_on_kickstarts_an_idle_job_that_already_runs_it(launchd, tmp_path):
    # Loaded but not running: the user quit it (a clean exit is not restarted).
    launchd.loaded, launchd.loaded_program = True, [*PROGRAM, "menubar"]
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.set_login_item(True, home=tmp_path, uid=UID) == "handoff"
    assert ["kickstart", f"gui/{UID}/{launch_agent.LABEL}"] in launchd.calls
    assert "bootout" not in launchd.subcommands
    assert "bootstrap" not in launchd.subcommands


def test_other_copy_turning_on_while_the_job_waits_for_the_lock(launchd, tmp_path):
    launchd.loaded, launchd.pid, launchd.loaded_program = True, 999, [*PROGRAM, "menubar"]
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.set_login_item(True, home=tmp_path, uid=UID) == "handoff"
    assert set(launchd.subcommands) == {"print"}


def test_other_copy_turning_on_reports_a_failed_bootstrap(launchd, tmp_path):
    launchd.bootstrap_rc = 5
    with pytest.raises(launch_agent.LoginItemNotStarted, match="Input/output error") as raised:
        launch_agent.set_login_item(True, home=tmp_path, uid=UID)
    assert isinstance(raised.value, ClaudeSwitchError)
    # Settings reads the same reason back.
    assert "Input/output error" in launch_agent.last_start_failure()["detail"]


def test_other_copy_turning_on_stops_a_job_that_never_started(launchd, monkeypatch, tmp_path):
    """Left loaded, a late start would wait on this copy's lock, exit 1, and
    be relaunched by KeepAlive for as long as this copy runs."""
    monkeypatch.setattr(launch_agent, "_HANDOFF_PID_TIMEOUT_SECONDS", 0.0)
    launchd.spawns = False
    with pytest.raises(launch_agent.LoginItemNotStarted, match="no process"):
        launch_agent.set_login_item(True, home=tmp_path, uid=UID)
    assert "bootstrap" in launchd.subcommands
    assert launchd.subcommands[-1] == "bootout"
    assert launchd.loaded is False


def test_other_copy_turning_off_boots_out_the_job_and_deletes_the_plist(launchd, tmp_path):
    launchd.loaded = True
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.set_login_item(False, home=tmp_path, uid=UID) == "disabled"
    assert ["bootout", f"gui/{UID}/{launch_agent.LABEL}"] in launchd.calls
    assert not launch_agent.plist_path(home=tmp_path).exists()


def test_other_copy_turning_off_a_plist_that_was_never_loaded(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.set_login_item(False, home=tmp_path, uid=UID) == "disabled"
    assert "bootout" not in launchd.subcommands
    assert not launch_agent.plist_path(home=tmp_path).exists()


def test_other_copy_turning_off_with_nothing_installed_is_unchanged(launchd, tmp_path):
    assert launch_agent.set_login_item(False, home=tmp_path, uid=UID) == "unchanged"
    assert "bootout" not in launchd.subcommands


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("as_job", [True, False])
def test_set_login_item_refuses_a_translocated_or_mounted_copy(
    launchd, monkeypatch, tmp_path, enabled, as_job
):
    if as_job:
        _as_the_login_item(monkeypatch)
    monkeypatch.setattr(launch_agent.bundle, "runs_from_temporary_location", lambda app=None: True)
    plist = _write_plist(tmp_path, PROGRAM) if not enabled else launch_agent.plist_path(home=tmp_path)
    with pytest.raises(ClaudeSwitchError, match="Move OpenSwap to Applications first"):
        launch_agent.set_login_item(enabled, home=tmp_path, uid=UID)
    assert plist.exists() is (not enabled)
    assert launchd.calls == []


def test_temporary_location_is_read_from_the_running_bundle(launchd, monkeypatch, tmp_path):
    monkeypatch.setattr(
        launch_agent.bundle, "runs_from_temporary_location", _RUNS_FROM_TEMPORARY_LOCATION
    )
    translocated = Path("/private/var/folders/xy/T/AppTranslocation/ABC/d/OpenSwap.app")
    monkeypatch.setattr(launch_agent.bundle, "app_path", lambda: translocated)
    with pytest.raises(ClaudeSwitchError, match="Move OpenSwap to Applications first"):
        launch_agent.set_login_item(True, home=tmp_path, uid=UID)
    assert launchd.calls == []


def test_a_uv_copy_beside_the_cask_will_not_turn_login_on(launchd, monkeypatch, tmp_path):
    # It would pin the login item back to uv (bundle.refuse_uv_beside_cask).
    monkeypatch.setattr(launch_agent.bundle, "cask_prefix", lambda: Path("/opt/homebrew"))
    with pytest.raises(ClaudeSwitchError, match="uv install will not open at login"):
        launch_agent.set_login_item(True, home=tmp_path, uid=UID)
    assert not launch_agent.plist_path(home=tmp_path).exists()
    assert launchd.calls == []


def test_set_login_item_refuses_off_macos(tmp_path):
    with patch.object(launch_agent.sys, "platform", "linux"):
        with pytest.raises(ClaudeSwitchError, match="only available on macOS"):
            launch_agent.set_login_item(True, home=tmp_path, uid=UID)


# --- handing over to the login item (plan 020, Steps 1 and 2) ---------------


def test_login_item_runs_this_compares_the_plists_program(launchd, tmp_path):
    assert launch_agent.login_item_runs_this(home=tmp_path) is False
    _write_plist(tmp_path, ["/Applications/Other.app/Contents/MacOS/OpenSwap"])
    assert launch_agent.login_item_runs_this(home=tmp_path) is False
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.login_item_runs_this(home=tmp_path) is True


def test_start_login_item_leaves_a_running_job_alone(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    launchd.loaded, launchd.pid, launchd.loaded_program = True, 999, [*PROGRAM, "menubar"]
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 999
    assert launchd.subcommands == ["print"]


def test_start_login_item_kickstarts_a_loaded_idle_job_that_runs_the_plist(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    launchd.loaded, launchd.loaded_program = True, [*PROGRAM, "menubar"]
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 4242
    assert ["kickstart", f"gui/{UID}/{launch_agent.LABEL}"] in launchd.calls
    assert "bootstrap" not in launchd.subcommands
    assert "bootout" not in launchd.subcommands


def test_start_login_item_reloads_a_job_loaded_with_another_program(launchd, tmp_path):
    """launchd keeps the definition it loaded: a kickstart would start the old one."""
    plist = _write_plist(tmp_path, PROGRAM)
    launchd.loaded = True
    launchd.loaded_program = ["/Applications/Old.app/Contents/MacOS/OpenSwap", "menubar"]
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 4242
    subs = launchd.subcommands
    assert "kickstart" not in subs
    assert subs.index("bootout") < subs.index("bootstrap")
    assert ["bootstrap", f"gui/{UID}", str(plist)] in launchd.calls
    assert launchd.loaded_program == [*PROGRAM, "menubar"]


def test_start_login_item_reloads_a_job_whose_program_it_cannot_read(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    launchd.loaded, launchd.loaded_program = True, None  # print without `arguments`
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 4242
    assert "kickstart" not in launchd.subcommands
    assert launchd.subcommands.index("bootout") < launchd.subcommands.index("bootstrap")
    assert launchd.loaded_program == [*PROGRAM, "menubar"]


def test_start_login_item_replaces_a_starting_job_that_runs_another_program(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    launchd.loaded, launchd.pid = True, 999
    launchd.loaded_program = ["/Applications/Old.app/Contents/MacOS/OpenSwap", "menubar"]
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 4242
    assert launchd.loaded_program == [*PROGRAM, "menubar"]
    assert launch_agent.running_login_item_pid(home=tmp_path, uid=UID) == 4242


def test_running_login_item_pid_ignores_a_job_loaded_with_another_program(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    launchd.loaded, launchd.pid = True, 999
    launchd.loaded_program = ["/Applications/Old.app/Contents/MacOS/OpenSwap", "menubar"]
    assert launch_agent.running_login_item_pid(home=tmp_path, uid=UID) is None
    launchd.loaded_program = [*PROGRAM, "menubar"]
    assert launch_agent.running_login_item_pid(home=tmp_path, uid=UID) == 999
    launchd.loaded_program = None  # cannot tell: it may be the old executable
    assert launch_agent.running_login_item_pid(home=tmp_path, uid=UID) is None


def test_start_login_item_reloads_a_running_job_whose_program_it_cannot_read(launchd, tmp_path):
    """The pid could belong to an executable loaded before the plist changed."""
    _write_plist(tmp_path, PROGRAM)
    launchd.loaded, launchd.pid, launchd.loaded_program = True, 999, None
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 4242
    assert launchd.subcommands.index("bootout") < launchd.subcommands.index("bootstrap")
    assert launchd.loaded_program == [*PROGRAM, "menubar"]


def test_turning_on_over_another_copys_loaded_plist_hands_over_to_the_new_program(launchd, tmp_path):
    """The cask's app is loaded but stopped; this copy takes the login item over."""
    other = ["/Applications/OpenSwap.app/Contents/MacOS/OpenSwap"]
    _write_plist(tmp_path, other)
    launchd.loaded, launchd.loaded_program = True, [*other, "menubar"]
    assert launch_agent.set_login_item(True, home=tmp_path, uid=UID) == "handoff"
    assert "kickstart" not in launchd.subcommands
    assert launchd.loaded_program == [*PROGRAM, "menubar"]
    assert _plist_program(tmp_path) == [*PROGRAM, "menubar"]


def test_start_login_item_bootstraps_a_plist_launchd_does_not_know(launchd, tmp_path):
    plist = _write_plist(tmp_path, PROGRAM)
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 4242
    assert ["bootstrap", f"gui/{UID}", str(plist)] in launchd.calls


def test_start_login_item_records_why_launchctl_refused(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    launchd.bootstrap_rc = 5
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) is None
    # The first bootstrap, then the one retry.
    assert launchd.subcommands.count("bootstrap") == 2
    failure = launch_agent.last_start_failure()
    assert failure["detail"].startswith("launchctl bootstrap failed (exit 5)")
    assert "; then launchctl bootstrap failed" in failure["detail"]
    assert isinstance(failure["failed_at"], float)
    assert launch_agent.login_item_status_path() == launchd.data / "login_item_status.json"


def test_start_login_item_unloads_a_job_that_never_came_up(launchd, monkeypatch, tmp_path):
    monkeypatch.setattr(launch_agent, "_HANDOFF_PID_TIMEOUT_SECONDS", 0.0)
    plist = _write_plist(tmp_path, PROGRAM)
    launchd.spawns = False
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) is None
    assert launchd.subcommands[-1] == "bootout"
    assert plist.exists()  # it still opens at the next login
    assert "no process after launchctl bootstrap" in launch_agent.last_start_failure()["detail"]


def test_a_kickstart_that_fails_while_the_job_unloads_falls_back_to_one_bootstrap(launchd, tmp_path):
    """The start-up migration booted out a uv extra just before: launchd
    still lists the job for a moment, and kickstart refuses it."""
    plist = _write_plist(tmp_path, PROGRAM)
    launchd.loaded, launchd.kickstart_rc = True, 37
    launchd.loaded_program = [*PROGRAM, "menubar"]

    def bootout_in_progress(argv, **kwargs):
        if argv[1] == "kickstart":
            launchd.loaded, launchd.unloading = False, 2  # gone after two more looks
        return FakeLaunchd.__call__(launchd, argv, **kwargs)

    with patch.object(launch_agent.subprocess, "run", side_effect=bootout_in_progress):
        assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 4242
    subs = launchd.subcommands
    assert subs.index("kickstart") < subs.index("bootstrap")
    assert subs.count("bootstrap") == 1
    assert ["bootstrap", f"gui/{UID}", str(plist)] in launchd.calls
    assert launch_agent.last_start_failure() is None


def test_a_start_that_works_clears_the_recorded_failure(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    launch_agent._record_start_failure("launchctl bootstrap failed (exit 5)")
    assert launch_agent.last_start_failure() is not None
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 4242
    assert launch_agent.last_start_failure() is None
    assert not launch_agent.login_item_status_path().exists()


def test_a_running_job_also_clears_the_recorded_failure(launchd, tmp_path):
    _write_plist(tmp_path, PROGRAM)
    launchd.loaded, launchd.pid, launchd.loaded_program = True, 999, [*PROGRAM, "menubar"]
    launch_agent._record_start_failure("old")
    assert launch_agent.start_login_item(home=tmp_path, uid=UID) == 999
    assert launch_agent.last_start_failure() is None


@pytest.mark.parametrize("text", ["", "not json", "[]", '{"detail": "no time"}'])
def test_an_unreadable_status_file_is_no_failure(launchd, text):
    path = launch_agent.login_item_status_path()
    path.parent.mkdir(parents=True)
    path.write_text(text)
    assert launch_agent.last_start_failure() is None


@pytest.fixture
def popen(monkeypatch):
    started = []
    monkeypatch.setattr(
        launch_agent.subprocess, "Popen", lambda argv, **kwargs: started.append((argv, kwargs))
    )
    return started


def test_unload_own_job_detaches_a_bootout_once_login_is_off(launchd, monkeypatch, popen, tmp_path):
    _as_the_login_item(monkeypatch)
    assert launch_agent.unload_own_job(home=tmp_path, uid=UID) is True
    [(argv, kwargs)] = popen
    assert argv == ["launchctl", "bootout", f"gui/{UID}/{launch_agent.LABEL}"]
    # A new session: launchd stopping this job's process group must not
    # take the bootout along with it.
    assert kwargs["start_new_session"] is True
    assert launchd.calls == []  # started, not waited for: the caller exits next


def test_unload_own_job_keeps_the_job_loaded_while_login_is_on(launchd, monkeypatch, popen, tmp_path):
    _as_the_login_item(monkeypatch)
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.unload_own_job(home=tmp_path, uid=UID) is False
    assert popen == []


def test_unload_own_job_does_nothing_outside_the_job(launchd, popen, tmp_path):
    assert launch_agent.unload_own_job(home=tmp_path, uid=UID) is False
    assert popen == []


def test_turning_login_off_from_the_job_then_quitting_leaves_nothing_loaded(
    launchd, monkeypatch, popen, tmp_path
):
    _as_the_login_item(monkeypatch)
    launchd.loaded, launchd.pid = True, os.getpid()
    _write_plist(tmp_path, PROGRAM)
    assert launch_agent.set_login_item(False, home=tmp_path, uid=UID) == "disabled_after_quit"
    assert popen == []  # the call itself never boots out this process
    assert launch_agent.unload_own_job(home=tmp_path, uid=UID) is True
    assert popen[0][0][:2] == ["launchctl", "bootout"]
