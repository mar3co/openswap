"""``openswap uninstall`` and the cask's uninstall hook.

Homebrew runs the cask's uninstall stanza on upgrade and reinstall too, so
the hook must tell those apart from an uninstall by the brew command that
started it. launchctl, pluginkit, ``ps``, the Keychain and ``brew`` are fakes
here; nothing touches a real LaunchAgent or file outside ``tmp_path``.
"""

from __future__ import annotations

import plistlib
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import cli, macos_keychain, uninstall
from openswap.exceptions import ClaudeSwitchError

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
        if state["disable_ok"] == "raise":
            raise ClaudeSwitchError("lifecycle lock unavailable")
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

    def test_app_leaves_a_uv_install_alone(self, mac, frozen_app, tmp_path):
        home, agents, seen, _ = mac
        uv = tmp_path / "uv-bin" / "openswap"
        uv.parent.mkdir()
        uv.write_text("#!/bin/sh\n")
        _plist(agents / "com.opensoft.openswap.menubar.plist", [str(uv), "menubar"])
        _plist(agents / "com.opensoft.openswap.worker.plist", [str(uv), "worker", "run"])
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == [] and removal.failed == []
        assert "com.opensoft.openswap.menubar" not in seen["menubar"]
        assert seen["disable"] == []
        assert (agents / "com.opensoft.openswap.menubar.plist").exists()

    def test_app_leaves_another_copy_of_the_app_alone(self, mac, frozen_app, tmp_path):
        """A dev build or a copy from Downloads outlives the cask's app."""
        home, agents, _, _ = mac
        other = tmp_path / "Downloads" / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"")
        _plist(agents / "com.opensoft.openswap.menubar.plist", [str(other)])
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == []
        assert (agents / "com.opensoft.openswap.menubar.plist").exists()

    def test_app_removes_an_agent_whose_program_is_gone(self, mac, frozen_app):
        home, agents, _, _ = mac
        _plist(agents / "com.opensoft.openswap.menubar.plist", ["/Applications/Old/OpenSwap.app/Contents/MacOS/OpenSwap"])
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == ["menu bar login item"]

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

    def test_force_boots_out_a_worker_whose_disable_raises(self, mac, frozen_app):
        """The app is about to go; a plist on it must not survive."""
        home, agents, seen, state = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run"])
        state["disable_ok"] = "raise"
        removal = uninstall.remove_integrations(home=home, uid=501, force=True)
        assert removal.removed == ["Remote tasks worker"] and removal.failed == []
        assert seen["worker"] == ["uninstall"]

    def test_disable_that_raises_is_a_failure_without_force(self, mac, frozen_app):
        home, agents, seen, state = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run"])
        state["disable_ok"] = "raise"
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.failed == ["Remote tasks worker: lifecycle lock unavailable"]
        assert seen["worker"] == []

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

    def test_bare_status_line_stays_with_the_install_that_wrote_it(self, mac, frozen_app):
        home, _, seen, state = mac
        state["statusline"] = "openswap statusline"  # hook_program is None for it
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == [] and seen["statusline"] == []

    def test_uv_install_restores_its_bare_status_line(self, mac):
        home, _, _, state = mac
        state["statusline"] = "openswap statusline"
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.removed == ["Claude Code status line"]

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
    def test_delete_saved_logins_covers_both_services(self, monkeypatch, tmp_path):
        calls = []
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: tmp_path / "none")
        monkeypatch.setattr(macos_keychain, "delete_all", lambda service: calls.append(service) or 2)
        assert uninstall.delete_saved_logins() == 4
        assert calls == ["openswap", "claude-swap"]
        assert "Claude Code-credentials" not in calls

    def test_delete_saved_logins_clears_session_profiles_first(self, monkeypatch, tmp_path):
        """A profile's item is named from its path; zap deletes the path."""
        backup = tmp_path / "OpenSwap"
        sessions = backup / "sessions"
        (sessions / "1-a-example-com").mkdir(parents=True)
        (sessions / "2-b-example-com").mkdir()
        (sessions / "stray.json").write_text("{}")
        order = []
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: backup)
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: tmp_path / "no-legacy")
        monkeypatch.setattr(
            "openswap.session.delete_macos_keychain_entry", lambda d: order.append(d.name)
        )
        monkeypatch.setattr("openswap.session.profile_is_quiescent", lambda d: d.name != "2-b-example-com")
        monkeypatch.setattr(macos_keychain, "delete_all", lambda service: order.append(service) or 0)
        uninstall.delete_saved_logins()
        # A profile Claude still runs on keeps its login.
        assert order == ["1-a-example-com", "openswap", "claude-swap"]

    def _profiles(self, monkeypatch, tmp_path, scans):
        sessions = tmp_path / "OpenSwap" / "sessions"
        for name in scans:
            (sessions / name).mkdir(parents=True)
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: tmp_path / "OpenSwap")
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: tmp_path / ".claude-swap-backup")
        monkeypatch.setattr("openswap.session.scan_live_sessions", lambda d: scans[d.name])

    def test_profiles_in_the_legacy_folder_are_checked_and_cleared(self, monkeypatch, tmp_path):
        """A zap deletes ~/.claude-swap-backup too, so its profiles count."""
        live = type("S", (), {"pid": 9})()
        self._profiles(monkeypatch, tmp_path, {"1-a": ([], 0), "3-c": ([live], 0)})
        legacy_profile = tmp_path / ".claude-swap-backup" / "sessions" / "3-c"
        legacy_profile.mkdir(parents=True)
        (tmp_path / "OpenSwap" / "sessions" / "3-c").rmdir()
        with pytest.raises(ClaudeSwitchError, match="3-c"):
            uninstall.refuse_live_sessions()
        cleared = []
        monkeypatch.setattr("openswap.session.profile_is_quiescent", lambda d: True)
        monkeypatch.setattr("openswap.session.delete_macos_keychain_entry", cleared.append)
        monkeypatch.setattr(macos_keychain, "delete_all", lambda service: 0)
        uninstall.delete_saved_logins()
        assert cleared == [tmp_path / "OpenSwap" / "sessions" / "1-a", legacy_profile]

    def test_live_session_refuses_a_zap(self, monkeypatch, tmp_path):
        live = type("S", (), {"pid": 4242})()
        self._profiles(monkeypatch, tmp_path, {"1-a": ([], 0), "2-b": ([live], 0)})
        with pytest.raises(ClaudeSwitchError, match=r"2-b \(PID 4242\)"):
            uninstall.refuse_live_sessions()

    def test_unreadable_session_records_refuse_a_zap(self, monkeypatch, tmp_path):
        self._profiles(monkeypatch, tmp_path, {"1-a": ([], 2)})
        with pytest.raises(ClaudeSwitchError, match="1-a"):
            uninstall.refuse_live_sessions()

    def test_idle_profiles_allow_a_zap(self, monkeypatch, tmp_path):
        self._profiles(monkeypatch, tmp_path, {"1-a": ([], 0)})
        uninstall.refuse_live_sessions()

    def test_data_paths_match_what_exists(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        backup = home / "Library" / "Application Support" / "OpenSwap"
        backup.mkdir(parents=True)
        logs = home / "Library" / "Logs"
        logs.mkdir(parents=True)
        (logs / "com.opensoft.openswap.menubar.err").write_text("x")
        (logs / "com.opensoft.openswap.worker.log").write_text("x")
        (logs / "com.other.app.log").write_text("x")
        legacy = home / ".claude-swap-backup"
        legacy.mkdir()
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: backup)
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: legacy)
        found = uninstall.data_paths(home)
        assert found == [
            backup,
            logs / "com.opensoft.openswap.worker.log",
            logs / "com.opensoft.openswap.menubar.err",
            legacy,
        ]
        # Homebrew's zap trashes the rest; the pre-XDG folder is not on its list.
        assert uninstall.data_paths(home, homebrew_zap=True) == [legacy]
        assert uninstall.delete_data(home) == found
        assert not backup.exists() and not legacy.exists()
        assert (logs / "com.other.app.log").exists()

    def test_legacy_root_that_holds_the_backup_root_is_not_deleted_twice(self, tmp_path, monkeypatch):
        legacy = tmp_path / ".claude-swap-backup"
        (legacy / "OpenSwap").mkdir(parents=True)
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: legacy / "OpenSwap")
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: legacy)
        assert uninstall.data_paths(tmp_path, homebrew_zap=True) == []


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
        seen: dict = {"remove": [], "logins": 0, "data": [], "exec": None, "purge": [], "order": []}
        state = {"brew": None, "failed": [], "command": None, "live": None, "purge": None, "collision": False}

        class FakeSwitcher:
            def __init__(self):
                seen["order"].append("engine")
                if state["collision"]:
                    from openswap.exceptions import MigrationError

                    raise MigrationError("Both legacy and new backup paths exist.")

            def purge(self, assume_yes=False):
                seen["purge"].append(assume_yes)
                if state["purge"]:
                    raise ClaudeSwitchError(state["purge"])

        def fake_remove(home=None, uid=None, force=False):
            seen["remove"].append(force)
            return uninstall.Removal(removed=["menu bar login item"], failed=list(state["failed"]))

        def fake_logins():
            seen["logins"] += 1
            return 3

        def fake_data(home=None, *, homebrew_zap=False):
            seen["data"].append(homebrew_zap)
            return [tmp_path / "data"]

        def fake_refuse():
            seen["order"].append("refuse")
            if state["live"]:
                raise ClaudeSwitchError(state["live"])

        def fake_exec(path, argv):
            seen["exec"] = argv
            raise SystemExit(0)

        monkeypatch.setattr(uninstall, "remove_integrations", fake_remove)
        monkeypatch.setattr(uninstall, "delete_saved_logins", fake_logins)
        monkeypatch.setattr(uninstall, "delete_data", fake_data)
        monkeypatch.setattr(uninstall, "homebrew_brew", lambda: state["brew"])
        monkeypatch.setattr(uninstall, "homebrew_command", lambda: state["command"])
        monkeypatch.setattr(uninstall, "refuse_live_sessions", fake_refuse)
        monkeypatch.setattr("openswap.engine.Engine", FakeSwitcher)
        monkeypatch.setattr(uninstall, "legacy_roots", lambda: [tmp_path / ".claude-swap-backup"])
        monkeypatch.setattr(
            uninstall, "delete_session_logins", lambda roots=None: seen["order"].append(("sessions", roots))
        )
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
        assert seen["logins"] == 1 and seen["data"] == [True]  # brew's zap trashes the rest
        assert "Deleted 3 saved OpenSwap logins" in capsys.readouterr().out

    def test_hook_zap_with_a_live_session_warns_and_goes_on(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["command"] = ("uninstall", ["--zap", "--cask", "openswap"])
        state["live"] = "Claude is running on session profile 1-a (PID 7)."
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert seen["logins"] == 1
        assert "Its saved login stays in the Keychain." in capsys.readouterr().err

    def test_hook_failure_still_exits_zero(self, harness, monkeypatch, capsys):
        _, state = harness
        state["command"] = ("uninstall", [])
        state["failed"] = ["Remote tasks worker: boom"]
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert "boom" in capsys.readouterr().err

    def test_cask_install_hands_off_to_brew(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["brew"] = brew = Path("/opt/homebrew/bin/brew")
        assert self._run(monkeypatch) == 0
        assert seen["exec"] == [str(brew), "uninstall", "--cask", "openswap"]
        assert seen["remove"] == [False] and seen["logins"] == 0 and seen["purge"] == []
        out = capsys.readouterr().out
        assert "Removed the menu bar login item." in out
        assert "brew uninstall --cask openswap" in out

    def test_zap_with_brew(self, harness, monkeypatch):
        seen, state = harness
        state["brew"] = brew = Path("/opt/homebrew/bin/brew")
        assert self._run(monkeypatch, "--zap", "--yes") == 0
        assert seen["exec"] == [str(brew), "uninstall", "--cask", "--zap", "openswap"]
        assert seen["purge"] == [True]
        assert seen["logins"] == 1 and seen["data"] == [True]

    def test_zap_clears_legacy_profiles_before_opening_the_data(self, harness, monkeypatch, tmp_path):
        """The Engine's migration moves the pre-XDG folder and with it the
        paths its profiles' Keychain items are named from."""
        seen, _ = harness
        assert self._run(monkeypatch, "--zap", "--yes") == 0
        assert seen["order"][:3] == ["refuse", ("sessions", [tmp_path / ".claude-swap-backup"]), "engine"]

    def test_zap_with_colliding_data_folders_deletes_both_without_purge(self, harness, monkeypatch):
        """No openswap command can open colliding folders, so nothing can be
        set up under them; the zap deletes both after the live check."""
        seen, state = harness
        state["collision"] = True
        assert self._run(monkeypatch, "--zap", "--yes") == 0
        assert seen["purge"] == []
        assert seen["order"].count("refuse") == 2
        assert seen["logins"] == 1 and seen["data"] == [False]

    def test_zap_stops_when_purge_refuses_after_the_prompt(self, harness, monkeypatch, capsys):
        """A session that went live while the prompt waited is caught by
        purge's own check, under the locks session setup takes."""
        seen, state = harness
        state["brew"] = Path("/opt/homebrew/bin/brew")
        state["purge"] = "Live session-mode Claude instance(s) found: 1-a (PID 7)."
        assert self._run(monkeypatch, "--zap", "--yes") == 1
        assert seen["logins"] == 0 and seen["data"] == [] and seen["exec"] is None
        err = capsys.readouterr().err
        assert "PID 7" in err and "still installed" in err

    def test_zap_refuses_while_a_session_is_live(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["brew"] = Path("/opt/homebrew/bin/brew")
        state["live"] = "Claude is running on session profile 1-a (PID 7). Exit it, then retry."
        assert self._run(monkeypatch, "--zap", "--yes") == 1
        assert seen["remove"] == [] and seen["logins"] == 0 and seen["exec"] is None
        assert "PID 7" in capsys.readouterr().err

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
        assert seen["purge"] == [True]
        assert seen["data"] == [False] and seen["exec"] is None
        assert "uv tool uninstall openswap" in capsys.readouterr().out

    def test_uv_beside_the_cask_is_refused(self, harness, monkeypatch, capsys, tmp_path):
        seen, _ = harness
        room = tmp_path / "homebrew" / "Caskroom" / "openswap"
        room.mkdir(parents=True)
        monkeypatch.setattr("openswap.bundle.CASKROOMS", (room,))
        assert self._run(monkeypatch) == 1
        assert seen["remove"] == []
        assert "installed with Homebrew" in capsys.readouterr().err

    # uninstall.run is the command's body after its checks and prompt.

    @staticmethod
    def _say():
        lines: list[tuple[str, bool]] = []
        return lines, lambda line, error=False: lines.append((line, error))

    @pytest.mark.parametrize("zap", [False, True])
    def test_run_removes_then_hands_off_to_brew(self, harness, zap):
        seen, state = harness
        state["brew"] = brew = Path("/opt/homebrew/bin/brew")
        lines, say = self._say()
        with pytest.raises(SystemExit):
            uninstall.run(zap, say)
        assert seen["remove"] == [False]
        assert seen["exec"] == [str(brew), "uninstall", "--cask", *(["--zap"] if zap else []), "openswap"]
        assert seen["purge"] == ([True] if zap else [])
        assert seen["logins"] == int(zap) and seen["data"] == ([True] if zap else [])
        assert ("Removed the menu bar login item.", False) in lines
        assert (("Deleted 3 saved logins from the Keychain.", False) in lines) == zap

    @pytest.mark.parametrize("zap", [False, True])
    def test_run_without_brew_names_what_is_left(self, harness, zap, frozen_app):
        seen, _ = harness
        lines, say = self._say()
        assert uninstall.run(zap, say) == 0
        assert seen["exec"] is None and seen["data"] == ([False] if zap else [])
        assert lines[-1] == (f"To finish, move {frozen_app} to the Trash.", False)

    @pytest.mark.parametrize("zap", [False, True])
    def test_run_stops_on_a_failed_removal(self, harness, zap):
        seen, state = harness
        state["brew"] = Path("/opt/homebrew/bin/brew")
        state["failed"] = ["Remote tasks worker: it did not stop"]
        lines, say = self._say()
        assert uninstall.run(zap, say) == 1
        assert seen["exec"] is None and seen["purge"] == [] and seen["logins"] == 0
        assert lines[-1] == (
            f"OpenSwap is still installed. Fix that, then run: openswap uninstall{' --zap' if zap else ''}",
            True,
        )

    def test_run_from_the_app_trashes_a_copy_not_installed_by_homebrew(
        self, harness, monkeypatch, frozen_app
    ):
        trashed = []
        monkeypatch.setattr("openswap.widget_install._move_to_trash", trashed.append)
        monkeypatch.setattr(uninstall, "_open", lambda *a: pytest.fail("revealed"))
        lines, say = self._say()
        assert uninstall.run(False, say, from_app=True) == 0
        assert trashed == [frozen_app]
        assert lines[-1] == (f"Moved {frozen_app} to the Trash.", False)

    def test_run_from_the_app_reveals_a_copy_it_cannot_trash(self, harness, monkeypatch, frozen_app):
        def refuse(path):
            raise ClaudeSwitchError(f"Could not move {path} to the Trash: permission denied")

        opened = []
        monkeypatch.setattr("openswap.widget_install._move_to_trash", refuse)
        monkeypatch.setattr(uninstall, "_open", lambda *a: opened.append(a))
        lines, say = self._say()
        assert uninstall.run(False, say, from_app=True) == 0
        assert opened == [("-R", str(frozen_app))]
        assert (f"Could not move {frozen_app} to the Trash: permission denied", True) in lines

    def test_run_from_the_app_opens_the_log_when_brew_fails(self, harness, monkeypatch, tmp_path):
        seen, state = harness
        state["brew"] = brew = Path("/opt/homebrew/bin/brew")
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        _, say = self._say()
        with pytest.raises(SystemExit):
            uninstall.run(True, say, from_app=True)
        sh, flag, script, *command = seen["exec"]
        assert (sh, flag) == ("/bin/sh", "-c")
        assert command == [str(brew), "uninstall", "--cask", "--zap", "openswap"]
        log = tmp_path / "home" / "Library" / "Logs" / "com.opensoft.openswap.uninstall.log"
        assert script == f'"$0" "$@" || exec /usr/bin/open {shlex.quote(str(log))}'

    def test_run_from_a_source_install_names_uv(self, harness):
        lines, say = self._say()
        assert uninstall.run(False, say, from_app=True) == 0
        assert lines[-1] == ("To finish, run: uv tool uninstall openswap", False)

    def test_from_app_waits_for_the_app_and_trashes_it(self, harness, monkeypatch, capsys, frozen_app):
        seen, _ = harness
        order = []
        monkeypatch.setattr(uninstall, "wait_for_app_to_quit", lambda: order.append("wait") or True)
        monkeypatch.setattr(uninstall, "show_app_log", lambda: order.append("log"))
        monkeypatch.setattr("openswap.widget_install._move_to_trash", lambda path: order.append("trash"))
        assert self._run(monkeypatch, "--yes", "--from-app") == 0
        assert order == ["wait", "trash"] and seen["remove"] == [False]
        assert f"Moved {frozen_app} to the Trash." in capsys.readouterr().out

    def test_from_app_failure_opens_the_log(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["live"] = "Claude is running on session profile 1-a (PID 7). Exit it, then retry."
        order = []
        monkeypatch.setattr(uninstall, "wait_for_app_to_quit", lambda: order.append("wait") or False)
        monkeypatch.setattr(uninstall, "show_app_log", lambda: order.append("log"))
        assert self._run(monkeypatch, "--yes", "--zap", "--from-app") == 1
        assert order == ["wait", "log"] and seen["remove"] == []
        captured = capsys.readouterr()
        assert "did not quit in time" in captured.out and "PID 7" in captured.err

    def test_terminal_command_never_trashes_or_waits(self, harness, monkeypatch, capsys, frozen_app):
        monkeypatch.setattr(uninstall, "wait_for_app_to_quit", lambda: pytest.fail("waited"))
        monkeypatch.setattr("openswap.widget_install._move_to_trash", lambda path: pytest.fail("trashed"))
        assert self._run(monkeypatch) == 0
        assert f"To finish, move {frozen_app} to the Trash." in capsys.readouterr().out


class TestStartFromApp:
    """The menu bar never removes anything itself: removing the login item
    would boot it out. It starts ``openswap uninstall`` and quits."""

    @pytest.fixture
    def popen(self, monkeypatch):
        started = []

        class FakePopen:
            def __init__(self, argv, **kwargs):
                started.append((argv, kwargs))

        monkeypatch.setattr(uninstall.subprocess, "Popen", FakePopen)
        monkeypatch.setattr(uninstall, "remove_integrations", lambda **k: pytest.fail("removed in-process"))
        return started

    def test_starts_the_app_executable_detached_with_a_log(self, popen, frozen_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XPC_SERVICE_NAME", "com.opensoft.openswap.menubar")
        home = tmp_path / "home"
        command = uninstall.start_from_app(True, home=home)
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        assert command == [exe, "uninstall", "--yes", "--zap", "--from-app"]
        (argv, kwargs), = popen
        assert argv == command
        assert kwargs["start_new_session"] is True
        assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stderr"] is subprocess.STDOUT
        log = home / "Library" / "Logs" / "com.opensoft.openswap.uninstall.log"
        assert kwargs["stdout"].name == str(log)
        assert "XPC_SERVICE_NAME" not in kwargs["env"]
        assert kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
        assert log.read_text().strip().endswith(" ".join(command))

    def test_without_zap(self, popen, frozen_app, tmp_path):
        command = uninstall.start_from_app(False, home=tmp_path)
        assert command[1:] == ["uninstall", "--yes", "--from-app"]

    def test_source_install_runs_its_console_script(self, popen, monkeypatch, tmp_path):
        monkeypatch.setattr("openswap.launch_agent.resolve_program", lambda: [UV_SCRIPT])
        command = uninstall.start_from_app(False, home=tmp_path)
        assert command == [UV_SCRIPT, "uninstall", "--yes", "--from-app"]
        assert "PYINSTALLER_RESET_ENVIRONMENT" not in popen[0][1]["env"]

    def test_the_uninstall_log_survives_a_zap(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        logs = home / "Library" / "Logs"
        logs.mkdir(parents=True)
        (logs / "com.opensoft.openswap.uninstall.log").write_text("x")
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: home / "none")
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: home / "none")
        assert uninstall.data_paths(home) == []

    def test_waits_until_the_app_has_quit(self, monkeypatch):
        parents = iter([700, 700, 700, 1])
        monkeypatch.setattr(uninstall.os, "getppid", lambda: next(parents))
        monkeypatch.setattr(uninstall.time, "sleep", lambda s: None)
        assert uninstall.wait_for_app_to_quit() is True

    def test_gives_up_waiting_after_the_timeout(self, monkeypatch):
        monkeypatch.setattr(uninstall.os, "getppid", lambda: 700)
        clock = iter([0.0, 10.0, 31.0])
        monkeypatch.setattr(uninstall.time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(uninstall.time, "sleep", lambda s: None)
        assert uninstall.wait_for_app_to_quit(timeout=30.0) is False


class TestAppDialog:
    def test_cask_install(self, frozen_app, monkeypatch):
        monkeypatch.setattr(uninstall, "homebrew_brew", lambda: Path("/opt/homebrew/bin/brew"))
        dialog = uninstall.app_dialog()
        assert dialog["title"] == "Uninstall OpenSwap?" and dialog["ok"] == "Uninstall"
        assert dialog["checkbox"] == "Also delete saved accounts, settings and logs"
        assert "• OpenSwap.app, through Homebrew" in dialog["message"]
        assert "uv tool" not in dialog["message"]

    def test_copy_not_installed_by_homebrew(self, frozen_app, monkeypatch):
        monkeypatch.setattr(uninstall, "homebrew_brew", lambda: None)
        assert "• OpenSwap.app, moved to the Trash" in uninstall.app_dialog()["message"]

    def test_source_install(self):
        message = uninstall.app_dialog()["message"]
        assert ".app" not in message
        assert message.endswith("run uv tool uninstall openswap afterwards.")


class TestMenuAction:
    @pytest.fixture
    def app(self, monkeypatch):
        from openswap import menubar
        from tests.menubar_harness import extract_class

        app = extract_class(
            menubar.__file__, "MenuBarApp", {"on_uninstall"}, {"ClaudeSwitchError": ClaudeSwitchError}
        )()
        app.calls = []
        app.answer = SimpleNamespace(clicked=1, text="", checked=False)
        app._confirm = lambda **kw: app.calls.append(("confirm", kw)) or app.answer
        app._alert = lambda **kw: app.calls.append(("alert", kw["message"])) or 1
        app.on_quit = lambda sender: app.calls.append("quit")
        monkeypatch.setattr(uninstall, "app_dialog", lambda: {"title": "Uninstall OpenSwap?", "ok": "Uninstall"})
        monkeypatch.setattr(uninstall, "start_from_app", lambda zap: app.calls.append(("start", zap)))
        monkeypatch.setattr(uninstall, "remove_integrations", lambda **k: pytest.fail("removed in-process"))
        monkeypatch.setattr(uninstall, "refuse_live_sessions", lambda: app.calls.append("refuse"))
        return app

    @pytest.mark.parametrize("zap", [False, True])
    def test_confirm_starts_the_child_then_quits(self, app, zap):
        app.answer = SimpleNamespace(clicked=1, text="", checked=zap)
        app.on_uninstall(None)
        (kind, shown), *rest = app.calls
        assert kind == "confirm" and shown["destructive"] is True and shown["cancel"] is True
        assert rest == [*(["refuse"] if zap else []), ("start", zap), "quit"]

    def test_cancel_does_nothing(self, app):
        app.answer = SimpleNamespace(clicked=0, text="", checked=True)
        app.on_uninstall(None)
        assert [c[0] for c in app.calls] == ["confirm"]

    def test_a_live_session_stops_a_zap_with_an_alert(self, app, monkeypatch):
        def refuse():
            raise ClaudeSwitchError("Claude is running on session profile 1-a (PID 7). Exit it, then retry.")

        monkeypatch.setattr(uninstall, "refuse_live_sessions", refuse)
        app.answer = SimpleNamespace(clicked=1, text="", checked=True)
        app.on_uninstall(None)
        assert app.calls[1:] == [("alert", "Claude is running on session profile 1-a (PID 7). Exit it, then retry.")]

    def test_a_child_that_cannot_start_keeps_the_app_running(self, app, monkeypatch):
        def fail(zap):
            raise PermissionError("Operation not permitted")

        monkeypatch.setattr(uninstall, "start_from_app", fail)
        app.on_uninstall(None)
        assert app.calls[-1] == ("alert", "OpenSwap could not start the uninstall: Operation not permitted")

    def test_the_overflow_menu_offers_it(self):
        from openswap import menubar

        text = Path(menubar.__file__).read_text(encoding="utf-8")
        rebuild = text[text.index("def rebuild_menu") : text.index("def _add_menu")]
        item = 'rumps.MenuItem("Uninstall OpenSwap…", callback=self.on_uninstall)'
        assert item in rebuild
        assert rebuild.index(item) < rebuild.index('rumps.MenuItem("Quit"')
