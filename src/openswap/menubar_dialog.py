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
    NSLineBreakByWordWrapping,
    NSPanel,
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
from Foundation import NSAttributedString, NSMakeRect, NSMutableParagraphStyle


DIALOG_WIDTH = 440.0
DIALOG_PADDING = 28.0
DIALOG_CONTENT_WIDTH = DIALOG_WIDTH - DIALOG_PADDING * 2
DIALOG_INPUT_HEIGHT = 40.0
ACTION_HEIGHT = 40.0
ACTION_GAP = 10.0


@dataclass(frozen=True)
class DialogResponse:
    clicked: int
    text: str


class _DialogSurface(NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        NSColor.windowBackgroundColor().setFill()
        NSBezierPath.fillRect_(self.bounds())
        footer = getattr(self, "footer_y", None)
        if footer is not None:
            NSColor.controlBackgroundColor().setFill()
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


class _DialogPanel(NSPanel):
    def cancelOperation_(self, sender):
        self.dialog.finish(0)

    def performKeyEquivalent_(self, event):
        # Accessory apps have no Edit menu; forward standard shortcuts to the
        # native field editor, including secure fields, while a prompt is open.
        modifiers = event.modifierFlags()
        if (modifiers & NSEventModifierFlagCommand
                and not modifiers & (NSEventModifierFlagControl | NSEventModifierFlagOption)):
            key = str(event.charactersIgnoringModifiers() or "").lower()
            action = {
                "a": "selectAll:", "c": "copy:", "x": "cut:", "v": "paste:",
                "z": "redo:" if modifiers & NSEventModifierFlagShift else "undo:",
            }.get(key)
            if action and NSApplication.sharedApplication().sendAction_to_from_(action, None, self):
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


class BrandDialog:
    """One reusable modal surface; 1 confirms, 0 cancels, -1 is the third action."""

    def __init__(
        self, *, title=None, message="", ok=None, cancel=None, other=None,
        default_text=None, secure=False, destructive=False,
    ):
        shown_title = str(title or "OpenSwap")
        if shown_title.casefold() == "openswap":
            shown_title = "OpenSwap"
        self._result = 0
        self._running = False
        self.textfield = None
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
        y = DIALOG_PADDING
        mark = NSImageView.alloc().initWithFrame_(NSMakeRect(DIALOG_PADDING, y, 28, 28))
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
        brand = _label("OpenSwap", NSFont.systemFontOfSize_weight_(12, NSFontWeightMedium),
                       NSColor.secondaryLabelColor(), 160)
        brand.setFrameOrigin_((DIALOG_PADDING + 38, y + (28 - brand.frame().size.height) / 2))
        surface.addSubview_(brand)
        y += 48

        heading = _label(shown_title, NSFont.systemFontOfSize_weight_(20, NSFontWeightSemibold),
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
            widths.append(max(96.0, button.frame().size.width + 16))
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
        stacked = sum(widths) + ACTION_GAP * (len(widths) - 1) > DIALOG_CONTENT_WIDTH
        footer_height = 40 + (len(widths) * (ACTION_HEIGHT + ACTION_GAP) - ACTION_GAP
                              if stacked else ACTION_HEIGHT)

        if message:
            y += 12
            body = _label(str(message), NSFont.systemFontOfSize_(13),
                          NSColor.secondaryLabelColor(), DIALOG_CONTENT_WIDTH, spaced=True)
            body.setSelectable_(True)
            self.body = body
            screen = window.screen()
            max_height = min(640, screen.visibleFrame().size.height - 80) if screen else 640
            input_space = DIALOG_INPUT_HEIGHT + 20 if default_text is not None else 0
            body_limit = max(80, max_height - y - input_space - 28 - footer_height)
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

        if default_text is not None:
            y += 20
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
            field.setAccessibilityLabel_(shown_title)
            field.cell().setUsesSingleLineMode_(True)
            field.cell().setScrollable_(True)
            surface.addSubview_(field)
            self.textfield = field
            window.setInitialFirstResponder_(field)
            y += DIALOG_INPUT_HEIGHT

        y += 28
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
        window.setContentSize_((DIALOG_WIDTH, y + footer_height))
        surface.setFrameSize_((DIALOG_WIDTH, y + footer_height))
        if self.textfield is not None:
            self.textfield.setNextKeyView_(self.buttons[0])
        for current, following in zip(self.buttons, self.buttons[1:]):
            current.setNextKeyView_(following)
        self.buttons[-1].setNextKeyView_(self.textfield or self.buttons[0])

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
        return DialogResponse(clicked, str(self.textfield.stringValue()) if self.textfield else "")


def make_dialog_alert(**kwargs):
    return BrandDialog(**kwargs)


def make_dialog_prompt(*, default_text="", **kwargs):
    return BrandDialog(default_text=default_text, **kwargs)
