"""Shared-install update coordination; never launch or quit the real Claude app."""
import json
import plistlib
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


def gate_free():
    lock = update.UpdateJournal().lock()
    if not lock.acquire(timeout=0):
        return False
    lock.release()
    return True


class Backend:
    def __init__(self, profiles):
        self.targets = {p.id: Native(i + 10) for i, p in enumerate(profiles)}
        self.now = 0
        self.current, self.target = "4.4212.0", "4.16336.0"
        self.allow_install = True
        self.installer = True
        self.refuse_quit = False
        self.hold_quit = False
        self.helpers = {p.id for p in profiles if p.kind != "default"}
        self.stale_helpers = False
        self.quits, self.restored, self.attached = [], [], []
        self.staged_checks = 0

    def installed(self):
        assert gate_free(), "signature checks must not hold the launch gate"
        return self.current, (self.current,)

    def staged(self):
        assert gate_free(), "signature checks must not hold the launch gate"
        self.staged_checks += 1
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
            if not self.stale_helpers:
                self.helpers -= {p for p, n in self.targets.items() if n is native}
        return True

    def exited(self, targets):
        return all(n.closed for n in targets.values())

    def claude_running(self):
        return not all(n.closed for n in self.targets.values())

    def docked(self, store, identifiers):
        return [i for i in identifiers if i in self.helpers]

    def restore(self, store, identifier):
        record = update.UpdateJournal().read()
        assert record is None or record["phase"] in {"restoring", "canceled"}
        if not self.targets[identifier].closed:
            return {"state": "already_running", "pid": self.targets[identifier].pid}
        self.restored.append(identifier)
        self.targets[identifier] = Native(100 + len(self.restored))
        return {"state": "launched", "pid": self.targets[identifier].pid}

    def attach(self, store, identifier):
        self.attached.append(identifier)
        self.helpers.add(identifier)
        return store.get(identifier).kind != "default"

    def sleep(self, seconds):
        self.now += seconds
        if self.allow_install and self.exited(self.targets):
            self.current = self.target


def launcher(version="4.16336.0", state="launched", during_open=None):
    """A validated Claude app double; ``during_open`` runs while the spawn holds its locks."""
    app = Mock()
    app.validate.return_value = {"version": version}
    app.running.return_value = None

    def open_(*_args, **_kwargs):
        if during_open:
            during_open()
        return {"state": state, "pid": 7 if state == "launched" else None}
    app.open.side_effect = open_
    return app


def write_journal(s, phase):
    journal = update.UpdateJournal()
    with journal.lock():
        journal.write({"schema": 1, "id": "a" * 32, "root": str(s.store.root), "before": "4.4212.0",
                       "target": "4.16336.0", "profiles": [], "docked": [], "restored": [], "phase": phase})


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
    assert result == {"state": "update_done", "version": "4.16336.0", "dock_warnings": []}
    assert s.backend.staged_checks == 2  # once to plan, once to confirm
    assert s.backend.quits == [10, 11]
    assert s.backend.restored == s.backend.attached == [p.id for p in s.profiles]
    assert stopped.id not in s.backend.restored
    assert s.store.registry.read_bytes() == registry
    assert not s.updater.pending()
    assert len(progress) == 3


@pytest.mark.parametrize("reason", ["refused", "held", "stalled"])
def test_failed_update_keeps_gate_and_saved_profiles(setup, reason):
    s = setup
    if reason == "refused": s.backend.refuse_quit = True
    if reason == "held": s.backend.hold_quit = True
    if reason == "stalled": s.backend.allow_install = False
    with pytest.raises(cd.ClaudeDesktopError):
        s.updater.execute(s.updater.plan())
    assert s.updater.pending()
    assert s.updater.journal.read()["profiles"] == [p.id for p in s.profiles]
    assert s.backend.restored == []
    other = cd.DesktopProfiles(s.store.root.parent / "another-store")
    for store, identifier in ((s.store, s.profiles[1].id), (other, other.add("Other").id)):
        app = launcher()
        with pytest.raises(cd.ClaudeDesktopError, match="Resume update"):
            store.open(identifier, app=app)
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


@pytest.mark.parametrize("failure", ["raised", "not_attached", "old_helper"])
def test_dock_failure_is_a_warning_that_finishes_the_update(setup, failure):
    s = setup
    work = s.profiles[1]
    if failure == "raised":
        s.backend.attach = Mock(side_effect=cd.ClaudeDesktopError("Dock unavailable"))
    if failure == "not_attached":
        s.backend.attach = Mock(return_value=False)
    if failure == "old_helper":
        s.backend.stale_helpers = True
    result = s.updater.execute(s.updater.plan())
    assert result["dock_warnings"] == [work.name]
    assert s.backend.restored == [p.id for p in s.profiles]
    assert not s.updater.pending()
    if failure == "old_helper":
        assert work.id not in s.backend.attached
    assert s.store.open(work.id, app=launcher())["state"] == "launched"


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
    app = launcher(state="not_running")
    with s.updater.journal.lock():  # never waits on the gate, even mid-launch
        result = s.store.open(s.profiles[1].id, app=app, existing_only=True)
    assert result["state"] == "not_running"
    assert app.open.call_args.kwargs == {"existing_only": True}


def test_corrupt_journal_blocks_launch_without_replacing_data(setup):
    s = setup
    with s.updater.journal.lock():
        s.updater.journal.path.write_text("{broken")
        s.updater.journal.path.chmod(0o600)
    app = launcher()
    with pytest.raises(cd.ClaudeDesktopError, match="unreadable"):
        s.store.open(s.profiles[1].id, app=app)
    app.open.assert_not_called()
    assert s.updater.journal.path.read_text() == "{broken"


@pytest.fixture
def stage(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Squirrel.Mac uses macOS file URLs and bundle paths")
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


def staged_info(s, version="4.16336.0"):
    (s.staged / "Contents").mkdir(exist_ok=True)
    (s.staged / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": cd.BUNDLE_ID, "CFBundleExecutable": "Claude", "CFBundleShortVersionString": version}))
    run = Mock()
    return lambda path: cd.ClaudeDesktopApp(path, run=run), run


@pytest.mark.parametrize("staged, expected", [("4.16336.0", "4.16336.0"), ("4.4212.0", ""), ("4.1.0", "")])
def test_downloaded_update_reads_staged_version_without_signature_check(stage, staged, expected):
    factory, run = staged_info(stage, staged)
    assert update.downloaded_update(stage.app, "4.4212.0", cache=stage.cache, app_factory=factory) == expected
    run.assert_not_called()


def test_no_state_file_means_no_update_downloaded(stage):
    stage.state.unlink()
    assert update.downloaded_update(stage.app, "4.4212.0", cache=stage.cache, app_factory=stage.factory) == ""
    stage.factory.assert_not_called()


@pytest.mark.parametrize("change", ["invalid", "target", "plist", "missing", "bundle", "version", "installed"])
def test_unreadable_download_state_is_unknown(stage, change):
    factory, run = staged_info(stage, "4.x" if change == "version" else "4.16336.0")
    installed = "unknown" if change == "installed" else "4.4212.0"
    if change == "invalid": stage.state.write_text("{")
    if change == "target":
        stage.data["targetBundleURL"] = (stage.app.path.parent / "Other.app").as_uri()
        stage.state.write_text(json.dumps(stage.data))
    if change == "plist": (stage.staged / "Contents" / "Info.plist").write_text("<plist")
    if change == "missing": (stage.staged / "Contents" / "Info.plist").unlink()
    if change == "bundle": (stage.staged / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "other"}))
    assert update.downloaded_update(stage.app, installed, cache=stage.cache, app_factory=factory) is None
    run.assert_not_called()


@pytest.fixture
def menu(setup, monkeypatch):
    monkeypatch.setattr("openswap.claude_desktop_menu.threading.Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    return ClaudeDesktopMenu(store=setup.store, updater=setup.updater, prompt=Mock(), confirm=Mock(return_value=1),
                             show_error=Mock(), changed=Mock(), notify=Mock(), app_factory=launcher)


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


@pytest.mark.parametrize("staged, label", [("4.16336.0", "Claude 4.16336.0 is ready to install"),
                                           ("", "Claude 4.4212.0 · No update downloaded"), (None, None)])
def test_status_check_reports_downloaded_update(menu, monkeypatch, staged, label):
    downloaded = Mock(return_value=staged)
    monkeypatch.setattr(update, "downloaded_update", downloaded)
    app = launcher("4.4212.0")
    app.profile_states.return_value = {}
    menu._app_factory = lambda: app
    menu.refresh_status()
    menu.poll()
    downloaded.assert_called_once_with(app, "4.4212.0")
    assert menu.settings_state()["update_label"] == label


def test_failed_app_check_has_no_update_label(menu, monkeypatch):
    downloaded = Mock(return_value="4.16336.0")
    monkeypatch.setattr(update, "downloaded_update", downloaded)
    app = launcher()
    app.validate.side_effect = cd.ClaudeDesktopError("Reinstall the official app.")
    app.profile_states.return_value = {}
    menu._app_factory = lambda: app
    menu._update_label = "Claude 4.16336.0 is ready to install"
    menu.refresh_status()
    menu.poll()
    downloaded.assert_not_called()
    assert menu.settings_state()["update_label"] is None


def test_failed_check_stops_the_label_until_a_check_passes(passive):
    p = passive
    p.app.validate.side_effect = cd.ClaudeDesktopError("Reinstall the official app.")
    p.app.profile_states.return_value = {}
    p.menu.refresh_status()
    p.menu.poll()
    p.now[0] += 61
    p.menu.poll()
    p.menu.poll()
    p.downloaded.assert_not_called()
    assert p.menu.settings_state()["update_label"] is None
    p.app.validate.side_effect = None
    p.app.validate.return_value = {"version": "4.4212.0"}
    p.menu.refresh_status()
    p.menu.poll()
    p.downloaded.reset_mock()
    p.now[0] += 61
    p.menu.poll()
    p.menu.poll()
    p.downloaded.assert_called_once()


def test_stale_check_cannot_set_update_label(menu):
    menu._label_checked_at = menu.clock()
    menu._checks.put((menu._check_generation - 1, "Old result", {}, "Claude 4.1.0 is ready to install", "old", True))
    menu._labels.put((menu._check_generation - 1, "Claude 4.1.0 is ready to install"))
    menu.poll()
    menu.poll()
    assert menu.settings_state()["update_label"] is None


def test_label_error_keeps_verified_app_message(menu, monkeypatch):
    monkeypatch.setattr(update, "downloaded_update", Mock(side_effect=RuntimeError("boom")))
    app = launcher("4.4212.0")
    app.profile_states.return_value = {}
    menu._app_factory = lambda: app
    menu.refresh_status()
    menu.poll()
    state = menu.settings_state()
    assert state["app_message"] == "Claude 4.4212.0 · Publisher verified."
    assert state["update_label"] is None


@pytest.fixture
def passive(menu, monkeypatch):
    now = [1000.0]
    menu.clock = lambda: now[0]
    downloaded = Mock(return_value="4.16336.0")
    monkeypatch.setattr(update, "downloaded_update", downloaded)
    app = launcher()
    app.bundle_info.return_value = {"CFBundleShortVersionString": "4.4212.0"}
    menu._app_factory = lambda: app
    return SimpleNamespace(menu=menu, now=now, downloaded=downloaded, app=app)


def test_poll_notices_download_and_install_without_a_full_check(passive):
    p = passive
    p.menu.poll()
    assert p.menu._update_label == "Claude 4.16336.0 is ready to install"
    p.downloaded.return_value = ""
    p.app.bundle_info.return_value = {"CFBundleShortVersionString": "4.16336.0"}
    p.now[0] += 59
    p.menu.poll()
    assert p.menu._update_label == "Claude 4.16336.0 is ready to install"
    p.now[0] += 2
    p.menu.poll()
    assert p.menu._update_label == "Claude 4.16336.0 · No update downloaded"
    p.app.validate.assert_not_called()
    p.app.profile_states.assert_not_called()


def test_opening_the_panel_refreshes_a_label_older_than_30_seconds(passive):
    p = passive
    p.menu._label_checked_at = p.now[0]
    p.now[0] += 29
    p.menu.settings_state()
    p.downloaded.assert_not_called()
    p.now[0] += 2
    p.menu.settings_state()
    p.menu.poll()
    assert p.menu.settings_state()["update_label"] == "Claude 4.16336.0 is ready to install"


@pytest.mark.parametrize("reason", ["busy", "checking", "pending", "disabled"])
def test_passive_label_refresh_waits(passive, setup, reason):
    p = passive
    if reason == "busy": p.menu.busy = True
    if reason == "checking": p.menu.checking = True
    if reason == "pending":
        setup.backend.refuse_quit = True
        with pytest.raises(cd.ClaudeDesktopError): setup.updater.execute(setup.updater.plan())
    if reason == "disabled": p.menu.enabled = lambda: False
    p.menu.poll()
    p.menu.settings_state()
    p.downloaded.assert_not_called()


def test_turning_desktop_features_off_clears_the_label(passive):
    p = passive
    p.menu.poll()
    assert p.menu._update_label
    p.menu.suspend_checks()
    assert p.menu.settings_state()["update_label"] is None


@pytest.mark.parametrize("state, main, settings", [
    ({"update_label": "Claude 4.16336.0 is ready to install"}, "Claude 4.16336.0 is ready to install", "Claude 4.16336.0 is ready to install"),
    ({"update_label": "Claude 4.4212.0 · No update downloaded"}, "Claude 4.4212.0 · No update downloaded", "Claude 4.4212.0 · No update downloaded"),
    ({}, "Claude Desktop update", "Update and restore profiles"),
    ({"update_label": "Claude 4.16336.0 is ready to install", "update_pending": True}, "Claude Desktop update", "Update and restore profiles"),
])
def test_update_row_label_follows_last_check(state, main, settings):
    rows = {r["id"]: r for r in claude_desktop_profile_rows(state)}
    setting_rows = {r["id"]: r for r in claude_desktop_settings_rows(state)}
    assert rows["claude_desktop_update"]["label"] == main
    assert setting_rows["claude_desktop_update"]["label"] == settings
    title = "Resume update…" if state.get("update_pending") else "Update Claude…"
    for row in (rows["claude_desktop_update"], setting_rows["claude_desktop_update"]):
        assert row["title"] == title and not row["disabled"]


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
               f"{update.SHIPIT_ID} {update._shipit_cache()}/ShipItState.plist")
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
        with pytest.raises(cd.ClaudeDesktopError, match="already working"):
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


def test_resume_accepts_a_newer_staged_update_after_original_is_replaced(setup):
    s = setup
    s.backend.refuse_quit = True
    with pytest.raises(cd.ClaudeDesktopError):
        s.updater.execute(s.updater.plan())
    s.backend.refuse_quit = False
    s.backend.target = "4.20000.0"
    plan = s.updater.plan()
    assert plan.target == "4.20000.0"
    s.updater.execute(plan)
    assert s.backend.restored == [p.id for p in s.profiles]
    assert not s.updater.pending()


@pytest.mark.parametrize("action", ["update", "cancel"])
@pytest.mark.parametrize("settles", [True, False])
def test_launch_already_opening_elsewhere_counts_as_reopened(setup, action, settles):
    s = setup
    work = s.profiles[1].id
    calls = []
    restore = s.backend.restore
    def opening_elsewhere(store, identifier):
        calls.append(identifier)
        if identifier == work and (not settles or calls.count(work) < 3):
            return {"state": "already_opening", "pid": None}
        return restore(store, identifier)
    if action == "cancel":
        cancel_after_failed_update(s, allow_install=False)
        s.backend.installer = False
    s.backend.restore = opening_elsewhere
    result = s.updater.execute(s.updater.plan()) if action == "update" else s.updater.cancel()
    assert result["dock_warnings"] == [] and result.get("failed", []) == []
    assert work in s.backend.attached
    assert calls.count(work) == 3
    assert not s.updater.pending()


@pytest.mark.parametrize("finish", ["resume", "cancel"])
def test_interrupted_restoration_keeps_reopened_profiles_for_dock_reattach(setup, finish):
    s = setup
    default, work = (p.id for p in s.profiles)
    restore = s.backend.restore
    def stop_on_work(store, identifier):
        if identifier == work:
            raise cd.ClaudeDesktopError("Claude exited during launch")
        return restore(store, identifier)
    s.backend.restore = stop_on_work
    with pytest.raises(cd.ClaudeDesktopError, match="exited"):
        s.updater.execute(s.updater.plan())
    record = s.updater.journal.read()
    assert (record["profiles"], record["restored"]) == ([work], [default])
    assert not s.backend.attached
    s.backend.restore = restore
    if finish == "resume":
        s.updater.execute(s.updater.plan())
    else:
        s.updater.cancel()
    assert s.backend.attached == [default, work]
    assert not s.updater.pending()


def test_interrupted_cancel_keeps_launches_open_and_resumes_reopening(setup):
    s = setup
    default, work = (p.id for p in s.profiles)
    cancel_after_failed_update(s, allow_install=False)
    s.backend.installer = False
    restore = s.backend.restore
    def crash_on_work(store, identifier):
        if identifier == work:
            raise RuntimeError("OpenSwap quit")
        return restore(store, identifier)
    s.backend.restore = crash_on_work
    with pytest.raises(RuntimeError):
        s.updater.cancel()
    record = s.updater.journal.read()
    assert (record["phase"], record["profiles"], record["restored"]) == ("canceled", [work], [default])
    assert s.store.open(work, app=launcher(version="4.4212.0"))["state"] == "launched"
    with pytest.raises(cd.ClaudeDesktopError, match="Cancel update"):
        s.updater.plan()
    s.backend.restore = restore
    result = s.updater.cancel()
    assert result["failed"] == [] and s.backend.restored == [default, work]
    assert s.backend.attached == [default, work]
    assert not s.updater.pending()


def test_resume_never_accepts_an_older_replacement_download(setup):
    s = setup
    s.backend.refuse_quit = True
    with pytest.raises(cd.ClaudeDesktopError):
        s.updater.execute(s.updater.plan())
    s.backend.target = "4.9999.0"
    with pytest.raises(cd.ClaudeDesktopError, match="older than"):
        s.updater.plan()
    assert s.updater.journal.read()["target"] == "4.16336.0"


@pytest.mark.parametrize("phase, installed, launches", [
    (None, "4.4212.0", True),
    ("waiting", "4.16336.0", False),
    ("restoring", "4.16336.0", True),
    ("restoring", "4.4212.0", False),
])
def test_launch_gate_by_update_phase(setup, phase, installed, launches):
    if phase:
        write_journal(setup, phase)
    app = launcher(version=installed)
    if launches:
        assert setup.store.open(setup.profiles[1].id, app=app)["state"] == "launched"
    else:
        with pytest.raises(cd.ClaudeDesktopError, match="Resume update"):
            setup.store.open(setup.profiles[1].id, app=app)
        app.open.assert_not_called()


def test_different_profiles_launch_while_one_is_being_validated(setup):
    s = setup
    other = launcher()
    first = launcher()
    first.validate.side_effect = lambda: (s.store.open(s.profiles[1].id, app=other), {"version": "4.16336.0"})[1]
    assert s.store.open(s.profiles[0].id, app=first)["state"] == "launched"
    other.open.assert_called_once()


def test_same_profile_opened_twice_reports_the_launch_in_progress(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(cd, "LAUNCH_LOCK_TIMEOUT", 0.1)
    second = []
    app = launcher(during_open=lambda: second.append(s.store.open(s.profiles[1].id, app=launcher())))
    assert s.store.open(s.profiles[1].id, app=app)["state"] == "launched"
    assert second[0]["state"] == "already_opening"


def test_restoring_coordinator_shares_launches_without_lock_errors(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(cd, "LAUNCH_LOCK_TIMEOUT", 0.1)
    other = s.store.add("Other")
    work = s.profiles[1].id
    seen = {}

    def validate():
        assert gate_free(), "validation held the launch gate"
        seen["other"] = s.store.open(other.id, app=launcher())["state"]
        return {"version": "4.16336.0"}

    def restore(store, identifier):
        s.backend.targets[identifier] = Native(200)
        if identifier != work:
            return store.open(identifier, app=launcher())
        app = launcher(during_open=lambda: seen.setdefault("same", s.store.open(work, app=launcher())["state"]))
        app.validate.side_effect = validate
        return store.open(identifier, app=app)
    s.backend.restore = restore
    s.updater.execute(s.updater.plan())
    assert seen == {"other": "launched", "same": "already_opening"}
    assert not s.updater.pending()


def cancel_after_failed_update(s, **failure):
    for name, value in failure.items():
        setattr(s.backend, name, value)
    with pytest.raises(cd.ClaudeDesktopError):
        s.updater.execute(s.updater.plan())
    assert s.updater.pending()
    s.backend.refuse_quit = s.backend.allow_install = False
    return s.updater.journal.read()


def test_cancel_while_profiles_still_run_reopens_only_closed_ones(setup):
    s = setup
    cancel_after_failed_update(s, refuse_quit=True)
    s.backend.targets[s.profiles[1].id].closed = True
    s.backend.helpers.clear()
    result = s.updater.cancel()
    assert result == {"state": "update_canceled", "failed": [], "unreadable": False, "dock_warnings": []}
    assert s.backend.restored == [s.profiles[1].id]
    assert s.backend.attached == [s.profiles[1].id]
    assert s.backend.quits == [10]
    assert not s.updater.pending()


def test_cancel_after_stalled_install_reopens_saved_profiles_except_archived(setup):
    s = setup
    archived = s.store.add("Archived")
    s.backend.targets[archived.id] = Native(30)
    cancel_after_failed_update(s, allow_install=False)
    s.store.update(archived.id, archived=True)
    s.backend.installer = False
    result = s.updater.cancel()
    assert result["failed"] == [] and result["dock_warnings"] == []
    assert s.backend.restored == [s.profiles[0].id, s.profiles[1].id]
    assert not s.updater.pending()
    assert s.store.open(s.profiles[1].id, app=launcher(version="4.4212.0"))["state"] == "launched"


def test_cancel_waits_while_the_installer_runs_with_every_profile_closed(setup):
    s = setup
    record = cancel_after_failed_update(s, allow_install=False)
    with pytest.raises(cd.ClaudeDesktopError, match="installing its update"):
        s.updater.cancel()
    assert s.updater.journal.read() == record
    assert not s.backend.restored


def test_cancel_during_restoration_reopens_remaining_profiles(setup):
    s = setup
    restore = s.backend.restore
    def stop_on_work(store, identifier):
        if identifier == s.profiles[1].id:
            raise cd.ClaudeDesktopError("Claude exited during launch")
        return restore(store, identifier)
    s.backend.restore = stop_on_work
    with pytest.raises(cd.ClaudeDesktopError, match="exited"):
        s.updater.execute(s.updater.plan())
    assert s.updater.journal.read()["phase"] == "restoring"
    s.backend.restore = restore
    s.updater.cancel()
    assert s.backend.restored == [s.profiles[0].id, s.profiles[1].id]
    assert not s.updater.pending()


def test_cancel_reports_profiles_that_did_not_reopen_and_still_unblocks(setup):
    s = setup
    cancel_after_failed_update(s, allow_install=False)
    s.backend.installer = False
    restore = s.backend.restore
    def fail_work(store, identifier):
        if identifier == s.profiles[1].id:
            raise cd.ClaudeDesktopError("Claude exited during launch")
        return restore(store, identifier)
    s.backend.restore = fail_work
    result = s.updater.cancel()
    assert result["failed"] == [s.profiles[1].name]
    assert not s.updater.pending()


def test_cancel_dock_reattach_waits_for_old_helpers_and_warns(setup):
    s = setup
    record = cancel_after_failed_update(s, allow_install=False, stale_helpers=True)
    assert record["docked"] == [s.profiles[1].id]
    s.backend.installer = False
    result = s.updater.cancel()
    assert result["dock_warnings"] == [s.profiles[1].name]
    assert s.profiles[1].id not in s.backend.attached
    assert s.backend.now >= 10


@pytest.mark.parametrize("blocker", ["coordinator", "other_store"])
def test_cancel_is_refused_without_changing_recovery(setup, blocker):
    s = setup
    record = cancel_after_failed_update(s, allow_install=False)
    s.backend.installer = False
    if blocker == "coordinator":
        with s.updater.journal.storage.lock("desktop-update-worker.lock"):
            with pytest.raises(cd.ClaudeDesktopError, match="already working"):
                s.updater.cancel()
    else:
        other = update.DesktopUpdate(cd.DesktopProfiles(s.store.root.parent / "other"), backend=s.backend)
        with pytest.raises(cd.ClaudeDesktopError, match="store that started"):
            other.cancel()
    assert s.updater.journal.read() == record
    assert not s.backend.restored


@pytest.mark.parametrize("answer", [1, 0])
def test_ui_cancel_update_requires_confirmation(menu, setup, monkeypatch, answer):
    monkeypatch.setattr(menu, "refresh_status", Mock())
    cancel_after_failed_update(setup, allow_install=False)
    setup.backend.installer = False
    rows = {r["id"]: r for r in claude_desktop_profile_rows(menu.settings_state())}
    assert not rows["claude_desktop_cancel_update"]["disabled"]
    menu.confirm.return_value = answer
    menu.perform("cancel_update")
    menu.poll()
    assert setup.updater.pending() == (not answer)
    if answer:
        assert menu.notify.call_args.args[0] == "Claude update canceled"
    else:
        menu.notify.assert_not_called()


def damage_journal(s):
    with s.updater.journal.lock():
        s.updater.journal.path.write_text("{broken")
        s.updater.journal.path.chmod(0o600)


@pytest.mark.parametrize("blocker", ["installer", "coordinator"])
def test_cancel_keeps_a_damaged_journal_while_refused(setup, blocker):
    s = setup
    damage_journal(s)
    s.backend.targets = {p: Native(1) for p in s.backend.targets}
    for native in s.backend.targets.values():
        native.closed = True
    if blocker == "installer":
        with pytest.raises(cd.ClaudeDesktopError, match="installing its update"):
            s.updater.cancel()
    else:
        s.backend.installer = False
        with s.updater.journal.storage.lock("desktop-update-worker.lock"):
            with pytest.raises(cd.ClaudeDesktopError, match="already working"):
                s.updater.cancel()
    assert s.updater.journal.path.read_text() == "{broken"
    with pytest.raises(cd.ClaudeDesktopError, match="Cancel update"):
        s.store.open(s.profiles[1].id, app=launcher())


def test_cancel_removes_a_damaged_journal_and_reopens_nothing(menu, setup, monkeypatch):
    s = setup
    monkeypatch.setattr(menu, "refresh_status", Mock())
    damage_journal(s)
    assert {r["id"] for r in claude_desktop_profile_rows(menu.settings_state())} >= {"claude_desktop_cancel_update"}
    menu.perform("cancel_update")
    assert "nothing is reopened" in menu.confirm.call_args.kwargs["message"]
    menu.poll()
    assert not s.updater.journal.path.exists() and not s.backend.restored
    assert "Reopen your profiles from OpenSwap." in menu.notify.call_args.args[1]
    assert s.store.open(s.profiles[1].id, app=launcher())["state"] == "launched"


@pytest.mark.parametrize("row, action", [("claude_desktop_update", "update"), ("claude_desktop_cancel_update", "cancel_update")])
def test_panel_update_rows_dispatch_their_actions(row, action):
    from openswap import menubar
    from tests.menubar_harness import extract_class
    cls = extract_class(Path(menubar.__file__).with_name("menubar_panel.py"), "MenuBarPanel", {"_emit_setting"}, {})
    panel = cls()
    panel._desktop_profile_action = Mock()
    panel._emit_setting(row, None)
    panel._desktop_profile_action.assert_called_once_with(action)
