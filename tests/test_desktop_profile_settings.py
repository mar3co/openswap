"""The main profile tab uses the existing store, separate from Desktop settings."""

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import menubar
from openswap.claude_desktop import DesktopProfiles
from openswap.claude_desktop_menu import ClaudeDesktopMenu
from openswap.menubar_display import claude_desktop_profile_rows, claude_desktop_settings_rows, settings_page_rows
from tests.menubar_harness import extract_class


@pytest.fixture
def controller(tmp_path):
    return ClaudeDesktopMenu(store=DesktopProfiles(tmp_path / "profiles"), prompt=Mock(),
                             show_error=Mock(), changed=Mock(), notify=Mock(), confirm=Mock(return_value=1))


def test_empty_main_tab_offers_add_and_settings_keep_guidance_collapsed():
    rows = claude_desktop_profile_rows()
    by_id = {row["id"]: row for row in rows}
    assert by_id["claude_desktop_profiles"]["items"] == []
    assert not by_id["claude_desktop_profiles"]["disabled"]
    settings = {row["id"]: row for row in claude_desktop_settings_rows()}
    assert settings["claude_desktop_help"]["value"] is False
    assert "claude_desktop_check" in settings and "claude_desktop_check" not in by_id
    assert "claude_desktop_profiles" not in settings and "claude_desktop_add" not in settings
    assert "added to Claude Code automatically" in by_id["claude_desktop_separation"]["label"]
    assert not any(row["id"].endswith(("open_hint", "signin_hint", "archive_hint")) for row in rows)
    assert not any(row.get("title") == "Manage…" for row in rows)


def test_archived_profiles_and_help_expand_independently(controller):
    active = controller.store.add("Personal")
    archived = controller.store.add("Work")
    controller.store.update(archived.id, archived=True)
    state = controller.settings_state()
    rows = claude_desktop_profile_rows(state)
    assert next(row for row in rows if row["kind"] == "profiles")["items"][0]["id"] == active.id
    assert not any(row["id"] == "claude_desktop_archived_profiles" for row in rows)
    expanded = claude_desktop_profile_rows(state, expanded={"claude_desktop_archived"})
    restored_row = next(row for row in expanded if row["id"] == "claude_desktop_archived_profiles")
    assert restored_row["items"][0]["id"] == archived.id
    assert not any(row["id"] == "claude_desktop_archive_hint" for row in expanded)
    help_rows = claude_desktop_settings_rows(state, expanded={"claude_desktop_help"})
    assert any(row["id"] == "claude_desktop_archive_hint" for row in help_rows)
    assert not any(row["id"] == "claude_desktop_archived_profiles" for row in help_rows)


def test_storage_error_is_not_an_empty_profile_list(controller):
    controller.store.add("Work")
    controller.store.registry.write_text("invalid")
    rows = claude_desktop_profile_rows(controller.settings_state())
    assert not any(row["kind"] == "profiles" for row in rows)
    assert next(row for row in rows if row["id"] == "claude_desktop_add")["disabled"]
    assert any(row.get("title") == "View details" for row in rows)
    controller.perform("storage_error")
    controller.show_error.assert_called_once()


def test_state_exposes_display_metadata_without_claiming_verified_identity(controller):
    profile = controller.store.add("Work")
    controller.store.update(profile.id, badge="999", color="blue")
    before = controller.store.registry.read_bytes()
    state = controller.settings_state()
    assert state["items"] == [{**asdict(controller.store.get(profile.id)), "status": "", "checked_at": None}]
    assert controller.store.registry.read_bytes() == before


def test_archive_cancel_then_confirm_and_restore_preserve_data(controller, monkeypatch):
    profile = controller.store.add("Work")
    opened = Mock()
    monkeypatch.setattr(controller.store, "open", opened)
    desktop, code = controller.store.paths(profile)
    sentinel = desktop / "test-data"
    sentinel.write_text("kept")
    controller.confirm.return_value = 0
    controller.perform("archive", profile.id)
    assert controller.store.get(profile.id).archived is False
    controller.confirm.return_value = 1
    controller.perform("archive", profile.id)
    assert controller.store.get(profile.id, include_archived=True).archived
    assert "Shortcuts" in controller.confirm.call_args.kwargs["message"]
    controller.perform("restore", profile.id)
    assert not controller.store.get(profile.id).archived
    assert sentinel.read_text() == "kept" and code.is_dir()
    opened.assert_not_called()


@pytest.mark.parametrize("gate", ["disabled", "busy", "blocked"])
@pytest.mark.parametrize("action", ["add", "open", "rename", "archive", "restore", "shortcut"])
def test_inline_actions_respect_current_gates(controller, monkeypatch, gate, action):
    controller.enabled = lambda: gate != "disabled"
    controller.blocked = lambda: gate == "blocked"
    controller.busy = gate == "busy"
    get = Mock()
    monkeypatch.setattr(controller.store, "get", get)
    controller.perform(action, "stale-id")
    get.assert_not_called()
    controller.prompt.assert_not_called()
    controller.confirm.assert_not_called()


def test_stale_row_uses_current_name_and_rejects_archived_open(controller, monkeypatch):
    profile = controller.store.add("Personal")
    controller.store.update(profile.id, name="Renamed")
    controller.prompt.return_value = SimpleNamespace(clicked=0)
    controller.perform("rename", profile.id)
    assert controller.prompt.call_args.kwargs["default_text"] == "Renamed"
    controller.store.update(profile.id, archived=True)
    opened = Mock()
    monkeypatch.setattr(controller, "open", opened)
    controller.perform("open", profile.id)
    opened.assert_not_called()
    controller.show_error.assert_called_once()


def test_chatgpt_help_retains_codex_explanation_and_parent_toggle():
    kwargs = dict(strategy="best", threshold=90, has_codex=True, desktop_app="chatgpt", section="desktop")
    settings = menubar.MenuBarSettings(desktop_features_enabled=True)
    rows = settings_page_rows(settings, **kwargs)
    by_id = {row["id"]: row for row in rows}
    assert "shared Codex account" in by_id["chatgpt_switching_hint"]["label"]
    assert by_id["chatgpt_auto_enabled"]["disabled"] is True
    assert "chatgpt_policy_location" not in by_id
    expanded = settings_page_rows(settings, expanded_sections={"chatgpt_help"}, **kwargs)
    assert any(row["id"] == "chatgpt_policy_location" for row in expanded)
    settings.desktop_features_enabled = False
    hidden = settings_page_rows(settings, expanded_sections={"chatgpt_help"}, **kwargs)
    assert not any(row["section"] == "desktop" for row in hidden)


def test_disclosures_are_local_and_action_failure_releases_panel():
    cls = extract_class(Path(menubar.__file__).with_name("menubar_panel.py"), "MenuBarPanel",
                        {"_emit_setting", "_desktop_profile_action"}, {})
    panel = cls()
    panel._settings_disclosures = set()
    panel._settings = lambda: menubar.MenuBarSettings(desktop_features_enabled=True)
    panel._on_setting = Mock()
    panel.reload = Mock()
    panel.is_shown = lambda: True
    panel._hold_overflow = Mock()
    panel._emit_setting("claude_desktop_help", None)
    assert panel._settings_disclosures == {"claude_desktop_help"}
    panel._emit_setting("claude_desktop_help", None)
    assert not panel._settings_disclosures
    panel._on_setting.assert_not_called()
    panel._on_claude_desktop_action = Mock(side_effect=RuntimeError("failed"))
    with pytest.raises(RuntimeError):
        panel._desktop_profile_action("open", "profile-id")
    assert [call.args for call in panel._hold_overflow.call_args_list] == [(True,), (False,)]
    panel._on_claude_desktop_action.assert_called_once_with("open", "profile-id")


def test_pending_app_switch_shows_waiting_not_the_last_operation(controller):
    from tests.test_claude_desktop import Item
    controller.operation = "Opening Claude…"
    controller.blocked = lambda: True
    titles = [item.title for item in controller.menu(SimpleNamespace(MenuItem=Item)).children]
    assert "Waiting for app switch…" in titles and "Opening Claude…" not in titles
    assert controller.settings_state()["operation"] == "Waiting for app switch…"
    controller.busy = True
    assert "Opening Claude…" in [item.title for item in controller.menu(SimpleNamespace(MenuItem=Item)).children]


@pytest.mark.parametrize("action", ["rename", "archive", "shortcut"])
def test_every_modal_pauses_background_work_while_open(controller, action):
    profile = controller.store.add("Work")
    seen = []
    def answer(**_kwargs):
        seen.append(controller._editing_details)
        return SimpleNamespace(clicked=0)
    controller.prompt.side_effect = answer
    controller.confirm = Mock(side_effect=lambda **kwargs: answer(**kwargs).clicked)
    getattr(controller, action)(profile)
    assert seen == [True] and not controller._editing_details


def test_default_profile_is_registered_once_and_then_only_read(controller, tmp_path, monkeypatch):
    controller.store.default_desktop = tmp_path / "Claude"
    assert controller.settings_state()["items"] == []
    controller.store.default_desktop.mkdir()
    assert controller.settings_state()["items"][0]["kind"] == "default"
    monkeypatch.setattr(controller.store, "ensure_default", Mock(return_value=None))
    assert controller.settings_state()["items"][0]["kind"] == "default"
    controller.suspend_checks()
    controller.settings_state()
    controller.store.ensure_default.assert_not_called()
    # A Default that disappeared from profiles.json is registered again on the next view.
    controller.store.save([p for p in controller.store.list(include_archived=True) if p.kind != "default"])
    controller.settings_state()
    controller.store.ensure_default.assert_called_once()


def test_profile_usage_matches_the_org_uuid_case_insensitively():
    from openswap.menubar_display import desktop_profile_usage
    profile = {"archived": False, "account_uuid": "acct", "organization_uuid": "ORG-UUID", "code_slot": "1",
               "email": "me@example.test"}
    card = {"num": "1", "provider": "claude", "identity": ("me@example.test", "org-uuid"),
            "windows": [{"label": "5h", "pct": 23}]}
    assert desktop_profile_usage(profile, [card]) == "Usage: 5h 23%"
