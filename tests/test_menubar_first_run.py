"""First run, widget and status line from the app (plan 020, phase 2).

No rumps or AppKit: the Settings and welcome models are pure, and controller
methods are lifted out of ``MenuBarApp`` with ``tests.menubar_harness``.
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


def test_this_mac_ends_general_with_the_status_line_then_the_widget_hint():
    rows = _general(status_line=False, bundled_widget=True)
    tail = _this_mac(rows)
    assert rows[-len(tail):] == tail  # the last group of General
    assert [row["id"] for row in tail][-2:] == ["claude_status_line", "widget_hint"]
    hint = tail[-1]
    assert hint["kind"] == "group" and hint["style"] == "hint"
    assert hint["label"] == "Add the widget: Edit Widgets, search OpenSwap"


def test_widget_hint_is_the_apps_only():
    """A uv install builds its widget with Xcode (`openswap widget --install`),
    and a translocated copy registers nothing."""
    ids = {row["id"] for row in _general(bundled_widget=False)}
    assert "widget_hint" not in ids
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
        assert not ids & {"group_this_mac", "claude_status_line", "widget_hint"}


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
        menubar.__file__, "MenuBarApp", {"_status_line_installed", "_toggle_status_line"}, scope
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
    assert app._status_line_installed() is False

    app._toggle_status_line()
    settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert settings["statusLine"]["command"] == "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline"
    assert app._status_line_installed() is True

    app._toggle_status_line()
    assert "statusLine" not in json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert app._status_line_installed() is False
    assert calls == [(claude, backup, True), (claude, backup, False)]
    assert errors == []


def test_status_line_toggle_flips_what_is_on_disk_now(tmp_path, monkeypatch):
    """`openswap statusline --install` may have run since the page was built."""
    app, claude, backup, _errors = _status_line_app(tmp_path, monkeypatch)
    from openswap import statusline

    statusline.set_installed(claude, backup, True)
    app._toggle_status_line()
    assert app._status_line_installed() is False


def test_status_line_toggle_reports_an_unreadable_settings_file(tmp_path, monkeypatch):
    app, claude, _backup, errors = _status_line_app(tmp_path, monkeypatch)
    (claude / "settings.json").write_text("{not json", encoding="utf-8")
    app._toggle_status_line()
    assert len(errors) == 1
    assert errors[0].startswith("Couldn't change the Claude Code status line:")
    assert (claude / "settings.json").read_text(encoding="utf-8") == "{not json"


# ---- the welcome page (plan 020 step 4) -------------------------------------


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
        # A translocated or mounted copy pins nothing and opens nothing.
        (True, False, True, False, "none"),
        # A uv install never shows it; `openswap setup` is that path.
        (False, False, False, False, "none"),
        (False, False, False, True, "none"),
    ],
)
def test_welcome_on_start(frozen, done, temporary, login_item, expected):
    settings = menubar.MenuBarSettings(welcome_done=done)
    assert menubar.welcome_on_start(
        settings, frozen=frozen, temporary=temporary, login_item_runs_this=login_item
    ) == expected


def test_welcome_with_no_accounts_offers_the_claude_account_step():
    model = menubar.welcome_page_model(claude_accounts=0)
    assert model["title"] == "Welcome to OpenSwap"
    assert model["body"]
    assert model["account"] == menubar.provider_empty_state("claude")
    assert model["account"]["button"] == "Sign in with Claude"
    assert model["account"]["secondary_button"] == "Save current login"
    # Open at login starts on, and nothing is written until Continue.
    assert model["open_at_login_label"] == "Open at login"
    assert model["open_at_login"] is True
    assert model["open_at_login_disabled"] is False
    assert model["hint"] == "Keeps OpenSwap in the menu bar after you restart."
    assert model["button"] == "Continue"


def test_welcome_with_accounts_drops_the_account_step():
    model = menubar.welcome_page_model(claude_accounts=2)
    assert model["account"] is None
    assert model["open_at_login"] is True


@pytest.mark.parametrize("state", ["loading", "error"])
def test_welcome_never_mistakes_an_unread_roster_for_an_empty_one(state):
    model = menubar.welcome_page_model(claude_accounts=0, claude_state=state)
    assert model["account"] == menubar.provider_empty_state("claude", state)
    assert model["account"]["button"] != "Sign in with Claude"


def test_welcome_from_a_temporary_location_cannot_open_at_login():
    model = menubar.welcome_page_model(claude_accounts=0, temporary=True, open_at_login=True)
    assert model["open_at_login"] is False
    assert model["open_at_login_disabled"] is True
    assert model["hint"] == menubar.OPEN_AT_LOGIN_MOVE_HINT  # as in Settings
    assert model["account"] is not None


def test_welcome_when_the_login_item_is_already_on():
    model = menubar.welcome_page_model(claude_accounts=1, login_item=True)
    assert model["open_at_login"] is True
    assert model["open_at_login_disabled"] is False
    assert model["hint"] == "OpenSwap already opens at login."


def test_welcome_keeps_the_users_pick_for_the_switch():
    model = menubar.welcome_page_model(claude_accounts=1, open_at_login=False)
    assert model["open_at_login"] is False
    assert model["open_at_login_disabled"] is False


# ---- the welcome page's controller ------------------------------------------

_WELCOME_METHODS = {
    "_welcome_state",
    "_login_item_runs_this",
    "_welcome_on_start",
    "_on_welcome_continue",
    # Phase 1's Open at login path, which Continue goes through.
    "_apply_login_item",
    "_login_item_worker",
    "_drain_login_item_result",
}


@pytest.fixture
def welcome_app(tmp_path, monkeypatch):
    """MenuBarApp's welcome methods over a fake app; the frozen bundle is
    faked per test (``frozen_app``) or left off (a uv install)."""
    import threading

    from openswap import bundle, launch_agent

    path = tmp_path / "menubar_settings.json"
    scope = {
        "ClaudeSwitchError": ClaudeSwitchError,
        "settings_path": path,
        "threading": threading,
        "welcome_on_start": menubar.welcome_on_start,
        "SETTINGS_PAGE": menubar.SETTINGS_PAGE,
        "OPEN_AT_LOGIN_BLOCKED_HINT": menubar.OPEN_AT_LOGIN_BLOCKED_HINT,
    }
    app = extract_class(menubar.__file__, "MenuBarApp", _WELCOME_METHODS, scope)()
    events: list = []
    app.settings = menubar.MenuBarSettings()
    app.switcher = SimpleNamespace(
        _logger=SimpleNamespace(
            debug=lambda *a, **k: None,
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
    )
    app._welcome_dismissed = False
    app._login_item_from_welcome = False
    app._login_item_busy = False
    app._login_item_last = {}
    app._login_item_result = None
    app._panel = None
    app._event_lock = threading.Lock()
    app._show_error = lambda message: events.append(("error", message))
    app.show_popover = lambda: events.append("show_popover")
    app.on_quit = lambda _sender: events.append("quit")
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


def test_first_launch_opens_the_popover_on_the_welcome_page(welcome_app, frozen_app):
    w = welcome_app
    w.app._welcome_on_start()
    assert w.events == ["show_popover"]
    assert not w.path.exists()  # nothing written until Continue
    assert w.app._welcome_state() == {"temporary": False, "login_item": False}


def test_after_openswap_setup_the_welcome_is_done_without_showing(welcome_app, frozen_app):
    w = welcome_app
    w.login["runs_this"] = True
    w.app._welcome_on_start()
    assert w.events == []
    assert json.loads(w.path.read_text())["welcome_done"] is True
    assert w.app._welcome_state() is None


def test_a_uv_install_never_shows_the_welcome(welcome_app, monkeypatch):
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    w = welcome_app
    w.app._welcome_on_start()
    assert w.events == []
    assert w.app._welcome_state() is None


def test_a_temporary_copy_does_not_open_by_itself(welcome_app, frozen_app):
    w = welcome_app
    w.temporary["value"] = True
    w.app._welcome_on_start()
    assert w.events == []
    # Opened by hand, it still shows the page, with Open at login disabled.
    assert w.app._welcome_state() == {"temporary": True, "login_item": False}


def test_welcome_state_reports_a_login_item_that_runs_this_app(welcome_app, frozen_app):
    # login_item_enabled() is true in the fixture: a plist for another copy
    # of the app is not this app's login item.
    assert welcome_app.app._welcome_state() == {"temporary": False, "login_item": False}
    welcome_app.login["runs_this"] = True
    assert welcome_app.app._welcome_state() == {"temporary": False, "login_item": True}


def test_an_unreadable_login_item_is_not_this_apps(welcome_app, frozen_app):
    w = welcome_app
    w.login["runs_this"] = OSError("Permission denied")
    assert w.app._welcome_state() == {"temporary": False, "login_item": False}
    w.app._welcome_on_start()
    assert w.events == ["show_popover"]


def test_continue_saves_welcome_done_then_turns_login_on(welcome_app, frozen_app):
    w = welcome_app
    w.app._on_welcome_continue(True)
    _drain(w.app)
    # set_login_item ran after welcome_done was on disk.
    assert w.login["set"] == [(True, True)]
    assert w.app._welcome_state() is None
    assert w.events == []


def test_continue_with_the_switch_off_turns_off_only_this_apps_login_item(welcome_app, frozen_app):
    w = welcome_app
    w.login["runs_this"] = True
    w.app._on_welcome_continue(False)
    _drain(w.app)
    assert w.login["set"] == [(False, True)]


def test_continue_with_the_switch_off_leaves_another_copys_login_item(welcome_app, frozen_app):
    """login_item_enabled() is true here, but the plist runs another copy (the
    cask's, while a dev build runs): turning it off would remove that one."""
    w = welcome_app
    w.app._on_welcome_continue(False)
    assert w.login["set"] == []
    assert w.app._login_item_busy is False
    assert json.loads(w.path.read_text())["welcome_done"] is True


def test_continue_hands_over_last(welcome_app, frozen_app):
    w = welcome_app
    w.login["result"] = "handoff"
    w.app._on_welcome_continue(True)
    _drain(w.app)
    assert json.loads(w.path.read_text())["welcome_done"] is True
    assert w.events == ["quit"]


def test_a_refused_login_item_is_an_alert_and_the_welcome_stays_done(welcome_app, frozen_app):
    """Continue went on to the cards, so Settings' hint alone would go unseen."""
    w = welcome_app
    w.login["result"] = ClaudeSwitchError("Move OpenSwap to Applications first.")
    w.app._on_welcome_continue(True)
    _drain(w.app)
    assert w.events == [("error", "Move OpenSwap to Applications first.")]
    assert json.loads(w.path.read_text())["welcome_done"] is True
    # Settings' This Mac still shows it under the switch.
    assert w.app._login_item_last == {"error": "Move OpenSwap to Applications first."}


def test_a_login_item_launchd_would_not_start_points_at_login_items(welcome_app, frozen_app):
    from openswap import launch_agent

    w = welcome_app
    w.login["result"] = launch_agent.LoginItemNotStarted("launchd did not start OpenSwap")
    w.app._on_welcome_continue(True)
    _drain(w.app)
    assert w.events == [("error", menubar.OPEN_AT_LOGIN_BLOCKED_HINT)]


def test_a_settings_toggle_failure_is_not_an_alert(welcome_app, frozen_app):
    """Only a change the welcome page started; Settings shows its own hint."""
    w = welcome_app
    w.login["result"] = ClaudeSwitchError("boom")
    w.app._apply_login_item(True)
    _drain(w.app)
    assert w.events == []
    assert w.app._login_item_last == {"error": "boom"}


def test_continue_from_a_temporary_copy_pins_nothing(welcome_app, frozen_app):
    """The copy moved to Applications shows the welcome page again."""
    w = welcome_app
    w.temporary["value"] = True
    w.app._on_welcome_continue(True)
    assert w.login["set"] == []
    assert not w.path.exists()
    assert w.app._welcome_state() is None  # not again in this run


def test_closing_without_continue_changes_nothing(welcome_app, frozen_app):
    w = welcome_app
    w.app._welcome_on_start()
    # No Continue: the next open is still the welcome page.
    assert w.app._welcome_state() is not None
    assert not w.path.exists()
    assert w.login["set"] == []


# ---- the welcome page in the panel (no AppKit) ------------------------------


def _welcome_panel(facts, on_continue=None):
    from unittest.mock import Mock

    methods = {
        "__init__", "toggle", "close", "_release_token_field", "is_shown",
        "_welcome_facts", "_welcome_switch", "_welcome_continue",
    }
    namespace = {
        "MAIN_PAGE": menubar.MAIN_PAGE,
        "WELCOME_PAGE": menubar.WELCOME_PAGE,
        "SETTINGS_SECTION_GENERAL": menubar.SETTINGS_SECTION_GENERAL,
    }
    panel_type = extract_class(
        Path(menubar.__file__).with_name("menubar_panel.py"), "MenuBarPanel", methods, namespace
    )
    state = {"facts": facts, "shown": False, "reloads": []}
    panel = panel_type(
        on_switch=Mock(), on_rotate=Mock(), on_best=Mock(), on_toggle_auto=Mock(),
        on_more=Mock(), auto_enabled=lambda: False, snapshot=lambda: {}, threshold=lambda: 90,
        welcome=lambda: state["facts"], on_welcome_continue=on_continue,
    )
    popover = Mock()
    popover.isShown.side_effect = lambda: state["shown"]
    popover.performClose_.side_effect = lambda _s: state.update(shown=False)
    panel._popover = popover
    panel._item = Mock(button=lambda: None)
    panel._sync_popover_appearance = lambda: None
    panel._clear_dismiss_watchers = lambda: None
    panel.reload = lambda: state["reloads"].append(panel._page)
    return panel, state


def test_every_open_starts_on_the_welcome_page_until_continue():
    panel, state = _welcome_panel({"temporary": False, "login_item": False})
    panel.toggle()
    assert panel._page == menubar.WELCOME_PAGE
    state["shown"] = True
    panel.toggle()  # close without Continue
    assert panel._page == menubar.MAIN_PAGE
    panel.toggle()
    assert panel._page == menubar.WELCOME_PAGE


def test_without_a_due_welcome_the_popover_opens_on_the_cards():
    panel, _state = _welcome_panel(None)
    panel.toggle()
    assert panel._page == menubar.MAIN_PAGE


def test_continue_passes_the_switch_and_returns_to_the_cards():
    picks: list[bool] = []
    panel, state = _welcome_panel({"temporary": False, "login_item": False}, picks.append)
    panel.toggle()
    state["shown"] = True
    panel._welcome_continue()
    assert picks == [True]  # the switch starts on
    assert panel._page == menubar.MAIN_PAGE
    assert state["reloads"][-1] == menubar.MAIN_PAGE

    panel._welcome_switch(SimpleNamespace(state=lambda: 0))
    panel._welcome_continue()
    assert picks == [True, False]


def test_continue_from_a_temporary_copy_never_asks_for_the_login_item():
    picks: list[bool] = []
    panel, _state = _welcome_panel({"temporary": True, "login_item": False}, picks.append)
    panel._welcome_continue()
    assert picks == [False]


def test_a_failing_welcome_read_is_no_welcome():
    panel, _state = _welcome_panel(None)
    panel._welcome = lambda: 1 / 0
    panel.toggle()
    assert panel._page == menubar.MAIN_PAGE


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
    assert app._status_line_installed() is False
