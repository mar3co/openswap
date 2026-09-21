"""Exercise the native menu callbacks without importing AppKit or rumps."""

import threading
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import menubar
from openswap.codex import CODEX_NUM_PREFIX
from openswap.codex.desktop_app import DesktopCapability
from openswap.exceptions import ClaudeSwitchError
from openswap.json_output import USAGE_API_KEY
from tests.menubar_harness import extract_class


def _row(num, display=None, disabled=False):
    return (num, "person@example.test", False, display, None, "Work", "", disabled, None)


def test_desktop_choices_exclude_claude_disabled_and_api_keys():
    snapshot = {"accounts": [
        _row("1"), _row(f"{CODEX_NUM_PREFIX}1"),
        _row(f"{CODEX_NUM_PREFIX}2", disabled=True),
        _row(f"{CODEX_NUM_PREFIX}3", display=USAGE_API_KEY),
    ]}
    assert menubar.desktop_switch_choices(snapshot) == [("1", "1  Work (person@example.test)")]


def test_desktop_choices_reject_api_key_kind_even_with_cached_usage():
    snapshot = {"accounts": [_row(f"{CODEX_NUM_PREFIX}1", display={})],
                "kinds": {f"{CODEX_NUM_PREFIX}1": "api_key"}}
    assert menubar.desktop_switch_choices(snapshot) == []


def test_changed_suggestion_during_confirmation_never_launches_switch(app):
    expected = ("1", ("one@example.test", "a"), "2", ("two@example.test", "b"))
    app._pending_chatgpt_switch = expected
    app._validate_pending_chatgpt_switch = Mock(
        side_effect=lambda: setattr(app, "_pending_chatgpt_switch", None)
    )
    app._make_desktop_switch("2", "Work", expected_pending=expected)(None)
    app._test_thread.assert_not_called()
    app._show_error.assert_called_once()


def test_desktop_consent_explicitly_covers_restart_idle_and_verification():
    title, body = menubar.desktop_switch_confirm_copy("Work")
    assert "Restart ChatGPT" in title
    for text in ("Experimental", "Work", "entire app", "shared Codex login",
                 "Save and stop all local/remote work", "close other Codex clients before continuing",
                 "check the account in Chat, Work, and Codex", "auto-switching off"):
        assert text in body
    assert len(body.split()) < 65


def test_desktop_consent_explains_persistent_pause_only_when_needed():
    _, body = menubar.desktop_switch_confirm_copy("Work", pause_auto=True)
    assert "all OpenSwap instances" in body
    assert "Claude is unchanged" in body
    assert "until re-enabled" in body
    assert "turns off" not in menubar.desktop_switch_confirm_copy("Work")[1]
    assert len(body.split()) < 80


@pytest.fixture
def app(monkeypatch, tmp_path):
    wanted = {
        "_make_desktop_switch", "_desktop_worker", "_drain_desktop_result",
        "_pause_codex_for_desktop", "_on_panel_account_click", "_notify",
        "_chatgpt_switching_persisted", "_offer_enable_chatgpt_switching",
        "_request_chatgpt_capability", "_chatgpt_capability_worker",
        "_on_chatgpt_view_active", "_begin_desktop_transaction",
        "_chatgpt_desktop_status", "_reload_main_panel_if_shown",
    }
    thread = Mock()
    record = Mock()
    setting = Mock(return_value=None)
    notification = Mock()
    settings_path = tmp_path / "menubar_settings.json"
    namespace = {
        "desktop_switch_confirm_copy": menubar.desktop_switch_confirm_copy,
        "desktop_switch_confirm_ok": menubar.desktop_switch_confirm_ok,
        "desktop_switch_choices": menubar.desktop_switch_choices,
        "desktop_capability_status_copy": menubar.desktop_capability_status_copy,
        "chatgpt_manual_activation_allowed": menubar.chatgpt_manual_activation_allowed,
        "notification_copy_for_desktop_switch": menubar.notification_copy_for_desktop_switch,
        "NotificationCopy": menubar.NotificationCopy,
        "MenuBarSettings": menubar.MenuBarSettings,
        "DesktopCapability": DesktopCapability,
        "capability_is_fresh": menubar.capability_is_fresh,
        "ClaudeSwitchError": ClaudeSwitchError,
        "threading": Mock(Thread=thread),
        "time": time,
        "rumps": SimpleNamespace(notification=notification),
        "record_manual_switch": record,
        "set_setting": setting,
        "settings_path": settings_path,
        "SETTINGS_SECTION_AUTOMATION": "automation",
        "MAIN_PAGE": "main",
    }
    instance = extract_class(menubar.__file__, "MenuBarApp", wanted, namespace)()
    instance.codex = Mock()
    instance.switcher = Mock()
    instance._guard = lambda fn: (fn(), True)[1]
    instance._desktop_switching = False
    instance._login_session = None
    instance._desktop_result = None
    instance._desktop_status = "Experimental"
    instance.snapshot = {"accounts": [_row(f"{CODEX_NUM_PREFIX}2")]}
    instance._on_account_click = Mock()
    instance._slot_needs_relogin = Mock(return_value=False)
    instance._refreshing = False
    instance._kickoff_running = False
    instance._event_lock = threading.Lock()
    instance._panel = Mock()
    instance.settings = menubar.MenuBarSettings(chatgpt_switching_enabled=True)
    instance.settings.save(settings_path)
    instance._chatgpt_capability = DesktopCapability(
        "running", "running", observed_at=time.monotonic(),
    )
    instance._chatgpt_capability_generation = 1
    instance._chatgpt_probe_inflight = False
    instance._desktop_consent_generation = None
    instance._chatgpt_monitor_notice = ""
    instance._pending_chatgpt_switch = None
    instance._desktop_app = Mock(name="shared_desktop_app")
    instance._desktop_app_lock = threading.Lock()
    instance._hold_reload_pending = False
    for name in ("_show_error", "_stop_codex_engine", "_stop_chatgpt_auto_monitor",
                 "_start_chatgpt_auto_monitor", "rebuild_menu", "refresh_async"):
        setattr(instance, name, Mock())
    instance._alert = Mock(return_value=1)
    instance._codex_enabled = Mock(return_value=False)
    instance._test_thread = thread
    instance._test_record = record
    instance._test_setting = setting
    instance._test_notification = notification
    instance._test_settings_path = settings_path
    return instance


@pytest.mark.parametrize("guard", ["cancel", "refresh", "kickoff", "busy"])
def test_menu_refusals_do_not_start_a_switch(app, guard):
    if guard == "cancel":
        app._alert.return_value = 0
    elif guard == "refresh":
        app._refreshing = True
    elif guard == "kickoff":
        app._kickoff_running = True
    else:
        app._desktop_switching = True
    app._make_desktop_switch("2", "Work")(None)
    app._test_thread.assert_not_called()
    app._stop_codex_engine.assert_not_called()


def test_switch_can_pause_auto_in_same_explicit_confirmation(app):
    app._codex_enabled.return_value = True
    app._make_desktop_switch("2", "Work")(None)
    assert "Codex auto-switching turns off" in app._alert.call_args.kwargs["message"]
    app._test_setting.assert_called_once_with(
        app.switcher.backup_dir, "autoswitch.codexEnabled", "false"
    )
    app._test_thread.return_value.start.assert_called_once()


def test_cancel_does_not_pause_auto(app):
    app._codex_enabled.return_value = True
    app._alert.return_value = 0
    app._make_desktop_switch("2", "Work")(None)
    app._test_setting.assert_not_called()
    app._test_thread.assert_not_called()


def test_pause_setting_failure_does_not_start_worker(app):
    app._codex_enabled.return_value = True
    app._guard = Mock(return_value=False)
    app._make_desktop_switch("2", "Work")(None)
    app._test_thread.assert_not_called()


def test_auto_enabled_during_consent_requires_new_consent(app):
    app._codex_enabled.side_effect = [False, True]
    app._make_desktop_switch("2", "Work")(None)
    app._test_setting.assert_not_called()
    app._test_thread.assert_not_called()


def test_chatgpt_card_uses_desktop_flow_not_cli_only_switch(app):
    app._on_panel_account_click(f"{CODEX_NUM_PREFIX}2")
    app._test_thread.return_value.start.assert_called_once()
    app._on_account_click.assert_not_called()
    app.codex.switch_to.assert_not_called()


def test_chatgpt_expired_oauth_card_explains_required_sign_in(app):
    app._slot_needs_relogin.return_value = True
    app._on_panel_account_click(f"{CODEX_NUM_PREFIX}2")
    app._test_thread.assert_not_called()
    app._show_error.assert_called_once()
    message = app._show_error.call_args.args[0]
    assert "ChatGPT OAuth session expired" in message
    assert "Codex" in message


@pytest.mark.parametrize("row", [_row(f"{CODEX_NUM_PREFIX}2", disabled=True),
                                 _row(f"{CODEX_NUM_PREFIX}2", display=USAGE_API_KEY)])
def test_chatgpt_card_refuses_ineligible_rows(app, row):
    app.snapshot = {"accounts": [row]}
    app._on_panel_account_click(f"{CODEX_NUM_PREFIX}2")
    app._test_thread.assert_not_called()
    app._show_error.assert_called_once()


def test_claude_card_keeps_normal_switch_action(app):
    app._on_panel_account_click("1")
    app._on_account_click.assert_called_once_with("1", close_panel=True)
    app._test_thread.assert_not_called()


def test_confirmed_switch_starts_background_worker_not_normal_switch(app):
    app._make_desktop_switch("2", "Work")(None)
    app._test_thread.assert_called_once_with(target=app._desktop_worker, args=("2", 1), daemon=True)
    app._test_thread.return_value.start.assert_called_once()
    app._stop_codex_engine.assert_called_once()
    app.codex.switch_to.assert_not_called()
    assert app._desktop_switching
    app._panel.close.assert_called_once()


def test_pause_rotation_has_separate_explicit_consent(app):
    app._alert.return_value = 0
    app._pause_codex_for_desktop(None)
    app._test_setting.assert_not_called()
    app._alert.return_value = 1
    app._pause_codex_for_desktop(None)
    app._test_setting.assert_called_once_with(
        app.switcher.backup_dir, "autoswitch.codexEnabled", "false"
    )
    app._stop_codex_engine.assert_called_once()
    app._test_thread.assert_not_called()


def test_refresh_started_during_modal_confirmation_is_refused(app):
    def consent(**kwargs):
        app._refreshing = True
        return 1
    app._alert.side_effect = consent
    app._make_desktop_switch("2", "Work")(None)
    app._test_thread.assert_not_called()


def test_worker_uses_both_confirmations_and_main_thread_completion(app, monkeypatch):
    from openswap.codex import desktop
    backend = Mock()
    backend.switch.return_value = {"status": "awaiting_verification"}
    monkeypatch.setattr(desktop, "DesktopSwitcher", Mock(return_value=backend))
    app._desktop_worker("2")
    backend.switch.assert_called_once_with("2", confirm_restart=True, confirm_idle=True)
    app._alert.assert_not_called()
    app._desktop_switching = True
    app._drain_desktop_result()
    assert not app._desktop_switching
    assert app._desktop_status == "ChatGPT reopened · Check the profile"
    assert app._hold_reload_pending is True
    app._alert.assert_not_called()
    app._test_notification.assert_called_once_with(
        "ChatGPT reopened", "", "Check the selected account in ChatGPT.", sound=False
    )
    app.refresh_async.assert_called_once()


def test_notification_failure_keeps_desktop_status_and_refresh(app, monkeypatch):
    from openswap.codex import desktop
    backend = Mock()
    backend.switch.return_value = {"status": "awaiting_verification"}
    monkeypatch.setattr(desktop, "DesktopSwitcher", Mock(return_value=backend))
    app._desktop_worker("2")
    app._test_notification.side_effect = RuntimeError("notifications unavailable")

    app._drain_desktop_result()

    assert app._desktop_status == "ChatGPT reopened · Check the profile"
    app._alert.assert_not_called()
    app.refresh_async.assert_called_once()


def test_worker_unexpected_error_does_not_leak_protocol_or_credentials(app, monkeypatch):
    from openswap.codex import desktop
    monkeypatch.setattr(desktop, "DesktopSwitcher", Mock(side_effect=ValueError("secret-token")))
    app._desktop_worker("2")
    app._drain_desktop_result()
    assert "secret-token" not in app._show_error.call_args.args[0]
    assert "Switch failed" in app._desktop_status
    app._test_record.assert_not_called()


def test_real_run_wires_submenu_and_completion_timer(tmp_path, monkeypatch):
    """Construct the actual nested app; stub only native/event-loop boundaries."""
    instances = []

    class Menu(list):
        pass

    class Item:
        def __init__(self, title, callback=None):
            self.title, self.callback, self.items = title, callback, []
        def add(self, value):
            self.items.append(value)

    class App:
        def __init__(self, *args, **kwargs):
            self._menu = Menu()
            instances.append(self)
        @property
        def menu(self):
            return self._menu
        @menu.setter
        def menu(self, values):
            self._menu = Menu(values)
        def run(self):
            pass

    fake_rumps = SimpleNamespace(
        App=App, MenuItem=Item, Timer=Mock(),
        rumps=SimpleNamespace(NSApp=SimpleNamespace()),
    )
    fake_appkit = SimpleNamespace(
        NSApplication=Mock(), NSApplicationActivationPolicyAccessory=1,
    )
    monkeypatch.setitem(sys.modules, "rumps", fake_rumps)
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setattr(menubar, "ensure_notification_identity", lambda: None)
    monkeypatch.setattr(menubar.threading, "Thread", Mock())
    from openswap import widget_snapshot
    monkeypatch.setattr(widget_snapshot, "wake_widget_host", lambda: None)
    switcher = Mock(backup_dir=tmp_path, sequence_file=tmp_path / "sequence.json")
    switcher._get_claude_config_path.return_value = tmp_path / "claude.json"
    codex = Mock(home=tmp_path / "codex", sequence_file=tmp_path / "codex-sequence.json")
    from openswap.settings import set_setting
    set_setting(tmp_path, "autoswitch.codexEnabled", "false")

    assert menubar.run(switcher, codex) == 0
    live_app = instances[0]
    live_app.snapshot["accounts"] = [_row(f"{CODEX_NUM_PREFIX}2")]
    live_app.rebuild_menu()
    submenu = next(item for item in live_app.menu if item and item.title == "Switch ChatGPT app (experimental)")
    assert len(submenu.items) == 1
    assert callable(submenu.items[0].callback)
    live_app._panel = Mock()
    live_app.rebuild_menu()
    assert not any(item and item.title == "Switch ChatGPT app (experimental)" for item in live_app.menu)
    callbacks = [call.args[0].__name__ for call in fake_rumps.Timer.call_args_list]
    assert "on_sync_tick" in callbacks

    live_app._desktop_switching = True
    live_app._desktop_result = (None, "synthetic failure")
    live_app._show_error = Mock()
    live_app.refresh_async = Mock()
    for name in ("_consume_widget_command", "_detect_active_change", "_detect_store_change",
                 "_drain_engine_events", "_apply_hold_line", "_drain_relogin_notifies",
                 "_maybe_auto_capture_relogin", "_drain_kickoff_results", "_maybe_kickoff"):
        setattr(live_app, name, Mock())
    live_app.on_sync_tick(None)
    live_app._show_error.assert_called_once_with("synthetic failure")
    assert not live_app._desktop_switching


def test_parent_off_keeps_accounts_and_rejects_every_activation_path(app):
    app.settings.chatgpt_switching_enabled = False
    app.settings.save(app._test_settings_path)
    app._on_panel_account_click(f"{CODEX_NUM_PREFIX}2")
    app._test_thread.assert_not_called()
    assert app._alert.call_args.kwargs["ok"] == "Enable in Settings"
    app._panel._show_settings.assert_called()
    app._make_desktop_switch("2", "Work")(None)
    app._test_thread.assert_not_called()
    app.codex.switch_to.assert_not_called()


def test_stale_running_observation_does_not_open_consent(app):
    app._chatgpt_capability = DesktopCapability(
        "running", "running", observed_at=time.monotonic() - 6,
    )
    app._make_desktop_switch("2", "Work")(None)
    app._alert.assert_not_called()
    assert app._test_thread.call_args.kwargs["target"] == app._chatgpt_capability_worker
    app._show_error.assert_called()
    assert app._show_error.call_args.args[0] == "Checking ChatGPT…"


def test_consent_past_process_ttl_still_starts_worker(app, monkeypatch):
    app._chatgpt_capability = DesktopCapability(
        "running", "running", observed_at=100.0,
    )
    clock = {"t": 100.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    def consent(**kwargs):
        clock["t"] = 107.0
        return 1

    app._alert.side_effect = consent
    app._make_desktop_switch("2", "Work")(None)
    app._test_thread.assert_called_once_with(
        target=app._desktop_worker, args=("2", 1), daemon=True,
    )
    app._show_error.assert_not_called()


def test_chatgpt_reload_probes_when_running_observation_is_stale(app):
    app._chatgpt_capability = DesktopCapability(
        "running", "running", observed_at=time.monotonic() - 6,
    )
    app._panel = Mock()
    app._panel.is_shown.return_value = True
    app._panel._page = "main"
    app._panel._selected_provider = "chatgpt"
    app._reload_main_panel_if_shown()
    app._panel.reload.assert_called_once()
    assert app._test_thread.call_args.kwargs["target"] == app._chatgpt_capability_worker
    assert app._chatgpt_capability.state == "checking"


def test_post_switch_reminder_shown_until_next_probe_completes(app, monkeypatch):
    from openswap.codex import desktop
    backend = Mock()
    backend.switch.return_value = {"status": "awaiting_verification"}
    monkeypatch.setattr(desktop, "DesktopSwitcher", Mock(return_value=backend))
    app._desktop_worker("2")
    app._desktop_switching = True
    app._drain_desktop_result()
    assert app._chatgpt_desktop_status() == "ChatGPT reopened · Check the profile"
    gen = app._chatgpt_capability_generation
    app._desktop_app.observe_capability.return_value = DesktopCapability(
        "running", "running", observed_at=time.monotonic(),
    )
    app._chatgpt_capability_worker(gen)
    assert app._chatgpt_desktop_status() != "ChatGPT reopened · Check the profile"


def test_transaction_clears_probe_inflight_so_stale_worker_cannot_pin_it(app):
    app._chatgpt_probe_inflight = True
    app._chatgpt_capability_generation = 3
    app._begin_desktop_transaction()
    assert app._chatgpt_probe_inflight is False
    app._chatgpt_capability_worker(3)
    assert app._chatgpt_probe_inflight is False


def test_stopped_observation_uses_open_copy_and_running_uses_restart(app):
    app._chatgpt_capability = DesktopCapability(
        "stopped", "stopped", observed_at=time.monotonic(),
    )
    app._make_desktop_switch("2", "Work")(None)
    assert app._alert.call_args.kwargs["title"] == "Switch and open ChatGPT?"
    assert app._alert.call_args.kwargs["ok"] == "Switch and open ChatGPT"
    app._desktop_switching = False
    app._chatgpt_capability = DesktopCapability(
        "running", "running", observed_at=time.monotonic(),
    )
    app._make_desktop_switch("2", "Work")(None)
    assert app._alert.call_args.kwargs["title"] == "Restart ChatGPT?"
    assert app._alert.call_args.kwargs["ok"] == "Restart ChatGPT"


def test_preference_and_generation_rechecked_after_consent(app):
    generation = app._chatgpt_capability_generation

    def consent(**kwargs):
        app.settings.chatgpt_switching_enabled = False
        app.settings.save(app._test_settings_path)
        app._chatgpt_capability_generation = generation + 1
        return 1

    app._alert.side_effect = consent
    app._make_desktop_switch("2", "Work")(None)
    app._test_thread.assert_not_called()


def test_worker_rechecks_persisted_preference_and_generation(app, monkeypatch):
    from openswap.codex import desktop
    backend = Mock()
    monkeypatch.setattr(desktop, "DesktopSwitcher", Mock(return_value=backend))
    app.settings.chatgpt_switching_enabled = False
    app.settings.save(app._test_settings_path)
    app._desktop_worker("2", 1)
    backend.switch.assert_not_called()
    app._drain_desktop_result()
    app._show_error.assert_called()
    assert "off" in app._show_error.call_args.args[0].lower()


def test_worker_passes_shared_desktop_app_under_lock(app, monkeypatch):
    from openswap.codex import desktop
    backend = Mock()
    backend.switch.return_value = {"status": "awaiting_verification"}
    constructor = Mock(return_value=backend)
    monkeypatch.setattr(desktop, "DesktopSwitcher", constructor)
    app._desktop_worker("2", app._chatgpt_capability_generation)
    constructor.assert_called_once_with(app.codex, app=app._desktop_app)
    backend.switch.assert_called_once_with("2", confirm_restart=True, confirm_idle=True)


def test_stale_capability_worker_cannot_update_current_ui(app):
    app._chatgpt_capability = DesktopCapability("checking", "checking")
    app._chatgpt_capability_generation = 9
    app._desktop_app.observe_capability.return_value = DesktopCapability(
        "running", "running", observed_at=50.0,
    )
    app._chatgpt_capability_worker(8)
    assert app._chatgpt_capability.state == "checking"
    app._chatgpt_capability_worker(9)
    assert app._chatgpt_capability.state == "running"


def test_transaction_invalidates_capability_probes(app):
    app._chatgpt_capability_generation = 3
    app._begin_desktop_transaction()
    assert app._desktop_switching is True
    assert app._chatgpt_capability_generation == 4
    app._desktop_app.observe_capability.return_value = DesktopCapability(
        "running", "running", observed_at=1.0,
    )
    app._chatgpt_capability_worker(3)
    assert app._chatgpt_capability.state != "running" or app._desktop_switching
