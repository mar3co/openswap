"""Branded macOS dialogs with native controls and modal keyboard behavior.

Imported only by the menu bar app. Keep presentation here so confirmations and
text prompts share the same layout without depending on NSAlert's OS-specific
icon well and button stacking.
"""

from __future__ import annotations

import math
import weakref
from dataclasses import dataclass
from pathlib import Path

import objc
from AppKit import (
    NSAccessibilityDialogSubrole,
    NSApplication,
    NSBackingStoreBuffered,
    NSBezierPath,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSControlSizeLarge,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSEventModifierFlagCommand,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSEventModifierFlagShift,
    NSFont,
    NSFontAttributeName,
    NSFontWeightMedium,
    NSFontWeightSemibold,
    NSFocusRingTypeExterior,
    NSForegroundColorAttributeName,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSKernAttributeName,
    NSLineBreakByWordWrapping,
    NSMenuItem,
    NSPanel,
    NSPopUpButton,
    NSParagraphStyleAttributeName,
    NSScrollView,
    NSSecureTextField,
    NSTextField,
    NSTextFieldCell,
    NSView,
    NSWindowAnimationBehaviorAlertPanel,
    NSWindowAnimationBehaviorNone,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
    NSWorkspace,
)
from Foundation import (
    NSAttributedString, NSMakeRect, NSMutableParagraphStyle, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer,
)


# The setup wizard (menubar_wizard) uses the same padding, wordmark, title
# size, surface and button height, so every OpenSwap window reads as one.
DIALOG_WIDTH = 440.0
DIALOG_PADDING = 36.0
DIALOG_CONTENT_WIDTH = DIALOG_WIDTH - DIALOG_PADDING * 2
DIALOG_INPUT_HEIGHT = 40.0
POPUP_HEIGHT = 32.0
ACTION_HEIGHT = 36.0
ACTION_GAP = 10.0
FOOTER_GAP = 32.0  # between the content and the footer's top edge
LOCKUP_MARK = 22.0  # the mark's side in the wordmark
LOCKUP_SPACE = 50.0  # from the wordmark's top to the title
# What a dialog returns for its ``close`` action (ends a whole flow, not one step).
DIALOG_CLOSE = -3


@dataclass(frozen=True)
class DialogResponse:
    clicked: int
    text: str
    checked: bool = False
    choice: int = -1  # the popup's selected row; -1 without ``choices``
    values: tuple[str, ...] = ()  # labeled fields, in their displayed order


class _DialogSurface(NSView):
    """White content over a grey footer, below ``footer_y``."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        NSColor.controlBackgroundColor().setFill()
        NSBezierPath.fillRect_(self.bounds())
        footer = getattr(self, "footer_y", None)
        if footer is not None:
            NSColor.windowBackgroundColor().setFill()
            NSBezierPath.fillRect_(NSMakeRect(
                0, footer, self.bounds().size.width,
                self.bounds().size.height - footer,
            ))
            NSColor.separatorColor().setFill()
            NSBezierPath.fillRect_(NSMakeRect(0, footer, self.bounds().size.width, 0.5))


class _DialogButton(NSButton):
    """Native button semantics with a consistent 40-point rounded surface."""

    def drawRect_(self, rect):
        primary = self.tag() == 1 and not self.hasDestructiveAction()
        if primary:
            fill = NSColor.controlAccentColor()
            ink = NSColor.whiteColor()
        else:
            fill = NSColor.quaternaryLabelColor().colorWithAlphaComponent_(0.08)
            ink = NSColor.systemRedColor() if self.hasDestructiveAction() else NSColor.labelColor()
        if self.isHighlighted():
            fill = fill.blendedColorWithFraction_ofColor_(0.12, NSColor.labelColor())
        fill.setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 10, 10).fill()
        title = NSAttributedString.alloc().initWithString_attributes_(self.title(), {
            NSFontAttributeName: self.font(), NSForegroundColorAttributeName: ink,
        })
        size = title.size()
        title.drawAtPoint_((
            (self.bounds().size.width - size.width) / 2,
            (self.bounds().size.height - size.height) / 2,
        ))

    def drawFocusRingMask(self):
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 10, 10).fill()

    def focusRingMaskBounds(self):
        return self.bounds()


class _PlainButton(NSButton):
    """Back, Skip and Close: a plain label that acts like a button, no fill."""

    def drawRect_(self, rect):
        ink = NSColor.secondaryLabelColor() if self.isEnabled() else NSColor.tertiaryLabelColor()
        if self.isHighlighted():
            ink = NSColor.labelColor()
        title = NSAttributedString.alloc().initWithString_attributes_(self.title(), {
            NSFontAttributeName: self.font(), NSForegroundColorAttributeName: ink,
        })
        size = title.size()
        title.drawAtPoint_((
            (self.bounds().size.width - size.width) / 2,
            (self.bounds().size.height - size.height) / 2,
        ))

    def drawFocusRingMask(self):
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 6, 6).fill()

    def focusRingMaskBounds(self):
        return self.bounds()


def forward_edit_shortcut(event, sender) -> bool:
    """Send Cmd+A/C/X/V/Z to the first responder; True when it was handled.

    Accessory apps have no Edit menu, so the native field editor (secure
    fields included) never sees these shortcuts unless they are forwarded.
    """
    modifiers = event.modifierFlags()
    if (modifiers & NSEventModifierFlagCommand
            and not modifiers & (NSEventModifierFlagControl | NSEventModifierFlagOption)):
        key = str(event.charactersIgnoringModifiers() or "").lower()
        action = {
            "a": "selectAll:", "c": "copy:", "x": "cut:", "v": "paste:",
            "z": "redo:" if modifiers & NSEventModifierFlagShift else "undo:",
        }.get(key)
        if action and NSApplication.sharedApplication().sendAction_to_from_(action, None, sender):
            return True
    return False


class _DialogPanel(NSPanel):
    def cancelOperation_(self, sender):
        self.dialog.finish(0)

    def performKeyEquivalent_(self, event):
        if forward_edit_shortcut(event, self):
            return True
        return objc.super(_DialogPanel, self).performKeyEquivalent_(event)

    def clicked_(self, sender):
        self.dialog.finish(int(sender.tag()))


class _DialogInputCell(NSTextFieldCell):
    """Center the text and the field editor in the taller native input."""

    def drawingRectForBounds_(self, bounds):
        rect = objc.super(_DialogInputCell, self).drawingRectForBounds_(bounds)
        height = self.cellSizeForBounds_(bounds).height
        if rect.size.height > height:
            rect.origin.y += (rect.size.height - height) / 2
            rect.size.height = height
        return rect

    def selectWithFrame_inView_editor_delegate_start_length_(self, frame, view, editor, delegate, start, length):
        objc.super(_DialogInputCell, self).selectWithFrame_inView_editor_delegate_start_length_(
            self.drawingRectForBounds_(frame), view, editor, delegate, start, length,
        )

    def editWithFrame_inView_editor_delegate_event_(self, frame, view, editor, delegate, event):
        objc.super(_DialogInputCell, self).editWithFrame_inView_editor_delegate_event_(
            self.drawingRectForBounds_(frame), view, editor, delegate, event,
        )


def _label(text, font, color, width, *, spaced=False):
    field = NSTextField.wrappingLabelWithString_(text)
    style = NSMutableParagraphStyle.alloc().init()
    style.setLineBreakMode_(NSLineBreakByWordWrapping)
    if spaced:
        style.setLineSpacing_(3)
    field.setAttributedStringValue_(NSAttributedString.alloc().initWithString_attributes_(
        text, {
            NSFontAttributeName: font,
            NSForegroundColorAttributeName: color,
            NSParagraphStyleAttributeName: style,
        },
    ))
    height = math.ceil(field.cell().cellSizeForBounds_(NSMakeRect(0, 0, width, 100000)).height)
    field.setFrame_(NSMakeRect(0, 0, width, height))
    return field


def draw_lockup(surface, x, y):
    """The OpenSwap wordmark: the OpenSoft mark, then "OpenSwap" in one
    weight with tight tracking, as the sibling products set theirs."""
    mark = NSImageView.alloc().initWithFrame_(NSMakeRect(x, y, LOCKUP_MARK, LOCKUP_MARK))
    image = NSImage.alloc().initWithContentsOfFile_(
        str(Path(__file__).with_name("assets") / "opensoft-symbol-64.png")
    )
    if image is not None:
        image.setTemplate_(True)
        mark.setImage_(image)
    mark.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    mark.setContentTintColor_(NSColor.labelColor())
    mark.setAccessibilityElement_(False)
    surface.addSubview_(mark)
    word = NSTextField.labelWithString_("")
    word.setAttributedStringValue_(NSAttributedString.alloc().initWithString_attributes_(
        "OpenSwap", {
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(15, NSFontWeightSemibold),
            NSForegroundColorAttributeName: NSColor.labelColor(),
            NSKernAttributeName: -0.4,
        },
    ))
    word.sizeToFit()
    size = word.frame().size
    word.setFrameOrigin_((x + LOCKUP_MARK + 8, y + (LOCKUP_MARK - size.height) / 2))
    surface.addSubview_(word)


class BrandDialog:
    """One reusable modal surface; 1 confirms, 0 cancels, -1 is the third action.

    ``checkbox`` adds an option below the message, unchecked; :meth:`run`
    reports it in ``DialogResponse.checked``. ``close`` adds a plain text
    action on the footer's left, as the setup wizard places Back; it returns
    ``DIALOG_CLOSE``. ``choices`` shows a popup of those rows (``selected``
    first) where a prompt has its text field; :meth:`run` reports the row.
    """

    def __init__(
        self, *, title=None, message="", ok=None, cancel=None, other=None,
        default_text=None, secure=False, destructive=False, checkbox=None, close=None,
        choices=None, selected=0, fields=None, input_label=None, placeholder=None, checkbox_checked=False,
        illustrations=None,
    ):
        shown_title = str(title or "OpenSwap")
        if shown_title.casefold() == "openswap":
            shown_title = "OpenSwap"
        self._result = 0
        self._running = False
        self.textfield = None
        self.fields = []
        self.checkbox = None
        self.popup = None
        self._window = _DialogPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, DIALOG_WIDTH, 320),
            NSWindowStyleMaskTitled | NSWindowStyleMaskFullSizeContentView,
            NSBackingStoreBuffered, False,
        )
        window = self._window
        window.dialog = weakref.proxy(self)
        window.setReleasedWhenClosed_(False)
        window.setTitle_(shown_title)
        window.setAccessibilitySubrole_(NSAccessibilityDialogSubrole)
        window.setTitleVisibility_(NSWindowTitleHidden)
        window.setTitlebarAppearsTransparent_(True)
        window.setMovableByWindowBackground_(True)
        window.setHidesOnDeactivate_(False)
        window.setHasShadow_(True)
        window.setAnimationBehavior_(
            NSWindowAnimationBehaviorNone
            if NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
            else NSWindowAnimationBehaviorAlertPanel
        )

        surface = _DialogSurface.alloc().initWithFrame_(NSMakeRect(0, 0, DIALOG_WIDTH, 320))
        window.setContentView_(surface)
        self.surface = surface
        y = DIALOG_PADDING - 4
        draw_lockup(surface, DIALOG_PADDING, y)
        y += LOCKUP_SPACE

        heading = _label(shown_title, NSFont.systemFontOfSize_weight_(22, NSFontWeightSemibold),
                         NSColor.labelColor(), DIALOG_CONTENT_WIDTH)
        heading.setFrameOrigin_((DIALOG_PADDING, y))
        surface.addSubview_(heading)
        y += heading.frame().size.height

        # Measure the actions first so even unusually long labels stay inside
        # the common width. Ordinary confirmation pairs sit side by side.
        titles = [(str(ok or "OK"), 1)]
        if cancel:
            titles.insert(0, (cancel if isinstance(cancel, str) else "Cancel", 0))
        if other:
            titles.insert(0, (str(other), -1))
        self.buttons = []
        widths = []
        for caption, response in titles:
            button = _DialogButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, ACTION_HEIGHT))
            button.setTitle_(caption)
            button.setBezelStyle_(NSBezelStyleRounded)
            button.setBordered_(False)
            button.setControlSize_(NSControlSizeLarge)
            button.setFont_(NSFont.systemFontOfSize_weight_(13, NSFontWeightMedium))
            button.setFocusRingType_(NSFocusRingTypeExterior)
            button.setTarget_(window)
            button.setAction_("clicked:")
            button.setTag_(response)
            button.sizeToFit()
            widths.append(max(96.0, button.frame().size.width + 24))
            if response == 1:
                # Destructive actions require explicit selection. Return may
                # dismiss via Cancel, but never silently removes an account.
                if not destructive:
                    button.setKeyEquivalent_("\r")
                    window.setDefaultButtonCell_(button.cell())
                else:
                    button.setHasDestructiveAction_(True)
            elif response == 0:
                button.setKeyEquivalent_("\r" if destructive else "\x1b")
                if destructive:
                    window.setDefaultButtonCell_(button.cell())
            self.buttons.append(button)
            surface.addSubview_(button)
        self.close_button = None
        close_width = 0.0
        if close:
            button = _PlainButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, ACTION_HEIGHT))
            button.setTitle_(str(close))
            button.setBordered_(False)
            button.setFont_(NSFont.systemFontOfSize_weight_(13, NSFontWeightMedium))
            button.setFocusRingType_(NSFocusRingTypeExterior)
            button.setTarget_(window)
            button.setAction_("clicked:")
            button.setTag_(DIALOG_CLOSE)
            button.sizeToFit()
            close_width = button.frame().size.width + 16
            self.close_button = button
            surface.addSubview_(button)
        row_width = sum(widths) + ACTION_GAP * (len(widths) - 1)
        stacked = row_width + (close_width + ACTION_GAP if close else 0) > DIALOG_CONTENT_WIDTH
        rows = (len(widths) + (1 if close else 0)) if stacked else 1
        footer_height = 40 + rows * (ACTION_HEIGHT + ACTION_GAP) - ACTION_GAP

        # Measured before the message so its scroll limit leaves them room.
        text_x, text_width = DIALOG_PADDING + 80, DIALOG_CONTENT_WIDTH - 80
        figures = []
        for item in illustrations or ():
            caption = _label(item["title"], NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold),
                             NSColor.labelColor(), text_width)
            detail = _label(item["body"], NSFont.systemFontOfSize_(13), NSColor.secondaryLabelColor(), text_width)
            figures.append((item, caption, detail, max(80, caption.frame().size.height + 5 + detail.frame().size.height)))
        illustration_space = sum(20 + height for _item, _caption, _detail, height in figures)

        if message:
            y += 8
            body = _label(str(message), NSFont.systemFontOfSize_(13),
                          NSColor.secondaryLabelColor(), DIALOG_CONTENT_WIDTH, spaced=True)
            body.setSelectable_(True)
            self.body = body
            screen = window.screen()
            max_height = min(640, screen.visibleFrame().size.height - 80) if screen else 640
            input_space = (DIALOG_INPUT_HEIGHT + 20 + (24 if input_label else 0) if default_text is not None else 0) + (
                POPUP_HEIGHT + 20 if choices is not None else 0) + len(fields or ()) * (DIALOG_INPUT_HEIGHT + 42)
            body_limit = max(80, max_height - y - input_space - illustration_space - FOOTER_GAP - footer_height)
            body_height = body.frame().size.height
            if body_height > body_limit:
                scroll = NSScrollView.alloc().initWithFrame_(
                    NSMakeRect(DIALOG_PADDING, y, DIALOG_CONTENT_WIDTH, body_limit)
                )
                scroll.setDrawsBackground_(False)
                scroll.setHasVerticalScroller_(True)
                scroll.setAutohidesScrollers_(True)
                scroll.setDocumentView_(body)
                surface.addSubview_(scroll)
                body.scrollPoint_((0, 0))
                self.body_scroll = scroll
                y += body_limit
            else:
                body.setFrameOrigin_((DIALOG_PADDING, y))
                surface.addSubview_(body)
                y += body_height

        self.illustration_views = []
        for item, caption, detail, height in figures:
            y += 20
            icon = NSImageView.alloc().initWithFrame_(NSMakeRect(DIALOG_PADDING, y, 64, 64))
            icon.setImage_(item.get("image"))
            icon.setImageScaling_(NSImageScaleProportionallyUpOrDown)
            icon.setAccessibilityLabel_(item["title"])
            surface.addSubview_(icon)
            if item.get("image") is not None:
                # The running dot illustrates the icon above it; no icon, no dot.
                dot = _label("●", NSFont.systemFontOfSize_(9), NSColor.secondaryLabelColor(), 12)
                dot.setFrameOrigin_((DIALOG_PADDING + 29, y + 64))
                dot.setAccessibilityLabel_("Running indicator example")
                surface.addSubview_(dot)
            caption.setFrameOrigin_((text_x, y))
            surface.addSubview_(caption)
            detail.setFrameOrigin_((text_x, y + caption.frame().size.height + 5))
            surface.addSubview_(detail)
            self.illustration_views.append((icon, caption, detail))
            y += height

        if checkbox:
            y += 16
            box = NSButton.checkboxWithTitle_target_action_(str(checkbox), None, None)
            box.setFont_(NSFont.systemFontOfSize_(13))
            box.setState_(NSControlStateValueOn if checkbox_checked else NSControlStateValueOff)
            box.sizeToFit()
            box.setFrameOrigin_((DIALOG_PADDING, y))
            surface.addSubview_(box)
            self.checkbox = box
            y += box.frame().size.height

        if default_text is not None:
            y += 20
            if input_label:
                label = _label(str(input_label), NSFont.systemFontOfSize_(12), NSColor.secondaryLabelColor(), DIALOG_CONTENT_WIDTH)
                label.setFrameOrigin_((DIALOG_PADDING, y))
                surface.addSubview_(label)
                y += label.frame().size.height + 6
            field_class = NSSecureTextField if secure else NSTextField
            field = field_class.alloc().initWithFrame_(
                NSMakeRect(DIALOG_PADDING, y, DIALOG_CONTENT_WIDTH, DIALOG_INPUT_HEIGHT)
            )
            if not secure:
                field.setCell_(_DialogInputCell.alloc().initTextCell_(""))
                field.setBezeled_(True)
                field.setEditable_(True)
                field.setSelectable_(True)
            field.setFont_(NSFont.systemFontOfSize_(14))
            field.setControlSize_(NSControlSizeLarge)
            field.setBezelStyle_(1)  # NSTextFieldRoundedBezel
            field.setStringValue_(str(default_text))
            field.setAccessibilityLabel_(str(input_label or shown_title))
            if placeholder is not None:
                field.setPlaceholderString_(str(placeholder))
            field.cell().setUsesSingleLineMode_(True)
            field.cell().setScrollable_(True)
            surface.addSubview_(field)
            self.textfield = field
            window.setInitialFirstResponder_(field)
            y += DIALOG_INPUT_HEIGHT

        for caption, value in fields or ():
            y += 16
            label = _label(caption, NSFont.systemFontOfSize_(12), NSColor.secondaryLabelColor(), DIALOG_CONTENT_WIDTH)
            label.setFrameOrigin_((DIALOG_PADDING, y))
            surface.addSubview_(label)
            y += label.frame().size.height + 6
            field = NSTextField.alloc().initWithFrame_(NSMakeRect(DIALOG_PADDING, y, DIALOG_CONTENT_WIDTH, DIALOG_INPUT_HEIGHT))
            field.setCell_(_DialogInputCell.alloc().initTextCell_(""))
            field.setBezeled_(True)
            field.setEditable_(True)
            field.setSelectable_(True)
            field.setFont_(NSFont.systemFontOfSize_(14))
            field.setControlSize_(NSControlSizeLarge)
            field.setBezelStyle_(1)
            field.setStringValue_(str(value))
            field.setAccessibilityLabel_(caption)
            field.cell().setUsesSingleLineMode_(True)
            field.cell().setScrollable_(True)
            surface.addSubview_(field)
            self.fields.append(field)
            y += DIALOG_INPUT_HEIGHT
        if self.fields:
            window.setInitialFirstResponder_(self.fields[0])

        if choices is not None:
            y += 20
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(DIALOG_PADDING, y, DIALOG_CONTENT_WIDTH, POPUP_HEIGHT), False,
            )
            popup.setControlSize_(NSControlSizeLarge)
            popup.setFont_(NSFont.systemFontOfSize_(13))
            for row in choices:
                # Menu items, not addItemWithTitle_, which drops a repeated title.
                popup.menu().addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(str(row), None, ""))
            if 0 <= selected < len(choices):
                popup.selectItemAtIndex_(selected)
            popup.setAccessibilityLabel_(shown_title)
            surface.addSubview_(popup)
            self.popup = popup
            window.setInitialFirstResponder_(popup)
            y += POPUP_HEIGHT

        y += FOOTER_GAP
        surface.footer_y = y
        action_y = y + 20
        action_x = DIALOG_PADDING if stacked else (
            DIALOG_WIDTH - DIALOG_PADDING - sum(widths) - ACTION_GAP * (len(widths) - 1)
        )
        for button, width in zip(self.buttons, widths):
            button.setFrame_(NSMakeRect(
                action_x, action_y, DIALOG_CONTENT_WIDTH if stacked else width, ACTION_HEIGHT,
            ))
            if stacked:
                action_y += ACTION_HEIGHT + ACTION_GAP
            else:
                action_x += width + ACTION_GAP
        if self.close_button is not None:
            # Left of the row, its text aligned with the content above.
            self.close_button.setFrame_(NSMakeRect(
                DIALOG_PADDING - 8 if not stacked else DIALOG_PADDING,
                action_y if stacked else y + 20,
                DIALOG_CONTENT_WIDTH if stacked else close_width, ACTION_HEIGHT,
            ))
        window.setContentSize_((DIALOG_WIDTH, y + footer_height))
        surface.setFrameSize_((DIALOG_WIDTH, y + footer_height))
        chain = [view for view in (self.checkbox, self.textfield, *self.fields, self.popup, self.close_button)
                 if view is not None]
        chain += self.buttons
        for current, following in zip(chain, chain[1:]):
            current.setNextKeyView_(following)
        chain[-1].setNextKeyView_(chain[0])

    def window(self):
        return self._window

    def finish(self, response):
        self._result = response
        if self._running:
            NSApplication.sharedApplication().stopModalWithCode_(response)

    def runModal(self):
        app = NSApplication.sharedApplication()
        self._result = 0
        self._running = True
        self._window.dialog = weakref.proxy(self)
        self._window.center()
        self._window.makeKeyAndOrderFront_(None)
        if self.textfield is not None:
            self._window.makeFirstResponder_(self.textfield)
            self.textfield.selectText_(None)
        elif self.fields:
            self._window.makeFirstResponder_(self.fields[0])
        try:
            app.runModalForWindow_(self._window)
            return self._result
        finally:
            self._running = False
            self._window.makeFirstResponder_(None)
            self._window.orderOut_(None)
            self._window.dialog = None

    def run(self):
        clicked = self.runModal()
        return DialogResponse(
            clicked,
            str(self.textfield.stringValue()) if self.textfield else "",
            self.checkbox is not None and self.checkbox.state() == NSControlStateValueOn,
            int(self.popup.indexOfSelectedItem()) if self.popup is not None else -1,
            tuple(str(field.stringValue()) for field in self.fields),
        )


def make_dialog_alert(**kwargs):
    return BrandDialog(**kwargs)


def make_dialog_prompt(*, default_text="", choices=None, fields=None, **kwargs):
    """A text prompt, or a popup of ``choices`` in place of the text field."""
    return BrandDialog(default_text=None if choices is not None or fields is not None else default_text,
                       choices=choices, fields=fields, **kwargs)


class _ChooserClose(NSObject):
    """The folder chooser's Close button: remember it, then cancel the panel."""

    def close_(self, _sender):
        self.closed = True
        self.panel.cancel_(None)


def choose_folder(*, title, message, close=None, run=None):
    """The native folder chooser (Finder-style browse and search).

    Returns the chosen path, None on Cancel, or ``DIALOG_CLOSE`` when the
    owner pressed ``close``, a plain button under the browser that ends the
    whole flow, as in the dialogs. ``run`` wraps the modal call (the menu
    bar's ``_dialog``).
    """
    from AppKit import NSModalResponseOK, NSOpenPanel, NSURL
    from Foundation import NSHomeDirectory

    panel = NSOpenPanel.openPanel()
    panel.setTitle_(title)
    panel.setMessage_(message)
    panel.setPrompt_("Approve")
    panel.setCanChooseDirectories_(True)
    panel.setCanChooseFiles_(False)
    panel.setCanCreateDirectories_(True)
    panel.setAllowsMultipleSelection_(False)
    panel.setDirectoryURL_(NSURL.fileURLWithPath_(NSHomeDirectory()))
    target = None
    if close:
        target = _ChooserClose.alloc().init()
        target.panel, target.closed = panel, False
        button = _PlainButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, ACTION_HEIGHT))
        button.setTitle_(str(close))
        button.setBordered_(False)
        button.setFont_(NSFont.systemFontOfSize_weight_(13, NSFontWeightMedium))
        button.setTarget_(target)
        button.setAction_("close:")
        button.sizeToFit()
        width = button.frame().size.width + 16
        accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width + 24, ACTION_HEIGHT + 12))
        button.setFrame_(NSMakeRect(12, 6, width, ACTION_HEIGHT))
        accessory.addSubview_(button)
        panel.setAccessoryView_(accessory)
        panel.setAccessoryViewDisclosed_(True)
    response = (run or (lambda modal: modal()))(panel.runModal)
    if target is not None and target.closed:
        return DIALOG_CLOSE
    if response != NSModalResponseOK:
        return None
    urls = panel.URLs()
    return str(urls[0].path()) if urls and len(urls) else None


# What ``run_waiting_dialog``'s dialog returns when its work ended (not a button).
WAITING_DONE = -2


class _WaitPoller(NSObject):
    """Timer target: ends a waiting dialog's modal session once ``done`` is set."""

    def initWithDialog_done_(self, dialog, done):
        self = objc.super(_WaitPoller, self).init()
        if self is None:
            return None
        self._dialog, self._done = dialog, done
        return self

    def poll_(self, _timer):
        if self._done.is_set():
            self._dialog.finish(WAITING_DONE)


def run_waiting_dialog(*, title, message, done, cancel="Cancel", run=None) -> bool:
    """A dialog with only ``cancel`` that also closes by itself once ``done`` is set.

    The poll timer runs in the common modes, so it fires inside the dialog's
    modal session. ``run`` wraps the modal call (the menu bar's ``_dialog``).
    Returns True when the owner pressed ``cancel`` before ``done`` was set.
    """
    if done.is_set():
        return False
    dialog = BrandDialog(title=title, message=message, ok=cancel)
    poller = _WaitPoller.alloc().initWithDialog_done_(dialog, done)
    timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(0.2, poller, "poll:", None, True)
    NSRunLoop.mainRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
    try:
        clicked = (run or (lambda modal: modal()))(dialog.runModal)
    finally:
        timer.invalidate()
    return clicked != WAITING_DONE and not done.is_set()
