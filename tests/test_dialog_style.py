"""Native layout and interaction checks (requires the macOS menu bar extra)."""

import sys

import pytest

if sys.platform != "darwin":
    pytest.skip("macOS dialogs", allow_module_level=True)
AppKit = pytest.importorskip("AppKit")
from Foundation import NSObject, NSRunLoop, NSTimer

from openswap.menubar_dialog import (
    DIALOG_WIDTH,
    make_dialog_alert,
    make_dialog_prompt,
)


@pytest.fixture(autouse=True)
def native_app():
    AppKit.NSApplication.sharedApplication()


def test_dialogs_keep_one_width_and_actions_below_content():
    for dialog in (
        make_dialog_alert(message="Ready."),
        make_dialog_alert(title="Restart ChatGPT?", message="Save all your work. " * 10,
                          ok="Restart ChatGPT", cancel=True),
        make_dialog_prompt(title="Rename account", message="Enter a short name.",
                           default_text="Work", ok="Save", cancel=True),
    ):
        assert dialog.surface.frame().size.width == DIALOG_WIDTH
        assert dialog.window().accessibilitySubrole() == AppKit.NSAccessibilityDialogSubrole
        for button in dialog.buttons:
            frame = button.frame()
            assert frame.origin.y > dialog.surface.footer_y
            assert frame.origin.x >= 0
            assert frame.origin.x + frame.size.width <= DIALOG_WIDTH
        if dialog.textfield:
            field = dialog.textfield.frame()
            assert field.origin.y + field.size.height < dialog.surface.footer_y


def test_long_messages_scroll_without_pushing_actions_off_screen():
    message = "All diagnostics must remain readable.\n" * 100
    dialog = make_dialog_alert(message=message, cancel=True)
    assert dialog.body.stringValue() == message
    assert dialog.body_scroll.hasVerticalScroller()
    assert dialog.body_scroll.documentVisibleRect().origin.y == 0
    assert dialog.surface.frame().size.height <= 640
    assert dialog.body_scroll.frame().origin.y + dialog.body_scroll.frame().size.height < dialog.surface.footer_y


def test_buttons_preserve_confirmation_and_cancellation_results():
    dialog = make_dialog_alert(ok="Continue", cancel=True, other="Review")
    for button in dialog.buttons:
        button.performClick_(None)
        assert dialog._result == button.tag()
    dialog.window().cancelOperation_(None)
    assert dialog._result == 0


def test_destructive_confirmation_defaults_to_cancel():
    dialog = make_dialog_alert(ok="Remove", cancel=True, destructive=True)
    cancel, remove = dialog.buttons
    assert remove.hasDestructiveAction()
    assert remove.keyEquivalent() == ""
    assert cancel.keyEquivalent() == "\r"
    assert dialog.window().defaultButtonCell() == cancel.cell()


def test_prompt_preserves_text_and_literal_percent_signs():
    dialog = make_dialog_prompt(message="100% yours", default_text="Work 50%", cancel=True)
    assert dialog.body.stringValue() == "100% yours"
    assert dialog.textfield.stringValue() == "Work 50%"
    assert dialog.textfield.nextKeyView() == dialog.buttons[0]
    assert dialog.buttons[-1].nextKeyView() == dialog.textfield


class DialogTestDriver(NSObject):
    def fire_(self, timer):
        try:
            self.callback()
        except BaseException as error:
            self.error = error
            self.dialog.finish(0)


def _press_key(dialog, character, code, modifiers=0):
    event = AppKit.NSEvent.keyEventWithType_location_modifierFlags_timestamp_windowNumber_context_characters_charactersIgnoringModifiers_isARepeat_keyCode_(
        AppKit.NSEventTypeKeyDown, (0, 0), modifiers, 0,
        dialog.window().windowNumber(), None, character, character, False, code,
    )
    if not dialog.window().performKeyEquivalent_(event):
        dialog.window().sendEvent_(event)


def _run_with_action(dialog, action):
    driver = DialogTestDriver.alloc().init()
    driver.callback = action
    driver.dialog = dialog
    driver.error = None
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        0.05, driver, "fire:", None, False,
    )
    # A failed keyboard dispatch must fail the test, not leave a modal open.
    timeout = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
        2, dialog.window(), "cancelOperation:", None, False,
    )
    loop = NSRunLoop.currentRunLoop()
    loop.addTimer_forMode_(timer, AppKit.NSModalPanelRunLoopMode)
    loop.addTimer_forMode_(timeout, AppKit.NSModalPanelRunLoopMode)
    try:
        result = dialog.run()
        assert timeout.isValid(), "dialog did not respond to keyboard input"
        if driver.error:
            raise driver.error
        assert not dialog.window().isVisible()
        assert not dialog._running
        return result
    finally:
        timer.invalidate()
        timeout.invalidate()


@pytest.mark.parametrize("character,code,destructive,expected", [
    ("\r", 36, False, 1),
    ("\x1b", 53, False, 0),
    ("\r", 36, True, 0),
    ("\x1b", 53, True, 0),
])
def test_modal_keyboard_result_and_cleanup(character, code, destructive, expected):
    dialog = make_dialog_alert(title="Dialog test", ok="Continue", cancel=True,
                               destructive=destructive)
    response = _run_with_action(dialog, lambda: _press_key(dialog, character, code))
    assert response.clicked == expected


def test_prompt_commits_field_editor_text_and_supports_select_all():
    dialog = make_dialog_prompt(title="Dialog test", default_text="Before", cancel=True)

    def edit():
        editor = dialog.textfield.currentEditor()
        assert editor is not None
        editor.setString_("After")
        _press_key(dialog, "a", 0, AppKit.NSEventModifierFlagCommand)
        assert editor.selectedRange().length == len("After")
        _press_key(dialog, "\r", 36)

    response = _run_with_action(dialog, edit)
    assert response.clicked == 1
    assert response.text == "After"
