"""Profile lifecycle and launch safety without real credentials or applications."""

import json
import os
import plistlib
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import claude_desktop as cd
from openswap import claude_desktop_menu as ui
from tests.test_codex_cli import run_cli


@pytest.fixture
def store(tmp_path):
    return cd.DesktopProfiles(tmp_path / "Desktop Profiles")


def test_empty_list_is_read_only(store):
    assert store.list() == []
    assert not store.root.exists()


def test_two_profiles_have_distinct_storage_and_rename_keeps_paths(store):
    a, b = store.add("Personal"), store.add("Work")
    da, ca = store.paths(a)
    db, cb = store.paths(b)
    assert len({da, ca, db, cb}) == 4
    (da / "user-work").write_text("keep")
    renamed = store.update(a.id, name="Another name")
    assert store.paths(renamed) == (da, ca)
    store.update(a.id, archived=True)
    assert store.list() == [b]
    with pytest.raises(cd.ClaudeDesktopError, match="not found"):
        store.get(a.id)
    store.update(a.id, archived=False)
    assert (da / "user-work").read_text() == "keep"
    assert store.get("ANOTHER NAME").id == a.id


@pytest.mark.parametrize("name", ["", " ", "x\ny", "x\x00y", "x" * 65, "x\u202ey"])
def test_invalid_names_do_not_create_storage(store, name):
    with pytest.raises(cd.ClaudeDesktopError):
        store.add(name)
    assert not store.root.exists()


def test_name_is_not_a_path_or_shell_expression(store, tmp_path):
    p = store.add("../../escaped $(touch nope)")
    assert store.paths(p)[0].parent.parent == store.root
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "nope").exists()


def test_duplicate_names_include_archived_and_unicode_equivalents(store):
    p = store.add("Café")
    store.update(p.id, archived=True)
    with pytest.raises(cd.ClaudeDesktopError, match="already exists"):
        store.add("CAFE\u0301")
    b = store.add("Other")
    with pytest.raises(cd.ClaudeDesktopError, match="already exists"):
        store.update(b.id, name="café")


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_managed_permissions(store):
    p = store.add("Personal")
    for path in (store.root, *store.paths(p), store.paths(p)[0].parent):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700
    for path in (store.registry, store.root / "profiles.lock"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_public_storage_refused(store):
    store.add("Personal")
    store.root.chmod(0o755)
    with pytest.raises(cd.ClaudeDesktopError, match="private"):
        store.add("Work")


@pytest.mark.parametrize("contents", ["{", "[]", '{"version":4,"profiles":[]}',
    '{"version":true,"profiles":[]}', '{"version":1,"profiles":[{}]}'])
def test_damaged_or_newer_registry_is_not_replaced(store, contents):
    store.add("Personal")
    store.registry.write_text(contents)
    with pytest.raises(cd.ClaudeDesktopError, match="metadata is invalid"):
        store.add("Work")
    assert store.registry.read_text() == contents


@pytest.mark.parametrize("change", ["id", "duplicate", "archived", "name"])
def test_invalid_rows_fail_closed(store, change):
    store.add("Personal")
    data = json.loads(store.registry.read_text())
    if change == "duplicate":
        data["profiles"].append(data["profiles"][0])
    else:
        data["profiles"][0][change] = {"id": "../escape", "archived": 1, "name": "\n"}[change]
    store.registry.write_text(json.dumps(data))
    with pytest.raises(cd.ClaudeDesktopError):
        store.list()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need Windows privileges")
@pytest.mark.parametrize("target", ["root", "registry", "lock", "parent", "desktop", "code"])
def test_symlink_refused_without_modifying_target(store, tmp_path, target):
    p = store.add("Personal")
    desktop, code = store.paths(p)
    selected = {"root": store.root, "registry": store.registry, "lock": store.root / "profiles.lock",
                "parent": desktop.parent, "desktop": desktop, "code": code}[target]
    moved = tmp_path / "original"
    selected.rename(moved)
    selected.symlink_to(moved, target_is_directory=moved.is_dir())
    app = Mock()
    app.validate.return_value = {"version": "test"}
    with pytest.raises(cd.ClaudeDesktopError, match="plain"):
        store.open(p.id, app=app)
    app.open.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="hardlinks")
def test_hardlinked_registry_refused(store, tmp_path):
    store.add("Personal")
    os.link(store.registry, tmp_path / "copy")
    with pytest.raises(cd.ClaudeDesktopError, match="plain"):
        store.add("Work")


def test_missing_data_not_recreated(store):
    p = store.add("Personal")
    store.paths(p)[0].rmdir()
    app = Mock()
    app.validate.return_value = {"version": "test"}
    with pytest.raises(cd.ClaudeDesktopError, match="missing"):
        store.open(p.id, app=app)
    app.open.assert_not_called()
    assert not store.paths(p)[0].exists()


def test_atomic_write_failure_keeps_registry_and_data(store, monkeypatch):
    p = store.add("Personal")
    original = store.registry.read_bytes()
    monkeypatch.setattr(cd.os, "replace", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        store.update(p.id, archived=True)
    assert store.registry.read_bytes() == original
    assert store.paths(p)[0].is_dir()
    assert not list(store.root.glob(".profiles-*"))


def test_concurrent_adds_do_not_lose_profiles(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(store.add, ["Personal", "Work"]))
    assert {p.name for p in store.list()} == {"Personal", "Work"}


def test_environment_preserves_managed_policy_and_trust_but_not_login(tmp_path):
    source = {"HOME": "/real/home", "PATH": "/usr/bin", "HTTPS_PROXY": "proxy", "NODE_EXTRA_CA_CERTS": "trust.pem",
              "CLAUDE_CODE_MANAGED_SETTINGS_PATH": "/policy", "CLAUDE_CONFIG_DIR": "/default",
              "CLAUDE_SECURESTORAGE_CONFIG_DIR": "", "CLAUDE_USER_DATA_DIR": "/wrong",
              "ANTHROPIC_API_KEY": "secret", "ANTHROPIC_AUTH_TOKEN": "secret", "CLAUDE_CODE_OAUTH_TOKEN": "secret",
              "ELECTRON_RUN_AS_NODE": "1", "NODE_OPTIONS": "--require injected", "CLAUDE_CDP_AUTH": "debug",
              "AWS_ACCESS_KEY_ID": "secret", "CLAUDE_CODE_USE_BEDROCK": "1"}
    env = cd.launch_environment(tmp_path, source)
    assert env["HOME"] == "/real/home"
    assert env["CLAUDE_CONFIG_DIR"] == env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(tmp_path)
    assert env["CLAUDE_CODE_MANAGED_SETTINGS_PATH"] == "/policy"
    assert env["HTTPS_PROXY"] == "proxy"
    assert env["NODE_EXTRA_CA_CERTS"] == "trust.pem"
    assert "secret" not in env.values()
    assert not {"NODE_OPTIONS", "ELECTRON_RUN_AS_NODE", "CLAUDE_CDP_AUTH", "CLAUDE_USER_DATA_DIR", "CLAUDE_CODE_USE_BEDROCK"} & env.keys()


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "Claude.app"
    (path / "Contents" / "MacOS").mkdir(parents=True)
    (path / "Contents" / "MacOS" / "Claude").touch()
    (path / "Contents" / "Resources").mkdir()
    (path / "Contents" / "Resources" / "app.asar").touch()
    (path / "Contents" / "_CodeSignature").mkdir()
    (path / "Contents" / "_CodeSignature" / "CodeResources").touch()
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": cd.BUNDLE_ID, "CFBundleExecutable": "Claude", "CFBundleShortVersionString": "3.6556.2",
    }))
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=f"Identifier={cd.BUNDLE_ID}\nTeamIdentifier={cd.TEAM_ID}\n"))
    return cd.ClaudeDesktopApp(path, platform="darwin", run=run, popen=Mock())


def test_check_signature_without_launching(app):
    result = app.validate()
    assert result["publisher_verified"] and not result["identity_verified"]
    assert "--deep" in app.run.call_args_list[0].args[0]
    app.popen.assert_not_called()


@pytest.mark.parametrize("failure", ["wrong-team", "bad-signature", "bad-plist", "truncated-plist", "missing", "platform", "timeout"])
def test_invalid_app_never_launches_or_creates_profile(store, app, failure):
    if failure == "wrong-team":
        app.run.return_value.stderr = f"Identifier={cd.BUNDLE_ID}\nTeamIdentifier=other"
    elif failure == "bad-signature":
        app.run.return_value.returncode = 1
    elif failure == "bad-plist":
        (app.path / "Contents" / "Info.plist").write_bytes(b"invalid")
    elif failure == "truncated-plist":
        (app.path / "Contents" / "Info.plist").write_bytes(b'<?xml version="1.0"?><plist><dict>')
    elif failure == "missing":
        app.binary.unlink()
    elif failure == "platform":
        app.platform = "linux"
    else:
        app.run.side_effect = subprocess.TimeoutExpired("codesign", 30)
    with pytest.raises(cd.ClaudeDesktopError):
        store.open("Personal", app=app)
    app.popen.assert_not_called()
    assert not store.root.exists()


def test_launch_is_direct_isolated_and_not_a_verified_identity(store, app):
    p = store.add("Personal")
    proc = Mock(pid=42)
    proc.wait.side_effect = [subprocess.TimeoutExpired("Claude", 0.75), 0]
    app.popen.return_value = proc
    result = store.open(p.id, app=app)
    assert result["state"] == "launched"
    assert result["identity_verified"] is False
    desktop, code = store.paths(p)
    args, kwargs = app.popen.call_args
    assert args[0] == [str(app.binary), f"--user-data-dir={desktop}"]
    assert kwargs["env"]["CLAUDE_CONFIG_DIR"] == str(code)
    assert kwargs["env"]["CLAUDE_SECURESTORAGE_CONFIG_DIR"] == str(code)
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_already_running_profile_is_reused_not_restarted(store, app):
    p = store.add("Personal")
    desktop, _ = store.paths(p)
    app.run.return_value.stdout = f"42 {app.binary} --user-data-dir={desktop}\n"
    assert store.open(p.id, app=app)["state"] == "already_running"
    app.popen.assert_not_called()


@pytest.mark.parametrize("failure", ["process-check", "process-timeout", "ambiguous", "exit-zero", "exit-error", "spawn"])
def test_launch_failures_keep_profile_and_never_claim_success(store, app, failure):
    p = store.add("Personal")
    desktop, code = store.paths(p)
    app.validate = Mock(return_value={"version": "test"})
    app._validation_stamp = app._stamp()
    if failure == "process-check":
        app.run.return_value.returncode = 1
    elif failure == "process-timeout":
        app.run.side_effect = subprocess.TimeoutExpired("ps", 5)
    elif failure == "ambiguous":
        app.run.return_value.stdout = f"42 {app.binary} --user-data-dir={desktop} --extra\n"
    elif failure == "spawn":
        app.popen.side_effect = OSError("spawn failed")
    else:
        app.popen.return_value.wait.return_value = 0 if failure == "exit-zero" else 1
    with pytest.raises(cd.ClaudeDesktopError):
        store.open(p.id, app=app)
    assert store.get(p.id) == p
    assert desktop.is_dir() and code.is_dir()
    if failure.startswith("process") or failure == "ambiguous":
        app.popen.assert_not_called()


def test_archived_profile_cannot_be_launched(store, app):
    p = store.add("Personal")
    store.update(p.id, archived=True)
    with pytest.raises(cd.ClaudeDesktopError, match="not found"):
        store.open(p.id, app=app)
    app.popen.assert_not_called()


def test_update_between_validation_and_launch_refused(store, app):
    p = store.add("Personal")
    validate = app.validate

    def updated():
        info = validate()
        (app.path / "Contents" / "Resources" / "app.asar").write_bytes(b"updated")
        return info

    app.validate = updated
    with pytest.raises(cd.ClaudeDesktopError, match="changed"):
        store.open(p.id, app=app)
    app.popen.assert_not_called()


def test_cli_roundtrip_does_not_construct_account_engine(store, monkeypatch, capsys):
    monkeypatch.setattr(cd, "default_root", lambda: store.root)
    monkeypatch.setattr("openswap.cli.ClaudeAccountSwitcher", Mock(side_effect=AssertionError("engine touched")))
    monkeypatch.setattr("openswap.cli._migrate_legacy_cswap_state", Mock(side_effect=AssertionError("migration touched")))
    assert run_cli(["claude-desktop", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "profiles": []}
    assert run_cli(["claude-desktop", "add", "Personal", "--json"]) == 0
    p = json.loads(capsys.readouterr().out)["profile"]
    for command in (["rename", p["id"], "Work"], ["archive", p["id"]], ["restore", p["id"]]):
        assert run_cli(["claude-desktop", *command, "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"]
    assert store.get("Work").id == p["id"]
    assert run_cli(["claude-desktop", "archive", "absent", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_zap_does_not_include_desktop_documents(temp_home, monkeypatch):
    from openswap.uninstall import app_dialog, data_paths
    root = cd.default_root()
    root.mkdir(parents=True)
    for path in data_paths(home=temp_home):
        assert not root.is_relative_to(path)
    monkeypatch.setattr("openswap.uninstall.paired_servers", lambda: [])
    assert "Claude Desktop profiles, including their sign-ins and local work, stay" in app_dialog()["message"]


class Item:
    def __init__(self, title, callback=None):
        self.title, self.callback, self.children = title, callback, []

    def add(self, item):
        self.children.append(item)


@pytest.fixture
def menu(store):
    return ui.ClaudeDesktopMenu(store=store, prompt=Mock(), show_error=Mock(), changed=Mock(), notify=Mock(), confirm=Mock(return_value=1))


def test_menu_profile_actions_resolve_stable_ids(menu, store, monkeypatch):
    p = store.add("Personal")
    row = next(i for i in menu.menu(SimpleNamespace(MenuItem=Item)).children if i.title == "Profile: Personal")
    monkeypatch.setattr(menu, "open", Mock())
    store.update(p.id, name="Renamed")
    row.children[0].callback(None)
    menu.open.assert_called_once_with(p.id)
    row.children[2].callback(None)
    assert store.list() == []
    archived = menu.menu(SimpleNamespace(MenuItem=Item)).children[-1]
    archived.children[0].children[0].callback(None)
    assert store.get(p.id).name == "Renamed"


def test_cancel_add_does_not_create_profile(menu, store):
    menu.prompt.return_value = SimpleNamespace(clicked=0, text="Personal")
    menu.add()
    assert store.list() == []


def test_background_open_error_only_reaches_ui_during_poll(menu, store, monkeypatch):
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    monkeypatch.setattr(store, "open", Mock(side_effect=cd.ClaudeDesktopError("Could not open")))
    menu.open("unknown")
    assert menu.busy
    menu.show_error.assert_not_called()
    menu.poll()
    assert not menu.busy
    menu.show_error.assert_called_once_with("Could not open")
    menu.notify.assert_not_called()


def test_double_click_does_not_open_twice_and_focuses_exact_pid(menu, store, monkeypatch):
    thread = Mock()
    monkeypatch.setattr(ui.threading, "Thread", thread)
    focus = Mock()
    monkeypatch.setattr(ui, "focus_profile", focus)
    menu.open("a")
    menu.open("b")
    thread.assert_called_once()
    menu._results.put(({"pid": 123, "profile": {"name": "Personal"}}, None))
    menu.poll()
    focus.assert_called_once_with(123)
    assert not menu.busy
    assert "Check the account" in menu.notify.call_args.args[1]


def test_notification_failure_does_not_break_completed_operation(menu, monkeypatch):
    monkeypatch.setattr(ui, "focus_profile", Mock())
    menu.busy = True
    menu.notify.side_effect = RuntimeError("notifications denied")
    menu._results.put(({"pid": 123, "profile": {"name": "Personal"}}, None))
    menu.poll()
    assert not menu.busy
    menu.changed.assert_called_once()
    menu.show_error.assert_not_called()


def test_profile_menu_holds_popover_until_closed(monkeypatch):
    from openswap import menubar
    from tests.menubar_harness import extract_class

    panel_path = Path(menubar.__file__).with_name("menubar_panel.py")
    panel = extract_class(panel_path, "MenuBarPanel", {"_claude_desktop_profiles"}, {"SETTINGS_PAGE": "settings"})()
    panel._settings = lambda: SimpleNamespace(desktop_features_enabled=True)
    panel._page = "settings"
    panel.is_shown = lambda: True
    panel.reload = Mock()
    panel._hold_overflow = Mock()
    panel._on_claude_desktop = Mock(side_effect=RuntimeError("menu failed"))
    with pytest.raises(RuntimeError):
        panel._claude_desktop_profiles("button")
    assert [c.args for c in panel._hold_overflow.call_args_list] == [(True,), (False,)]
    panel._on_claude_desktop.assert_called_once_with("button")
    panel.reload.assert_called_once()


def test_running_status_checks_all_profiles_once_without_launching(store, app):
    running, stopped, archived, conflict = [store.add(n) for n in ("Running", "Stopped", "Archived", "Conflict")]
    store.update(archived.id, archived=True)
    def command(p):
        return f"{app.binary} --user-data-dir={store.paths(p)[0]}"
    app.run.return_value.stdout = "\n".join((f"11 {command(running)}", f"12 {command(archived)}",
                                             f"13 {command(conflict)}", f"14 {command(conflict)}"))
    assert app.profile_states(store) == {running.id: "Running", stopped.id: "Not running",
                                        archived.id: "Running", conflict.id: "Could not confirm"}
    app.run.assert_called_once()
    app.popen.assert_not_called()


def test_empty_status_check_does_not_create_storage_or_scan_processes(store, app):
    assert app.profile_states(store) == {}
    app.run.assert_not_called()
    assert not store.root.exists()


@pytest.mark.parametrize("failure", [OSError("denied"), subprocess.TimeoutExpired("ps", 5), None])
def test_failed_status_check_is_unknown_not_stopped(store, app, failure):
    profile = store.add("Personal")
    app.run.side_effect = failure
    app.run.return_value.returncode = 1
    assert app.profile_states(store) == {profile.id: "Could not confirm"}
    app.popen.assert_not_called()


def test_app_check_is_read_only_and_only_updates_ui_when_polled(menu, store, app, monkeypatch):
    profile = store.add("Personal")
    menu._app_factory = lambda: app
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    menu.refresh_status()
    calls = app.run.call_count
    menu.refresh_status()  # a second click cannot enqueue another check
    assert app.run.call_count == calls
    assert menu.settings_state()["checking"]
    assert menu.settings_state()["app_message"] == "Checking Claude Desktop…"
    menu.poll()
    assert not menu.checking
    assert "Publisher verified" in menu.settings_state()["app_message"]
    assert menu._profile_states[profile.id] == "Not running"
    row = next(i for i in menu.menu(SimpleNamespace(MenuItem=Item)).children if i.title == "Profile: Personal")
    assert any("Not running at" in child.title for child in row.children)
    assert any(child.title == "Sign-in: Check inside Claude" for child in row.children)
    app.popen.assert_not_called()
    menu.show_error.assert_not_called()


def test_failed_app_check_keeps_management_available_and_supports_retry(menu, store, app, monkeypatch):
    store.add("Personal")
    menu._app_factory = lambda: app
    original_validate = app.validate
    app.validate = Mock(side_effect=cd.ClaudeDesktopError("Reinstall the official app."))
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    menu.refresh_status()
    menu.poll()
    assert menu.settings_state()["app_message"] == "Reinstall the official app."
    assert not menu.busy and not menu.checking
    assert menu.settings_state()["profiles"] == 1
    app.validate = original_validate
    menu.refresh_status()
    menu.poll()
    assert "Publisher verified" in menu.settings_state()["app_message"]
    app.popen.assert_not_called()


def test_check_thread_failure_does_not_leave_check_button_disabled(menu, monkeypatch):
    monkeypatch.setattr(ui.threading, "Thread", Mock(side_effect=RuntimeError("unavailable")))
    menu.refresh_status()
    menu.poll()
    assert not menu.checking
    assert menu.settings_state()["app_message"] == "Could not check Claude Desktop. Try again."


def test_archived_count_and_restore_do_not_launch_claude(menu, store, monkeypatch):
    p = store.add("Personal")
    opened = Mock()
    monkeypatch.setattr(store, "open", opened)
    menu.edit(p, archived=True)
    state = menu.settings_state()
    assert state["profiles"] == 0 and state["archived"] == 1
    assert "open windows kept" in menu.notify.call_args.args[1]
    menu.edit(p, archived=False)
    state = menu.settings_state()
    assert state["profiles"] == 1 and state["archived"] == 0
    assert "restoring does not launch" in menu.notify.call_args.args[1].lower()
    opened.assert_not_called()


def test_bad_profile_storage_does_not_look_like_empty_roster(menu, store):
    store.add("Personal")
    store.registry.write_text("invalid")
    state = menu.settings_state()
    assert "storage_error" in state
    assert "profiles" not in state


def test_profiles_shortcut_opens_desktop_settings_and_checks_app():
    from openswap import menubar
    from tests.menubar_harness import extract_class
    cls = extract_class(Path(menubar.__file__).with_name("menubar_panel.py"), "MenuBarPanel",
                        {"_show_claude_desktop_settings", "_select_settings_section", "_check_desktop_settings_app"}, dict(vars(menubar)))
    panel = cls()
    panel._settings = lambda: menubar.MenuBarSettings(desktop_features_enabled=True)
    panel._page = "main"
    panel._on_claude_desktop_check = Mock()
    panel.is_shown = lambda: True
    panel.reload = Mock()
    panel._show_claude_desktop_settings()
    assert panel._page == "settings" and panel._settings_section == "desktop"
    panel._on_claude_desktop_check.assert_called_once()
    panel.reload.assert_called_once()


def test_desktop_updates_preserve_open_menu_anchor_and_do_not_reload_account_cards():
    from openswap import menubar
    from tests.menubar_harness import extract_class
    cls = extract_class(menubar.__file__, "MenuBarApp", {"_claude_desktop_changed"}, dict(vars(menubar)))
    app = cls()
    panel = SimpleNamespace(_menu_open=True, _page="settings", _settings_section="desktop", is_shown=lambda: True, reload=Mock())
    app._panel = panel
    app.rebuild_menu = Mock()
    app._claude_desktop_changed()
    app.rebuild_menu.assert_not_called()
    panel.reload.assert_not_called()
    assert app._claude_desktop_update_pending
    panel._menu_open = False
    app._claude_desktop_changed()
    assert not app._claude_desktop_update_pending
    app.rebuild_menu.assert_called_once()
    panel.reload.assert_called_once()
    panel.reload.reset_mock()
    panel._page = "main"
    app._claude_desktop_changed()
    panel.reload.assert_not_called()
    panel._selected_provider = "claude_desktop"
    app._claude_desktop_changed()
    panel.reload.assert_called_once()


def test_hidden_desktop_blocks_stale_actions_and_checks(menu, store, monkeypatch):
    profile = store.add('Personal')
    original = store.registry.read_bytes()
    menu.enabled = lambda: False
    thread = Mock()
    monkeypatch.setattr(ui.threading, 'Thread', thread)
    menu.add()
    menu.rename(profile)
    menu.edit(profile, archived=True)
    menu.open(profile.id)
    menu.refresh_status()
    menu.prompt.assert_not_called()
    thread.assert_not_called()
    assert store.registry.read_bytes() == original


def test_disabling_discards_inflight_check_and_cannot_overwrite_new_check(menu, monkeypatch):
    thread = Mock()
    monkeypatch.setattr(ui.threading, 'Thread', thread)
    menu.refresh_status()
    old_generation = menu._check_generation
    menu.suspend_checks()
    menu.refresh_status()
    menu._checks.put((old_generation, 'Old result', {}, 'old'))
    menu.poll()
    assert menu.checking
    assert menu.settings_state()['app_message'] == 'Checking Claude Desktop…'
    menu._checks.put((menu._check_generation, 'Current result', {}, 'now'))
    menu.poll()
    assert not menu.checking
    assert menu.settings_state()['app_message'] == 'Current result'
