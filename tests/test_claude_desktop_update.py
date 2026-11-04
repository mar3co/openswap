"""Shared-install update coordination; never launch or quit the real Claude app."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import claude_desktop as cd
from openswap import claude_desktop_update as update
from openswap.claude_desktop_menu import ClaudeDesktopMenu
from openswap.menubar_display import claude_desktop_profile_rows, claude_desktop_settings_rows


class Native:
    def __init__(self, pid):
        self.pid, self.closed = pid, False

    def processIdentifier(self):
        return self.pid

    def isTerminated(self):
        return self.closed


class Backend:
    def __init__(self, profiles):
        self.targets = {p.id: Native(i + 10) for i, p in enumerate(profiles)}
        self.now = 0
        self.current, self.target = "4.4212.0", "4.16336.0"
        self.allow_install = True
        self.installer = True
        self.refuse_quit = False
        self.hold_quit = False
        self.helpers = True
        self.quits, self.restored, self.attached = [], [], []
        self.attach_error = None

    def installed(self):
        return self.current, (self.current,)

    def staged(self):
        return self.target

    def installer_running(self):
        return self.installer

    def snapshot(self, store):
        return {p: n for p, n in self.targets.items() if not n.closed}

    def quit(self, native):
        # The gate and recovery set must already be durable before any quit.
        assert update.UpdateJournal().read()["phase"] == "waiting"
        self.quits.append(native.pid)
        if self.refuse_quit:
            return False
        if not self.hold_quit:
            native.closed = True
        return True

    def exited(self, targets):
        return all(n.closed for n in targets.values())

    def helpers_exited(self, store, identifiers):
        return self.helpers

    def restore(self, store, identifier):
        assert update.UpdateJournal().read()["phase"] == "restoring"
        assert update.version(self.current) >= update.version(self.target)
        self.restored.append(identifier)
        self.targets[identifier] = Native(100 + len(self.restored))

    def attach(self, store, identifier):
        if self.attach_error:
            raise self.attach_error
        self.attached.append(identifier)

    def sleep(self, seconds):
        self.now += seconds
        if self.allow_install and self.exited(self.targets):
            self.current = self.target


@pytest.fixture
def setup(tmp_path):
    default = tmp_path / "Claude"
    default.mkdir()
    store = cd.DesktopProfiles(tmp_path / "profiles", default_desktop=default)
    profiles = [store.ensure_default(), store.add("Work")]
    backend = Backend(profiles)
    coordinator = update.DesktopUpdate(store, backend=backend, clock=lambda: backend.now, sleep=backend.sleep)
    return SimpleNamespace(store=store, profiles=profiles, backend=backend, updater=coordinator)


def test_update_restores_exact_open_profiles_then_releases_launch_gate(setup):
    s = setup
    stopped = s.store.add("Closed")
    registry = s.store.registry.read_bytes()
    progress = []
    result = s.updater.execute(s.updater.plan(), progress=progress.append)
    assert result == {"state": "update_done", "version": "4.16336.0"}
    assert s.backend.quits == [10, 11]
    assert s.backend.restored == s.backend.attached == [p.id for p in s.profiles]
    assert stopped.id not in s.backend.restored
    assert s.store.registry.read_bytes() == registry
    assert not s.updater.pending()
    assert len(progress) == 3


@pytest.mark.parametrize("reason", ["refused", "held", "stalled", "helpers"])
def test_failed_update_keeps_gate_and_saved_profiles(setup, reason):
    s = setup
    if reason == "refused": s.backend.refuse_quit = True
    if reason == "held": s.backend.hold_quit = True
    if reason == "stalled": s.backend.allow_install = False
    if reason == "helpers": s.backend.helpers = False
    with pytest.raises(cd.ClaudeDesktopError):
        s.updater.execute(s.updater.plan())
    assert s.updater.pending()
    assert s.updater.journal.read()["profiles"] == [p.id for p in s.profiles]
    assert s.backend.restored == []
    for store in (s.store, cd.DesktopProfiles(s.store.root.parent / "another-store")):
        app = Mock()
        with pytest.raises(cd.ClaudeDesktopError, match="Resume update"):
            store.open("anything", app=app)
        app.validate.assert_not_called()
        app.open.assert_not_called()


def test_resume_after_restart_restores_only_after_verified_install(setup):
    s = setup
    s.backend.allow_install = False
    with pytest.raises(cd.ClaudeDesktopError, match="not finished"):
        s.updater.execute(s.updater.plan())
    s.backend.current = s.backend.target
    recovered = update.DesktopUpdate(s.store, backend=s.backend, clock=lambda: s.backend.now, sleep=s.backend.sleep)
    plan = recovered.plan()
    assert plan.installed and plan.names == [p.name for p in s.profiles]
    recovered.execute(plan)
    assert s.backend.quits == [10, 11]  # recovery does not quit new sessions
    assert s.backend.restored == [p.id for p in s.profiles]
    assert not recovered.pending()


def test_resume_waiting_install_quits_only_current_confirmed_processes(setup):
    s = setup
    s.backend.refuse_quit = True
    with pytest.raises(cd.ClaudeDesktopError):
        s.updater.execute(s.updater.plan())
    s.backend.refuse_quit = False
    s.backend.targets[s.profiles[1].id].closed = True
    s.updater.execute(s.updater.plan())
    assert s.backend.quits == [10, 10]
    assert s.backend.restored == [p.id for p in s.profiles]


@pytest.mark.parametrize("change", ["new-process", "restarted", "exited", "version", "download", "installer", "disabled"])
def test_changes_during_confirmation_do_not_quit_or_record_an_update(setup, change):
    s = setup
    plan = s.updater.plan()
    if change == "new-process": s.backend.targets[s.store.add("Other").id] = Native(99)
    if change == "restarted": s.backend.targets[s.profiles[0].id] = Native(99)
    if change == "exited": s.backend.targets[s.profiles[0].id].closed = True
    if change == "version": s.backend.current = "4.9999.0"
    if change == "download": s.backend.target = "4.20000.0"
    if change == "installer": s.backend.installer = False
    with pytest.raises(cd.ClaudeDesktopError):
        s.updater.execute(plan, enabled=lambda: change != "disabled")
    assert not s.updater.pending()
    assert s.backend.quits == []


def test_dock_failure_keeps_only_unfinished_restorations_for_retry(setup):
    s = setup
    attach = s.backend.attach
    def fail_second(store, identifier):
        if identifier == s.profiles[1].id:
            raise cd.ClaudeDesktopError("Dock unavailable")
        attach(store, identifier)
    s.backend.attach = fail_second
    with pytest.raises(cd.ClaudeDesktopError, match="Dock unavailable"):
        s.updater.execute(s.updater.plan())
    assert s.updater.journal.read()["profiles"] == [s.profiles[1].id]
    s.backend.attach = attach
    s.updater.execute(s.updater.plan())
    assert s.backend.restored.count(s.profiles[0].id) == 1
    assert s.backend.attached == [p.id for p in s.profiles]
    assert not s.updater.pending()


def test_archiving_during_install_does_not_reopen_profile(setup):
    s = setup
    def progress(message):
        if message.startswith("Waiting"):
            s.store.update(s.profiles[1].id, archived=True)
    s.updater.execute(s.updater.plan(), progress=progress)
    assert s.backend.restored == [s.profiles[0].id]


def test_disabling_during_install_keeps_recovery_state(setup):
    s = setup
    allowed = [True]
    def progress(message):
        if message.startswith("Waiting"):
            allowed[0] = False
    with pytest.raises(cd.ClaudeDesktopError, match="paused"):
        s.updater.execute(s.updater.plan(), progress=progress, enabled=lambda: allowed[0])
    assert s.updater.pending() and not s.backend.restored


def test_missing_signature_after_install_never_restores(setup):
    s = setup
    installed = s.backend.installed
    def verify():
        if s.backend.current == s.backend.target:
            raise cd.ClaudeDesktopError("Publisher validation failed")
        return installed()
    s.backend.installed = verify
    with pytest.raises(cd.ClaudeDesktopError):
        s.updater.execute(s.updater.plan())
    assert s.updater.pending() and not s.backend.restored


def test_other_profile_store_cannot_take_over_recovery(setup):
    s = setup
    s.backend.refuse_quit = True
    with pytest.raises(cd.ClaudeDesktopError): s.updater.execute(s.updater.plan())
    other = update.DesktopUpdate(cd.DesktopProfiles(s.store.root.parent / "other"), backend=s.backend)
    with pytest.raises(cd.ClaudeDesktopError, match="store that started"):
        other.plan()


def test_existing_only_attachment_is_allowed_while_launch_is_blocked(setup):
    s = setup
    s.backend.refuse_quit = True
    with pytest.raises(cd.ClaudeDesktopError): s.updater.execute(s.updater.plan())
    app = Mock()
    app.validate.return_value = {"version": "4.4212.0"}
    app.open.return_value = {"state": "not_running", "pid": None}
    result = s.store.open(s.profiles[1].id, app=app, existing_only=True)
    assert result["state"] == "not_running"
    assert app.open.call_args.kwargs == {"existing_only": True}


def test_corrupt_journal_blocks_launch_without_replacing_data(setup):
    s = setup
    with s.updater.journal.lock():
        s.updater.journal.path.write_text("{broken")
        s.updater.journal.path.chmod(0o600)
    with pytest.raises(cd.ClaudeDesktopError, match="unreadable"):
        s.store.open(s.profiles[1].id)
    assert s.updater.journal.path.read_text() == "{broken"


@pytest.fixture
def stage(tmp_path):
    cache = tmp_path / update.SHIPIT_ID
    staged = cache / "update.fixture" / "Claude.app"
    staged.mkdir(parents=True)
    app = SimpleNamespace(path=tmp_path / "Applications" / "Claude.app")
    data = {"targetBundleURL": app.path.as_uri(), "updateBundleURL": staged.as_uri(), "bundleIdentifier": cd.BUNDLE_ID}
    state = cache / "ShipItState.plist"
    state.write_text(json.dumps(data))
    factory = Mock(return_value=SimpleNamespace(validate=Mock(return_value={"version": "4.16336.0"})))
    return SimpleNamespace(cache=cache, app=app, staged=staged, data=data, state=state, factory=factory)


def test_staged_payload_must_pass_publisher_validation(stage):
    s = stage
    assert update.staged_update(s.app, cache=s.cache, app_factory=s.factory) == "4.16336.0"
    s.factory.assert_called_once_with(s.staged)
    s.factory.return_value.validate.side_effect = cd.ClaudeDesktopError("Untrusted publisher")
    with pytest.raises(cd.ClaudeDesktopError, match="Untrusted publisher"):
        update.staged_update(s.app, cache=s.cache, app_factory=s.factory)


@pytest.mark.parametrize("change", ["remote", "outside", "target", "bundle", "invalid", "huge", "list"])
def test_unusable_staging_state_is_not_trusted(stage, change):
    s = stage
    if change == "remote": s.data["updateBundleURL"] = "file://remote/a/Claude.app"
    if change == "outside": s.data["updateBundleURL"] = (s.cache.parent / "update.a/Claude.app").as_uri()
    if change == "target": s.data["targetBundleURL"] = (s.app.path.parent / "Other.app").as_uri()
    if change == "bundle": s.data["bundleIdentifier"] = "other"
    s.state.write_text("{" if change == "invalid" else "x" * 33000 if change == "huge" else "[]" if change == "list" else json.dumps(s.data))
    with pytest.raises(cd.ClaudeDesktopError, match="No ready"):
        update.staged_update(s.app, cache=s.cache, app_factory=s.factory)
    s.factory.assert_not_called()


@pytest.fixture
def menu(setup, monkeypatch):
    monkeypatch.setattr("openswap.claude_desktop_menu.threading.Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    return ClaudeDesktopMenu(store=setup.store, updater=setup.updater, prompt=Mock(), confirm=Mock(return_value=1),
                             show_error=Mock(), changed=Mock(), notify=Mock())


def test_ui_cancel_never_quits_or_writes_recovery(menu, setup):
    menu.confirm.return_value = 0
    menu.perform("update")
    assert menu.busy
    menu.confirm.assert_not_called()
    menu.poll()
    assert not menu.busy and not setup.updater.pending()
    assert not setup.backend.quits
    assert "Default" in menu.confirm.call_args.kwargs["message"]


def test_ui_progress_keeps_busy_until_completion(menu, setup, monkeypatch):
    monkeypatch.setattr(menu, "refresh_status", Mock())
    menu.perform("update")
    menu.poll()  # confirmation; execute queues progress and completion
    for _ in range(3):
        menu.poll()
        assert menu.busy
    menu.poll()
    assert not menu.busy
    menu.notify.assert_called_once()
    menu.refresh_status.assert_called_once()


def test_pending_update_has_recovery_action_with_disabled_profile_controls(menu, setup):
    setup.backend.refuse_quit = True
    with pytest.raises(cd.ClaudeDesktopError): setup.updater.execute(setup.updater.plan())
    state = menu.settings_state()
    assert state["update_pending"]
    rows = {r["id"]: r for r in claude_desktop_profile_rows(state)}
    assert rows["claude_desktop_profiles"]["disabled"]
    assert rows["claude_desktop_add"]["disabled"]
    assert rows["claude_desktop_update"]["title"] == "Resume update…"
    assert not rows["claude_desktop_update"]["disabled"]
    settings = {r["id"]: r for r in claude_desktop_settings_rows(state)}
    assert settings["claude_desktop_check"]["disabled"]
    assert not settings["claude_desktop_update"]["disabled"]
    menu.perform("open", setup.profiles[0].id)
    menu.show_error.assert_called_once()


@pytest.mark.parametrize("problem", [None, "unknown", "archived", "executable", "bundle", "starting"])
def test_native_snapshot_requires_exact_known_active_processes(setup, monkeypatch, problem):
    s = setup
    app = cd.ClaudeDesktopApp(Path("/Applications/Claude.app"))
    profile = s.profiles[1]
    desktop, code = s.store.paths(profile)
    app.processes = Mock(return_value=f"42 {' '.join(app.launch_argv(desktop, code))}")
    native = Mock()
    native.processIdentifier.return_value = 42
    native.isTerminated.return_value = False
    native.executableURL.return_value.path.return_value = str(app.binary)
    native.bundleURL.return_value.path.return_value = str(app.path)
    if problem == "unknown": native.processIdentifier.return_value = 99
    if problem == "archived": s.store.update(profile.id, archived=True)
    if problem == "executable": native.executableURL.return_value.path.return_value = "/other/Claude"
    if problem == "bundle": native.bundleURL.return_value.path.return_value = "/other/Claude.app"
    query = Mock(return_value=[] if problem == "starting" else [native])
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSRunningApplication=SimpleNamespace(runningApplicationsWithBundleIdentifier_=query)))
    backend = update.MacUpdateBackend(app_factory=lambda: app)
    if problem:
        with pytest.raises(cd.ClaudeDesktopError): backend.snapshot(s.store)
    else:
        assert backend.snapshot(s.store) == {profile.id: native}
    native.terminate.assert_not_called()


def test_native_quit_never_refetches_a_reused_pid():
    native = Mock()
    native.isTerminated.return_value = True
    assert update.MacUpdateBackend().quit(native)
    native.terminate.assert_not_called()
    native.isTerminated.return_value = False
    native.terminate.return_value = False
    assert not update.MacUpdateBackend().quit(native)
    native.forceTerminate.assert_not_called()


def test_installer_detection_matches_exact_claude_helper(monkeypatch, tmp_path):
    app = cd.ClaudeDesktopApp()
    command = (f"{app.path}/Contents/Frameworks/Squirrel.framework/Resources/ShipIt "
               f"{update.SHIPIT_ID} {Path.home()}/Library/Caches/{update.SHIPIT_ID}/ShipItState.plist")
    backend = update.MacUpdateBackend(app_factory=lambda: app)
    app.processes = Mock(return_value=f" 12 {command}\n")
    assert backend.installer_running()
    for text in ["", "\n  ", "not-a-pid " + command, "12 /bin/echo " + command, "12 " + command + " --unrecognized", "12 " + command.replace("Claude.app", "Other.app")]:
        app.processes.return_value = text
        assert not backend.installer_running()


def test_second_coordinator_cannot_close_profiles(setup):
    s = setup
    plan = s.updater.plan()
    with s.updater.journal.storage.lock("desktop-update-worker.lock"):
        with pytest.raises(Exception, match="acquire lock"):
            s.updater.execute(plan)
    assert not s.updater.pending() and not s.backend.quits


def test_journal_failure_happens_before_any_quit(setup, monkeypatch):
    monkeypatch.setattr(setup.updater.journal, "write", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        setup.updater.execute(setup.updater.plan())
    assert not setup.backend.quits


def test_staging_symlink_is_not_validated(stage):
    s = stage
    moved = s.staged.with_name("original.app")
    s.staged.rename(moved)
    try:
        s.staged.symlink_to(moved, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(cd.ClaudeDesktopError, match="No ready"):
        update.staged_update(s.app, cache=s.cache, app_factory=s.factory)
    s.factory.assert_not_called()


def test_panel_update_button_dispatches_coordinated_action():
    from openswap import menubar
    from tests.menubar_harness import extract_class
    cls = extract_class(Path(menubar.__file__).with_name("menubar_panel.py"), "MenuBarPanel", {"_emit_setting"}, {})
    panel = cls()
    panel._desktop_profile_action = Mock()
    panel._emit_setting("claude_desktop_update", None)
    panel._desktop_profile_action.assert_called_once_with("update")
