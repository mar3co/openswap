"""Native layout and interaction checks (requires the macOS menu bar extra)."""

import os
import sys

import pytest

if sys.platform != "darwin":
    pytest.skip("macOS dialogs", allow_module_level=True)
# These open real windows: each xdist worker gets a Dock icon and steals focus.
if os.environ.get("OPENSWAP_NATIVE_UI_TESTS") != "1":
    pytest.skip("set OPENSWAP_NATIVE_UI_TESTS=1 to run native dialog tests", allow_module_level=True)
AppKit = pytest.importorskip("AppKit")
from Foundation import NSObject, NSRunLoop, NSTimer

from openswap.menubar_dialog import (
    DIALOG_CLOSE,
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


def test_close_sits_left_of_the_actions_and_ends_the_flow():
    from openswap.worker.guided_setup import DIALOG_CLOSED

    assert DIALOG_CLOSE == DIALOG_CLOSED
    dialog = make_dialog_prompt(title="Set up Remote tasks", message="Account?", default_text="1",
                                ok="Continue", cancel="Skip", close="Close Setup")
    close = dialog.close_button.frame()
    skip, _continue = (button.frame() for button in dialog.buttons)
    assert close.origin.y == skip.origin.y > dialog.surface.footer_y
    assert close.origin.x + close.size.width < skip.origin.x
    dialog.close_button.performClick_(None)
    assert dialog._result == DIALOG_CLOSE


def test_a_popup_takes_the_text_fields_place_and_reports_its_row():
    dialog = make_dialog_prompt(title="Set up Remote tasks", message="Account", ok="Continue", cancel="Skip",
                                choices=["Work · a@x.com · Codex", "Work · a@x.com · Codex"], selected=1)
    assert dialog.textfield is None and dialog.popup.numberOfItems() == 2  # a repeated row is kept
    popup = dialog.popup.frame()
    assert popup.origin.y + popup.size.height < dialog.surface.footer_y
    assert dialog.popup.nextKeyView() == dialog.buttons[0]
    response = _run_with_action(dialog, lambda: _press_key(dialog, "\r", 36))
    assert response.clicked == 1 and response.choice == 1


def test_the_folder_chooser_offers_close_and_reports_it():
    from openswap.menubar_dialog import choose_folder

    def press_close(modal):
        panel = modal.self  # the bound panel of a PyObjC selector
        (close,) = panel.accessoryView().subviews()
        assert close.title() == "Close Setup"
        close.performClick_(None)
        return AppKit.NSModalResponseCancel

    assert choose_folder(title="Set up Remote tasks", message="Folder", close="Close Setup",
                         run=press_close) == DIALOG_CLOSE
    plain = choose_folder(title="Set up Remote tasks", message="Folder",
                          run=lambda modal: modal.self.accessoryView() or AppKit.NSModalResponseCancel)
    assert plain is None  # no accessory without ``close``; Cancel is None


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


def test_checkbox_starts_unchecked_and_is_reported():
    dialog = make_dialog_alert(title="Uninstall OpenSwap?", message="Removes what it set up.",
                               checkbox="Also delete saved accounts, settings and logs",
                               ok="Uninstall", cancel=True, destructive=True)
    box = dialog.checkbox.frame()
    assert dialog.checkbox.state() == AppKit.NSControlStateValueOff
    assert dialog.body.frame().origin.y + dialog.body.frame().size.height < box.origin.y
    assert box.origin.y + box.size.height < dialog.surface.footer_y
    assert box.origin.x + box.size.width <= DIALOG_WIDTH
    assert dialog.checkbox.nextKeyView() == dialog.buttons[0]
    assert dialog.buttons[-1].nextKeyView() == dialog.checkbox
    dialog.runModal = lambda: 1
    assert dialog.run().checked is False
    dialog.checkbox.performClick_(None)
    assert dialog.run().checked is True
    assert make_dialog_alert(message="No option.").checkbox is None
