"""Desktop visibility, upgrade behavior, and stopping background work safely."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from openswap import menubar
from tests.menubar_harness import extract_class


@pytest.mark.parametrize('saved', [{}, {'chatgpt_switching_enabled': False}])
def test_new_and_cli_only_settings_keep_desktop_off(tmp_path, saved):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(saved))
    settings = menubar.MenuBarSettings.load(path, has_desktop_profiles=lambda: False)
    assert not settings.desktop_features_enabled
    assert 'desktop' not in dict(menubar.settings_sections(settings))
    rows = menubar.settings_page_rows(settings, strategy='best', threshold=90, has_codex=True)
    ids = {r['id'] for r in rows}
    assert 'desktop_features_enabled' in ids
    assert not {'desktop_app', 'claude_desktop_profiles', 'chatgpt_switching_enabled', 'chatgpt_auto_enabled'} & ids
    assert all(r['section'] != 'desktop' for r in rows)


@pytest.mark.parametrize('saved,profiles', [({}, True), ({'chatgpt_switching_enabled': True}, False),
                                           ({'chatgpt_auto_enabled': True}, False)])
def test_existing_desktop_users_keep_access_on_upgrade(tmp_path, saved, profiles):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(saved))
    settings = menubar.MenuBarSettings.load(path, has_desktop_profiles=lambda: profiles)
    assert settings.desktop_features_enabled
    assert json.loads(path.read_text()) == saved  # loading remains read-only
    settings.save(path)
    assert menubar.MenuBarSettings.load(path).desktop_features_enabled


def test_profiles_can_migrate_without_an_existing_settings_file(tmp_path):
    path = tmp_path / 'missing.json'
    assert menubar.MenuBarSettings.load(path, has_desktop_profiles=lambda: True).desktop_features_enabled
    assert not path.exists()


@pytest.mark.parametrize('value', [False, 'true', 1, None])
def test_explicit_off_or_invalid_preference_is_not_overridden(tmp_path, value):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'desktop_features_enabled': value, 'chatgpt_switching_enabled': True,
                                'chatgpt_auto_enabled': True}))
    has_profiles = Mock(return_value=True)
    settings = menubar.MenuBarSettings.load(path, has_desktop_profiles=has_profiles)
    assert not settings.desktop_features_enabled
    assert not menubar.chatgpt_tab_desktop(settings)
    assert not menubar.chatgpt_suggestions_effective(settings)
    has_profiles.assert_not_called()


def test_unreadable_profile_hint_does_not_prevent_cli_startup(tmp_path):
    settings = menubar.MenuBarSettings.load(tmp_path / 'missing.json', has_desktop_profiles=Mock(side_effect=OSError('denied')))
    assert not settings.desktop_features_enabled


def test_hidden_desktop_section_falls_back_to_general_and_codex_labels():
    settings = menubar.MenuBarSettings(menu_bar_provider='chatgpt', chatgpt_switching_enabled=True,
                                      chatgpt_auto_enabled=True, desktop_features_enabled=False)
    rows = menubar.settings_page_rows(settings, strategy='best', threshold=90, section='desktop', has_codex=True)
    assert all(r['section'] == 'general' for r in rows)
    picker = next(r for r in rows if r['id'] == 'menu_bar_provider')
    assert dict(picker['options'])['chatgpt'] == 'Codex'
    assert menubar.format_menu_bar_title({}, settings) == 'Codex · No account'


@pytest.mark.parametrize('app,expected,absent', [('claude', 'claude_desktop_profiles', 'chatgpt_switching_enabled'),
                                                ('chatgpt', 'chatgpt_switching_enabled', 'claude_desktop_profiles')])
def test_desktop_app_picker_keeps_controls_in_their_own_section(app, expected, absent):
    settings = menubar.MenuBarSettings(desktop_features_enabled=True)
    rows = menubar.settings_page_rows(settings, strategy='best', threshold=90, section='desktop',
                                     desktop_app=app, has_codex=True)
    ids = {r['id'] for r in rows}
    assert 'desktop_app' in ids and expected in ids and absent not in ids
    assert all(r['section'] == 'desktop' for r in rows)


@pytest.fixture
def app(tmp_path):
    scope = {**vars(menubar), 'settings_path': tmp_path / 'settings.json'}
    app = extract_class(menubar.__file__, 'MenuBarApp', {'_toggle_desktop_features'}, scope)()
    app.settings = menubar.MenuBarSettings(desktop_features_enabled=True, chatgpt_switching_enabled=True,
                                          chatgpt_auto_enabled=True, auto_switch_enabled=True)
    app.settings.save(scope['settings_path'])
    app._settings_path = scope['settings_path']
    app._desktop_switching = False
    app._restart_engine = Mock()
    app._claude_desktop_profiles = Mock(busy=False)
    app._chatgpt_capability_generation = 7
    app._chatgpt_probe_inflight = True
    app._panel = Mock(_page='settings')
    for method in ('_show_error', '_stop_chatgpt_auto_monitor', '_ensure_codex_engine', '_start_chatgpt_auto_monitor',
                   'rebuild_menu', '_reload_main_panel_if_shown'):
        setattr(app, method, Mock())
    return app


def test_turning_off_stops_desktop_work_preserves_preferences_and_does_not_touch_apps(app):
    app._toggle_desktop_features()
    saved = menubar.MenuBarSettings.load(app._settings_path, has_desktop_profiles=lambda: True)
    assert not saved.desktop_features_enabled
    assert saved.chatgpt_switching_enabled and saved.chatgpt_auto_enabled and saved.auto_switch_enabled
    app._stop_chatgpt_auto_monitor.assert_called_once_with(clear_pending=True)
    app._claude_desktop_profiles.suspend_checks.assert_called_once()
    app._claude_desktop_profiles.open.assert_not_called()
    app._claude_desktop_profiles.edit.assert_not_called()
    assert not app._chatgpt_probe_inflight and app._chatgpt_capability_generation == 8
    app._ensure_codex_engine.assert_called_once()
    app._panel.reload.assert_called_once()


def test_enabling_does_not_open_apps_or_switch_logins(app):
    app.settings.desktop_features_enabled = False
    app._toggle_desktop_features()
    assert menubar.MenuBarSettings.load(app._settings_path).desktop_features_enabled
    app._start_chatgpt_auto_monitor.assert_called_once()
    app._claude_desktop_profiles.open.assert_not_called()
    app._stop_chatgpt_auto_monitor.assert_not_called()


def test_save_failure_preserves_ui_and_background_state(app, monkeypatch):
    monkeypatch.setattr(app.settings, 'save', Mock(side_effect=OSError('disk full')))
    app._toggle_desktop_features()
    assert app.settings.desktop_features_enabled
    assert app._chatgpt_probe_inflight and app._chatgpt_capability_generation == 7
    app._stop_chatgpt_auto_monitor.assert_not_called()
    app._claude_desktop_profiles.suspend_checks.assert_not_called()
    app._show_error.assert_called_once()


@pytest.mark.parametrize('opening', ['claude', 'chatgpt'])
def test_visibility_cannot_change_halfway_through_an_app_operation(app, opening):
    app._desktop_switching = opening == 'chatgpt'
    app._claude_desktop_profiles.busy = opening == 'claude'
    app._toggle_desktop_features()
    assert app.settings.desktop_features_enabled
    app._stop_chatgpt_auto_monitor.assert_not_called()
    app._show_error.assert_called_once()


@pytest.mark.parametrize('enabled', [False, True])
def test_desktop_app_navigation_is_gated_and_does_not_change_accounts(enabled):
    cls = extract_class(Path(menubar.__file__).with_name('menubar_panel.py'), 'MenuBarPanel',
                        {'_emit_setting', '_check_desktop_settings_app'}, dict(vars(menubar)))
    panel = cls()
    panel._settings = lambda: menubar.MenuBarSettings(desktop_features_enabled=enabled)
    panel._desktop_settings_app = 'claude'
    panel._on_claude_desktop_check = Mock()
    panel._on_chatgpt_view_active = Mock()
    panel._on_setting = Mock()
    panel.reload = Mock()
    panel._emit_setting('desktop_app', 'chatgpt')
    assert panel._desktop_settings_app == ('chatgpt' if enabled else 'claude')
    assert panel._on_chatgpt_view_active.call_count == int(enabled)
    assert panel.reload.call_count == int(enabled)
    panel._on_claude_desktop_check.assert_not_called()
    panel._on_setting.assert_not_called()
