"""``openswap uninstall`` and the cask's uninstall hook.

Homebrew runs the cask's uninstall stanza on upgrade and reinstall too, so
the hook must tell those apart from an uninstall by the brew command that
started it. launchctl, pluginkit, ``ps``, the Keychain and ``brew`` are fakes
here; nothing touches a real LaunchAgent or file outside ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import copy
import json
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import cli, launch_agent, macos_keychain, menubar_instance, uninstall
from openswap.claude_desktop_dock import tile_path
from openswap.exceptions import ClaudeSwitchError
from openswap.menubar_display import MenuBarSettings, menubar_settings_path

# The mac fixture fakes launch_agent.uninstall; a test that needs the real
# one (to see the bootout) puts this back.
REAL_LAUNCH_AGENT_UNINSTALL = launch_agent.uninstall

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
        if state.get("worker_uninstall_raises"):
            raise OSError("could not unlink the plist")
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
        assert removal.job_stop_unconfirmed  # so a zap keeps its job state
        # And a later zap, after a reinstall, too.
        assert (home / "Library" / "Application Support" / "com.opensoft.openswap" / "stop-unconfirmed").is_file()

    def test_a_forced_boot_out_that_fails_still_keeps_the_job_state(self, mac, frozen_app):
        """It can boot the worker out and then fail on the plist: the task may still run."""
        home, agents, seen, state = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run"])
        state["disable_ok"] = False
        state["worker_uninstall_raises"] = True
        removal = uninstall.remove_integrations(home=home, uid=501, force=True)
        assert len(removal.failed) == 1 and removal.failed[0].startswith("Remote tasks worker")
        assert removal.job_stop_unconfirmed
        assert (home / "Library" / "Application Support" / "com.opensoft.openswap" / "stop-unconfirmed").is_file()

    def test_a_stop_it_cannot_record_leaves_the_worker_loaded(self, mac, frozen_app):
        """Then the loaded worker is what shows a later zap that a task may run."""
        home, agents, seen, state = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run"])
        state["disable_ok"] = False
        support = home / "Library" / "Application Support"
        support.mkdir(parents=True, exist_ok=True)
        (support / "com.opensoft.openswap").write_text("not a folder")
        removal = uninstall.remove_integrations(home=home, uid=501, force=True)
        assert seen["worker"] == []  # not booted out
        assert len(removal.failed) == 1 and "stays loaded" in removal.failed[0]
        assert removal.job_stop_unconfirmed

    def test_force_boots_out_a_worker_whose_disable_raises(self, mac, frozen_app):
        """The app is about to go; a plist on it must not survive."""
        home, agents, seen, state = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run"])
        state["disable_ok"] = "raise"
        removal = uninstall.remove_integrations(home=home, uid=501, force=True)
        assert removal.removed == ["Remote tasks worker"] and removal.failed == []
        assert seen["worker"] == ["uninstall"]
        assert removal.job_stop_unconfirmed

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


class FakeDock:
    def __init__(self, apps):
        self.apps = apps
        self.managed = Mock(return_value=False)
        self.refresh = Mock()
        self.writes: list = []

    def snapshot(self):
        return copy.deepcopy(self.apps)

    def write(self, apps):
        self.writes.append(copy.deepcopy(apps))
        self.apps = copy.deepcopy(apps)


def tile(path, guid=1):
    return {"GUID": guid, "tile-type": "file-tile", "tile-data": {
        "file-data": {"_CFURLString": Path(path).as_uri() + "/", "_CFURLStringType": 15}}}


def launcher(folder, name, profile_id, root, *, bundle_id=None, marker=None):
    """A bundle shaped like create_shortcut's output: only its plist matters here."""
    path = folder / f"Claude {name}.app"
    (path / "Contents").mkdir(parents=True)
    info = {"CFBundleIdentifier": bundle_id or f"com.opensoft.openswap.claude-profile.{profile_id}",
            "OpenSwapProfile": marker if marker is not None else {"version": 1, "profile": profile_id, "root": str(root)}}
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return path


@pytest.fixture
def profile_mac(mac, monkeypatch, tmp_path):
    """The mac fixture's home as this Mac's own, with the Dock faked and the
    profile store elsewhere under tmp_path (absent until a test makes it)."""
    home, _agents, _seen, state = mac
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("openswap.claude_desktop_links.restore_for_uninstall", lambda: False)
    root = tmp_path / "store"
    monkeypatch.setattr("openswap.claude_desktop.default_root", lambda: root)
    dock = FakeDock([tile("/Applications/Claude.app", 1), tile("/Applications/Other.app", 2)])
    monkeypatch.setattr(uninstall, "_dock", lambda: dock)
    return SimpleNamespace(home=home, folder=home / "Applications" / "OpenSwap Profiles", root=root, dock=dock, state=state)


@pytest.mark.skipif(sys.platform == "win32", reason="the Dock fixtures use drive-less POSIX paths")
class TestClaudeDesktopCleanup:
    def test_owned_launchers_their_tiles_and_the_empty_folder_go(self, profile_mac):
        m = profile_mac
        m.root.mkdir()
        work = launcher(m.folder, "Work", "a" * 32, m.root)
        launcher(m.folder, "Personal", "b" * 32, m.root)
        (m.folder / ".openswap-shortcuts.lock").touch()
        m.dock.apps.insert(1, tile(work, 3))
        removal = uninstall.remove_integrations(home=m.home, uid=501)
        assert removal.removed == ["Claude profile shortcuts"]
        assert removal.failed == [] and removal.warnings == []
        assert not m.folder.exists()
        assert [tile_path(row) for row in m.dock.apps] == [Path("/Applications/Claude.app").resolve(),
                                                           Path("/Applications/Other.app").resolve()]
        m.dock.refresh.assert_called_once()
        [backup] = list((m.root / "dock-backups").iterdir())
        assert plistlib.loads(backup.read_bytes())["persistent-apps"][1] == tile(work, 3)
        assert not (m.root / "profiles.json").exists() and not (m.root / "profiles.lock").exists()

    def test_only_bundles_with_openswap_marker_go_and_the_folder_then_stays(self, profile_mac, monkeypatch):
        m = profile_mac
        foreign = m.folder / "Other.app"
        (foreign / "Contents").mkdir(parents=True)
        (foreign / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.example.other"}))
        lookalike = launcher(m.folder, "Fake", "c" * 32, m.root, bundle_id="com.example.fake")
        broken = launcher(m.folder, "Broken", "d" * 32, m.root, marker="not a marker")
        stray = m.folder / "notes.txt"
        stray.write_text("mine")
        monkeypatch.setattr(uninstall, "_dock", Mock(side_effect=AssertionError("the Dock was read")))
        removal = uninstall.remove_integrations(home=m.home, uid=501)
        assert removal.removed == [] and removal.failed == [] and removal.warnings == []
        assert foreign.is_dir() and lookalike.is_dir() and broken.is_dir() and stray.exists()

    def test_a_launcher_that_will_not_delete_fails_the_step_and_keeps_its_tile(self, profile_mac, monkeypatch):
        m = profile_mac
        work = launcher(m.folder, "Work", "a" * 32, m.root)
        stuck = launcher(m.folder, "Stuck", "b" * 32, m.root)
        m.dock.apps += [tile(work, 3), tile(stuck, 4)]
        real = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            if Path(path) == stuck:
                raise OSError("Operation not permitted")
            real(path, *args, **kwargs)

        monkeypatch.setattr(uninstall.shutil, "rmtree", rmtree)
        removal = uninstall.remove_integrations(home=m.home, uid=501)
        assert removal.removed == [] and removal.warnings == []
        [failure] = removal.failed
        assert failure.startswith("Claude profile shortcuts: could not delete Claude Stuck.app")
        assert not work.exists() and stuck.is_dir() and m.folder.is_dir()
        assert [tile_path(row) for row in m.dock.apps][-1] == stuck.resolve() and len(m.dock.apps) == 3

    @pytest.mark.parametrize("trouble", ["managed", "changed", "refresh", "no_appkit", "objc", "refresh_native"])
    def test_dock_trouble_is_a_warning_not_a_failure(self, profile_mac, monkeypatch, trouble):
        m = profile_mac
        work = launcher(m.folder, "Work", "a" * 32, m.root)
        m.dock.apps.append(tile(work, 3))
        if trouble == "managed":
            m.dock.managed.return_value = True
        if trouble == "changed":
            snapshots = iter([copy.deepcopy(m.dock.apps), [tile("/Applications/Claude.app", 1)]])
            m.dock.snapshot = lambda: next(snapshots)
        if trouble == "refresh":
            m.dock.refresh.side_effect = ClaudeSwitchError("Shortcuts were saved. Reopen the Dock to refresh their icons.")
        if trouble == "no_appkit":
            monkeypatch.setattr(uninstall, "_dock", Mock(side_effect=ImportError("No module named Foundation")))
        if trouble == "objc":
            m.dock.managed.side_effect = AttributeError("NSUserDefaults does not recognize the selector")
        if trouble == "refresh_native":
            m.dock.refresh.side_effect = RuntimeError("NSRunningApplication unavailable")
        removal = uninstall.remove_integrations(home=m.home, uid=501)
        assert removal.removed == ["Claude profile shortcuts"] and removal.failed == []
        [warning] = removal.warnings
        assert "Dock" in warning
        assert not m.folder.exists()
        assert (m.dock.writes == []) is (trouble not in ("refresh", "refresh_native"))

    def test_the_dock_backup_never_creates_the_profile_store(self, profile_mac):
        m = profile_mac
        work = launcher(m.folder, "Work", "a" * 32, m.root)
        m.dock.apps.append(tile(work, 3))
        removal = uninstall.remove_integrations(home=m.home, uid=501)
        assert removal.warnings == [] and not m.root.exists()
        [backup] = (m.home / "Library" / "Logs").glob("com.opensoft.openswap.dock-backup.*.plist")
        assert len(plistlib.loads(backup.read_bytes())["persistent-apps"]) == 3 and len(m.dock.apps) == 2

    def test_another_home_is_left_alone(self, mac, monkeypatch, tmp_path):
        home, _agents, _seen, _state = mac
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(uninstall, "_dock", Mock(side_effect=AssertionError("the Dock was read")))
        work = launcher(home / "Applications" / "OpenSwap Profiles", "Work", "a" * 32, tmp_path)
        assert uninstall.remove_integrations(home=home, uid=501).removed == []
        assert work.is_dir()

    @pytest.mark.parametrize("outcome", ["restored", "nothing", "refused", "native"])
    def test_link_handback_never_fails_the_uninstall(self, profile_mac, monkeypatch, outcome):
        from openswap.claude_desktop import ClaudeDesktopError
        m = profile_mac
        remedy = "Could not restore Claude links. Open Claude once to restore its link handling."

        def restore():
            if outcome == "refused":
                raise ClaudeDesktopError(remedy)
            if outcome == "native":
                raise RuntimeError("objc: no run loop")
            return outcome == "restored"

        monkeypatch.setattr("openswap.claude_desktop_links.restore_for_uninstall", restore)
        removal = uninstall.remove_integrations(home=m.home, uid=501)
        assert removal.failed == []
        assert removal.removed == (["Claude Desktop link handling"] if outcome == "restored" else [])
        expected = {"refused": [remedy], "native": [f"Could not restore Claude links (objc: no run loop). "
                                                     "Open Claude once to restore its link handling."]}
        assert removal.warnings == expected.get(outcome, [])


class TestUndoPhaseOneAndTwo:
    """What plan 020's first two phases add outside the zapped data, undone
    by an uninstall without zap, and safe to run twice."""

    @staticmethod
    def _launchd(monkeypatch, loaded: set[str]) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []

        def fake(*args):
            calls.append(args)
            label = args[-1].rsplit("/", 1)[-1]
            if args[0] == "print":
                return subprocess.CompletedProcess(args, 0 if label in loaded else 113, "", "")
            if args[0] == "bootout":
                loaded.discard(label)
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr("openswap.launch_agent.uninstall", REAL_LAUNCH_AGENT_UNINSTALL)
        monkeypatch.setattr(launch_agent, "_launchctl", fake)
        monkeypatch.setattr(launch_agent, "_require_macos", lambda: None)
        return calls

    @pytest.mark.parametrize("with_plist", [True, False])
    def test_a_loaded_login_item_is_booted_out(self, mac, frozen_app, monkeypatch, with_plist):
        """launch_agent.uninstall boots the job out, not only deletes the
        plist; a job left loaded after Open at login was turned off from it
        (no plist) goes too."""
        home, agents, _, _ = mac
        plist = agents / f"{launch_agent.LABEL}.plist"
        if with_plist:
            _plist(plist, [str(frozen_app / "Contents" / "MacOS" / "OpenSwap")])
        calls = self._launchd(monkeypatch, {launch_agent.LABEL})
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert ("bootout", f"gui/501/{launch_agent.LABEL}") in calls
        assert removal.removed == ["menu bar login item"] and not plist.exists()
        calls.clear()
        again = uninstall.remove_integrations(home=home, uid=501)
        assert again.removed == [] and again.failed == []
        assert not any(call[0] == "bootout" for call in calls)

    @staticmethod
    def _runtime_files(backup: Path) -> list[Path]:
        backup.mkdir(parents=True, exist_ok=True)
        files = [
            menubar_instance.lock_path(backup),
            menubar_instance.show_request_path(backup),
            menubar_instance.handoff_request_path(backup),
            launch_agent.login_item_status_path(backup),
        ]
        for path in files:
            path.write_text("x")
        return files

    @pytest.mark.skipif(sys.platform == "win32", reason="the menu bar lock is a flock")
    def test_runtime_files_go_once_no_extra_holds_the_lock(self, mac, frozen_app):
        home, _, _, _ = mac
        backup = uninstall.paths.get_backup_root()
        files = self._runtime_files(backup)
        removal = uninstall.remove_integrations(home=home, uid=501)
        assert removal.failed == [] and removal.removed == []  # transient: not reported
        assert not any(path.exists() for path in files)
        assert uninstall.remove_integrations(home=home, uid=501).failed == []

    @pytest.mark.skipif(sys.platform == "win32", reason="the menu bar lock is a flock")
    def test_runtime_files_stay_while_another_extra_runs(self, mac, frozen_app):
        """A uv install's extra beside the cask owns them, and unlinking its
        held lock would let a second extra lock a new file."""
        home, _, _, _ = mac
        backup = uninstall.paths.get_backup_root()
        files = self._runtime_files(backup)
        held = menubar_instance.menubar_lock(backup)
        try:
            assert uninstall.remove_integrations(home=home, uid=501).failed == []
            assert all(path.exists() for path in files)
        finally:
            held.release()

    def test_no_data_folder_is_created_for_them(self, mac, frozen_app):
        home, _, _, _ = mac
        backup = uninstall.paths.get_backup_root()
        assert not backup.exists()
        assert uninstall.remove_integrations(home=home, uid=501).failed == []
        assert not backup.exists()

    def test_the_app_offers_its_welcome_again_after_a_reinstall(self, mac, frozen_app):
        """welcome_done says the login item was decided; the uninstall just
        removed it. Other settings stay without zap."""
        home, _, _, _ = mac
        path = menubar_settings_path(uninstall.paths.get_backup_root())
        path.parent.mkdir(parents=True)
        MenuBarSettings(welcome_done=True, show_account_name=False).save(path)
        assert uninstall.remove_integrations(home=home, uid=501).failed == []
        kept = MenuBarSettings.load(path)
        assert kept.welcome_done is False and kept.show_account_name is False
        assert uninstall.remove_integrations(home=home, uid=501).failed == []

    def test_a_source_install_leaves_the_apps_welcome_alone(self, mac):
        home, _, _, _ = mac
        path = menubar_settings_path(uninstall.paths.get_backup_root())
        path.parent.mkdir(parents=True)
        MenuBarSettings(welcome_done=True).save(path)
        uninstall.remove_integrations(home=home, uid=501)
        assert MenuBarSettings.load(path).welcome_done is True

    def test_the_bundled_widget_registration_is_undone(self, mac, frozen_app):
        """The app's start-up registration (register_on_start) and
        `widget --install` register the same bundled appex this unregisters."""
        home, _, seen, _ = mac
        uninstall.remove_integrations(home=home, uid=501)
        assert seen["widget"] == ["uninstall"]

    def test_the_status_line_the_toggle_pinned_is_restored(self, mac, frozen_app, monkeypatch):
        """The Settings toggle and `statusline --install` both pin
        paint_command(), this app's executable."""
        home, _, seen, state = mac
        exe = frozen_app / "Contents" / "MacOS" / "OpenSwap"
        state["statusline"] = f"{exe} statusline"
        monkeypatch.setattr("openswap.statusline.hook_program", lambda config_home: str(exe))
        assert uninstall.remove_integrations(home=home, uid=501).removed == ["Claude Code status line"]
        assert len(seen["statusline"]) == 1

    def test_a_second_uninstall_from_the_app_finds_nothing_and_succeeds(
        self, mac, frozen_app, monkeypatch
    ):
        """So "choose Uninstall OpenSwap… again" works after a failure."""
        home, agents, _, _ = mac
        exe = str(frozen_app / "Contents" / "MacOS" / "OpenSwap")
        _plist(agents / "com.opensoft.openswap.menubar.plist", [exe])
        _plist(agents / "com.opensoft.openswap.worker.plist", [exe, "worker", "run", "--managed"])
        monkeypatch.setattr(Path, "home", lambda: home)
        monkeypatch.setattr(uninstall, "homebrew_brew", lambda: None)

        def trash(path):
            if path.exists():
                shutil.rmtree(path)

        monkeypatch.setattr("openswap.widget_install._move_to_trash", trash)
        for attempt in range(2):
            lines: list[tuple[str, bool]] = []
            assert uninstall.run(False, lambda line, error=False: lines.append((line, error)), from_app=True) == 0
            assert not any(error for _, error in lines)
            removed = [line for line, _ in lines if line.startswith("Removed the ")]
            assert removed == (
                ["Removed the menu bar login item.", "Removed the Remote tasks worker."] if attempt == 0 else []
            )
        assert not frozen_app.exists() and not any(agents.iterdir())


class TestStatusLineUndoWithTheRealHook:
    """The status line through the real statusline module. Turning the
    toggle on may refuse to rewrite an unusual hook (statusline._with_command);
    an uninstall never rewrites one: it puts back the user's own line in
    place of a hook this app runs, or leaves another program's as written."""

    @pytest.fixture
    def claude(self, tmp_path, monkeypatch, frozen_app):
        from openswap import statusline

        config = tmp_path / "claude-config"
        config.mkdir()
        monkeypatch.setattr("openswap.paths.get_claude_config_home", lambda: config)
        statusline.save_wrap(uninstall.paths.get_backup_root(), inner_command="my-line", created=False)
        exe = shlex.quote(str(frozen_app / "Contents" / "MacOS" / "OpenSwap"))

        def hook(command=None):
            settings = config / "settings.json"
            if command is not None:
                settings.write_text(json.dumps({"statusLine": {"type": "command", "command": command}}))
            return json.loads(settings.read_text()).get("statusLine", {}).get("command")

        return hook, exe

    @pytest.mark.parametrize("form", [
        "{exe} statusline",
        "echo ready&&{exe} statusline",  # an operator with no spaces
        "FOO=$(true) {exe} statusline",  # a prefix the toggle will not rewrite
    ])
    def test_a_hook_this_app_runs_gives_back_the_users_line(self, claude, form):
        hook, exe = claude
        hook(form.format(exe=exe))
        assert uninstall._restore_status_line() is True
        assert hook() == "my-line"
        assert uninstall._restore_status_line() is False  # twice: nothing left

    def test_another_openswaps_unusual_hook_is_left_as_written(self, claude, tmp_path):
        hook, _ = claude
        uv = tmp_path / "uv-bin" / "openswap"
        uv.parent.mkdir()
        uv.write_text("#!/bin/sh\n")
        written = f"FOO=$(true) {shlex.quote(str(uv))} statusline"
        hook(written)
        assert uninstall._restore_status_line() is False
        assert hook() == written


class TestNoRelaunchAndExplicitPaths:
    def test_a_clean_quit_is_not_relaunched_while_the_child_works(self, tmp_path):
        """Uninstall OpenSwap… quits through on_quit, a clean exit. KeepAlive
        restarts only a failed exit and nothing else in the plist starts the
        job, so launchd cannot bring the extra back while the child removes
        it. (unload_own_job leaves a job with its plist alone:
        test_launch_agent.)"""
        exe = "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap"
        plist = plistlib.loads(launch_agent.build_plist([exe], home=tmp_path))
        assert plist["KeepAlive"] == {"SuccessfulExit": False}
        assert not {"StartInterval", "StartCalendarInterval", "WatchPaths", "QueueDirectories",
                    "StartOnMount"} & plist.keys()
        from openswap import menubar

        text = Path(menubar.__file__).read_text(encoding="utf-8")
        action = text[text.index("def on_uninstall") : text.index("def on_toggle_name")]
        assert action.rstrip().endswith("self.on_quit(None)")

    def test_brew_is_the_cask_prefixs_own_not_one_on_path(self, frozen_app, monkeypatch, tmp_path):
        prefix = tmp_path / "homebrew"
        brew = prefix / "bin" / "brew"
        brew.parent.mkdir(parents=True)
        brew.write_text("#!/bin/sh\n")
        brew.chmod(0o755)
        monkeypatch.setenv("PATH", "")
        monkeypatch.setattr(uninstall.bundle, "installed_by_homebrew", lambda home=None: True)
        monkeypatch.setattr(uninstall.bundle, "cask_prefix", lambda: prefix)
        assert uninstall.homebrew_brew() == brew and brew.is_absolute()
        monkeypatch.setattr(uninstall.bundle, "installed_by_homebrew", lambda home=None: False)
        assert uninstall.homebrew_brew() is None

    @pytest.mark.skipif(sys.platform == "win32", reason="no execute bit")
    def test_a_brew_that_cannot_run_is_not_used(self, frozen_app, monkeypatch, tmp_path):
        prefix = tmp_path / "homebrew"
        brew = prefix / "bin" / "brew"
        brew.parent.mkdir(parents=True)
        brew.write_text("")
        brew.chmod(0o644)
        monkeypatch.setattr(uninstall.bundle, "installed_by_homebrew", lambda home=None: True)
        monkeypatch.setattr(uninstall.bundle, "cask_prefix", lambda: prefix)
        assert uninstall.homebrew_brew() is None

    def test_every_program_the_child_runs_is_an_absolute_path(self, frozen_app, monkeypatch):
        """The extra's PATH (launchd's, or Finder's bare one) is never asked."""
        assert Path(uninstall.app_command(True)[0]).is_absolute()
        ran = []
        monkeypatch.setattr(uninstall.subprocess, "run", lambda argv, **k: ran.append(argv))
        uninstall._open("-R", str(frozen_app))
        uninstall.show_app_log()
        assert [argv[0] for argv in ran] == ["/usr/bin/open", "/usr/bin/open"]


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
        with pytest.raises(ClaudeSwitchError) as exc:
            uninstall.refuse_live_sessions()
        assert str(exc.value) == "Claude Code is running on session profile 2-b (PID 4242). Quit it, then try again."

    def test_live_sessions_on_several_profiles_are_named_together(self, monkeypatch, tmp_path):
        a, b = type("S", (), {"pid": 7})(), type("S", (), {"pid": 8})()
        self._profiles(monkeypatch, tmp_path, {"1-a": ([a], 0), "2-b": ([b], 0)})
        with pytest.raises(ClaudeSwitchError) as exc:
            uninstall.refuse_live_sessions()
        assert str(exc.value) == (
            "Claude Code is running on session profiles 1-a (PID 7); 2-b (PID 8). Quit them, then try again."
        )

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
        # The worker's pinned Claude Code copies and job locks, and their old place.
        support = home / "Library" / "Application Support" / "com.opensoft.openswap"
        (support / "claude-cli").mkdir(parents=True)
        caches = home / "Library" / "Caches" / "com.opensoft.openswap"
        (caches / "job-locks").mkdir(parents=True)
        prefs = home / "Library" / "Preferences"
        prefs.mkdir(parents=True)
        (prefs / "com.opensoft.openswap.plist").write_text("x")
        (prefs / "com.opensoft.openswap.menubar.plist").write_text("x")
        (prefs / "com.other.app.plist").write_text("x")
        legacy = home / ".claude-swap-backup"
        legacy.mkdir()
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: backup)
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: legacy)
        found = uninstall.data_paths(home)
        # The job state first, while the roots' lifecycle.lock files remain.
        assert found == [
            support,
            caches,
            backup,
            prefs / "com.opensoft.openswap.plist",
            prefs / "com.opensoft.openswap.menubar.plist",
            logs / "com.opensoft.openswap.worker.log",
            logs / "com.opensoft.openswap.menubar.err",
            legacy,
        ]
        # Homebrew's zap trashes the rest; neither the job state (an older
        # worker's in Caches included) nor the pre-XDG folder is on its list.
        assert uninstall.data_paths(home, homebrew_zap=True) == [support, caches, legacy]
        # A task that may still run keeps its handles and locks.
        kept = uninstall.data_paths(home, keep_job_state=True)
        assert support not in kept and caches not in kept
        assert uninstall.data_paths(home, homebrew_zap=True, keep_job_state=True) == [legacy]
        assert uninstall.delete_data(home) == found
        assert not backup.exists() and not legacy.exists() and not support.exists()
        assert (logs / "com.other.app.log").exists() and (prefs / "com.other.app.plist").exists()

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
        state = {"brew": None, "failed": [], "command": None, "live": None, "purge": None, "collision": False,
                 "busy": None, "held": True, "holding": False}

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
            return uninstall.Removal(removed=["menu bar login item"], failed=list(state["failed"]),
                                     warnings=list(state.get("warnings", [])))

        def fake_logins():
            seen["logins"] += 1
            return 3

        def fake_data(home=None, *, homebrew_zap=False, keep_job_state=False):
            seen["data"].append(homebrew_zap)
            seen["kept"] = keep_job_state
            seen["deleted_while_holding"] = state["holding"]
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
        def fake_busy(removal, uid=None):
            seen["checked_while_holding"] = state["holding"]
            return state["busy"]

        @contextlib.contextmanager
        def fake_hold():
            state["holding"] = True
            try:
                yield state["held"]
            finally:
                state["holding"] = False

        monkeypatch.setattr(uninstall, "job_state_busy", fake_busy)
        monkeypatch.setattr(uninstall, "worker_starts_held", fake_hold)
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

    def test_hook_zap_keeps_the_job_state_of_a_task_that_may_run(self, harness, monkeypatch, capsys):
        """The forced boot-out proves nothing about the task, so its handles stay."""
        seen, state = harness
        state["command"] = ("uninstall", ["--zap", "--cask", "openswap"])
        state["busy"] = "the Remote tasks worker was removed before its task was confirmed stopped"
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert seen["kept"] is True
        assert "Delete it once no Remote task runs." in capsys.readouterr().out

    @pytest.mark.parametrize("held", [True, False])
    def test_hook_checks_and_deletes_job_state_under_the_lifecycle_locks(self, harness, monkeypatch, capsys, held):
        seen, state = harness
        state["command"] = ("uninstall", ["--zap", "--cask", "openswap"])
        state["held"] = held
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert seen["deleted_while_holding"] and seen["kept"] is not held
        assert seen.get("checked_while_holding", False) is held

    def test_hook_names_the_server_when_its_cleanup_fails(self, harness, monkeypatch, capsys):
        """Homebrew's zap still trashes the settings, so the notice must not depend on the cleanup."""
        _, state = harness
        state["command"] = ("uninstall", ["--zap", "--cask", "openswap"])
        monkeypatch.setattr(uninstall, "paired_servers", lambda: ["https://opentag.me"])

        def failing_data(home=None, *, homebrew_zap=False, keep_job_state=False):
            raise ClaudeSwitchError("Could not delete /x: busy")

        monkeypatch.setattr(uninstall, "delete_data", failing_data)
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        out = capsys.readouterr()
        assert "Could not delete /x: busy" in out.err
        assert "OpenTag still lists this Mac for Remote tasks." in out.out

    def test_hook_zap_names_the_server_that_still_lists_this_mac(self, harness, monkeypatch, capsys):
        _, state = harness
        state["command"] = ("uninstall", ["--zap", "--cask", "openswap"])
        monkeypatch.setattr(uninstall, "paired_servers", lambda: ["https://opentag.me"])
        assert self._run(monkeypatch, "--homebrew-hook") == 0
        assert capsys.readouterr().out.endswith(
            "OpenTag still lists this Mac for Remote tasks. To remove it, open https://opentag.me "
            "and remove it there.\n"
        )

    def test_hook_zap_with_a_live_session_warns_and_goes_on(self, harness, monkeypatch, capsys):
        seen, state = harness
        state["command"] = ("uninstall", ["--zap", "--cask", "openswap"])
        state["live"] = "Claude Code is running on session profile 1-a (PID 7). Quit it, then try again."
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
        state["live"] = "Claude Code is running on session profile 1-a (PID 7). Quit it, then try again."
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

    @pytest.mark.parametrize("brew", [Path("/opt/homebrew/bin/brew"), None])
    def test_zap_names_the_server_that_still_lists_this_mac(self, harness, monkeypatch, tmp_path, brew, frozen_app):
        """Read before purge deletes the settings that name it; said before brew takes over."""
        seen, state = harness
        state["brew"] = brew
        monkeypatch.setattr(
            uninstall, "paired_servers", lambda: seen["order"].append("server") or ["https://worker.example"]
        )
        lines, say = self._say()
        if brew is None:
            assert uninstall.run(True, say) == 0
        else:
            with pytest.raises(SystemExit):
                uninstall.run(True, say)
        assert seen["order"].index("server") < seen["order"].index("engine")
        notice = ("https://worker.example still lists this Mac for Remote tasks. To remove it, open "
                  "https://worker.example and remove it there.", False)
        deleted = lines.index((f"Deleted {tmp_path / 'data'}", False))  # harness's delete_data
        assert lines.index(notice) == deleted + 1
        if brew is not None:
            assert lines[deleted + 2][0].startswith("Removing the app: brew uninstall")

    @pytest.mark.parametrize("settings_gone", [True, False])
    def test_a_failed_zap_names_the_server_once_purge_deleted_its_settings(self, harness, monkeypatch, settings_gone):
        """A retry reads no pairing once purge deleted the settings; until then it names it itself."""
        servers = iter([["https://opentag.me"], [] if settings_gone else ["https://opentag.me"]])
        monkeypatch.setattr(uninstall, "paired_servers", lambda: next(servers))

        def failing_data(home=None, *, homebrew_zap=False, keep_job_state=False):
            raise ClaudeSwitchError("Could not delete /x: busy")

        monkeypatch.setattr(uninstall, "delete_data", failing_data)
        lines, say = self._say()
        assert uninstall.run(True, say) == 1
        assert any("OpenTag still lists this Mac" in line for line, _ in lines) == settings_gone
        assert lines[-1][0].startswith("OpenSwap is still installed.")

    @pytest.mark.parametrize("brew", [Path("/opt/homebrew/bin/brew"), None])
    def test_zap_keeps_the_job_state_of_a_task_that_may_run(self, harness, brew, frozen_app):
        seen, state = harness
        state["brew"] = brew
        state["busy"] = "a Remote task is still running"
        lines, say = self._say()
        if brew is None:
            assert uninstall.run(True, say) == 0
        else:
            with pytest.raises(SystemExit):
                uninstall.run(True, say)
        assert seen["kept"] is True
        assert (f"Kept {uninstall.job_state_paths()[0]}: a Remote task is still running. "
                "Delete it once no Remote task runs.", False) in lines

    def test_zap_deletes_idle_job_state(self, harness, frozen_app):
        seen, _ = harness
        lines, say = self._say()
        assert uninstall.run(True, say) == 0
        assert seen["kept"] is False and not any(line.startswith("Kept ") for line, _ in lines)
        # No worker can start between the check and the deletion.
        assert seen["checked_while_holding"] and seen["deleted_while_holding"]

    def test_zap_keeps_job_state_when_it_cannot_hold_worker_starts(self, harness, frozen_app):
        seen, state = harness
        state["held"] = False
        lines, say = self._say()
        assert uninstall.run(True, say) == 0
        assert seen["kept"] is True and "checked_while_holding" not in seen
        assert any(line.startswith("Kept ") and uninstall.WORKER_LIFECYCLE_BUSY in line for line, _ in lines)

    def test_zap_names_each_server_it_forgot(self, harness, monkeypatch, frozen_app):
        monkeypatch.setattr(uninstall, "paired_servers", lambda: ["https://opentag.me", "https://worker.example"])
        lines, say = self._say()
        assert uninstall.run(True, say) == 0
        notices = [line for line, _ in lines if "still lists this Mac" in line]
        assert [notice.split(" ")[0] for notice in notices] == ["OpenTag", "https://worker.example"]

    def test_no_server_notice_without_zap(self, harness, monkeypatch, frozen_app):
        monkeypatch.setattr(uninstall, "paired_servers", lambda: ["https://opentag.me"])
        lines, say = self._say()
        assert uninstall.run(False, say) == 0
        assert not any("still lists" in line for line, _ in lines)

    @pytest.mark.parametrize("zap", [False, True])
    def test_run_without_brew_names_what_is_left(self, harness, zap, frozen_app):
        seen, _ = harness
        lines, say = self._say()
        assert uninstall.run(zap, say) == 0
        assert seen["exec"] is None and seen["data"] == ([False] if zap else [])
        assert lines[-1] == (f"To finish, move {frozen_app} to the Trash.", False)


    def test_warnings_are_said_but_never_stop_the_uninstall(self, harness):
        seen, state = harness
        remedy = "Could not restore Claude links. Open Claude once to restore its link handling."
        state["warnings"] = [remedy]
        lines, say = self._say()
        assert uninstall.run(False, say) == 0
        assert (f"Warning: {remedy}", True) in lines
        assert not any(line.startswith("OpenSwap is still installed") for line, _ in lines)

    def test_kept_profiles_are_named_with_how_to_get_their_shortcuts_back(self, harness, monkeypatch, tmp_path):
        root = tmp_path / "OpenSwap Claude Desktop"
        root.mkdir()
        monkeypatch.setattr("openswap.claude_desktop.default_root", lambda: root)
        lines, say = self._say()
        assert uninstall.run(False, say) == 0
        assert (f"Claude Desktop profiles, including sign-ins and local work, are kept at {root}. "
                "Their shortcuts are removed; recreate them from the profile menu after reinstalling.", False) in lines
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

    def test_run_from_the_app_says_how_to_try_again(self, harness):
        _, state = harness
        state["failed"] = ["Remote tasks worker: it did not stop"]
        lines, say = self._say()
        assert uninstall.run(True, say, from_app=True) == 1
        assert lines[-1] == (
            "OpenSwap is still installed. Fix the error above, then open OpenSwap and choose "
            "Uninstall OpenSwap… again.",
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

    def test_run_from_the_app_keeps_a_cask_app_brew_cannot_remove(
        self, harness, monkeypatch, frozen_app, tmp_path
    ):
        """Trashing it would leave the cask registered with no app and its
        uninstall hook never run; nothing is removed instead."""
        seen, _ = harness  # homebrew_brew() is None: brew is missing or cannot run
        monkeypatch.setattr(uninstall.bundle, "installed_by_homebrew", lambda home=None: True)
        monkeypatch.setattr(uninstall.bundle, "cask_prefix", lambda: tmp_path / "homebrew")
        monkeypatch.setattr("openswap.widget_install._move_to_trash", lambda path: pytest.fail("trashed"))
        lines, say = self._say()
        assert uninstall.run(True, say, from_app=True) == 1
        assert seen["remove"] == [] and seen["purge"] == [] and seen["exec"] is None
        brew = tmp_path / "homebrew" / "bin" / "brew"
        assert lines == [
            (f"Error: Homebrew installed OpenSwap, but {brew} cannot run, so nothing was removed.", True),
            ("Fix Homebrew, then open OpenSwap and choose Uninstall OpenSwap… again, "
             "or run: brew uninstall --cask openswap", True),
        ]

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
        state["live"] = "Claude Code is running on session profile 1-a (PID 7). Quit it, then try again."
        order = []
        monkeypatch.setattr(uninstall, "wait_for_app_to_quit", lambda: order.append("wait") or True)
        monkeypatch.setattr(uninstall, "show_app_log", lambda: order.append("log"))
        assert self._run(monkeypatch, "--yes", "--zap", "--from-app") == 1
        assert order == ["wait", "log"] and seen["remove"] == []
        assert "PID 7" in capsys.readouterr().err

    def test_from_app_removes_nothing_while_the_app_still_runs(self, harness, monkeypatch, capsys):
        """A running extra's engines could recreate what a zap deletes."""
        seen, _ = harness
        order = []
        monkeypatch.setattr(uninstall, "wait_for_app_to_quit", lambda: order.append("wait") or False)
        monkeypatch.setattr(uninstall, "show_app_log", lambda: order.append("log"))
        monkeypatch.setattr(uninstall, "refuse_live_sessions", lambda: pytest.fail("went on"))
        assert self._run(monkeypatch, "--yes", "--zap", "--from-app") == 1
        assert order == ["wait", "log"]
        assert seen["remove"] == [] and seen["purge"] == [] and seen["data"] == [] and seen["exec"] is None
        err = capsys.readouterr().err
        assert "did not quit within 30 seconds, so nothing was removed." in err
        assert "Quit OpenSwap, then open it and choose Uninstall OpenSwap… again." in err

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
        # Whether brew can run now does not change what the app is: the child
        # refuses then (test_run_from_the_app_keeps_a_cask_app_brew_cannot_remove).
        monkeypatch.setattr(uninstall.bundle, "installed_by_homebrew", lambda home=None: True)
        monkeypatch.setattr(uninstall, "homebrew_brew", lambda: None)
        dialog = uninstall.app_dialog()
        assert dialog["title"] == "Uninstall OpenSwap?" and dialog["ok"] == "Uninstall"
        assert dialog["checkbox"] == "Also delete saved accounts, settings and logs"
        assert "• OpenSwap.app, through Homebrew" in dialog["message"]
        assert "\n\nClaude Code and Codex stay signed in, each to the account it uses now. Remote tasks' " \
            "results stay in their folders. Your saved accounts stay unless you select the checkbox " \
            "below. Deleting them cannot be undone." in dialog["message"]
        assert "uv tool" not in dialog["message"]

    def test_copy_not_installed_by_homebrew(self, frozen_app, monkeypatch):
        monkeypatch.setattr(uninstall.bundle, "installed_by_homebrew", lambda home=None: False)
        assert "• OpenSwap.app, moved to the Trash" in uninstall.app_dialog()["message"]

    def test_source_install(self):
        message = uninstall.app_dialog()["message"]
        assert ".app" not in message
        assert message.endswith("run “uv tool uninstall openswap” afterwards.")
        assert "pairing" not in message

    def test_profile_shortcuts_are_listed_only_when_an_owned_launcher_is_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr(uninstall, "paired_servers", lambda: [])
        assert "profile shortcuts" not in uninstall.app_dialog()["message"]
        folder = tmp_path / "Applications" / "OpenSwap Profiles"
        folder.mkdir(parents=True)
        launcher(folder, "Fake", "c" * 32, tmp_path, bundle_id="com.example.fake")
        assert "profile shortcuts" not in uninstall.app_dialog()["message"]
        launcher(folder, "Work", "a" * 32, tmp_path / "another store")
        assert "• the Claude profile shortcuts and their Dock tiles" in uninstall.app_dialog()["message"]

    def test_two_pairings_are_both_named(self, monkeypatch):
        monkeypatch.setattr(uninstall, "paired_servers", lambda: ["https://opentag.me", "https://worker.example"])
        assert ("forgets this Mac's pairing with OpenTag and https://worker.example. Each still lists the Mac "
                "until you remove it there.") in uninstall.app_dialog()["message"]

    @pytest.mark.parametrize(("url", "name"), [("https://opentag.me", "OpenTag"),
                                               ("https://worker.example", "https://worker.example")])
    def test_a_paired_mac_is_told_the_server_keeps_listing_it(self, monkeypatch, url, name):
        """Nobody reads the log of an uninstall the app starts, so the dialog says it."""
        monkeypatch.setattr(uninstall, "paired_servers", lambda: [url])
        message = uninstall.app_dialog()["message"]
        assert (f"\n\nDeleting saved accounts also forgets this Mac's pairing with {name}. {name} still "
                "lists the Mac until you remove it there.\n\nTo remove the openswap command") in message


class TestJobStateBusy:
    @pytest.fixture
    def labels(self, monkeypatch, tmp_path):
        state = {"labels": set()}
        monkeypatch.setattr(uninstall, "job_state_paths", lambda home=None: [tmp_path / "support", tmp_path / "caches"])
        monkeypatch.setattr(uninstall, "_loaded_labels", lambda uid=None: state["labels"])
        monkeypatch.setattr(uninstall, "_worker_process_may_run", lambda root: False)
        return state

    def test_idle(self, labels):
        labels["labels"] = {"com.opensoft.openswap.menubar", "com.apple.Finder"}
        assert uninstall.job_state_busy(uninstall.Removal()) is None

    def test_a_forced_boot_out_proves_nothing(self, labels):
        assert "confirmed stopped" in uninstall.job_state_busy(uninstall.Removal(job_stop_unconfirmed=True))

    def test_a_loaded_worker_shares_the_folder(self, labels):
        """Another install's worker: this per-user folder is its too."""
        labels["labels"] = {"com.opensoft.openswap.worker"}
        assert uninstall.job_state_busy(uninstall.Removal()) == "a Remote tasks worker is still loaded"

    def test_a_loaded_job_label(self, labels):
        labels["labels"] = {"com.opensoft.openswap.worker.job.abc123"}
        assert uninstall.job_state_busy(uninstall.Removal()) == "a Remote task is still running"

    def test_a_worker_started_by_hand_holds_its_instance_lock(self, labels, monkeypatch, tmp_path):
        """No label and, while idle, no job: only its process-lifetime lock shows it."""
        (tmp_path / "worker").mkdir()
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: tmp_path)
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: tmp_path)
        monkeypatch.setattr(uninstall, "_worker_process_may_run", lambda root: root == tmp_path)
        assert uninstall.job_state_busy(uninstall.Removal()) == "a Remote tasks worker process is still running"

    @pytest.mark.skipif(sys.platform == "win32", reason="the worker's locks are flocks")
    def test_worker_starts_held_takes_each_roots_lifecycle_lock(self, monkeypatch, tmp_path):
        from openswap.locking import FileLock

        (tmp_path / "worker").mkdir(mode=0o700)
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: tmp_path)
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: tmp_path)
        monkeypatch.setattr("openswap.worker.cli._LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.2)
        lifecycle = tmp_path / "worker" / "lifecycle.lock"
        with uninstall.worker_starts_held() as held:
            assert held is True
            assert not FileLock(lifecycle, timeout=0).acquire(timeout=0)  # a start would wait
        with FileLock(lifecycle, timeout=0):  # another command is changing Remote tasks
            with uninstall.worker_starts_held() as held:
                assert held is False

    @pytest.mark.skipif(sys.platform == "win32", reason="the worker's locks are flocks")
    def test_worker_starts_held_locks_a_root_without_worker_state(self, monkeypatch, tmp_path):
        """A `worker enable` meanwhile could create it, so it is locked too."""
        from openswap.locking import FileLock

        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: tmp_path)
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: tmp_path)
        with uninstall.worker_starts_held() as held:
            assert held is True
            assert not FileLock(tmp_path / "worker" / "lifecycle.lock", timeout=0).acquire(timeout=0)

    @pytest.mark.skipif(sys.platform == "win32", reason="the worker's locks are flocks")
    def test_worker_process_may_run_reads_the_instance_lock(self, tmp_path):
        from openswap.locking import FileLock

        (tmp_path / "worker").mkdir(mode=0o700)
        assert uninstall._worker_process_may_run(tmp_path) is False
        with FileLock(tmp_path / "worker" / "instance.lock", timeout=0):
            assert uninstall._worker_process_may_run(tmp_path) is True

    def test_an_earlier_forced_boot_out_still_keeps_it(self, labels, tmp_path):
        """The task may outlive a reinstall; only the user deletes the marker."""
        (tmp_path / "support").mkdir()
        (tmp_path / "support" / "stop-unconfirmed").write_text("x")
        assert "earlier uninstall" in uninstall.job_state_busy(uninstall.Removal())

    def test_launchd_that_cannot_say_keeps_it(self, labels):
        labels["labels"] = None
        assert "could not say" in uninstall.job_state_busy(uninstall.Removal())

    def test_loaded_labels_read_the_gui_domain(self, monkeypatch):
        """Named explicitly: from SSH a plain `launchctl list` reads another domain."""
        out = ("gui/501 = {\n\ttype = gui\n\tsubdomains = {\n\t\tpid/1\n\t}\n\n\tservices = {\n"
               "\t\t   50860    -15 \tcom.opensoft.openswap.menubar\n"
               "\t\t       0      0 \tcom.opensoft.openswap.worker.job.abc\n\t}\n\n\tendpoints = {\n"
               "\t\t     0x0  com.apple.x\n\t}\n}\n")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, out, "")

        monkeypatch.setattr(uninstall.subprocess, "run", fake_run)
        assert uninstall._loaded_labels(501) == {"com.opensoft.openswap.menubar",
                                                 "com.opensoft.openswap.worker.job.abc"}
        assert calls == [["/bin/launchctl", "print", "gui/501"]]

    @pytest.mark.parametrize(("code", "out"), [(113, ""), (0, "gui/501 = {\n\ttype = gui\n}\n"),
                                               (0, "gui/501 = {\n\tservices = {\n\t\t 0 0 \tx\n")])
    def test_a_gui_domain_it_cannot_read_names_no_labels(self, monkeypatch, code, out):
        """No GUI domain, no services block, or a cut-off one: the caller keeps the folder."""
        monkeypatch.setattr(uninstall.subprocess, "run",
                            lambda argv, **k: subprocess.CompletedProcess(argv, code, out, ""))
        assert uninstall._loaded_labels(501) is None


class TestPairedServer:
    def test_reads_the_configured_url(self, monkeypatch, tmp_path):
        from openswap.settings import configure_worker_service

        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: tmp_path)
        assert uninstall.paired_servers() == []
        configure_worker_service(tmp_path, "https://opentag.me")
        assert uninstall.paired_servers() == ["https://opentag.me"]

    def test_reads_a_pre_xdg_folder_not_yet_migrated(self, monkeypatch, tmp_path):
        """The zap reads it before Engine() migrates the folder, so look there too."""
        from openswap.settings import configure_worker_service

        backup, legacy = tmp_path / "OpenSwap", tmp_path / ".claude-swap-backup"
        legacy.mkdir()
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: backup)
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: legacy)
        configure_worker_service(legacy, "https://worker.example")
        assert uninstall.paired_servers() == ["https://worker.example"]
        assert not backup.exists()  # read without creating anything

    def test_colliding_roots_name_each_server_once(self, monkeypatch, tmp_path):
        """A zap deletes both folders, so each pairing it forgets is named."""
        from openswap.settings import configure_worker_service

        backup, legacy = tmp_path / "OpenSwap", tmp_path / ".claude-swap-backup"
        backup.mkdir()
        legacy.mkdir()
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: backup)
        monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: legacy)
        configure_worker_service(backup, "https://opentag.me")
        configure_worker_service(legacy, "https://worker.example")
        assert uninstall.paired_servers() == ["https://opentag.me", "https://worker.example"]
        configure_worker_service(legacy, "https://opentag.me")
        assert uninstall.paired_servers() == ["https://opentag.me"]

    def test_unreadable_settings_name_no_server(self, monkeypatch, tmp_path):
        monkeypatch.setattr("openswap.paths.get_backup_root", lambda: tmp_path)
        monkeypatch.setattr("openswap.settings.load_worker_settings", lambda root: 1 / 0)
        assert uninstall.paired_servers() == []


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
            raise ClaudeSwitchError("Claude Code is running on session profile 1-a (PID 7). Quit it, then try again.")

        monkeypatch.setattr(uninstall, "refuse_live_sessions", refuse)
        app.answer = SimpleNamespace(clicked=1, text="", checked=True)
        app.on_uninstall(None)
        assert app.calls[1:] == [("alert", "Claude Code is running on session profile 1-a (PID 7). Quit it, then try again.")]

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
