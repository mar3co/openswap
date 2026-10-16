"""The setup wizard's window (plan 021).

Imported only by the menu bar app, lazily, like ``menubar_dialog``. It draws
one :func:`openswap.setup_wizard.wizard_page` at a time and reports the
action its buttons and switches name; the app decides what each one does.
Return is the primary button; Escape and the close button report ``close``.
"""

from __future__ import annotations

import weakref
from pathlib import Path

from AppKit import (
    NSApplication,
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSBezierPath,
    NSColor,
    NSControlSizeLarge,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFocusRingTypeExterior,
    NSFont,
    NSFontWeightMedium,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSPasteboard,
    NSPasteboardTypeString,
    NSSwitch,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskFullSizeContentView,
    NSWindowStyleMaskTitled,
    NSWindowTitleHidden,
)
from Foundation import NSMakeRect, NSObject

from openswap.menubar_dialog import _DialogButton, _DialogSurface, _label
from openswap.setup_wizard import ACTION_CLOSE, ACTION_COPY_COMMAND

WIDTH = 540.0
PAD = 36.0
CONTENT = WIDTH - PAD * 2
BUTTON_H = 36.0
SMALL_BUTTON_H = 30.0
GAP = 10.0


class _WizardButton(_DialogButton):
    """A dialog button that carries the action it reports."""


class _WizardSwitch(NSSwitch):
    """An NSSwitch that carries the action it reports."""


class _Box(NSView):
    """A rounded, lightly filled group behind accounts, commands and summaries."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        NSColor.quaternaryLabelColor().colorWithAlphaComponent_(0.07).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 10, 10).fill()


class _Dots(NSView):
    """Step n of total, as dots."""

    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        current, total = getattr(self, "progress", (0, 0))
        for i in range(total):
            color = NSColor.controlAccentColor() if i < current else NSColor.tertiaryLabelColor()
            color.setFill()
            NSBezierPath.bezierPathWithOvalInRect_(NSMakeRect(i * 14, 0, 7, 7)).fill()


class _Target(NSObject):
    def clicked_(self, sender):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner._clicked(sender)


class _Window(NSWindow):
    def cancelOperation_(self, sender):
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner._report(ACTION_CLOSE)


class _Delegate(NSObject):
    def windowShouldClose_(self, sender):
        # The close button is Not Now: the app decides, then closes it.
        owner = getattr(self, "owner", None)
        if owner is not None:
            owner._report(ACTION_CLOSE)
        return False


def _font(size, weight=NSFontWeightRegular):
    return NSFont.systemFontOfSize_weight_(size, weight)


class SetupWizard:
    """One window for the whole wizard; :meth:`show` draws a page."""

    def __init__(self, on_action):
        self._on_action = on_action
        self._target = _Target.alloc().init()
        self._target.owner = weakref.proxy(self)
        self._delegate = _Delegate.alloc().init()
        self._delegate.owner = weakref.proxy(self)
        window = _Window.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, 480),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskFullSizeContentView,
            NSBackingStoreBuffered, False,
        )
        window.owner = weakref.proxy(self)
        window.setReleasedWhenClosed_(False)
        window.setTitle_("Set Up OpenSwap")
        window.setTitleVisibility_(NSWindowTitleHidden)
        window.setTitlebarAppearsTransparent_(True)
        window.setMovableByWindowBackground_(True)
        window.setHidesOnDeactivate_(False)
        window.setDelegate_(self._delegate)
        self._window = window
        self._placed = False
        self.page = None

    # ---- what the app calls -------------------------------------------------
    def show(self, page: dict) -> None:
        """Draw ``page`` and bring the window to the front."""
        self.render(page)
        if not self._placed:
            self._window.center()
            self._placed = True
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def render(self, page: dict) -> None:
        """Redraw in place, keeping the window's top edge where it was."""
        self.page = page
        surface, height = self._build(page)
        window = self._window
        frame = window.frame()
        top = frame.origin.y + frame.size.height
        window.setContentView_(surface)
        content = window.frameRectForContentRect_(NSMakeRect(0, 0, WIDTH, height))
        new = NSMakeRect(frame.origin.x, top - content.size.height, content.size.width, content.size.height)
        window.setFrame_display_animate_(new, True, self._placed and window.isVisible())
        primary = getattr(surface, "primary", None)
        if primary is not None:
            window.setDefaultButtonCell_(primary.cell())
            window.makeFirstResponder_(primary)

    def close(self) -> None:
        self._window.orderOut_(None)

    def is_visible(self) -> bool:
        return bool(self._window.isVisible())

    def window(self):
        return self._window

    # ---- actions ------------------------------------------------------------
    def _report(self, action):
        if action:
            self._on_action(action)

    def _clicked(self, sender):
        action = getattr(sender, "wizard_action", None)
        if action == ACTION_COPY_COMMAND:
            self._copy(sender)
            return
        self._report(action)

    def _copy(self, sender):
        text = getattr(sender, "wizard_text", "") or ""
        board = NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(text, NSPasteboardTypeString)
        sender.setTitle_("Copied")
        sender.setNeedsDisplay_(True)

    # ---- drawing ------------------------------------------------------------
    def _button(self, surface, title, action, *, primary=False, small=False, text=None):
        height = SMALL_BUTTON_H if small else BUTTON_H
        button = _WizardButton.alloc().initWithFrame_(NSMakeRect(0, 0, 100, height))
        button.setTitle_(title)
        button.setBezelStyle_(NSBezelStyleRounded)
        button.setBordered_(False)
        button.setControlSize_(NSControlSizeLarge)
        button.setFont_(_font(12 if small else 13, NSFontWeightMedium))
        button.setFocusRingType_(NSFocusRingTypeExterior)
        button.setTarget_(self._target)
        button.setAction_("clicked:")
        button.setTag_(1 if primary else 0)
        button.wizard_action = action
        button.wizard_text = text
        if primary:
            button.setKeyEquivalent_("\r")
        button.sizeToFit()
        width = max(96.0 if not small else 80.0, button.frame().size.width + 24)
        button.setFrameSize_((width, height))
        surface.addSubview_(button)
        return button

    def _text(self, surface, text, y, *, size=13, weight=NSFontWeightRegular,
              color=None, x=PAD, width=CONTENT, spaced=False):
        field = _label(str(text), _font(size, weight), color or NSColor.labelColor(), width, spaced=spaced)
        field.setFrameOrigin_((x, y))
        surface.addSubview_(field)
        return field.frame().size.height

    def _build(self, page):
        surface = _DialogSurface.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 480))
        y = PAD - 4
        mark = NSImageView.alloc().initWithFrame_(NSMakeRect(PAD, y, 26, 26))
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
        brand = _label("OpenSwap", _font(12, NSFontWeightMedium), NSColor.secondaryLabelColor(), 160)
        brand.setFrameOrigin_((PAD + 36, y + (26 - brand.frame().size.height) / 2))
        surface.addSubview_(brand)
        progress = page.get("progress")
        if progress:
            dots = _Dots.alloc().initWithFrame_(NSMakeRect(0, 0, progress[1] * 14, 8))
            dots.progress = progress
            dots.setFrameOrigin_((WIDTH - PAD - progress[1] * 14 + 7, y + 9))
            dots.setAccessibilityLabel_(f"Step {progress[0]} of {progress[1]}")
            surface.addSubview_(dots)
        y += 50

        y += self._text(surface, page["title"], y, size=22, weight=NSFontWeightSemibold)
        if page.get("body"):
            y += 8
            y += self._text(surface, page["body"], y, color=NSColor.secondaryLabelColor(), spaced=True)
        for row in page.get("rows") or []:
            y += 18
            y += self._row(surface, row, y)

        y += 32
        surface.footer_y = y
        footer_y = y + 20
        primary_spec = page.get("primary")
        secondary_spec = page.get("secondary")
        x = WIDTH - PAD
        surface.primary = None
        if primary_spec:
            button = self._button(surface, primary_spec["title"], primary_spec["action"], primary=True)
            button.setEnabled_(bool(primary_spec.get("enabled", True)))
            width = button.frame().size.width
            x -= width
            button.setFrameOrigin_((x, footer_y))
            x -= GAP
            surface.primary = button
        if secondary_spec:
            button = self._button(surface, secondary_spec["title"], secondary_spec["action"])
            if not primary_spec:
                button.setKeyEquivalent_("\r")
            width = button.frame().size.width
            x -= width
            button.setFrameOrigin_((x, footer_y))
        if page.get("back"):
            button = self._button(surface, "Back", "back")
            button.setFrameOrigin_((PAD, footer_y))
        height = footer_y + BUTTON_H + 20
        surface.setFrameSize_((WIDTH, height))
        return surface, height

    def _row(self, surface, row, y) -> float:
        kind = row.get("kind")
        if kind == "note":
            color = NSColor.systemOrangeColor() if row.get("tone") == "warning" else NSColor.secondaryLabelColor()
            return self._text(surface, row["text"], y, size=12, color=color)
        if kind == "features":
            top = y
            for title, text in row["items"]:
                y += self._text(surface, title, y, size=13, weight=NSFontWeightSemibold)
                y += 2
                y += self._text(surface, text, y, size=12, color=NSColor.secondaryLabelColor())
                y += 12
            return y - top - 12
        if kind == "item":
            top = y
            y += self._text(surface, row["title"], y, size=13, weight=NSFontWeightSemibold)
            y += 2
            y += self._text(surface, row["text"], y, size=12, color=NSColor.secondaryLabelColor())
            return y - top
        if kind == "actions":
            x = PAD
            for item in row["items"]:
                button = self._button(surface, item["title"], item["action"], small=True)
                button.setEnabled_(bool(item.get("enabled", True)))
                width = button.frame().size.width
                if x > PAD and x + width > WIDTH - PAD:
                    x = PAD
                    y += SMALL_BUTTON_H + 8
                button.setFrameOrigin_((x, y))
                x += width + 8
            return SMALL_BUTTON_H
        if kind == "switch":
            top = y
            switch = _WizardSwitch.alloc().initWithFrame_(NSMakeRect(0, 0, 40, 24))
            switch.setState_(NSControlStateValueOn if row.get("value") else NSControlStateValueOff)
            switch.setEnabled_(not row.get("disabled"))
            switch.setTarget_(self._target)
            switch.setAction_("clicked:")
            switch.wizard_action = row["action"]
            switch.setAccessibilityLabel_(row["label"])
            switch.sizeToFit()
            size = switch.frame().size
            label_h = self._text(surface, row["label"], y + 2, size=13, width=CONTENT - size.width - 12)
            switch.setFrameOrigin_((WIDTH - PAD - size.width, y))
            surface.addSubview_(switch)
            y += max(label_h + 2, size.height)
            if row.get("hint"):
                y += 4
                y += self._text(surface, row["hint"], y, size=12, color=NSColor.secondaryLabelColor(),
                                width=CONTENT - size.width - 12)
            return y - top
        if kind == "accounts":
            items = row["items"]
            line = 22.0
            box_h = 16 + line * len(items)
            box = _Box.alloc().initWithFrame_(NSMakeRect(PAD, y, CONTENT, box_h))
            surface.addSubview_(box)
            ly = y + 8
            for title, subtitle in items:
                check = _label("✓", _font(12, NSFontWeightSemibold), NSColor.systemGreenColor(), 16)
                check.setFrameOrigin_((PAD + 14, ly + 2))
                surface.addSubview_(check)
                text = str(title) + (f"  ·  {subtitle}" if subtitle else "")
                field = _label(text, _font(13), NSColor.labelColor(), CONTENT - 44)
                field.setFrameOrigin_((PAD + 32, ly + 1))
                field.setFrameSize_((CONTENT - 44, line))
                surface.addSubview_(field)
                ly += line
            return box_h
        if kind == "summary":
            items = row["items"]
            line = 24.0
            box_h = 16 + line * len(items)
            box = _Box.alloc().initWithFrame_(NSMakeRect(PAD, y, CONTENT, box_h))
            surface.addSubview_(box)
            ly = y + 8
            for label, value in items:
                left = _label(label, _font(13), NSColor.secondaryLabelColor(), CONTENT / 2)
                left.setFrameOrigin_((PAD + 16, ly + 3))
                surface.addSubview_(left)
                right = _label(value, _font(13, NSFontWeightMedium), NSColor.labelColor(), CONTENT / 2 - 32)
                width = min(CONTENT / 2 - 32, right.attributedStringValue().size().width + 4)
                right.setFrame_(NSMakeRect(
                    WIDTH - PAD - 16 - width, ly + 3, width, right.frame().size.height,
                ))
                surface.addSubview_(right)
                ly += line
            return box_h
        if kind == "command":
            top = y
            y += self._text(surface, row["title"], y, size=13, weight=NSFontWeightSemibold)
            y += 2
            y += self._text(surface, row["text"], y, size=12, color=NSColor.secondaryLabelColor())
            y += 8
            copy_w = 80.0
            text_w = CONTENT - copy_w - 8 - 20
            field = _label(
                row["command"], NSFont.monospacedSystemFontOfSize_weight_(11, NSFontWeightRegular),
                NSColor.labelColor(), text_w,
            )
            field.setSelectable_(True)
            box_h = max(SMALL_BUTTON_H, field.frame().size.height + 14)
            box = _Box.alloc().initWithFrame_(NSMakeRect(PAD, y, CONTENT - copy_w - 8, box_h))
            surface.addSubview_(box)
            field.setFrameOrigin_((PAD + 10, y + (box_h - field.frame().size.height) / 2))
            surface.addSubview_(field)
            button = self._button(surface, "Copy", row["action"], small=True, text=row["command"])
            button.setFrame_(NSMakeRect(WIDTH - PAD - copy_w, y, copy_w, SMALL_BUTTON_H))
            y += box_h
            return y - top
        return 0.0


def make_setup_wizard(on_action) -> SetupWizard:
    return SetupWizard(on_action)

