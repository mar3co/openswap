"""First-use guidance never creates a session until the create form confirms."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import menubar
from openswap.claude_desktop import DesktopProfiles
from openswap.claude_desktop_menu import ClaudeDesktopMenu
from tests.menubar_harness import extract_class


def answer(clicked=1, text="Work", checked=True):
    return SimpleNamespace(clicked=clicked, text=text, checked=checked)


@pytest.fixture
def controller(tmp_path, monkeypatch):
    monkeypatch.setattr("openswap.claude_desktop_guide.illustrations", lambda: [])
    controller = ClaudeDesktopMenu(store=DesktopProfiles(tmp_path / "profiles"), prompt=Mock(),
                                   show_error=Mock(), changed=Mock(), notify=Mock(), mark_guide_seen=Mock())
    controller.open = Mock()
    return controller


def test_first_add_shows_guide_then_create_and_later_add_skips_guide(controller):
    controller.prompt.side_effect = [answer(), answer(), answer(text="Personal")]
    controller.add()
    calls = controller.prompt.call_args_list
    assert calls[0].kwargs["ok"] == "Continue"
    assert "illustrations" in calls[0].kwargs and calls[0].kwargs["default_text"] is None
    assert calls[1].kwargs["ok"] == "Create and open"
    controller.mark_guide_seen.assert_called_once_with()
    controller.open.assert_called_once_with(controller.store.get("Work").id, setup_shortcuts=True)
    controller.add()
    assert controller.prompt.call_count == 3
    assert controller.store.get("Personal")


@pytest.mark.parametrize("responses", [[answer(0)], [answer(), answer(0)]])
def test_cancel_either_step_creates_nothing(controller, responses):
    controller.prompt.side_effect = responses
    controller.add()
    assert controller.store.list() == []
    controller.open.assert_not_called()
    assert not controller._editing_details
    assert controller.mark_guide_seen.call_count == len(responses) - 1


def test_saved_preference_skips_guide_but_replay_never_creates(controller):
    controller.guide_seen = lambda: True
    controller.prompt.return_value = answer(0)
    controller.add()
    assert controller.prompt.call_args.kwargs["ok"] == "Create and open"
    controller.prompt.return_value = answer()
    assert controller.show_guide()
    assert controller.prompt.call_args.kwargs["ok"] == "Done"
    assert controller.store.list() == []
    controller.open.assert_not_called()


def test_background_signin_cannot_interrupt_guide_or_create(controller):
    def prompt(**kwargs):
        assert controller._editing_details
        return answer(0) if kwargs.get("default_text") is not None else answer()
    controller.prompt.side_effect = prompt
    controller.add()
    assert not controller._editing_details


def test_disabling_desktop_during_intro_prevents_creation(controller):
    def prompt(**kwargs):
        controller.enabled = lambda: False
        return answer()
    controller.prompt.side_effect = prompt
    controller.add()
    assert controller.prompt.call_count == 1
    assert controller.store.list() == []


def test_replay_guards_and_modal_exception_cleanup(controller):
    controller.busy = True
    assert not controller.show_guide()
    controller.prompt.assert_not_called()
    controller.busy = False
    controller.prompt.side_effect = RuntimeError("fixture")
    with pytest.raises(RuntimeError): controller.show_guide()
    assert not controller._editing_details


def test_guide_preference_persists_and_save_failure_rolls_back(tmp_path):
    path = tmp_path / "settings.json"
    app = extract_class(menubar.__file__, "MenuBarApp", {"_mark_claude_desktop_guide_seen"},
                        {"settings_path": path})()
    app.settings = menubar.MenuBarSettings()
    app._show_error = Mock()
    assert not app.settings.claude_desktop_guide_seen
    app._mark_claude_desktop_guide_seen()
    assert menubar.MenuBarSettings.load(path).claude_desktop_guide_seen
    app.settings.claude_desktop_guide_seen = False
    app.settings.save = Mock(side_effect=OSError())
    app._mark_claude_desktop_guide_seen()
    assert not app.settings.claude_desktop_guide_seen
    app._show_error.assert_called_once()


def test_settings_offer_replay_without_expanding_help():
    rows = menubar.claude_desktop_settings_rows()
    assert next(r for r in rows if r["id"] == "claude_desktop_guide")["title"] == "Show guide"
    app = extract_class(menubar.__file__, "MenuBarApp", {"_on_setting"}, {})()
    app._claude_desktop_profiles = Mock()
    app._on_setting("claude_desktop_guide", None)
    app._claude_desktop_profiles.show_guide.assert_called_once_with()


def test_missing_claude_artwork_keeps_instructions_readable(monkeypatch, tmp_path):
    from openswap import claude_desktop_guide
    monkeypatch.setattr(claude_desktop_guide, "APP_PATH", tmp_path / "missing.app")
    rows = claude_desktop_guide.illustrations()
    assert len(rows) == 2 and all(r["image"] is None and r["running"] for r in rows)
    assert "Default → Open" in rows[0]["body"]
    assert "dot" in rows[1]["body"]
