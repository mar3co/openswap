"""Settings › General › This Mac › Open at login (plan 020, Step 3).

The page model is pure; the controller methods are lifted out of
``MenuBarApp`` without AppKit (``tests/menubar_harness.py``), with
``launch_agent.set_login_item`` and the worker thread faked.
"""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from openswap import bundle, launch_agent, menubar, menubar_instance
from openswap.exceptions import ClaudeSwitchError
from tests.menubar_harness import extract_class


def _rows(login_item=None, section=menubar.SETTINGS_SECTION_GENERAL, **settings):
    return menubar.settings_page_rows(
        menubar.MenuBarSettings(**settings),
        strategy="best",
        threshold=90,
        login_item=login_item,
        section=section,
    )


def _by_id(rows):
    return {row["id"]: row for row in rows}


# --- the page model ----------------------------------------------------------


@pytest.mark.parametrize("provider", ["claude", "chatgpt", "both", "logo"])
def test_this_mac_closes_the_general_section(provider):
    rows = _rows({"enabled": True}, menu_bar_provider=provider)
    ids = [row["id"] for row in rows]
    at = ids.index("group_this_mac")
    # The last group of General: Open at login, then the rest of setup
    # (phase 2's status line toggle; the widget hint only in the app).
    assert ids[at:] == ["group_this_mac", "open_at_login", "claude_status_line"]
    group, toggle = rows[at:at + 2]
    assert group["kind"] == "group" and group["style"] == "section"
    assert group["label"] == "This Mac"
    assert toggle["kind"] == "toggle" and toggle["label"] == "Open at login"
    assert toggle["value"] is True and toggle["disabled"] is False


def test_open_at_login_value_comes_from_the_login_item_not_menubar_settings():
    assert _by_id(_rows({"enabled": False}))["open_at_login"]["value"] is False
    assert _by_id(_rows({"enabled": True}))["open_at_login"]["value"] is True
    # Without a reading the row still renders, off, and can be switched.
    toggle = _by_id(_rows(None))["open_at_login"]
    assert toggle["value"] is False and toggle["disabled"] is False
    assert not hasattr(menubar.MenuBarSettings(), "open_at_login")


@pytest.mark.parametrize(
    "section", [menubar.SETTINGS_SECTION_AUTOMATION, menubar.SETTINGS_SECTION_REMOTE]
)
def test_this_mac_rows_only_appear_under_general(section):
    ids = {row["id"] for row in _rows({"enabled": True, "start_failed": True}, section=section)}
    assert not ids & {"group_this_mac", "open_at_login", "open_at_login_hint", "open_at_login_failed"}


def test_the_unfiltered_model_keeps_this_mac_in_general():
    rows = _rows({"enabled": True}, section=None)
    by_id = _by_id(rows)
    assert by_id["group_this_mac"]["section"] == menubar.SETTINGS_SECTION_GENERAL
    assert by_id["open_at_login"]["section"] == menubar.SETTINGS_SECTION_GENERAL
    ids = [row["id"] for row in rows]
    assert ids.index("open_at_login") < ids.index("group_claude")


def test_a_temporary_copy_cannot_switch_it_and_says_why():
    rows = _rows({"enabled": False, "temporary": True, "start_failed": True})
    by_id = _by_id(rows)
    assert by_id["open_at_login"]["disabled"] is True
    hint = by_id["open_at_login_hint"]
    assert hint["style"] == "hint"
    assert hint["label"] == menubar.OPEN_AT_LOGIN_MOVE_HINT
    assert "Applications" in hint["label"]
    assert "open_at_login_failed" not in by_id
    ids = [row["id"] for row in rows]
    assert ids[ids.index("open_at_login"):][:3] == [
        "open_at_login", "open_at_login_hint", "claude_status_line",
    ]


def test_a_change_in_flight_disables_the_switch():
    toggle = _by_id(_rows({"enabled": True, "busy": True}))["open_at_login"]
    assert toggle["disabled"] is True


def test_turning_it_off_from_the_login_item_says_it_stays_until_quit():
    hint = _by_id(_rows({"enabled": False, "outcome": "disabled_after_quit"}))["open_at_login_hint"]
    assert hint["label"] == menubar.OPEN_AT_LOGIN_AFTER_QUIT_HINT
    assert "next login" in hint["label"]


@pytest.mark.parametrize("outcome", ["enabled", "disabled", "unchanged", "handoff"])
def test_other_outcomes_need_no_hint(outcome):
    assert "open_at_login_hint" not in _by_id(_rows({"enabled": True, "outcome": outcome}))


def test_a_failed_start_puts_a_warning_right_after_the_switch():
    rows = _rows({"enabled": True, "start_failed": True, "as_login_item": False})
    ids = [row["id"] for row in rows]
    at = ids.index("group_this_mac")
    # Phase 2's rows follow the warning.
    assert ids[at:at + 4] == ["group_this_mac", "open_at_login", "open_at_login_failed", "claude_status_line"]
    warning = _by_id(rows)["open_at_login_failed"]
    assert warning["kind"] == "button"
    assert warning["label"] == "macOS isn’t starting OpenSwap at login"
    assert warning["title"] == "Open Login Items"
    assert warning["value"] == menubar.LOGIN_ITEMS_SETTINGS_URL
    assert warning["value"] == "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


@pytest.mark.parametrize(
    "state",
    [
        {"enabled": False, "start_failed": True},  # off: nothing claims to work
        {"enabled": True, "start_failed": False},  # the last start worked
        {"enabled": True, "start_failed": True, "as_login_item": True},  # launchd started this copy
    ],
)
def test_the_failed_start_warning_needs_on_failed_and_not_the_job(state):
    assert "open_at_login_failed" not in _by_id(_rows(state))


def test_other_errors_are_shown_as_is_with_room_for_them():
    short = _by_id(_rows({"enabled": False, "error": "Move OpenSwap to Applications first."}))
    assert short["open_at_login_hint"]["label"] == "Move OpenSwap to Applications first."
    assert short["open_at_login_hint"]["lines"] == 2
    assert "open_at_login_failed" not in short
    long_error = (
        "OpenSwap is installed with Homebrew, so this uv install will not open at login. "
        "Use /opt/homebrew/bin/openswap (or OpenSwap.app) instead, "
        "and remove this one with: uv tool uninstall openswap"
    )
    hint = _by_id(_rows({"enabled": False, "error": long_error}))["open_at_login_hint"]
    assert hint["label"] == long_error
    assert hint["lines"] >= 3


# --- the controller ----------------------------------------------------------


METHODS = {
    "_on_show_request",
    "_on_handoff_request",
    "_login_item_state",
    "_toggle_login_item",
    "_apply_login_item",
    "_login_item_worker",
    "_drain_login_item_result",
    "show_popover",
    "_on_setting",
}


class _Threads:
    """Stands in for ``threading``: records each worker instead of running it."""

    Lock = threading.Lock

    def __init__(self):
        self.started = []

    def Thread(self, target, args=(), daemon=None):  # noqa: N802 - mirrors threading.Thread
        thread = types.SimpleNamespace(target=target, args=args, daemon=daemon)
        thread.start = lambda: self.started.append(thread)
        return thread

    def run_all(self):
        while self.started:
            thread = self.started.pop(0)
            thread.target(*thread.args)


class _Panel:
    def __init__(self, shown=False, page=menubar.SETTINGS_PAGE):
        self.shown = shown
        self._page = page
        self.toggles = 0
        self.reloads = 0

    def is_shown(self):
        return self.shown

    def toggle(self):
        self.toggles += 1
        self.shown = not self.shown

    def reload(self):
        self.reloads += 1


@pytest.fixture
def app(monkeypatch, tmp_path):
    threads = _Threads()
    opened = []
    scope = dict(vars(menubar))
    scope["threading"] = threads
    scope["subprocess"] = types.SimpleNamespace(run=lambda argv, **k: opened.append(argv))
    instance = extract_class(menubar.__file__, "MenuBarApp", METHODS, scope)()
    logged = []
    instance.switcher = types.SimpleNamespace(
        backup_dir=tmp_path,
        _logger=types.SimpleNamespace(
            info=lambda *a, **k: logged.append(("info", a)),
            warning=lambda *a, **k: logged.append(("warning", a)),
            error=lambda *a, **k: logged.append(("error", a)),
        )
    )
    instance._event_lock = threading.Lock()
    instance._login_item_busy = False
    instance._login_item_from_welcome = False  # phase 2: only Settings' toggle here
    instance._login_item_result = None
    instance._login_item_last = {}
    instance._show_popover_pending = False
    instance._panel = _Panel(shown=True)
    instance.quits = 0
    instance.on_quit = lambda _sender: setattr(instance, "quits", instance.quits + 1)
    instance.threads = threads
    instance.opened = opened
    instance.logged = logged
    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: False)
    monkeypatch.setattr(launch_agent, "login_item_enabled", lambda home=None: False)
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    return instance


def _set_login_item(monkeypatch, outcome=None, raises=None):
    calls = []

    def fake(enabled, *, home=None, uid=None, backup_dir=None):
        calls.append(enabled)
        if raises is not None:
            raise raises
        return outcome

    monkeypatch.setattr(launch_agent, "set_login_item", fake)
    return calls


def test_the_switch_reads_the_plist_every_time_the_page_is_built(app, monkeypatch):
    assert app._login_item_state()["enabled"] is False
    monkeypatch.setattr(launch_agent, "login_item_enabled", lambda home=None: True)
    state = app._login_item_state()
    assert state == {
        "enabled": True, "temporary": False, "busy": False,
        "start_failed": False, "as_login_item": False,
    }


def test_the_page_reads_a_failed_start_from_the_status_file(app, monkeypatch, tmp_path):
    monkeypatch.setattr(launch_agent, "login_item_enabled", lambda home=None: True)
    launch_agent._record_start_failure("launchctl kickstart failed (exit 37)", tmp_path)
    state = app._login_item_state()
    assert state["start_failed"] is True
    assert "open_at_login_failed" in _by_id(_rows(state))
    monkeypatch.setenv("XPC_SERVICE_NAME", launch_agent.LABEL)
    assert app._login_item_state()["as_login_item"] is True


def test_switching_on_runs_off_the_main_thread_then_quits_for_launchds_copy(app, monkeypatch):
    calls = _set_login_item(monkeypatch, outcome="handoff")
    app._on_setting("open_at_login", None)

    # Nothing ran on the caller's thread: the work waits in a worker.
    assert calls == []
    assert len(app.threads.started) == 1 and app.threads.started[0].daemon is True
    assert app._login_item_state()["busy"] is True
    assert app._panel.reloads == 1  # the page shows the switch disabled

    app.threads.run_all()
    assert calls == [True]
    assert app.quits == 0  # only the main-thread drain acts on the result

    app._drain_login_item_result()
    assert app.quits == 1
    assert app._login_item_busy is False


def test_switching_off_from_the_login_item_shows_the_after_quit_hint(app, monkeypatch):
    monkeypatch.setattr(launch_agent, "login_item_enabled", lambda home=None: True)
    calls = _set_login_item(monkeypatch, outcome="disabled_after_quit")
    app._toggle_login_item()
    monkeypatch.setattr(launch_agent, "login_item_enabled", lambda home=None: False)
    app.threads.run_all()
    app._drain_login_item_result()

    assert calls == [False]
    assert app.quits == 0
    assert app._panel.reloads == 1
    state = app._login_item_state()
    assert state["outcome"] == "disabled_after_quit" and state["enabled"] is False
    hint = _by_id(_rows(state))["open_at_login_hint"]
    assert hint["label"] == menubar.OPEN_AT_LOGIN_AFTER_QUIT_HINT


def test_a_job_launchd_did_not_start_is_reported_by_the_warning_row(app, monkeypatch, tmp_path):
    def not_started(enabled, *, home=None, uid=None, backup_dir=None):
        # start_login_item records the reason before set_login_item raises.
        launch_agent._record_start_failure("no process after launchctl bootstrap", backup_dir)
        raise launch_agent.LoginItemNotStarted("no process after launchctl bootstrap")

    monkeypatch.setattr(launch_agent, "set_login_item", not_started)
    app._toggle_login_item()
    monkeypatch.setattr(launch_agent, "login_item_enabled", lambda home=None: True)  # the plist was written
    app.threads.run_all()
    app._drain_login_item_result()

    assert app.quits == 0
    rows = _by_id(_rows(app._login_item_state()))
    assert "open_at_login_failed" in rows
    assert "open_at_login_hint" not in rows  # one message, not two
    assert any(level == "warning" for level, _args in app.logged)

    app._on_setting("open_at_login_failed", menubar.LOGIN_ITEMS_SETTINGS_URL)
    assert app.opened == [["open", menubar.LOGIN_ITEMS_SETTINGS_URL]]


def test_a_refusal_is_shown_as_its_message(app, monkeypatch):
    _set_login_item(monkeypatch, raises=ClaudeSwitchError("Move OpenSwap to Applications first."))
    app._toggle_login_item()
    app.threads.run_all()
    app._drain_login_item_result()
    state = app._login_item_state()
    assert state["error"] == "Move OpenSwap to Applications first."


def test_an_unexpected_failure_is_logged_and_shown_plainly(app, monkeypatch):
    _set_login_item(monkeypatch, raises=RuntimeError("boom"))
    app._toggle_login_item()
    app.threads.run_all()
    app._drain_login_item_result()
    assert "openswap.log" in app._login_item_state()["error"]
    assert any(level == "error" for level, _args in app.logged)


def test_a_new_change_clears_the_last_note(app, monkeypatch):
    app._login_item_last = {"error": "old"}
    _set_login_item(monkeypatch, outcome="disabled")
    app._toggle_login_item()
    assert "error" not in app._login_item_state()


def test_apply_sets_the_asked_value_not_a_flip(app, monkeypatch):
    # The setup wizard's Finish applies its switch as is (plans 020, 021).
    monkeypatch.setattr(launch_agent, "login_item_enabled", lambda home=None: True)
    calls = _set_login_item(monkeypatch, outcome="handoff")
    app._apply_login_item(True)
    app.threads.run_all()
    assert calls == [True]


def test_no_second_change_while_one_is_running(app, monkeypatch):
    _set_login_item(monkeypatch, outcome="handoff")
    app._toggle_login_item()
    app._toggle_login_item()
    assert len(app.threads.started) == 1


def test_a_temporary_copy_never_starts_a_change(app, monkeypatch):
    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
    _set_login_item(monkeypatch, outcome="handoff")
    app._toggle_login_item()
    assert app.threads.started == []
    assert app._login_item_state()["temporary"] is True


def test_the_settings_page_reloads_only_when_it_is_the_one_shown(app, monkeypatch):
    _set_login_item(monkeypatch, outcome="enabled")
    app._panel = _Panel(shown=True, page=menubar.MAIN_PAGE)
    app._toggle_login_item()
    app.threads.run_all()
    app._drain_login_item_result()
    assert app._panel.reloads == 0


# --- show_popover ------------------------------------------------------------


def test_show_popover_opens_a_closed_popover(app):
    app._panel = _Panel(shown=False)
    app.show_popover()
    assert app._panel.toggles == 1 and app._panel.shown is True


def test_show_popover_leaves_an_open_popover_open(app):
    app._panel = _Panel(shown=True)
    app.show_popover()
    assert app._panel.toggles == 0


def test_show_popover_before_the_panel_attaches_waits_for_it(app):
    app._panel = None
    app.show_popover()
    assert app._show_popover_pending is True


def _quitting_app(quits, logged):
    scope = dict(vars(menubar))
    scope["rumps"] = types.SimpleNamespace(quit_application=lambda: quits.append(1))
    app = extract_class(menubar.__file__, "MenuBarApp", {"on_quit"}, scope)()
    app._status_icon_animator = None
    app._login_session = None
    app._claude_login = None
    app._stop_chatgpt_auto_monitor = lambda clear_pending: None
    app._stop_engine = lambda: None
    app.switcher = types.SimpleNamespace(
        _logger=types.SimpleNamespace(warning=lambda *a, **k: logged.append(a[0]))
    )
    return app


@pytest.fixture
def launchd_copy(monkeypatch, tmp_path):
    """This process as a launchd-started extra, with the real unload_own_job:
    only the detached bootout it would start is recorded."""
    monkeypatch.setattr(launch_agent.sys, "platform", "darwin")
    monkeypatch.setattr(launch_agent.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("XPC_SERVICE_NAME", launch_agent.LABEL)
    started = []
    monkeypatch.setattr(
        launch_agent.subprocess, "Popen", lambda argv, **kwargs: started.append((argv, kwargs))
    )
    return started


def test_a_fresh_launchd_copy_with_no_plist_unloads_its_job_on_quit(launchd_copy):
    """Login was turned off by an earlier copy; then the cask's kickstart -k
    (or KeepAlive) started this one. The rule is the plist, not who turned
    login off, so its Quit still leaves nothing loaded."""
    quits, logged = [], []
    assert not launch_agent.login_item_enabled()
    _quitting_app(quits, logged).on_quit(None)
    [(argv, kwargs)] = launchd_copy
    assert argv == ["launchctl", "bootout", f"gui/501/{launch_agent.LABEL}"]
    assert kwargs["start_new_session"] is True
    assert quits == [1] and logged == []


def test_a_launchd_copy_with_login_on_stays_loaded_after_quit(launchd_copy, tmp_path):
    plist = launch_agent.plist_path(home=tmp_path)
    plist.parent.mkdir(parents=True)
    plist.write_bytes(launch_agent.build_plist(["/Users/x/.local/bin/openswap"], home=tmp_path))
    quits = []
    _quitting_app(quits, []).on_quit(None)
    assert launchd_copy == [] and quits == [1]


def test_a_copy_launchd_did_not_start_never_unloads_on_quit(launchd_copy, monkeypatch):
    monkeypatch.delenv("XPC_SERVICE_NAME")
    quits = []
    _quitting_app(quits, []).on_quit(None)
    assert launchd_copy == [] and quits == [1]


def test_quit_unloads_the_job_only_through_unload_own_job(monkeypatch):
    """Quit calls it every time; it decides (job, plist gone) and never raises."""
    quits, unloads, logged = [], [], []
    app = _quitting_app(quits, logged)

    monkeypatch.setattr(launch_agent, "unload_own_job", lambda **k: unloads.append(1) or True)
    app.on_quit(None)
    assert unloads == [1] and quits == [1]

    def boom(**_k):
        raise OSError("launchctl missing")

    monkeypatch.setattr(launch_agent, "unload_own_job", boom)
    app.on_quit(None)
    assert quits == [1, 1]
    assert logged == ["could not unload the login item"]


def test_the_extra_listens_for_show_requests_and_reopen_events():
    from pathlib import Path

    text = Path(menubar.__file__).read_text(encoding="utf-8")
    # rumps.rumps.NSApp is rumps' NSObject delegate class, not the shared
    # NSApplication: rumps' App.run does `NSApp.alloc().init()` and
    # `setDelegate_` with it, and the panel registers menu callbacks on it.
    assert "handle_reopen(rumps.rumps.NSApp, self._on_reopen)" in text
    assert "observe_show_requests(SHOW_NOTIFICATION, self._on_show_request)" in text
    # One failing hook must not take the others down with it.
    init = text[text.index("def __init__(self):") : text.index("self.rebuild_menu()")]
    assert init.count("install()") == 1 and "for name, install in hooks" in init


def test_the_show_notification_opens_the_popover_once_per_request(app, tmp_path):
    app._panel = _Panel(shown=False)
    menubar_instance.leave_show_request(tmp_path, post=lambda: None)
    app._on_show_request()
    assert app._panel.toggles == 1
    # A second notification for the same request (or a stray one) does nothing.
    app._panel.shown = False
    app._on_show_request()
    assert app._panel.toggles == 1


def test_a_stale_show_request_does_not_open_the_popover(app, tmp_path, monkeypatch):
    app._panel = _Panel(shown=False)
    menubar_instance.leave_show_request(tmp_path, post=lambda: None)
    real = menubar_instance.consume_show_request
    monkeypatch.setattr(
        menubar_instance, "consume_show_request", lambda backup_dir: real(backup_dir, now=time.time() + 31)
    )
    app._on_show_request()
    assert app._panel.toggles == 0


def _attach(app, monkeypatch):
    """Run _attach_panel_once with a stand-in for the AppKit panel module."""
    panel = _Panel(shown=False)
    fake_panel = types.ModuleType("openswap.menubar_panel")
    fake_panel.MenuBarPanel = lambda **_kwargs: panel
    fake_panel.pin_status_item = lambda item: None
    fake_panel.fit_status_item = lambda item: None
    panel.attach = lambda item: None
    monkeypatch.setitem(sys.modules, "openswap.menubar_panel", fake_panel)
    attach_type = extract_class(
        menubar.__file__, "MenuBarApp",
        {"_attach_panel_once", "_on_show_request", "_on_handoff_request", "show_popover"},
        dict(vars(menubar)),
    )

    class App(attach_type):
        def __getattr__(self, name):  # every other callback the panel is wired to
            return lambda *a, **k: None

    attached = App()
    attached.switcher = app.switcher
    attached._nsapp = types.SimpleNamespace(nsstatusitem=object())
    attached._panel = None
    attached._show_popover_pending = False
    attached.quits = 0
    attached.on_quit = lambda _sender: setattr(attached, "quits", attached.quits + 1)
    attached._attach_panel_once(types.SimpleNamespace(stop=lambda: None))
    return attached, panel


def test_a_request_left_before_the_observer_existed_opens_the_popover_at_start_up(app, monkeypatch, tmp_path):
    """Another copy lost the lock (or handed over) before this one listened."""
    menubar_instance.leave_show_request(tmp_path, post=lambda: None)
    attached, panel = _attach(app, monkeypatch)
    assert panel.toggles == 1
    assert not menubar_instance.show_request_path(tmp_path).exists()
    # The notification that comes in afterwards finds the request gone.
    attached._on_show_request()
    assert panel.toggles == 1


# --- stepping aside for `openswap setup` ---------------------------------------


def test_a_handoff_request_makes_a_finder_extra_quit(app, monkeypatch, tmp_path):
    """Open at login was off; `openswap setup` then started launchd's copy,
    which waits for this copy's lock."""
    monkeypatch.setattr(menubar_instance, "login_item_waiting", lambda logger=None: 4242)
    menubar_instance.leave_handoff_request(tmp_path, post=lambda: None)
    assert app._on_handoff_request() is True
    assert app.quits == 1
    # launchd's copy opens its popover once it has the lock.
    assert menubar_instance.show_request_path(tmp_path).exists()
    assert not menubar_instance.handoff_request_path(tmp_path).exists()


def test_an_extra_stays_when_launchds_copy_is_not_up(app, monkeypatch, tmp_path):
    monkeypatch.setattr(menubar_instance, "login_item_waiting", lambda logger=None: None)
    menubar_instance.leave_handoff_request(tmp_path, post=lambda: None)
    assert app._on_handoff_request() is False
    assert app.quits == 0
    assert not menubar_instance.show_request_path(tmp_path).exists()


def test_no_request_no_check(app, monkeypatch):
    monkeypatch.setattr(
        menubar_instance, "login_item_waiting", lambda logger=None: pytest.fail("checked without a request")
    )
    assert app._on_handoff_request() is False
    assert app.quits == 0


def test_a_handoff_request_left_before_the_observer_existed_is_honoured_at_start_up(
    app, monkeypatch, tmp_path
):
    monkeypatch.setattr(menubar_instance, "login_item_waiting", lambda logger=None: 4242)
    menubar_instance.leave_handoff_request(tmp_path, post=lambda: None)
    menubar_instance.leave_show_request(tmp_path, post=lambda: None)
    attached, panel = _attach(app, monkeypatch)
    assert attached.quits == 1
    # It quits instead of showing; the show request waits for launchd's copy.
    assert panel.toggles == 0
    assert menubar_instance.show_request_path(tmp_path).exists()


def test_the_extra_listens_for_handoff_requests():
    from pathlib import Path

    text = Path(menubar.__file__).read_text(encoding="utf-8")
    assert "observe_show_requests(HANDOFF_NOTIFICATION, self._on_handoff_request)" in text


def test_start_up_without_a_request_leaves_the_popover_closed(app, monkeypatch):
    assert _attach(app, monkeypatch)[1].toggles == 0
