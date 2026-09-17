"""Focused safety/lifecycle tests for deferred ChatGPT auto-switching."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from openswap import menubar
from tests.menubar_harness import extract_class


METHODS = {
    "_chatgpt_target_identity",
    "_start_chatgpt_auto_monitor",
    "_stop_chatgpt_auto_monitor",
    "_run_chatgpt_auto_engine",
    "_reconcile_chatgpt_auto_mode",
    "_on_chatgpt_auto_event",
    "_validate_pending_chatgpt_switch",
    "_review_chatgpt_switch",
    "on_toggle_chatgpt_auto",
}


def _app(monkeypatch, tmp_path):
    engine_type = Mock()
    load = Mock(return_value=SimpleNamespace(codex_enabled=False))
    set_setting = Mock()
    thread_type = Mock()
    scope = {
        "AutoSwitchEngine": engine_type,
        "desktop_switch_choices": menubar.desktop_switch_choices,
        "load_settings": load,
        "set_setting": set_setting,
        "settings_path": tmp_path / "menubar_settings.json",
        "threading": SimpleNamespace(Thread=thread_type),
        "time": time,
    }
    app = extract_class(menubar.__file__, "MenuBarApp", METHODS, scope)()
    app.settings = menubar.MenuBarSettings(chatgpt_auto_enabled=True)
    app.switcher = Mock(backup_dir=tmp_path, _logger=Mock())
    app.codex = Mock(state_dir=tmp_path)
    app.codex.slot_identity.side_effect = lambda n: {
        "1": ("source@example.test", "workspace-source"),
        "2": ("target@example.test", "workspace-target"),
    }.get(str(n))
    app.codex.current_account_number.return_value = "1"
    app.codex.live_identity.return_value = ("source@example.test", "workspace-source")
    app.codex.switchable_account_numbers.return_value = ["1", "2"]
    app._chatgpt_auto_engine = None
    app._chatgpt_auto_generation = 0
    app._pending_chatgpt_switch = None
    app._desktop_switching = False
    app._login_session = None
    app._desktop_status = "Experimental · Switching reopens ChatGPT"
    app._chatgpt_monitor_notice = ""
    app._chatgpt_auto_retry_at = 0.0
    app._event_lock = threading.Lock()
    app._dirty = False
    app._hold_reload_pending = False
    app._codex_enabled = Mock(return_value=False)
    app._run_engine = Mock()
    app._stop_codex_engine = Mock()
    app._guard = lambda fn: (fn(), True)[1]
    app._alert = Mock(return_value=1)
    app._show_error = Mock()
    app.rebuild_menu = Mock()
    app._reload_main_panel_if_shown = Mock()
    app.snapshot = {
        "accounts": [
            ("codex:1", "source@example.test", True, {}, None, "Source", "Pro", False, None),
            ("codex:2", "target@example.test", False, {}, None, "Target", "Pro", False, None),
        ]
    }
    app._make_desktop_switch = Mock(return_value=Mock())
    app._engine_type = engine_type
    app._load = load
    app._set_setting = set_setting
    app._thread_type = thread_type
    return app


def _switch_event(*, source="1", target="2", dry_run=True):
    return SimpleNamespace(
        kind="switch", dry_run=dry_run,
        from_ref={"number": source}, to_ref={"number": target},
    )


def test_monitor_is_dry_run_and_independent_of_claude_master(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    app.settings.auto_switch_enabled = False
    engine = Mock()
    app._engine_type.return_value = engine

    app._start_chatgpt_auto_monitor()

    assert app._engine_type.call_args.kwargs["dry_run"] is True
    assert app._engine_type.call_args.args[0] is app.codex
    assert app._engine_type.call_args.kwargs["state_path"].parent == app.codex.state_dir
    assert app._chatgpt_auto_engine is engine
    app._thread_type.return_value.start.assert_called_once()


def test_stale_engine_and_generation_events_are_ignored(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    current = Mock()
    app._chatgpt_auto_engine = current
    app._chatgpt_auto_generation = 4
    app._pending_chatgpt_switch = ("old",)

    app._on_chatgpt_auto_event(_switch_event(), Mock(), 4)
    app._on_chatgpt_auto_event(_switch_event(), current, 3)

    assert app._pending_chatgpt_switch == ("old",)


def test_candidate_is_bound_to_source_and_target_workspace_identities(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    engine = Mock()
    app._chatgpt_auto_engine = engine
    app._chatgpt_auto_generation = 2

    app._on_chatgpt_auto_event(_switch_event(), engine, 2)

    assert app._pending_chatgpt_switch == (
        "1", ("source@example.test", "workspace-source"),
        "2", ("target@example.test", "workspace-target"),
    )


def test_no_candidate_clears_pending(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    engine = Mock()
    app._chatgpt_auto_engine = engine
    app._chatgpt_auto_generation = 1
    app._pending_chatgpt_switch = ("pending",)

    app._on_chatgpt_auto_event(SimpleNamespace(kind="no-switch"), engine, 1)

    assert app._pending_chatgpt_switch is None
    assert app._hold_reload_pending is True


def test_error_event_clears_pending_without_exposing_raw_error(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    engine = Mock()
    app._chatgpt_auto_engine = engine
    app._chatgpt_auto_generation = 1
    app._pending_chatgpt_switch = ("pending",)

    app._on_chatgpt_auto_event(
        SimpleNamespace(kind="error", message="secret-token"), engine, 1
    )

    assert app._pending_chatgpt_switch is None
    assert app._chatgpt_monitor_notice == "Auto-switch paused · Couldn’t check accounts"
    assert "secret-token" not in app._chatgpt_monitor_notice


def test_monitor_error_clears_pending_and_surfaces_safe_status(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    engine = Mock()
    engine.run_loop.side_effect = RuntimeError("secret auth detail")
    app._chatgpt_auto_engine = engine
    app._chatgpt_auto_generation = 3
    app._pending_chatgpt_switch = ("pending",)

    app._run_chatgpt_auto_engine(engine, 3)

    assert app._chatgpt_auto_engine is None
    assert app._pending_chatgpt_switch is None
    assert app._chatgpt_monitor_notice == "Auto-switch paused · Monitor unavailable"
    assert "secret" not in app._chatgpt_monitor_notice


def test_source_change_workspace_replacement_and_removed_target_invalidate(monkeypatch, tmp_path):
    for mutation in ("source", "identity", "removed"):
        app = _app(monkeypatch, tmp_path)
        app._pending_chatgpt_switch = (
            "1", ("source@example.test", "workspace-source"),
            "2", ("target@example.test", "workspace-target"),
        )
        if mutation == "source":
            app.codex.current_account_number.return_value = "3"
        elif mutation == "identity":
            app.codex.slot_identity.side_effect = lambda n: (
                ("replacement@example.test", "workspace-new") if str(n) == "2"
                else ("source@example.test", "workspace-source")
            )
        else:
            app.codex.switchable_account_numbers.return_value = ["1"]

        app._validate_pending_chatgpt_switch()
        assert app._pending_chatgpt_switch is None, mutation


def test_review_only_calls_existing_confirmed_desktop_path(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    app._pending_chatgpt_switch = (
        "1", ("source@example.test", "workspace-source"),
        "2", ("target@example.test", "workspace-target"),
    )
    callback = Mock()
    app._make_desktop_switch.return_value = callback

    app._review_chatgpt_switch()

    app._make_desktop_switch.assert_called_once_with(
        "2", "2  Target (target@example.test)",
        expected_pending=app._pending_chatgpt_switch,
    )
    callback.assert_called_once_with(None)
    app.codex.switch_to.assert_not_called()


def test_cancelled_enable_changes_nothing(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    app.settings.chatgpt_auto_enabled = False
    app._codex_enabled.return_value = True
    app._alert.return_value = 0

    app.on_toggle_chatgpt_auto(None)

    assert app.settings.chatgpt_auto_enabled is False
    app._set_setting.assert_not_called()
    app._stop_codex_engine.assert_not_called()


def test_enable_disables_only_legacy_codex_rotation(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    app.settings.chatgpt_auto_enabled = False
    app.settings.auto_switch_enabled = True
    app._codex_enabled.return_value = True
    app._start_chatgpt_auto_monitor = Mock()

    app.on_toggle_chatgpt_auto(None)

    app._set_setting.assert_called_once_with(
        app.switcher.backup_dir, "autoswitch.codexEnabled", "false"
    )
    assert app.settings.auto_switch_enabled is True
    assert app.settings.chatgpt_auto_enabled is True
    app._stop_codex_engine.assert_called_once()
    app._start_chatgpt_auto_monitor.assert_called_once()


def test_turning_off_stops_monitor_and_clears_pending(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    engine = Mock()
    app._chatgpt_auto_engine = engine
    app._pending_chatgpt_switch = ("pending",)

    app.on_toggle_chatgpt_auto(None)

    assert app.settings.chatgpt_auto_enabled is False
    assert app._pending_chatgpt_switch is None
    engine.stop.assert_called_once()


def test_external_live_rotation_conflict_holds_monitor(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    engine = Mock()
    app._chatgpt_auto_engine = engine
    app._pending_chatgpt_switch = ("pending",)
    app._codex_enabled.return_value = True

    app._reconcile_chatgpt_auto_mode()

    engine.stop.assert_called_once()
    assert app._chatgpt_auto_engine is None
    assert app._pending_chatgpt_switch is None
    assert app._chatgpt_monitor_notice == "Auto-switch paused · Live Codex rotation is on"


def test_monitor_does_not_start_during_login_or_desktop_transaction(monkeypatch, tmp_path):
    for field in ("_login_session", "_desktop_switching"):
        app = _app(monkeypatch, tmp_path)
        setattr(app, field, Mock() if field == "_login_session" else True)
        app._start_chatgpt_auto_monitor()
        app._engine_type.assert_not_called()


def test_reconcile_resumes_after_onboarding_or_transaction_finishes(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    app._start_chatgpt_auto_monitor = Mock()
    app._login_session = Mock()
    app._reconcile_chatgpt_auto_mode()
    app._start_chatgpt_auto_monitor.assert_not_called()

    app._login_session = None
    app._desktop_switching = False
    app._reconcile_chatgpt_auto_mode()
    app._start_chatgpt_auto_monitor.assert_called_once()
