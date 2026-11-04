"""Opt-in checks of the actual profile retry buttons and their layout."""

import os
import sys
from unittest.mock import Mock

import pytest

if sys.platform != "darwin" or os.environ.get("OPENSWAP_NATIVE_UI_TESTS") != "1":
    pytest.skip("set OPENSWAP_NATIVE_UI_TESTS=1 on macOS", allow_module_level=True)
AppKit = pytest.importorskip("AppKit")
from openswap import menubar_panel
from openswap.menubar_display import MenuBarSettings


def descendants(view):
    yield view
    for child in view.subviews():
        yield from descendants(child)


@pytest.fixture
def profiles():
    AppKit.NSApplication.sharedApplication()
    state = {"items": [
        {"id": "a" * 32, "name": "Default", "kind": "default", "account_status": "Keychain access needed",
         "account_action": "retry_detection"},
        {"id": "b" * 32, "name": "VS Personal", "account_status": "Keychain access needed",
         "account_action": "retry_detection", "email": "test@example.test", "organization": "Personal",
         "code_status": "Continue Code setup", "code_action": "connect_code"},
    ]}
    action = Mock()
    panel = menubar_panel.MenuBarPanel(
        on_switch=None, on_rotate=None, on_best=None, on_toggle_auto=None, on_more=None,
        auto_enabled=lambda: False, snapshot=lambda: {"accounts": []}, threshold=lambda: 90,
        settings=lambda: MenuBarSettings(desktop_features_enabled=True),
        claude_desktop_state=lambda: state, on_claude_desktop_action=action,
    )
    panel._selected_provider = "claude_desktop"
    panel._popover = AppKit.NSPopover.alloc().init()
    panel.reload()
    yield panel, state, action
    panel.close()


def retry_buttons(panel):
    return [view for view in descendants(panel._controller.view())
            if isinstance(view, AppKit.NSButton) and str(view.title()) == "Retry detection"]


def test_retry_buttons_target_their_profile_and_fit_without_overlapping_code_setup(profiles):
    panel, state, action = profiles
    buttons = retry_buttons(panel)
    assert len(buttons) == 2
    for button, profile in zip(buttons, state["items"]):
        assert button.isEnabled()
        assert profile["name"] in button.accessibilityLabel()
        frame, bounds = button.frame(), button.superview().bounds()
        assert frame.origin.y >= 0 and frame.origin.y + frame.size.height <= bounds.size.height
        assert frame.origin.x >= 0 and frame.origin.x + frame.size.width <= bounds.size.width
        button.performClick_(None)
        action.assert_called_with("retry_detection", profile["id"])
    root = panel._controller.view()
    setup = next(v for v in descendants(root) if isinstance(v, AppKit.NSButton) and v.title() == "Continue setup")
    assert buttons[1].frame().origin.y + buttons[1].frame().size.height <= setup.frame().origin.y
    labels = [str(v.stringValue()) for v in descendants(root) if isinstance(v, AppKit.NSTextField)]
    assert any("Keychain access" in text and "stay signed in" in text for text in labels)


def test_busy_detection_disables_retries_and_archived_profiles_have_no_retry(profiles):
    panel, state, action = profiles
    state.update(busy=True, operation="Detecting account… Allow Keychain access if macOS asks.")
    panel.reload()
    assert all(not button.isEnabled() for button in retry_buttons(panel))
    for button in retry_buttons(panel):
        button.performClick_(None)
    action.assert_not_called()
    state["items"][1]["archived"] = True
    panel._settings_disclosures.add("claude_desktop_archived")
    state["busy"] = False
    panel.reload()
    assert len(retry_buttons(panel)) == 1
