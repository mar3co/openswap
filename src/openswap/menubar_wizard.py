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
    NSButton,
    NSBezierPath,
    NSColor,
    NSControlSizeLarge,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFocusRingTypeExterior,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSFontWeightMedium,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSKernAttributeName,
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
from Foundation import NSAttributedString, NSMakeRect, NSObject

from openswap.menubar_dialog import _DialogButton, _DialogSurface, _label
from openswap.setup_wizard import ACTION_CLOSE, ACTION_COPY_COMMAND

WIDTH = 540.0
PAD = 36.0
CONTENT = WIDTH - PAD * 2
BUTTON_H = 36.0
SMALL_BUTTON_H = 30.0
GAP = 10.0
LOCKUP_MARK = 22.0  # the mark's side in the wordmark
ACCOUNTS_SHOWN = 6  # account rows before "and N more", so the footer stays on screen


class _WizardButton(_DialogButton):
    """A dialog button that carries the action it reports."""


class _WizardSwitch(NSSwitch):
    """An NSSwitch that carries the action it reports."""


class _TextButton(NSButton):
    """Back and Skip: a plain label that acts like a button, no fill."""

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


class _WizardSurface(_DialogSurface):
    """White content over a grey footer (the dialog surface does the
    reverse, which reads as a grey sheet in light mode)."""

    def drawRect_(self, rect):
        NSColor.controlBackgroundColor().setFill()
        NSBezierPath.fillRect_(self.bounds())
        footer = getattr(self, "footer_y", None)
        if footer is not None:
            NSColor.windowBackgroundColor().setFill()
            NSBezierPath.fillRect_(NSMakeRect(
                0, footer, self.bounds().size.width, self.bounds().size.height - footer,
            ))
            NSColor.separatorColor().setFill()
            NSBezierPath.fillRect_(NSMakeRect(0, footer, self.bounds().size.width, 0.5))


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
        else:
            # Or Return would press the last page's button, which is gone.
            window.setDefaultButtonCell_(None)
            window.makeFirstResponder_(None)

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
    def _button(self, surface, title, action, *, primary=False, small=False, text=None, plain=False):
        """A footer or content button. ``primary`` is the one accent-filled
        button on the page (Return presses it); ``plain`` is text only."""
        height = SMALL_BUTTON_H if small else BUTTON_H
        cls = _TextButton if plain else _WizardButton
        button = cls.alloc().initWithFrame_(NSMakeRect(0, 0, 100, height))
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
            surface.primary = button
        button.sizeToFit()
        if plain:
            width = button.frame().size.width + 16
        else:
            width = max(96.0 if not small else 80.0, button.frame().size.width + 24)
        button.setFrameSize_((width, height))
        surface.addSubview_(button)
        return button

    def _symbol(self, surface, name, x, y, *, size=16.0):
        """A subtle SF Symbol in the secondary label color; nothing when the
        system has no such symbol."""
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if image is None:
            return
        view = NSImageView.alloc().initWithFrame_(NSMakeRect(x, y, size, size))
        view.setImage_(image)
        view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
        view.setContentTintColor_(NSColor.secondaryLabelColor())
        view.setAccessibilityElement_(False)
        surface.addSubview_(view)

    def _lead(self, surface, row, y):
        """Draw a row's symbol, if any; return where its text starts."""
        if row.get("symbol"):
            self._symbol(surface, row["symbol"], PAD, y, size=16)
            return PAD + 26, CONTENT - 26
        return PAD, CONTENT

    def _text(self, surface, text, y, *, size=13, weight=NSFontWeightRegular,
              color=None, x=PAD, width=CONTENT, spaced=False):
        field = _label(str(text), _font(size, weight), color or NSColor.labelColor(), width, spaced=spaced)
        field.setFrameOrigin_((x, y))
        surface.addSubview_(field)
        return field.frame().size.height

    def _lockup(self, surface, x, y):
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
                NSFontAttributeName: _font(15, NSFontWeightSemibold),
                NSForegroundColorAttributeName: NSColor.labelColor(),
                NSKernAttributeName: -0.4,
            },
        ))
        word.sizeToFit()
        size = word.frame().size
        word.setFrameOrigin_((x + LOCKUP_MARK + 8, y + (LOCKUP_MARK - size.height) / 2))
        surface.addSubview_(word)

    def _build(self, page):
        surface = _WizardSurface.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 480))
        surface.primary = None
        y = PAD - 4
        self._lockup(surface, PAD, y)
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
        # Right to left: the accent primary, a gray secondary, a text Skip.
        # Back is text on the left. `surface.primary` may already be an
        # actions item (the account steps).
        primary_spec = page.get("primary")
        secondary_spec = page.get("secondary")
        skip_spec = page.get("skip")
        x = WIDTH - PAD
        if primary_spec:
            button = self._button(surface, primary_spec["title"], primary_spec["action"], primary=True)
            button.setEnabled_(bool(primary_spec.get("enabled", True)))
            x -= button.frame().size.width
            button.setFrameOrigin_((x, footer_y))
            x -= GAP
        if secondary_spec:
            button = self._button(surface, secondary_spec["title"], secondary_spec["action"])
            if surface.primary is None:
                button.setKeyEquivalent_("\r")
            x -= button.frame().size.width
            button.setFrameOrigin_((x, footer_y))
            x -= GAP
        if skip_spec:
            button = self._button(surface, skip_spec["title"], skip_spec["action"], plain=True)
            x -= button.frame().size.width
            button.setFrameOrigin_((x, footer_y))
        if page.get("back"):
            button = self._button(surface, "‹ Back", "back", plain=True)
            button.setFrameOrigin_((PAD - 8, footer_y))
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
            for symbol, title, text in row["items"]:
                self._symbol(surface, symbol, PAD, y + 1, size=18)
                x, w = PAD + 30, CONTENT - 30
                y += self._text(surface, title, y, size=13, weight=NSFontWeightSemibold, x=x, width=w)
                y += 2
                y += self._text(surface, text, y, size=12, color=NSColor.secondaryLabelColor(), x=x, width=w)
                y += 12
            return y - top - 12
        if kind == "item":
            top = y
            x, w = self._lead(surface, row, y)
            y += self._text(surface, row["title"], y, size=13, weight=NSFontWeightSemibold, x=x, width=w)
            y += 2
            y += self._text(surface, row["text"], y, size=12, color=NSColor.secondaryLabelColor(), x=x, width=w)
            return y - top
        if kind == "actions":
            x = PAD
            for item in row["items"]:
                button = self._button(
                    surface, item["title"], item["action"], small=True,
                    primary=bool(item.get("primary")),
                )
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
            x, w = self._lead(surface, row, y + 1)
            label_h = self._text(surface, row["label"], y + 2, size=13, x=x, width=w - size.width - 12)
            switch.setFrameOrigin_((WIDTH - PAD - size.width, y))
            surface.addSubview_(switch)
            y += max(label_h + 2, size.height)
            if row.get("hint"):
                y += 4
                y += self._text(surface, row["hint"], y, size=12, color=NSColor.secondaryLabelColor(),
                                x=x, width=w - size.width - 12)
            return y - top
        if kind == "accounts":
            items = list(row["items"])
            more = max(0, len(items) - ACCOUNTS_SHOWN)
            if more:
                items = items[:ACCOUNTS_SHOWN]
            line = 22.0
            box_h = 16 + line * (len(items) + (1 if more else 0))
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
            if more:
                field = _label(f"and {more} more", _font(13), NSColor.secondaryLabelColor(), CONTENT - 44)
                field.setFrameOrigin_((PAD + 32, ly + 1))
                surface.addSubview_(field)
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
            x, w = self._lead(surface, row, y)
            y += self._text(surface, row["title"], y, size=13, weight=NSFontWeightSemibold, x=x, width=w)
            y += 2
            y += self._text(surface, row["text"], y, size=12, color=NSColor.secondaryLabelColor(), x=x, width=w)
            y += 6
            copy_w = 64.0
            field = _label(
                row["command"], NSFont.monospacedSystemFontOfSize_weight_(11, NSFontWeightRegular),
                NSColor.secondaryLabelColor(), w - copy_w - 12,
            )
            field.setSelectable_(True)
            line_h = max(SMALL_BUTTON_H, field.frame().size.height)
            field.setFrameOrigin_((x, y + (line_h - field.frame().size.height) / 2))
            surface.addSubview_(field)
            button = self._button(surface, "Copy", row["action"], small=True, text=row["command"])
            button.setFrame_(NSMakeRect(WIDTH - PAD - copy_w, y, copy_w, SMALL_BUTTON_H))
            y += line_h
            return y - top
        return 0.0


def make_setup_wizard(on_action) -> SetupWizard:
    return SetupWizard(on_action)

