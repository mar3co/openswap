"""Exercise panel navigation without requiring AppKit on CI."""

from pathlib import Path
import threading
from unittest.mock import Mock

import pytest

from openswap import menubar
from tests.menubar_harness import extract_class


def _panel():
    methods = {
        "__init__", "_select_provider", "_show_settings", "_show_main",
        "_select_settings_section", "close",
    }
    namespace = {
        "MAIN_PAGE": "main",
        "SETTINGS_PAGE": "settings",
        "SETTINGS_SECTION_GENERAL": "general",
        "SETTINGS_SECTION_AUTOMATION": "automation",
        "SETTINGS_SECTIONS": (("general", "General"), ("automation", "Automation")),
    }
    panel_type = extract_class(
        Path(menubar.__file__).with_name("menubar_panel.py"), "MenuBarPanel", methods, namespace
    )
    actions = {key: Mock() for key in (
        "on_switch", "on_rotate", "on_best", "on_toggle_auto", "on_more"
    )}
    panel = panel_type(
        **actions, auto_enabled=lambda: False, snapshot=lambda: {}, threshold=lambda: 90
    )
    panel.is_shown = Mock(return_value=True)
    panel.reload = Mock()
    panel._clear_dismiss_watchers = Mock()
    return panel, actions


def test_provider_navigation_never_calls_account_or_policy_actions():
    panel, actions = _panel()
    assert panel._selected_provider == "claude"
    panel._select_provider("chatgpt")
    assert panel._selected_provider == "chatgpt"
    panel.reload.assert_called_once()
    for action in actions.values():
        action.assert_not_called()


def test_selected_provider_survives_close_and_settings():
    panel, _ = _panel()
    panel._select_provider("chatgpt")
    panel._show_settings()
    assert panel._page == "settings"
    assert panel._settings_section == "automation"
    panel._on_chatgpt_view_active = Mock()
    panel._show_main()
    assert panel._page == "main"
    assert panel._selected_provider == "chatgpt"
    panel._on_chatgpt_view_active.assert_called_once()
    panel.close()
    assert panel._selected_provider == "chatgpt"


def test_settings_sections_are_navigation_only():
    panel, actions = _panel()
    panel._show_settings()
    assert panel._settings_section == "general"
    panel.reload.reset_mock()

    panel._select_settings_section("automation")

    assert panel._settings_section == "automation"
    panel.reload.assert_called_once()
    for action in actions.values():
        action.assert_not_called()


def test_chatgpt_tab_notifies_capability_probe_without_switching():
    panel, actions = _panel()
    panel._on_chatgpt_view_active = Mock()
    panel._select_provider("chatgpt")
    panel._on_chatgpt_view_active.assert_called_once()
    actions["on_switch"].assert_not_called()


def test_card_activation_paths_share_one_controller_entry():
    source = Path(menubar.__file__).with_name("menubar_panel.py").read_text(encoding="utf-8")
    assert "def _activate_card(self):" in source
    assert "def accessibilityPerformPress(self):" in source
    assert "def keyDown_(self, event):" in source
    assert source.index("self._activate_card()") < source.index("def accessibilityPerformPress")
    assert "accessibilityPerformPress" in source
    assert "self.on_switch(self.card[\"num\"])" in source
    build = source[source.index("def _build") : source.index("def _build_settings")]
    assert "apply_chatgpt_activation" in build
    assert "activation_disabled" in build


def test_activate_card_noops_when_activation_is_disabled():
    panel_path = Path(menubar.__file__).with_name("menubar_panel.py")
    view_type = extract_class(
        panel_path,
        "_CardView",
        {"_activate_card", "acceptsFirstResponder", "canBecomeKeyView"},
        {},
    )
    view = view_type()
    assert view.acceptsFirstResponder() is True
    assert view.canBecomeKeyView() is True
    view.on_switch = Mock()
    view.card = {"num": "codex:1", "disabled": False, "activation_disabled": True}
    view._activate_card()
    view.on_switch.assert_not_called()
    view.card["activation_disabled"] = False
    view._activate_card()
    view.on_switch.assert_called_once_with("codex:1")

    view.on_switch.reset_mock()
    view.card = menubar.apply_chatgpt_activation(
        [{"num": "codex:1", "disabled": False}],
        menubar.MenuBarSettings(chatgpt_switching_enabled=False),
        menubar.DesktopCapability("stopped", "stopped", observed_at=1.0),
        now=1.0,
    )[0]
    view._activate_card()
    view.on_switch.assert_called_once_with("codex:1")


def test_unknown_provider_does_not_change_view_or_reload():
    panel, _ = _panel()
    panel._select_provider("unknown")
    assert panel._selected_provider == "claude"
    panel.reload.assert_not_called()


def test_hidden_panel_selection_does_not_rebuild_native_views():
    panel, _ = _panel()
    panel.is_shown.return_value = False
    panel._select_provider("chatgpt")
    assert panel._selected_provider == "chatgpt"
    panel.reload.assert_not_called()


@pytest.mark.parametrize("provider,name", [("claude", "Claude"), ("chatgpt", "ChatGPT")])
def test_empty_state_gives_provider_specific_add_guidance(provider, name):
    state = menubar.provider_empty_state(provider)
    assert name in state["title"]
    if provider == "chatgpt":
        assert state["body"] == "Add an account without signing out."
        assert state["action"] == "start"
        assert state["button"] == "Sign in with ChatGPT"
        assert state["secondary_action"] == "capture"
        assert state["secondary_button"] == "Save current login"
    else:
        assert "Sign in" in state["body"]
        assert state["action"] == "add"
        assert state["button"] == "Add current login"
        assert "Does not switch" in state["hint"]


@pytest.mark.parametrize("stage", ["starting", "waiting", "ready", "error", "saving", "cancelling", "saved"])
def test_login_panel_state_is_concise_and_safe(stage):
    state = menubar.login_panel_state({
        "stage": stage,
        "email": "person@example.com",
        "plan": "Plus",
        "has_url": True,
        "message": "Try https://example.test and auth_secret_value",
        "device_code": "ABCD-1234",
    })
    assert state["stage"] == stage
    assert "https://" not in state["message"]
    assert "auth_secret" not in state["message"]
    if stage == "waiting":
        assert "cancel" in state["actions"]
        assert "copy_link" in state["actions"]
        assert "device" in state["actions"]
    if stage in ("saving", "cancelling"):
        assert state["actions"] == []
    if stage == "saved":
        assert state["actions"] == ["dismiss"]
        assert state["body"] == "Ready to use."
        assert state["hint"] == ""


@pytest.mark.parametrize("provider", ["claude", "chatgpt"])
@pytest.mark.parametrize("status", ["loading", "error", "unavailable"])
def test_empty_load_states_never_offer_account_import(provider, status):
    state = menubar.provider_empty_state(provider, status)
    assert state["action"] == ("retry" if status == "error" else None)
    assert "first" not in state["title"]


def test_waiting_only_offers_available_login_actions():
    assert menubar.login_panel_state({"stage": "waiting"})["actions"] == ["cancel", "device"]
    device = menubar.login_panel_state({"stage": "waiting", "mode": "device"})
    assert device["actions"] == ["cancel"]
    assert device["body"] == "Preparing your code…"
    device = menubar.login_panel_state({
        "stage": "waiting", "mode": "device", "has_url": True, "device_code": "ABCD-EFGH"
    })
    assert device["actions"] == ["open_browser", "cancel", "copy_code", "copy_link"]


def test_empty_action_callback_routes_provider_without_switching():
    action = extract_class(menubar.__file__, "MenuBarApp", {"_on_empty_action"}, {})._on_empty_action
    app = Mock(_desktop_switching=False, _refreshing=False)
    action(app, "claude", "add")
    app.on_add_login.assert_called_once_with(None)
    app.on_add_codex_login.assert_not_called()
    action(app, "chatgpt", "add")
    app._on_login_action.assert_called_once_with("start")
    action(app, "chatgpt", "capture")
    app.on_add_codex_login.assert_called_once_with(None)
    action(app, "chatgpt", "retry")
    app.refresh_async.assert_called_once_with()
    app._make_desktop_switch.assert_not_called()
    app._on_account_click.assert_not_called()
    app.reset_mock()
    app._desktop_switching = True
    action(app, "claude", "add")
    app.on_add_login.assert_not_called()


def test_snapshot_failure_is_an_error_not_an_empty_roster():
    worker = extract_class(menubar.__file__, "MenuBarApp", {"_worker"}, {})._worker
    app = Mock()
    app._account_states = {"claude": "loading", "chatgpt": "loading"}
    app.snapshot = {"accounts": []}
    app._snapshot_source.take.side_effect = RuntimeError("synthetic read failure")
    worker(app, False)
    assert app._account_states == {"claude": "error", "chatgpt": "error"}
    assert app._refreshing is False
    assert app._hold_reload_pending is True


@pytest.mark.parametrize("codex_fails", [False, True])
def test_worker_resolves_loading_states_per_provider(monkeypatch, codex_fails):
    from openswap import process_detection, widget_snapshot
    monkeypatch.setattr(process_detection, "get_running_instances", lambda: ([], []))
    monkeypatch.setattr(process_detection, "get_running_codex_instances", lambda: [])
    monkeypatch.setattr(widget_snapshot, "publish_widget_snapshot", Mock())
    scope = dict(vars(menubar))
    scope["_adapt_snapshot"] = lambda *_: {"accounts": []}
    worker = extract_class(menubar.__file__, "MenuBarApp", {"_worker"}, scope)._worker
    app = Mock()
    app._account_states = {"claude": "loading", "chatgpt": "loading"}
    app.snapshot = {"accounts": []}
    app._engine = None
    app._event_lock = threading.Lock()
    app._kickoff_action_required = {}
    app._pending_relogin_notifies = set()
    app._relogin_notified = set()
    app._hold_line_for.return_value = None
    if codex_fails:
        app._codex_source.take.side_effect = RuntimeError("synthetic failure")
    worker(app, False)
    assert app._account_states == {"claude": "ready", "chatgpt": "error" if codex_fails else "ready"}
    assert app._refreshing is False
    assert app._hold_reload_pending is True
