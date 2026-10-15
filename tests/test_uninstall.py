"""``openswap uninstall`` and the cask's uninstall hook.

Homebrew runs the cask's uninstall stanza on upgrade and reinstall too, so
the hook must tell those apart from an uninstall by the brew command that
started it. launchctl, pluginkit, ``ps``, the Keychain and ``brew`` are fakes
here; nothing touches a real LaunchAgent or file outside ``tmp_path``.
"""

from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from openswap import cli, macos_keychain, uninstall

BREW_RB = "/opt/homebrew/Library/Homebrew/brew.rb"
RUBY = "/opt/homebrew/Library/Homebrew/vendor/portable-ruby/current/bin/ruby"
UV_SCRIPT = "/Users/x/.local/bin/openswap"


def _processes(monkeypatch, table: dict[int, tuple[int, list[str]]]) -> None:
    monkeypatch.setattr(uninstall, "_process", lambda pid: table.get(pid))


class TestHomebrewCommand:
    def test_reads_the_command_and_its_arguments_from_brew_rb(self, monkeypatch):
        _processes(monkeypatch, {
            40: (30, [RUBY, "-W1", "--disable=gems,rubyopt", BREW_RB, "uninstall", "--zap", "--cask", "openswap"]),
        })
        assert uninstall.homebrew_command(40) == ("uninstall", ["--zap", "--cask", "openswap"])

    @pytest.mark.parametrize("command", ["upgrade", "reinstall", "install"])
    def test_an_upgrade_or_reinstall_is_not_an_uninstall(self, monkeypatch, command):
        _processes(monkeypatch, {40: (30, [RUBY, BREW_RB, command, "--cask", "openswap"])})
        found = uninstall.homebrew_command(40)
        assert found is not None and found[0] == command
        assert found[0] not in uninstall.UNINSTALL_COMMANDS

    def test_looks_past_a_wrapper_process(self, monkeypatch):
        _processes(monkeypatch, {
            40: (30, ["/usr/bin/env", "PATH=/usr/bin", "/bin/sh"]),
            30: (20, [RUBY, BREW_RB, "rm", "openswap"]),
        })
        assert uninstall.homebrew_command(40) == ("rm", ["openswap"])

    def test_none_outside_homebrew(self, monkeypatch):
        _processes(monkeypatch, {40: (30, ["/bin/zsh"]), 30: (1, ["/sbin/launchd"])})
        assert uninstall.homebrew_command(40) is None

    def test_none_when_ps_cannot_say(self, monkeypatch):
        _processes(monkeypatch, {})
        assert uninstall.homebrew_command(40) is None

    def test_process_parses_ps(self, monkeypatch):
        def fake_run(argv, **kwargs):
            assert argv == ["/bin/ps", "-o", "ppid=,args=", "-p", "40"]
            return subprocess.CompletedProcess(argv, 0, f"  30 {RUBY} {BREW_RB} upgrade\n", "")

        monkeypatch.setattr(uninstall.subprocess, "run", fake_run)
        assert uninstall._process(40) == (30, [RUBY, BREW_RB, "upgrade"])


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """A temp home with the menu bar, worker, status line and widget faked."""
    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    seen: dict[str, list] = {"menubar": [], "disable": [], "worker": [], "statusline": [], "widget": []}
    state = {"worker_loaded": False, "disable_ok": True, "statusline": None}

    def fake_menubar_uninstall(label, home=None, uid=None):
        seen["menubar"].append(label)
        plist = agents / f"{label}.plist"
        existed = plist.exists()
        plist.unlink(missing_ok=True)
        return {"label": label, "was_loaded": existed, "removed_plist": existed}

    def fake_disable(backup_root):
        seen["disable"].append(backup_root)
        if state["disable_ok"]:
            (agents / "com.opensoft.openswap.worker.plist").unlink(missing_ok=True)
            return True, {}, None
        return False, {}, "job_stop_not_confirmed"

    def fake_worker_uninstall(home=None, uid=None):
        seen["worker"].append("uninstall")
        (agents / "com.opensoft.openswap.worker.plist").unlink(missing_ok=True)
        return {"was_loaded": True, "removed_plist": True}

    def fake_statusline_uninstall(config_home, backup_root):
        seen["statusline"].append(config_home)
        return {"restored": True}

    def fake_widget_uninstall(home=None, uid=None):
        seen["widget"].append("uninstall")
        return {"unregistered": True, "removed_app": False, "removed_plist": False, "was_loaded": False}

    monkeypatch.setattr("openswap.launch_agent.uninstall", fake_menubar_uninstall)
    monkeypatch.setattr("openswap.worker.cli.disable_worker", fake_disable)
    monkeypatch.setattr("openswap.worker.launch_agent.uninstall", fake_worker_uninstall)
    monkeypatch.setattr("openswap.worker.launch_agent._is_loaded", lambda uid=None: state["worker_loaded"])
    monkeypatch.setattr("openswap.statusline.uninstall", fake_statusline_uninstall)
    monkeypatch.setattr("openswap.statusline.installed_command", lambda config_home: state["statusline"])
    monkeypatch.setattr("openswap.statusline.hook_program", lambda config_home: None)
    monkeypatch.setattr("openswap.widget_install.uninstall_widget", fake_widget_uninstall)
    monkeypatch.setattr(uninstall.shutil, "which", lambda name: None)
    return home, agents, seen, state


def _plist(path: Path, program: list[str]) -> None:
    path.write_bytes(plistlib.dumps({"Label": path.stem, "ProgramArguments": program}))


class TestRemoveIntegrations:
    def test_app_removes_its_own_login_item_and_worker(self, mac, frozen_app):
        home, agents, seen, _ = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.menubar.plist", [exe])
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run", "--managed"])
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.failed == []
        assert removal.removed == ["menu bar login item", "Remote tasks worker"]
        assert len(seen["disable"]) == 1
        assert not any(agents.iterdir())

    def test_app_leaves_a_uv_install_alone(self, mac, frozen_app):
        home, agents, seen, _ = mac
        _plist(agents / "com.opensoft.openswap.menubar.plist", [UV_SCRIPT, "menubar"])
        _plist(agents / "com.opensoft.openswap.worker.plist", [UV_SCRIPT, "worker", "run"])
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == [] and removal.failed == []
        assert "com.opensoft.openswap.menubar" not in seen["menubar"]
        assert seen["disable"] == []
        assert (agents / "com.opensoft.openswap.menubar.plist").exists()

    def test_uv_install_removes_its_own(self, mac):
        home, agents, _, _ = mac
        _plist(agents / "com.opensoft.openswap.menubar.plist", [UV_SCRIPT, "menubar"])
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == ["menu bar login item"]

    def test_nothing_installed_removes_nothing(self, mac, frozen_app):
        home, _, seen, _ = mac
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == [] and removal.failed == []
        assert seen["disable"] == [] and seen["statusline"] == []

    def test_a_worker_that_will_not_stop_is_a_failure(self, mac, frozen_app):
        home, agents, seen, state = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.menubar.plist", [exe])
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run"])
        state["disable_ok"] = False
        removal = uninstall.remove_integrations(home=home, uid=501)
        # The rest still ran.
        assert removal.removed == ["menu bar login item"]
        assert len(removal.failed) == 1 and "job_stop_not_confirmed" in removal.failed[0]
        assert seen["worker"] == []

    def test_force_boots_out_a_worker_that_will_not_stop(self, mac, frozen_app):
        home, agents, seen, state = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run"])
        state["disable_ok"] = False
        removal = uninstall.remove_integrations(home=home, uid=501, force=True)
        assert removal.removed == ["Remote tasks worker"] and removal.failed == []
        assert seen["worker"] == ["uninstall"]

    def test_status_line_on_this_app_is_restored(self, mac, frozen_app, monkeypatch):
        home, _, _, state = mac
        exe = frozen_app / "Contents" / "MacOS" / "OpenSwap"
        state["statusline"] = f"{exe} statusline"
        monkeypatch.setattr("openswap.statusline.hook_program", lambda config_home: str(exe))
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == ["Claude Code status line"]

    def test_status_line_on_a_uv_install_stays(self, mac, frozen_app, monkeypatch, tmp_path):
        home, _, seen, state = mac
        uv = tmp_path / "uv-bin" / "openswap"
        uv.parent.mkdir()
        uv.write_text("#!/bin/sh\n")
        state["statusline"] = f"{uv} statusline"
        monkeypatch.setattr("openswap.statusline.hook_program", lambda config_home: str(uv))
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == [] and seen["statusline"] == []

    def test_someone_elses_status_line_is_not_touched(self, mac, frozen_app):
        home, _, seen, state = mac
        state["statusline"] = None  # not OpenSwap's
        uninstall.remove_integrations(home=home, uid=501)
        assert seen["statusline"] == []

    def test_app_widget_unregisters_quietly(self, mac, frozen_app, monkeypatch):
        home, _, _, _ = mac
        from openswap.exceptions import ClaudeSwitchError

        def failing(home=None, uid=None):
            raise ClaudeSwitchError("pluginkit failed")

        monkeypatch.setattr("openswap.widget_install.uninstall_widget", failing)
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.failed == [] and "widget" not in removal.removed


class TestZapData:
    def test_delete_saved_logins_covers_both_services(self, monkeypatch):
        calls = []
        monkeypatch.setattr(macos_keychain, "delete_all", lambda service: calls.append(service) or 2)
        assert uninstall.delete_saved_logins() == 4
        assert calls == ["openswap", "claude-swap"]
        assert "Claude Code-credentials" not in calls

    def test_data_paths_match_what_exists(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        backup = home / "Library" / "Application Support" / "OpenSwap"
        backup.mkdir(parents=True)
        logs = home / "Library" / "Logs"
        logs.mkdir(parents=True)
        (logs / "com.opensoft.openswap.menubar.err").write_text("x")
        (logs / "com.opensoft.openswap.worker.log").write_text("x")
        (logs / "com.other.app.log").write_text("x")
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: backup)
        found = uninstall.data_paths(home)
        assert found == [backup, logs / "com.opensoft.openswap.worker.log", logs / "com.opensoft.openswap.menubar.err"]
        assert uninstall.delete_data(home) == found
        assert not backup.exists() and (logs / "com.other.app.log").exists()


class TestKeychainDeleteAll:
    def test_deletes_until_not_found(self, monkeypatch):
        codes = iter([0, 0, 0, 44])
        monkeypatch.setattr(
            macos_keychain.subprocess, "run",
            lambda argv, **k: subprocess.CompletedProcess(argv, next(codes), "", ""),
        )
        assert macos_keychain.delete_all("openswap") == 3

    def test_other_errors_raise(self, monkeypatch):
        monkeypatch.setattr(
            macos_keychain.subprocess, "run",
            lambda argv, **k: subprocess.CompletedProcess(argv, 51, "", "locked"),
        )
        with pytest.raises(macos_keychain.KeychainError):
            macos_keychain.delete_all("openswap")


class TestUninstallCommand:
    @pytest.fixture
    def harness(self, monkeypatch, tmp_path):
        seen: dict = {"remove": [], "logins": 0, "data": 0, "exec": None}
        state = {"brew": None, "failed": [], "command": None}

        def fake_remove(home=None, uid=None, force=False):
            seen["remove"].append(force)
            return uninstall.Removal(removed=["menu bar login item"], failed=list(state["failed"]))

        def fake_logins():
            seen["logins"] += 1
            return 3

        def fake_data(home=None):
            seen["data"] += 1
            return [tmp_path / "data"]

        def fake_exec(path, argv):
            seen["exec"] = argv
            raise SystemExit(0)

        monkeypatch.setattr(uninstall, "remove_integrations", fake_remove)
        monkeypatch.setattr(uninstall, "delete_saved_logins", fake_logins)
        monkeypatch.setattr(uninstall, "delete_data", fake_data)
        monkeypatch.setattr(uninstall, "homebrew_brew", lambda: state["brew"])
        monkeypatch.setattr(uninstall, "homebrew_command", lambda: state["command"])
        monkeypatch.setattr(cli.os, "execv", fake_exec)
        return seen, state

    def _run(self, monkeypatch, *args: str) -> int:
        monkeypatch.setattr(sys, "argv", ["openswap", "uninstall", *args])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        return exc.value.code

    @pytest.mark.parametrize("command", ["upgrade", "reinstall", None])
    def test_hook_keeps_everything_on_upgrade(self, harness, monkeypatch, capsys, command):
        seen, state = harness
        state["command"] = None if command is None else (command, ["--cask", "openswap"])
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert seen["remove"] == [] and seen["logins"] == 0
        assert capsys.readouterr().out == ""

    def test_hook_cleans_up_on_uninstall(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["command"] = ("uninstall", ["--cask", "openswap"])
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert seen["remove"] == [True] and seen["logins"] == 0
        assert "Removed OpenSwap's menu bar login item." in capsys.readouterr().out

    def test_hook_zap_deletes_saved_logins(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["command"] = ("uninstall", ["--zap", "--cask", "openswap"])
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert seen["logins"] == 1 and seen["data"] == 0  # brew's zap trashes the files
        assert "Deleted 3 saved OpenSwap logins" in capsys.readouterr().out

    def test_hook_failure_still_exits_zero(self, harness, monkeypatch, capsys):
        _, state = harness
        state["command"] = ("uninstall", [])
        state["failed"] = ["Remote tasks worker: boom"]
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert "boom" in capsys.readouterr().err

    def test_cask_install_hands_off_to_brew(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["brew"] = Path("/opt/homebrew/bin/brew")
        assert self._run(monkeypatch) == 0
        assert seen["exec"] == ["/opt/homebrew/bin/brew", "uninstall", "--cask", "openswap"]
        assert seen["remove"] == [False] and seen["logins"] == 0
        out = capsys.readouterr().out
        assert "Removed the menu bar login item." in out
        assert "brew uninstall --cask openswap" in out

    def test_zap_with_brew(self, harness, monkeypatch):
        seen, state = harness
        state["brew"] = Path("/opt/homebrew/bin/brew")
        assert self._run(monkeypatch, "--zap", "--yes") == 0
        assert seen["exec"] == ["/opt/homebrew/bin/brew", "uninstall", "--cask", "--zap", "openswap"]
        assert seen["logins"] == 1 and seen["data"] == 0

    def test_zap_needs_a_yes_without_a_terminal(self, harness, monkeypatch, capsys):
        seen, _ = harness
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
        assert self._run(monkeypatch, "--zap") == 2
        assert seen["remove"] == [] and seen["exec"] is None
        assert "--yes" in capsys.readouterr().err

    def test_zap_prompt_can_be_declined(self, harness, monkeypatch):
        seen, _ = harness
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
        monkeypatch.setattr("builtins.input", lambda prompt: "n")
        assert self._run(monkeypatch, "--zap") == 1
        assert seen["remove"] == []

    def test_failure_stops_before_brew(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["brew"] = Path("/opt/homebrew/bin/brew")
        state["failed"] = ["Remote tasks worker: it did not stop"]
        assert self._run(monkeypatch) == 1
        assert seen["exec"] is None
        assert "still installed" in capsys.readouterr().err

    def test_uv_install_finishes_with_uv(self, harness, monkeypatch, capsys):
        seen, _ = harness
        assert self._run(monkeypatch, "--zap", "--yes") == 0
        assert seen["data"] == 1 and seen["exec"] is None
        assert "uv tool uninstall openswap" in capsys.readouterr().out

    def test_uv_beside_the_cask_is_refused(self, harness, monkeypatch, capsys, tmp_path):
        seen, _ = harness
        room = tmp_path / "homebrew" / "Caskroom" / "openswap"
        room.mkdir(parents=True)
        monkeypatch.setattr("openswap.bundle.CASKROOMS", (room,))
        assert self._run(monkeypatch) == 1
        assert seen["remove"] == []
        assert "installed with Homebrew" in capsys.readouterr().err
