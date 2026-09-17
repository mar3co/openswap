"""Native app orchestration with authentication and UI boundaries stubbed."""

import ast
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import menubar
from openswap.exceptions import ClaudeSwitchError


@pytest.fixture
def app(monkeypatch):
    tree = ast.parse(Path(menubar.__file__).read_text())
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "MenuBarApp")
    names = {"_on_login_action", "_login_run_worker", "_login_cancel_worker",
             "_login_save_worker", "_poll_login", "_enable_codex_account"}
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    thread = Mock()
    scope = {"threading": SimpleNamespace(Thread=thread), "ClaudeSwitchError": ClaudeSwitchError}
    exec(compile(module, "<onboarding-menu>", "exec"), scope)
    instance = scope["MenuBarApp"]()
    instance.codex = Mock()
    instance._desktop_switching = False
    instance._login_session = None
    instance._login_ui_state = {"stage": "idle"}
    instance._login_save_result = None
    instance._login_cancelled_session = None
    instance._event_lock = threading.Lock()
    instance._hold_reload_pending = False
    instance._show_login_panel = Mock()
    instance._panel = Mock()
    instance._show_error = Mock()
    instance._stop_chatgpt_auto_monitor = Mock()
    instance._start_chatgpt_auto_monitor = Mock()
    instance.refresh_async = Mock()
    instance.on_add_codex_login = Mock()
    instance._alert = Mock(return_value=1)
    instance._guard = lambda fn: (fn(), True)[1]
    factory = Mock()
    monkeypatch.setitem(sys.modules, "openswap.codex.onboarding", SimpleNamespace(LoginSession=factory))
    instance.factory = factory
    instance.thread = thread
    return instance


def test_start_is_background_only_and_does_not_switch(app):
    app._on_login_action("start")
    app._stop_chatgpt_auto_monitor.assert_called_once_with(clear_pending=True)
    app.factory.assert_called_once_with(app.codex, mode="browser")
    assert app._login_ui_state == {"stage": "starting"}
    app.thread.return_value.start.assert_called_once()
    app.factory.return_value.run.assert_not_called()
    app.codex.switch_to.assert_not_called()
    app.codex.add_account.assert_not_called()


def test_start_reopens_existing_attempt_instead_of_duplicating(app):
    app._login_session = Mock()
    app._on_login_action("start")
    app.factory.assert_not_called()
    app._show_login_panel.assert_called_once()


def test_device_fallback_cleans_previous_attempt_before_running_next(app):
    old, new = Mock(), Mock()
    events = []
    old.cancel.side_effect = lambda: events.append("cancel")
    new.run.side_effect = lambda: events.append("run")
    app._login_run_worker(old, new)
    assert events == ["cancel", "run"]
    app._login_session = old
    app._on_login_action("device")
    app.factory.assert_called_once_with(app.codex, mode="device")


def test_save_requires_ready_and_only_commits_after_click(app):
    session = Mock()
    session.state.return_value = {"stage": "waiting"}
    app._login_session = session
    app._on_login_action("save", "Work")
    app.thread.assert_not_called()
    session.state.return_value = {"stage": "ready", "email": "work@example.test"}
    app._poll_login()
    session.save.assert_not_called()
    app._on_login_action("save", "Work")
    assert app._login_ui_state["stage"] == "saving"
    app.thread.assert_called_once_with(target=app._login_save_worker, args=(session, "Work"), daemon=True)


def test_success_needs_no_enable_step_and_does_not_switch(app):
    session = Mock()
    session.save.return_value = "2"
    app._login_session = session
    app._login_save_worker(session, "Work")
    app._poll_login()
    assert app._login_ui_state == {"stage": "saved"}
    app.codex.set_account_disabled.assert_not_called()
    app._alert.assert_not_called()
    app._on_login_action("dismiss")
    assert app._login_ui_state == {"stage": "idle"}
    app.codex.switch_to.assert_not_called()


def test_cancel_cleanup_finishes_before_ui_returns_to_idle(app):
    session = Mock()
    app._login_session = session
    app._on_login_action("cancel")
    assert app._login_ui_state == {"stage": "cancelling"}
    app._on_login_action("start")
    app.factory.assert_not_called()
    app._login_cancel_worker(session)
    app._poll_login()
    session.cancel.assert_called_once()
    session.save.assert_not_called()
    assert app._login_ui_state == {"stage": "idle"}
    assert app._login_session is None


def test_stale_completion_does_not_replace_new_attempt(app):
    old, current = Mock(), Mock()
    current.state.return_value = {"stage": "waiting"}
    app._login_session = current
    app._login_cancelled_session = old
    app._login_save_result = (old, "2", None)
    app._poll_login()
    assert app._login_session is current
    assert app._login_ui_state == {"stage": "waiting"}
    app.refresh_async.assert_not_called()


def test_save_error_is_sanitized_and_remains_visible(app):
    session = Mock()
    session.state.return_value = {"stage": "ready"}
    session.save.side_effect = RuntimeError("secret-token")
    app._login_session = session
    app._login_save_worker(session, None)
    app._poll_login()
    app._poll_login()
    assert app._login_ui_state["stage"] == "error"
    assert "secret-token" not in app._login_ui_state["message"]


def test_no_login_action_during_desktop_switch(app):
    app._desktop_switching = True
    app._on_login_action("start")
    app.factory.assert_not_called()


def test_missing_native_panel_never_starts_invisible_login(app):
    app._panel = None
    app._on_login_action("start")
    app.factory.assert_not_called()
    app._show_error.assert_called_once()


def test_constructor_error_can_be_cancelled_without_session(app):
    app.factory.side_effect = RuntimeError("private details")
    app._on_login_action("start")
    assert app._login_ui_state["stage"] == "error"
    assert "private" not in app._login_ui_state["message"]
    app._on_login_action("cancel")
    assert app._login_ui_state == {"stage": "idle"}


def test_cancelled_state_can_be_dismissed(app):
    app._login_session = Mock()
    app._login_ui_state = {"stage": "cancelled"}
    app._on_login_action("dismiss")
    assert app._login_session is None
    assert app._login_ui_state == {"stage": "idle"}
