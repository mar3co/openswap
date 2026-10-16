"""First run, widget and status line from the app (plans 020 and 021).

No rumps or AppKit: the Settings model is pure, the setup wizard's pages are
covered in ``tests/test_setup_wizard.py``, and controller methods are lifted
out of ``MenuBarApp`` with ``tests.menubar_harness``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar
from openswap.exceptions import ClaudeSwitchError
from tests.menubar_harness import extract_class


def _general(**kwargs) -> list[dict]:
    return menubar.settings_page_rows(
        menubar.MenuBarSettings(),
        strategy="best",
        threshold=90,
        section=menubar.SETTINGS_SECTION_GENERAL,
        **kwargs,
    )


def _this_mac(rows: list[dict]) -> list[dict]:
    ids = [row["id"] for row in rows]
    return rows[ids.index("group_this_mac"):]


# ---- Settings › General › This Mac -----------------------------------------


@pytest.mark.parametrize("installed", [True, False])
def test_status_line_toggle_shows_what_is_on_disk(installed):
    rows = {row["id"]: row for row in _general(status_line=installed)}
    row = rows["claude_status_line"]
    assert row["kind"] == "toggle"
    assert row["label"] == "Claude Code status line"
    assert row["value"] is installed
    assert row["section"] == menubar.SETTINGS_SECTION_GENERAL


def test_this_mac_ends_general_with_the_status_line_the_widget_hint_and_setup():
    rows = _general(status_line=False, bundled_widget=True)
    tail = _this_mac(rows)
    assert rows[-len(tail):] == tail  # the last group of General
    assert [row["id"] for row in tail][-3:] == ["claude_status_line", "widget_hint", "setup_assistant"]
    hint = tail[-2]
    assert hint["kind"] == "group" and hint["style"] == "hint"
    assert hint["label"] == "Add the widget: Edit Widgets, search OpenSwap"
    setup = tail[-1]
    assert setup["kind"] == "button"
    assert (setup["label"], setup["title"]) == ("Setup assistant", "Open")


def test_widget_hint_and_setup_are_the_apps_only():
    """A uv install builds its widget with Xcode (`openswap widget --install`)
    and sets up with `openswap setup`; a translocated copy pins nothing."""
    ids = {row["id"] for row in _general(bundled_widget=False)}
    assert "widget_hint" not in ids
    assert "setup_assistant" not in ids
    assert "claude_status_line" in ids


def test_this_mac_stays_out_of_the_other_sections():
    for section in (menubar.SETTINGS_SECTION_AUTOMATION, menubar.SETTINGS_SECTION_REMOTE):
        rows = menubar.settings_page_rows(
            menubar.MenuBarSettings(),
            strategy="best",
            threshold=90,
            status_line=True,
            bundled_widget=True,
            section=section,
        )
        ids = {row["id"] for row in rows}
        assert not ids & {"group_this_mac", "claude_status_line", "widget_hint", "setup_assistant"}


def test_bundled_widget_means_the_app_at_a_stable_location(frozen_app, monkeypatch):
    from openswap import bundle

    assert menubar._bundled_widget() is True
    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
    assert menubar._bundled_widget() is False


def test_bundled_widget_is_false_in_a_uv_install(monkeypatch):
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert menubar._bundled_widget() is False


# ---- the status line toggle's controller ------------------------------------


def _status_line_app(tmp_path: Path, monkeypatch):
    errors: list[str] = []
    scope = {"ClaudeSwitchError": ClaudeSwitchError}
    app = extract_class(
        menubar.__file__, "MenuBarApp", {"_status_line_state", "_toggle_status_line"}, scope
    )()
    backup = tmp_path / "OpenSwap"
    backup.mkdir()
    app.switcher = SimpleNamespace(backup_dir=backup, _logger=SimpleNamespace(debug=lambda *a, **k: None))
    app._show_error = errors.append
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setattr("openswap.paths.get_claude_config_home", lambda: claude)
    monkeypatch.setattr(
        "openswap.statusline.paint_command",
        lambda: "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline",
    )
    return app, claude, backup, errors


def test_status_line_toggle_wraps_and_restores_through_the_shared_function(tmp_path, monkeypatch):
    app, claude, backup, errors = _status_line_app(tmp_path, monkeypatch)
    calls: list[tuple] = []
    from openswap import statusline

    real = statusline.set_installed
    monkeypatch.setattr(
        statusline, "set_installed", lambda *a: calls.append(a) or real(*a)
    )
    assert app._status_line_state() == "off"

    app._toggle_status_line()
    settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert settings["statusLine"]["command"] == "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline"
    assert app._status_line_state() == "on"

    app._toggle_status_line()
    assert "statusLine" not in json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert app._status_line_state() == "off"
    assert calls == [(claude, backup, True), (claude, backup, False)]
    assert errors == []


def test_status_line_toggle_flips_what_is_on_disk_now(tmp_path, monkeypatch):
    """`openswap statusline --install` may have run since the page was built."""
    app, claude, backup, _errors = _status_line_app(tmp_path, monkeypatch)
    from openswap import statusline

    statusline.set_installed(claude, backup, True)
    app._toggle_status_line()
    assert app._status_line_state() == "off"


def test_status_line_toggle_reports_an_unreadable_settings_file(tmp_path, monkeypatch):
    app, claude, _backup, errors = _status_line_app(tmp_path, monkeypatch)
    (claude / "settings.json").write_text("{not json", encoding="utf-8")
    app._toggle_status_line()
    assert len(errors) == 1
    assert errors[0].startswith("Couldn't change the Claude Code status line:")
    assert (claude / "settings.json").read_text(encoding="utf-8") == "{not json"


# ---- when the setup wizard opens (plans 020 step 4, 021) --------------------


def test_welcome_done_is_persisted_and_defaults_off(tmp_path):
    path = tmp_path / "menubar_settings.json"
    assert menubar.MenuBarSettings().welcome_done is False
    settings = menubar.MenuBarSettings.load(path)
    settings.welcome_done = True
    settings.save(path)
    assert menubar.MenuBarSettings.load(path).welcome_done is True
    path.write_text(json.dumps({"welcome_done": "yes"}), encoding="utf-8")
    assert menubar.MenuBarSettings.load(path).welcome_done is False


@pytest.mark.parametrize(
    "frozen, done, temporary, login_item, expected",
    [
        (True, False, False, False, "show"),
        # `openswap setup` already ran: the login item runs this app.
        (True, False, False, True, "done"),
        (True, True, False, False, "none"),
        # A disk image or download copy opens on Move, set up or not: a
        # newer DMG is how a Finder install updates.
        (True, False, True, False, "move"),
        (True, True, True, False, "move"),
        (True, True, True, True, "move"),
        # A uv install never shows it; `openswap setup` is that path.
        (False, False, False, False, "none"),
        (False, False, False, True, "none"),
        (False, False, True, False, "none"),
    ],
)
def test_welcome_on_start(frozen, done, temporary, login_item, expected):
    settings = menubar.MenuBarSettings(welcome_done=done)
    assert menubar.welcome_on_start(
        settings, frozen=frozen, temporary=temporary, login_item_runs_this=login_item
    ) == expected


# ---- the wizard's controller ---------------------------------------------------

_WELCOME_METHODS = {
    "_login_item_runs_this",
    "_welcome_on_start",
    "_on_welcome_continue",
    "_on_reopen",
    # Phase 1's Open at login path, which Finish goes through.
    "_apply_login_item",
    "_login_item_worker",
    "_drain_login_item_result",
}


@pytest.fixture
def welcome_app(tmp_path, monkeypatch):
    """MenuBarApp's first-run methods over a fake app; the frozen bundle is
    faked per test (``frozen_app``) or left off (a uv install). Opening the
    wizard is recorded, not drawn."""
    import threading

    from openswap import bundle, launch_agent, menubar_instance

    path = tmp_path / "menubar_settings.json"
    scope = {
        "ClaudeSwitchError": ClaudeSwitchError,
        "settings_path": path,
        "threading": threading,
        "welcome_on_start": menubar.welcome_on_start,
        "SETTINGS_PAGE": menubar.SETTINGS_PAGE,
        "OPEN_AT_LOGIN_FAILED_ALERT": menubar.OPEN_AT_LOGIN_FAILED_ALERT,
    }
    app = extract_class(menubar.__file__, "MenuBarApp", _WELCOME_METHODS, scope)()
    events: list = []
    app.settings = menubar.MenuBarSettings()
    app.switcher = SimpleNamespace(
        backup_dir=tmp_path,
        _logger=SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
    )
    app._login_item_from_welcome = False
    app._login_item_busy = False
    app._login_item_last = {}
    app._login_item_result = None
    app._panel = None
    app._event_lock = threading.Lock()
    app._show_error = lambda message: events.append(("error", message))
    app.show_popover = lambda: events.append("show_popover")
    app._open_setup_wizard = lambda step=None: events.append(("wizard", step))
    app.on_quit = lambda _sender: events.append("quit")
    monkeypatch.setattr(
        menubar_instance, "leave_show_request",
        lambda backup_dir, **_k: events.append("show_request"),
    )
    login = {"runs_this": False, "set": [], "result": "enabled"}

    def set_login_item(enabled, **_k):
        login["set"].append((enabled, path.exists() and json.loads(path.read_text())["welcome_done"]))
        if isinstance(login["result"], Exception):
            raise login["result"]
        return login["result"]

    def runs_this(home=None):
        if isinstance(login["runs_this"], Exception):
            raise login["runs_this"]
        return login["runs_this"]

    # A plist that exists but runs another program is not "already on".
    monkeypatch.setattr(launch_agent, "login_item_enabled", lambda home=None: True, raising=False)
    monkeypatch.setattr(launch_agent, "login_item_runs_this", runs_this, raising=False)
    monkeypatch.setattr(launch_agent, "set_login_item", set_login_item, raising=False)
    temporary = {"value": False}
    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: temporary["value"])
    return SimpleNamespace(app=app, events=events, path=path, login=login, temporary=temporary)


def _drain(app):
    import time

    deadline = time.monotonic() + 5
    while app._login_item_result is None and time.monotonic() < deadline:
        time.sleep(0.01)
    app._drain_login_item_result()


def _done(w) -> bool:
    return w.path.exists() and json.loads(w.path.read_text())["welcome_done"] is True


def test_first_launch_opens_the_wizard(welcome_app, frozen_app):
    w = welcome_app
    w.app._welcome_on_start()
    assert w.events == [("wizard", None)]
    assert not w.path.exists()  # nothing written until Finish


def test_after_openswap_setup_the_wizard_is_done_without_showing(welcome_app, frozen_app):
    w = welcome_app
    w.login["runs_this"] = True
    w.app._welcome_on_start()
    assert w.events == []
    assert _done(w)


def test_a_uv_install_never_shows_the_wizard(welcome_app, monkeypatch):
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    w = welcome_app
    w.app._welcome_on_start()
    w.app._on_reopen()
    assert w.events == ["show_popover"]


@pytest.mark.parametrize("done", [False, True])
def test_a_temporary_copy_opens_the_wizard_on_move(welcome_app, frozen_app, done):
    w = welcome_app
    w.temporary["value"] = True
    w.app.settings.welcome_done = done
    w.app._welcome_on_start()
    assert w.events == [("wizard", None)]
    assert w.login["set"] == []


def test_an_unreadable_login_item_is_not_this_apps(welcome_app, frozen_app):
    w = welcome_app
    w.login["runs_this"] = OSError("Permission denied")
    w.app._welcome_on_start()
    assert w.events == [("wizard", None)]


def test_reopening_brings_the_wizard_back_until_it_is_finished(welcome_app, frozen_app):
    w = welcome_app
    w.app._on_reopen()
    assert w.events == [("wizard", None)]
    w.app.settings.welcome_done = True
    w.app._on_reopen()
    assert w.events[-1] == "show_popover"
    w.temporary["value"] = True
    w.app._on_reopen()
    assert w.events[-1] == ("wizard", None)


def test_finish_saves_welcome_done_then_turns_login_on_and_opens_the_popover(welcome_app, frozen_app):
    w = welcome_app
    w.app._on_welcome_continue(True)
    _drain(w.app)
    # set_login_item ran after welcome_done was on disk.
    assert w.login["set"] == [(True, True)]
    assert w.events == ["show_popover"]


def test_finish_with_the_switch_off_turns_off_only_this_apps_login_item(welcome_app, frozen_app):
    w = welcome_app
    w.login["runs_this"] = True
    w.app._on_welcome_continue(False)
    _drain(w.app)
    assert w.login["set"] == [(False, True)]


def test_finish_with_the_switch_off_leaves_another_copys_login_item(welcome_app, frozen_app):
    """login_item_enabled() is true here, but the plist runs another copy (the
    cask's, while a dev build runs): turning it off would remove that one."""
    w = welcome_app
    w.app._on_welcome_continue(False)
    assert w.login["set"] == []
    assert w.app._login_item_busy is False
    assert _done(w)


def test_finish_hands_over_last_and_asks_the_login_item_for_the_popover(welcome_app, frozen_app):
    w = welcome_app
    w.login["result"] = "handoff"
    w.app._on_welcome_continue(True)
    _drain(w.app)
    assert _done(w)
    assert w.events == ["show_request", "quit"]


def test_a_settings_hand_over_leaves_no_show_request(welcome_app, frozen_app):
    w = welcome_app
    w.login["result"] = "handoff"
    w.app._apply_login_item(True)
    _drain(w.app)
    assert w.events == ["quit"]


def test_a_refused_login_item_is_an_alert_and_setup_stays_done(welcome_app, frozen_app):
    """The wizard has closed, so Settings' hint alone would go unseen."""
    w = welcome_app
    w.login["result"] = ClaudeSwitchError("Move OpenSwap to Applications first.")
    w.app._on_welcome_continue(True)
    _drain(w.app)
    assert w.events == [("error", "Move OpenSwap to Applications first.")]
    assert _done(w)
    # Settings' This Mac still shows it under the switch.
    assert w.app._login_item_last == {"error": "Move OpenSwap to Applications first."}


def test_a_login_item_launchd_would_not_start_points_at_login_items(welcome_app, frozen_app):
    from openswap import launch_agent

    w = welcome_app
    w.login["result"] = launch_agent.LoginItemNotStarted("launchd did not start OpenSwap")
    w.app._on_welcome_continue(True)
    _drain(w.app)
    assert w.events == [("error", menubar.OPEN_AT_LOGIN_FAILED_ALERT)]
    assert w.events[0][1].startswith("macOS isn’t starting OpenSwap at login.")


def test_a_settings_toggle_failure_is_not_an_alert(welcome_app, frozen_app):
    """Only a change the wizard started; Settings shows its own hint."""
    w = welcome_app
    w.login["result"] = ClaudeSwitchError("boom")
    w.app._apply_login_item(True)
    _drain(w.app)
    assert w.events == []
    assert w.app._login_item_last == {"error": "boom"}


def test_finish_from_a_temporary_copy_pins_nothing(welcome_app, frozen_app):
    """The wizard only offers Move there; even so, nothing is pinned."""
    w = welcome_app
    w.temporary["value"] = True
    w.app._on_welcome_continue(True)
    assert w.login["set"] == []
    assert not w.path.exists()


def test_closing_without_finishing_changes_nothing(welcome_app, frozen_app):
    w = welcome_app
    w.app._welcome_on_start()
    assert not w.path.exists()
    assert w.login["set"] == []
    # The next launch opens it again.
    w.app._welcome_on_start()
    assert w.events == [("wizard", None), ("wizard", None)]


# ---- the status line from a temporary copy (plan 020 step 6, revised) -------


def _status_rows(installed, temporary):
    rows = _general(status_line=installed, login_item={"enabled": False, "temporary": temporary})
    ids = [row["id"] for row in rows]
    return rows, ids


def test_a_temporary_copy_cannot_turn_the_status_line_on():
    """paint_command() would pin a path that disappears."""
    rows, ids = _status_rows(installed=False, temporary=True)
    by_id = {row["id"]: row for row in rows}
    assert by_id["claude_status_line"]["value"] is False
    assert by_id["claude_status_line"]["disabled"] is True
    at = ids.index("claude_status_line")
    hint = rows[at + 1]
    assert hint["id"] == "claude_status_line_hint"
    assert hint["style"] == "hint"
    assert hint["label"] == menubar.STATUS_LINE_MOVE_HINT
    assert hint["label"].startswith("Move OpenSwap to Applications")
    # Open at login says the same thing above it.
    assert by_id["open_at_login_hint"]["label"] == menubar.OPEN_AT_LOGIN_MOVE_HINT


def test_a_temporary_copy_can_still_turn_the_status_line_off():
    rows, ids = _status_rows(installed=True, temporary=True)
    toggle = {row["id"]: row for row in rows}["claude_status_line"]
    assert toggle["value"] is True and toggle["disabled"] is False
    assert "claude_status_line_hint" not in ids


@pytest.mark.parametrize("installed", [True, False])
def test_a_stable_copy_switches_the_status_line_both_ways(installed):
    rows, ids = _status_rows(installed=installed, temporary=False)
    assert {row["id"]: row for row in rows}["claude_status_line"]["disabled"] is False
    assert "claude_status_line_hint" not in ids


def test_the_toggle_refuses_to_turn_on_from_a_temporary_copy(tmp_path, monkeypatch):
    from openswap import bundle

    app, claude, _backup, errors = _status_line_app(tmp_path, monkeypatch)
    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
    app._toggle_status_line()
    assert errors == ["Couldn't change the Claude Code status line: Move OpenSwap to Applications first."]
    assert not (claude / "settings.json").exists()


def test_the_toggle_turns_off_from_a_temporary_copy(tmp_path, monkeypatch):
    from openswap import bundle, statusline

    app, claude, backup, errors = _status_line_app(tmp_path, monkeypatch)
    statusline.set_installed(claude, backup, True)
    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
    app._toggle_status_line()
    assert errors == []
    assert app._status_line_state() == "off"



# ---- a status line hook for another OpenSwap (plan 020 step 6, revised) -----


def test_a_hook_for_another_openswap_shows_off_with_a_hint():
    rows = _general(status_line=False, status_line_other=True)
    ids = [row["id"] for row in rows]
    toggle = rows[ids.index("claude_status_line")]
    assert toggle["value"] is False and toggle["disabled"] is False
    hint = rows[ids.index("claude_status_line") + 1]
    assert hint["id"] == "claude_status_line_hint"
    assert hint["label"] == menubar.STATUS_LINE_OTHER_HINT
    assert "another OpenSwap" in hint["label"]


def test_a_temporary_copy_says_to_move_before_it_can_repoint():
    rows = _general(
        status_line=False, status_line_other=True, login_item={"enabled": False, "temporary": True}
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id["claude_status_line"]["disabled"] is True
    assert by_id["claude_status_line_hint"]["label"] == menubar.STATUS_LINE_MOVE_HINT


def _hook(claude: Path, command: str) -> None:
    (claude / "settings.json").write_text(
        json.dumps({"statusLine": {"type": "command", "command": command}}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "hook",
    [
        "/Users/x/.local/bin/openswap statusline",  # a uv install's
        "openswap statusline",  # bare: PATH decides which openswap runs
    ],
    ids=["another-program", "bare"],
)
def test_a_hook_for_another_program_reads_as_other_and_turning_on_repoints_it(
    tmp_path, monkeypatch, hook
):
    from openswap import statusline

    app, claude, backup, errors = _status_line_app(tmp_path, monkeypatch)
    _hook(claude, hook)
    statusline.save_wrap(backup, inner_command="~/.claude/statusline.sh", created=False)
    assert app._status_line_state() == "other"

    app._toggle_status_line()
    assert errors == []
    settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert settings["statusLine"]["command"] == "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline"
    assert app._status_line_state() == "on"
    # The wrapped command survives the repoint.
    assert statusline.load_wrap(backup)["innerCommand"] == "~/.claude/statusline.sh"


# ---- start-up order: the welcome is settled before a show request ------------


def _attach_at_start(tmp_path, monkeypatch, *, runs_this):
    """Run the real _attach_panel_once with a stand-in AppKit panel module.

    Returns the app and the list of ``welcome_done`` values seen each time
    the popover opened.
    """
    import sys as _sys
    import threading
    import types

    from openswap import bundle, launch_agent

    monkeypatch.setattr(launch_agent, "login_item_runs_this", lambda home=None: runs_this)
    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: False)
    path = tmp_path / "menubar_settings.json"
    opened_with: list[bool] = []

    class _FakePanel:
        def __init__(self):
            self.shown = False

        def attach(self, _item):
            pass

        def is_shown(self):
            return self.shown

        def toggle(self):
            opened_with.append(attached.settings.welcome_done)
            self.shown = not self.shown

    fake_module = types.ModuleType("openswap.menubar_panel")
    fake_module.MenuBarPanel = lambda **_kwargs: _FakePanel()
    fake_module.pin_status_item = lambda item: None
    fake_module.fit_status_item = lambda item: None
    monkeypatch.setitem(_sys.modules, "openswap.menubar_panel", fake_module)
    scope = dict(vars(menubar))
    scope["settings_path"] = path
    attach_type = extract_class(
        menubar.__file__,
        "MenuBarApp",
        {
            "_attach_panel_once", "_on_show_request", "_on_handoff_request", "show_popover",
            "_welcome_on_start", "_login_item_runs_this",
        },
        scope,
    )

    class App(attach_type):
        def __getattr__(self, name):  # every other callback the panel is wired to
            return lambda *a, **k: None

    attached = App()
    attached.switcher = SimpleNamespace(
        backup_dir=tmp_path,
        _logger=SimpleNamespace(debug=lambda *a, **k: None, info=lambda *a, **k: None,
                                warning=lambda *a, **k: None),
    )
    attached.settings = menubar.MenuBarSettings()
    attached._nsapp = SimpleNamespace(nsstatusitem=object())
    attached._panel = None
    attached._show_popover_pending = False
    attached._event_lock = threading.Lock()
    return attached, opened_with, path


def test_a_copy_setup_started_opens_a_left_show_request_on_the_cards(frozen_app, tmp_path, monkeypatch):
    """`openswap setup` took over from a Finder copy, which left a show request:
    setup is done before that request opens the popover."""
    from openswap import menubar_instance

    attached, opened_with, path = _attach_at_start(tmp_path, monkeypatch, runs_this=True)
    menubar_instance.leave_show_request(tmp_path, post=lambda: None)
    attached._attach_panel_once(SimpleNamespace(stop=lambda: None))
    assert opened_with == [True]  # the popover opened, after welcome_done
    assert json.loads(path.read_text())["welcome_done"] is True


def test_a_first_launch_opens_the_wizard(frozen_app, tmp_path, monkeypatch):
    attached, opened_with, path = _attach_at_start(tmp_path, monkeypatch, runs_this=False)
    wizard = []
    attached._open_setup_wizard = lambda step=None: wizard.append(step)
    attached._attach_panel_once(SimpleNamespace(stop=lambda: None))
    assert wizard == [None]
    assert opened_with == []  # the popover waits for the wizard
    assert not path.exists()


def test_a_hook_the_toggle_cannot_repoint_is_an_alert_and_stays_as_written(tmp_path, monkeypatch):
    """Shell syntax the rewrite does not take apart: the toggle reports the
    exact command to use, changes nothing, and the page still reads `other`."""
    app, claude, _backup, errors = _status_line_app(tmp_path, monkeypatch)
    hook = "echo $(date) && /Users/x/.local/bin/openswap statusline"
    _hook(claude, hook)
    assert app._status_line_state() == "other"
    app._toggle_status_line()
    assert len(errors) == 1
    assert errors[0].startswith("Couldn't change the Claude Code status line:")
    assert "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline" in errors[0]
    assert json.loads((claude / "settings.json").read_text(encoding="utf-8"))["statusLine"]["command"] == hook
    assert app._status_line_state() == "other"
