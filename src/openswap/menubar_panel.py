"""Click popover for the macOS menu bar — drawn usage bars, not a text menu.

Imported only from ``menubar.run`` after rumps/AppKit are available. The
status item stays a short title; this panel is what opens on click.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSApp,
    NSAppearance,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSApplication,
    NSBezierPath,
    NSButton,
    NSButtonTypeSwitch,
    NSColor,
    NSControlSizeSmall,
    NSEvent,
    NSEventMaskLeftMouseDown,
    NSFont,
    NSFontAttributeName,
    NSFontWeightMedium,
    NSFontWeightRegular,
    NSFontWeightSemibold,
    NSGraphicsContext,
    NSLineBreakByTruncatingTail,
    NSNoImage,
    NSPopUpButton,
    NSPopover,
    NSPopoverBehaviorApplicationDefined,
    NSRectEdgeMinY,
    NSTextField,
    NSTrackingArea,
    NSTrackingActiveAlways,
    NSTrackingMouseEnteredAndExited,
    NSView,
    NSViewController,
    NSVisualEffectBlendingModeBehindWindow,
    NSVisualEffectMaterialMenu,
    NSVisualEffectStateActive,
    NSVisualEffectView,
)
try:
    from AppKit import NSSwitch
except ImportError:  # macOS 14 and older
    NSSwitch = None
from Foundation import (
    NSAttributedString,
    NSDistributedNotificationCenter,
    NSMakeRect,
    NSObject,
    NSPointInRect,
    NSTimer,
    NSUserDefaults,
)

from openswap.menubar import (
    MAIN_PAGE,
    MenuBarSettings,
    PANEL_WIDTH,
    POPOVER_AUTO_CLOSE_S,
    SETTINGS_PAGE,
    SETTINGS_POPUP_W,
    panel_accounts,
    resolve_popover_theme,
    settings_header_frames,
    settings_page_rows,
    window_suffix,
    status_item_length,
    trailing_header_frames,
)
from openswap.theme import (
    ACCENT,
    ACCENT_LIGHT,
    CRIT_PCT,
    FOREGROUND,
    FOREGROUND_LIGHT,
    MUTED,
    MUTED_LIGHT,
    SEV_CRIT,
    SEV_CRIT_LIGHT,
    SEV_OK,
    SEV_OK_LIGHT,
    SEV_WARN,
    SEV_WARN_LIGHT,
    TRACK,
    TRACK_LIGHT,
    WARN_PCT,
)

STATUS_AUTOSAVE_NAME = "com.opensoft.openswap.menubar"


def pin_status_item(nsstatusitem) -> None:
    """Remember Cmd-drag order across launches.

    macOS has no API to sit next to another app's extra (e.g. Claude). Setting
    an autosave name is what makes a user-placed position actually stick.
    """
    nsstatusitem.setAutosaveName_(STATUS_AUTOSAVE_NAME)


def fit_status_item(nsstatusitem, *, compact: bool, title: str | None = None) -> None:
    """Put the title on the button and size the extra.

    rumps writes ``NSStatusItem.setTitle_`` (deprecated). The visible extra
    is the button, so we set that too. The extra is text (optional ✻ in the
    string), so the image is always cleared. Compact (icon off) uses a tight
    length; otherwise the extra is variable-width.
    """
    button = nsstatusitem.button()
    if button is None:
        return
    if title is not None:
        try:
            button.setTitle_(title)
        except Exception:
            pass
    try:
        button.setImage_(None)
        button.setImagePosition_(NSNoImage)
    except Exception:
        pass
    shown = str(button.title() or title or "")
    width = 0.0
    if shown:
        font = button.font() or NSFont.menuBarFontOfSize_(0)
        width = (
            NSAttributedString.alloc()
            .initWithString_attributes_(shown, {NSFontAttributeName: font})
            .size()
            .width
        )
    nsstatusitem.setLength_(status_item_length(width, compact=compact))


PAD = 12.0
HEADER_H = 36.0
HOLD_LINE_H = 16.0
RUNNING_LINE_H = 16.0
FOOTER_H = 38.0
SECTION_H = 18.0
SETTINGS_TOGGLE_H = 30.0
SETTINGS_GROUP_H = 22.0
SETTINGS_CHOICE_LABEL_H = 16.0
SETTINGS_BTN_H = 22.0
SETTINGS_BTN_GAP_X = 6.0
SETTINGS_BTN_GAP_Y = 4.0
SETTINGS_ROW_GAP = 6.0
SETTINGS_POPUP_H = 24.0
SETTINGS_BACK_W = 64.0
SETTINGS_BACK_H = 22.0
CARD_GAP = 8.0
CARD_PAD = 11.0
CARD_RADIUS = 10.0
TITLE_H = 18.0
SUBTITLE_H = 14.0
ROW_H = 22.0
BAR_H = 6.0
BAR_MAX_W = 80.0
LABEL_W = 44.0
PCT_W = 48.0
COUNT_W = 60.0
COL_GAP = 8.0


def _card_height(card) -> float:
    n = len(card.get("windows") or [])
    if card.get("note"):
        n += 1
    n = max(n, 1)
    sub = SUBTITLE_H if card.get("subtitle") else 0.0
    return CARD_PAD * 2 + TITLE_H + sub + 6 + n * ROW_H


def _hex(color: str, alpha: float = 1.0):
    h = color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha)


# Dynamic NSColor providers are ObjC blocks over Python callables; keep them
# rooted so the GC cannot collect a provider the catalog still calls.
_DYNAMIC_PROVIDERS: list = []
_PALETTE = None


def _dynamic(light_hex, dark_hex, light_alpha=1.0, dark_alpha=1.0, *, name=None):
    def provider(appearance):
        match = appearance.bestMatchFromAppearancesWithNames_(
            [NSAppearanceNameDarkAqua, NSAppearanceNameAqua]
        )
        if match == NSAppearanceNameDarkAqua:
            return _hex(dark_hex, dark_alpha)
        return _hex(light_hex, light_alpha)

    _DYNAMIC_PROVIDERS.append(provider)
    return NSColor.colorWithName_dynamicProvider_(name, provider)


def _colors() -> dict:
    """Light/dark tokens that resolve against the drawing appearance."""
    global _PALETTE
    if _PALETTE is None:
        _PALETTE = {
            "fg": _dynamic(FOREGROUND_LIGHT, FOREGROUND, name="openswap.fg"),
            "muted": _dynamic(MUTED_LIGHT, MUTED, name="openswap.muted"),
            "accent": _dynamic(ACCENT_LIGHT, ACCENT, name="openswap.accent"),
            "card": _dynamic("#000000", "#ffffff", 0.04, 0.06, name="openswap.card"),
            "card_hover": _dynamic(
                "#000000", "#ffffff", 0.07, 0.10, name="openswap.cardHover"
            ),
            "card_active": _dynamic(
                "#000000", "#ffffff", 0.06, 0.09, name="openswap.cardActive"
            ),
            "ok": _dynamic(SEV_OK_LIGHT, SEV_OK, name="openswap.ok"),
            "warn": _dynamic(SEV_WARN_LIGHT, SEV_WARN, name="openswap.warn"),
            "crit": _dynamic(SEV_CRIT_LIGHT, SEV_CRIT, name="openswap.crit"),
            "track": _dynamic(TRACK_LIGHT, TRACK, name="openswap.track"),
            "hairline": _dynamic(
                "#000000", "#ffffff", 0.08, 0.08, name="openswap.hairline"
            ),
        }
    return _PALETTE


def _system_popover_appearance():
    """Named Aqua/DarkAqua for the popover, matching System Settings."""
    try:
        app_name = NSApplication.sharedApplication().effectiveAppearance().name()
    except Exception:
        app_name = None
    try:
        style = NSUserDefaults.standardUserDefaults().stringForKey_("AppleInterfaceStyle")
    except Exception:
        style = None
    theme = resolve_popover_theme(
        app_appearance_name=app_name, interface_style=style
    )
    key = NSAppearanceNameDarkAqua if theme == "dark" else NSAppearanceNameAqua
    return NSAppearance.appearanceNamed_(key)


def _sev(pct: float, pal: dict):
    if pct >= CRIT_PCT:
        return pal["crit"]
    if pct >= WARN_PCT:
        return pal["warn"]
    return pal["ok"]


def _button_width(title, font) -> float:
    """Width a rounded small button needs to show ``title`` untruncated."""
    btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 0, SETTINGS_BTN_H))
    btn.setTitle_(title)
    btn.setBezelStyle_(1)
    btn.setControlSize_(1)
    btn.setFont_(font)
    btn.sizeToFit()
    return btn.frame().size.width


def _measure_text(text, font) -> float:
    return (
        NSAttributedString.alloc()
        .initWithString_attributes_(text or "", {NSFontAttributeName: font})
        .size()
        .width
    )


def _label(text, font, color, frame, align="left"):
    field = NSTextField.alloc().initWithFrame_(frame)
    field.setStringValue_(text or "")
    field.setBezeled_(False)
    field.setBordered_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setFont_(font)
    field.setTextColor_(color)
    field.setLineBreakMode_(NSLineBreakByTruncatingTail)
    if align == "right":
        field.setAlignment_(2)  # NSTextAlignmentRight
    elif align == "center":
        field.setAlignment_(1)
    return field


class _PopupButton(NSPopUpButton):
    """Time popup whose menu keeps the popover from auto-closing."""

    def initWithPanel_frame_(self, panel, frame):
        self = objc.super(_PopupButton, self).initWithFrame_pullsDown_(frame, False)
        if self is None:
            return None
        self._panel = panel
        return self

    def mouseDown_(self, event):
        panel = getattr(self, "_panel", None)
        if panel is not None:
            panel._hold_overflow(True)
        try:
            objc.super(_PopupButton, self).mouseDown_(event)
        finally:
            if panel is not None:
                panel._hold_overflow(False)


class _Trampoline(NSObject):
    """ObjC target that forwards ``act:`` to a Python callable."""

    def initWithCallback_(self, callback):
        self = objc.super(_Trampoline, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def act_(self, sender):
        cb = getattr(self, "_callback", None)
        if cb:
            cb(sender)


class _AppearanceObserver(NSObject):
    """KVO + distributed-notification shim so the popover tracks Dark Mode."""

    def initWithCallback_(self, callback):
        self = objc.super(_AppearanceObserver, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def observeValueForKeyPath_ofObject_change_context_(self, keyPath, obj, change, context):
        cb = getattr(self, "_callback", None)
        if cb:
            cb()

    def themeChanged_(self, _note):
        cb = getattr(self, "_callback", None)
        if cb:
            cb()


class _FillView(NSView):
    """1px hairline (or other strip) that re-resolves its fill on appearance changes."""

    def initWithColor_(self, color):
        self = objc.super(_FillView, self).initWithFrame_(NSMakeRect(0, 0, 1, 1))
        if self is None:
            return None
        self._fill = color
        return self

    def isFlipped(self):
        return True

    def viewDidChangeEffectiveAppearance(self):
        objc.super(_FillView, self).viewDidChangeEffectiveAppearance()
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        self._fill.setFill()
        NSBezierPath.bezierPathWithRect_(self.bounds()).fill()


class _BarView(NSView):
    def initWithPct_threshold_stale_(self, pct, threshold, stale):
        self = objc.super(_BarView, self).initWithFrame_(NSMakeRect(0, 0, 100, BAR_H))
        if self is None:
            return None
        self.pct = max(0.0, min(float(pct), 100.0))
        self.threshold = threshold
        self.stale = bool(stale)
        return self

    def isFlipped(self):
        return True

    def viewDidChangeEffectiveAppearance(self):
        objc.super(_BarView, self).viewDidChangeEffectiveAppearance()
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        pal = _colors()
        bounds = self.bounds()
        radius = bounds.size.height / 2.0
        track = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, radius, radius
        )
        pal["track"].setFill()
        track.fill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, radius, radius
        ).addClip()
        if self.pct > 0:
            width = max(bounds.size.height, bounds.size.width * self.pct / 100.0)
            fill_rect = NSMakeRect(0, 0, width, bounds.size.height)
            fill = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                fill_rect, radius, radius
            )
            if self.stale:
                pal["muted"].colorWithAlphaComponent_(0.45).setFill()
            else:
                _sev(self.pct, pal).setFill()
            fill.fill()
        if self.threshold and not self.stale:
            x = bounds.size.width * max(0.0, min(float(self.threshold), 100.0)) / 100.0
            pal["warn"].colorWithAlphaComponent_(0.7).setFill()
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(x - 0.5, 0, 1.0, bounds.size.height)
            ).fill()


class _CardView(NSView):
    def initWithCard_onSwitch_(self, card, on_switch):
        self = objc.super(_CardView, self).initWithFrame_(NSMakeRect(0, 0, 100, 40))
        if self is None:
            return None
        self.card = card
        self.on_switch = on_switch
        self._hover = False
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, _event):
        # Accessory popover is often not key; without this the first click
        # focuses the window and the second click actually switches.
        return True

    def viewDidChangeEffectiveAppearance(self):
        objc.super(_CardView, self).viewDidChangeEffectiveAppearance()
        self.setNeedsDisplay_(True)

    def drawRect_(self, _rect):
        pal = _colors()
        if self._hover:
            color = pal["card_hover"]
        elif self.card.get("active"):
            color = pal["card_active"]
        else:
            color = pal["card"]
        bounds = self.bounds()
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            bounds, CARD_RADIUS, CARD_RADIUS
        )
        color.setFill()
        path.fill()
        if self.card.get("active"):
            NSGraphicsContext.saveGraphicsState()
            path.addClip()
            pal["accent"].setFill()
            NSBezierPath.bezierPathWithRect_(
                NSMakeRect(0, 0, 3, bounds.size.height)
            ).fill()
            NSGraphicsContext.restoreGraphicsState()

    def updateTrackingAreas(self):
        areas = list(self.trackingAreas() or [])
        for area in areas:
            self.removeTrackingArea_(area)
        options = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None
        )
        self.addTrackingArea_(area)
        objc.super(_CardView, self).updateTrackingAreas()

    def mouseEntered_(self, _event):
        self._hover = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, _event):
        self._hover = False
        self.setNeedsDisplay_(True)

    def hitTest_(self, point):
        # Labels sit on top of the card; route every hit to the card so a
        # click anywhere on the row switches.
        if objc.super(_CardView, self).hitTest_(point) is not None:
            return self
        return None

    def mouseUp_(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        if not NSPointInRect(loc, self.bounds()):
            return
        if self.on_switch and not self.card.get("disabled"):
            self.on_switch(self.card["num"])


class _RootView(NSVisualEffectView):
    """Flipped popover chrome; reports pointer enter/leave for auto-close."""

    def initWithHover_(self, hover):
        self = objc.super(_RootView, self).initWithFrame_(NSMakeRect(0, 0, 1, 1))
        if self is None:
            return None
        self._hover = hover
        return self

    def isFlipped(self):
        return True

    def updateTrackingAreas(self):
        for area in list(self.trackingAreas() or []):
            self.removeTrackingArea_(area)
        options = NSTrackingMouseEnteredAndExited | NSTrackingActiveAlways
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            self.bounds(), options, self, None
        )
        self.addTrackingArea_(area)
        objc.super(_RootView, self).updateTrackingAreas()

    def mouseEntered_(self, _event):
        cb = getattr(self, "_hover", None)
        if cb:
            cb(True)

    def mouseExited_(self, _event):
        cb = getattr(self, "_hover", None)
        if cb:
            cb(False)


class _PanelController(NSViewController):
    pass


class MenuBarPanel:
    """Status-item click target: transient popover with account usage bars."""

    def __init__(
        self,
        *,
        on_switch,
        on_rotate,
        on_best,
        on_toggle_auto,
        on_more,
        auto_enabled,
        snapshot,
        threshold,
        on_setting=None,
        settings=None,
        strategy=None,
    ):
        self._on_switch = on_switch
        self._on_rotate = on_rotate
        self._on_best = on_best
        self._on_toggle_auto = on_toggle_auto
        self._on_more = on_more
        self._on_setting = on_setting
        self._auto_enabled = auto_enabled
        self._snapshot = snapshot
        self._threshold = threshold
        self._settings = settings
        self._strategy = strategy
        self._page = MAIN_PAGE
        self._item = None
        self._popover = None
        self._controller = None
        self._tramps: list = []
        self._toggle_tramp = None
        self._appearance_obs = None
        self._close_timer = None
        self._close_tramp = None
        self._click_monitor = None
        self._click_handler = None
        self._menu_open = False

    def attach(self, nsstatusitem) -> None:
        self._item = nsstatusitem
        nsstatusitem.setMenu_(None)
        button = nsstatusitem.button()
        if button is None:
            return
        self._toggle_tramp = _Trampoline.alloc().initWithCallback_(self.toggle)
        button.setTarget_(self._toggle_tramp)
        button.setAction_("act:")
        self._popover = NSPopover.alloc().init()
        # ApplicationDefined: Transient closes the popover when More opens an
        # NSMenu (the extra is an accessory app and never becomes key).
        self._popover.setBehavior_(NSPopoverBehaviorApplicationDefined)
        self._popover.setAnimates_(True)
        self._sync_popover_appearance()
        self._watch_appearance()

    def _watch_appearance(self) -> None:
        if self._appearance_obs is not None:
            return
        obs = _AppearanceObserver.alloc().initWithCallback_(self._on_system_appearance)
        self._appearance_obs = obs
        try:
            NSApp.addObserver_forKeyPath_options_context_(
                obs, "effectiveAppearance", 1, None
            )
        except Exception:
            pass
        try:
            NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
                obs,
                "themeChanged:",
                "AppleInterfaceThemeChangedNotification",
                None,
            )
        except Exception:
            pass

    def _sync_popover_appearance(self) -> None:
        if self._popover is None:
            return
        try:
            self._popover.setAppearance_(_system_popover_appearance())
        except Exception:
            pass

    def _on_system_appearance(self) -> None:
        self._sync_popover_appearance()
        if self.is_shown():
            self.reload()

    def is_shown(self) -> bool:
        return bool(self._popover is not None and self._popover.isShown())

    def close(self) -> None:
        self._page = MAIN_PAGE
        self._clear_dismiss_watchers()
        if self._popover is not None and self._popover.isShown():
            self._popover.performClose_(None)

    def toggle(self, _sender=None) -> None:
        if self._popover is None or self._item is None:
            return
        if self._popover.isShown():
            self.close()
            return
        self._sync_popover_appearance()
        self.reload()
        button = self._item.button()
        if button is None:
            return
        self._popover.showRelativeToRect_ofView_preferredEdge_(
            button.bounds(), button, NSRectEdgeMinY
        )
        self._arm_auto_close()

    def reload(self) -> None:
        if self._popover is None:
            return
        view = self._build()
        controller = _PanelController.alloc().init()
        controller.setView_(view)
        self._controller = controller
        self._popover.setContentSize_(view.frame().size)
        self._popover.setContentViewController_(controller)
        if self._popover.isShown():
            self._arm_auto_close()

    def _cancel_close_timer(self) -> None:
        timer = self._close_timer
        self._close_timer = None
        if timer is not None:
            try:
                timer.invalidate()
            except Exception:
                pass

    def _reset_close_timer(self) -> None:
        self._cancel_close_timer()
        if not self.is_shown():
            return
        if self._close_tramp is None:
            self._close_tramp = _Trampoline.alloc().initWithCallback_(
                lambda *_a: self.close()
            )
        self._close_timer = (
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                POPOVER_AUTO_CLOSE_S, self._close_tramp, "act:", None, False
            )
        )

    def _mouse_in_popover(self) -> bool:
        try:
            vc = self._popover.contentViewController() if self._popover else None
            view = vc.view() if vc is not None else None
            win = view.window() if view is not None else None
            if win is None:
                return False
            return bool(NSPointInRect(NSEvent.mouseLocation(), win.frame()))
        except Exception:
            return False

    def _status_item_screen_rect(self):
        try:
            button = self._item.button() if self._item is not None else None
            if button is None:
                return None
            bwin = button.window()
            if bwin is None:
                return None
            return bwin.convertRectToScreen_(
                button.convertRect_toView_(button.bounds(), None)
            )
        except Exception:
            return None

    def _pointer_over_status_item(self) -> bool:
        rect = self._status_item_screen_rect()
        if rect is None:
            return False
        try:
            return bool(NSPointInRect(NSEvent.mouseLocation(), rect))
        except Exception:
            return False

    def _pointer_over_ui(self) -> bool:
        return self._mouse_in_popover() or self._pointer_over_status_item()

    def _on_hover(self, inside: bool) -> None:
        if self._menu_open:
            return
        if inside or self._pointer_over_status_item():
            self._cancel_close_timer()
        else:
            self._reset_close_timer()

    def _on_global_click(self, event) -> None:
        if not self.is_shown() or self._menu_open:
            return
        try:
            pt = event.locationInWindow()
            vc = self._popover.contentViewController()
            view = vc.view() if vc is not None else None
            win = view.window() if view is not None else None
            if win is not None and NSPointInRect(pt, win.frame()):
                return
            rect = self._status_item_screen_rect()
            if rect is not None and NSPointInRect(pt, rect):
                return
        except Exception:
            pass
        self.close()

    def _ensure_dismiss_watchers(self) -> None:
        if self._click_monitor is not None:
            return
        self._click_handler = self._on_global_click
        try:
            self._click_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                NSEventMaskLeftMouseDown, self._click_handler
            )
        except Exception:
            self._click_monitor = None
            self._click_handler = None

    def _clear_dismiss_watchers(self) -> None:
        self._cancel_close_timer()
        monitor = self._click_monitor
        self._click_monitor = None
        self._click_handler = None
        if monitor is not None:
            try:
                NSEvent.removeMonitor_(monitor)
            except Exception:
                pass

    def _arm_auto_close(self) -> None:
        self._ensure_dismiss_watchers()
        # Open from the extra leaves the pointer on the status item, not in the
        # popover. Do not start a leave timer until mouseExited actually fires.
        if self._menu_open or self._pointer_over_ui():
            self._cancel_close_timer()

    def _hold_overflow(self, holding: bool) -> None:
        """Pause leave-delay close while an NSMenu (More, time popup) is up."""
        self._menu_open = bool(holding)
        if holding:
            self._cancel_close_timer()
            return
        if self.is_shown():
            if self._pointer_over_ui():
                self._cancel_close_timer()
            else:
                self._reset_close_timer()

    def _more(self, sender):
        self._hold_overflow(True)
        try:
            self._on_more(sender)
        finally:
            self._hold_overflow(False)

    def _tramp(self, fn) -> _Trampoline:
        t = _Trampoline.alloc().initWithCallback_(fn)
        self._tramps.append(t)
        return t

    def _show_settings(self, _sender=None):
        self._page = SETTINGS_PAGE
        self.reload()

    def _show_main(self, _sender=None):
        self._page = MAIN_PAGE
        self.reload()

    def _emit_setting(self, row_id, value):
        cb = self._on_setting
        if cb is not None:
            cb(row_id, value)

    def _add_popup(self, root, options, current, frame, row_id, font):
        btn = _PopupButton.alloc().initWithPanel_frame_(self, frame)
        btn.setControlSize_(NSControlSizeSmall)
        btn.setFont_(font)
        btn.removeAllItems()
        selected = 0
        for i, (value, lab) in enumerate(options or []):
            btn.addItemWithTitle_(lab)
            item = btn.lastItem()
            if item is not None:
                item.setRepresentedObject_(value)
            if value == current:
                selected = i
        if btn.numberOfItems() > 0:
            btn.selectItemAtIndex_(selected)
        menu = btn.menu()
        if menu is not None:
            try:
                menu.setMinimumWidth_(frame.size.width)
            except Exception:
                pass
        btn.setTarget_(
            self._tramp(lambda sender, rid=row_id: self._on_popup(rid, sender))
        )
        btn.setAction_("act:")
        root.addSubview_(btn)
        return btn

    def _on_popup(self, row_id, sender):
        item = sender.selectedItem() if sender is not None else None
        value = item.representedObject() if item is not None else None
        self._emit_setting(row_id, value)

    def _add_button(self, root, title, frame, cb, font):
        btn = NSButton.alloc().initWithFrame_(frame)
        btn.setTitle_(title)
        btn.setBezelStyle_(1)  # rounded
        btn.setControlSize_(1)  # small
        btn.setFont_(font)
        btn.setTarget_(self._tramp(cb))
        btn.setAction_("act:")
        root.addSubview_(btn)
        return btn

    def _alloc_switch(self, on: bool, callback):
        if NSSwitch is not None:
            ctl = NSSwitch.alloc().initWithFrame_(NSMakeRect(0, 0, 54, 24))
            try:
                ctl.setControlSize_(NSControlSizeSmall)
            except Exception:
                pass
        else:
            ctl = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 40, 16))
            ctl.setButtonType_(NSButtonTypeSwitch)
            ctl.setTitle_("")
        ctl.sizeToFit()
        ctl.setState_(1 if on else 0)
        ctl.setTarget_(self._tramp(callback))
        ctl.setAction_("act:")
        return ctl

    def _build(self):
        self._tramps = []
        if self._page == SETTINGS_PAGE:
            return self._build_settings()
        snap = self._snapshot()
        cards = panel_accounts(snap)
        hold_line = snap.get("hold_line") or ""
        if not self._auto_enabled():
            hold_line = ""
        running_line = snap.get("running_line") or ""
        pal = _colors()

        body_h = 0.0
        if not cards:
            body_h = 48.0
        else:
            for card in cards:
                body_h += _card_height(card)
            body_h += CARD_GAP * (len(cards) - 1)
            if any(card.get("provider") == "codex" for card in cards):
                body_h += SECTION_H

        hold_h = HOLD_LINE_H if hold_line else 0.0
        running_h = RUNNING_LINE_H if running_line else 0.0
        height = PAD + HEADER_H + hold_h + 4 + body_h + PAD + running_h + FOOTER_H
        root = _RootView.alloc().initWithHover_(self._on_hover)
        root.setFrame_(NSMakeRect(0, 0, PANEL_WIDTH, height))
        root.setMaterial_(NSVisualEffectMaterialMenu)
        root.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        root.setState_(NSVisualEffectStateActive)

        font_title = NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold)
        font_body = NSFont.systemFontOfSize_weight_(12, NSFontWeightRegular)
        font_small = NSFont.systemFontOfSize_weight_(11, NSFontWeightRegular)
        font_digits = NSFont.monospacedDigitSystemFontOfSize_weight_(11, NSFontWeightMedium)
        font_label = NSFont.monospacedDigitSystemFontOfSize_weight_(11, NSFontWeightRegular)

        # Header: title on the left, auto-switch hugging the top-right.
        root.addSubview_(
            _label("openswap", font_title, pal["fg"], NSMakeRect(PAD, PAD, 80, 20))
        )
        auto_label = _label(
            "Auto-switch",
            font_small,
            pal["muted"],
            NSMakeRect(0, 0, 80, 18),
        )
        auto_label.sizeToFit()
        ls = auto_label.frame().size
        auto = self._alloc_switch(bool(self._auto_enabled()), self._on_toggle_auto)
        cs = auto.frame().size
        lab_f, ctl_f = trailing_header_frames(
            PANEL_WIDTH, PAD, (ls.width, ls.height), (cs.width, cs.height)
        )
        auto_label.setFrame_(NSMakeRect(*lab_f))
        auto.setFrame_(NSMakeRect(*ctl_f))
        root.addSubview_(auto_label)
        root.addSubview_(auto)

        inner_w = PANEL_WIDTH - PAD * 2
        y = PAD + HEADER_H
        if hold_line:
            root.addSubview_(
                _label(
                    hold_line,
                    font_small,
                    pal["muted"],
                    NSMakeRect(PAD, y, inner_w, HOLD_LINE_H),
                )
            )
            y += HOLD_LINE_H
        y += 4

        if not cards:
            root.addSubview_(
                _label(
                    "No managed accounts",
                    font_body,
                    pal["muted"],
                    NSMakeRect(PAD, y, inner_w, 20),
                )
            )
        else:
            sectioned = False
            for card in cards:
                if card.get("provider") == "codex" and not sectioned:
                    root.addSubview_(
                        _label(
                            "Codex",
                            font_small,
                            pal["muted"],
                            NSMakeRect(PAD, y, inner_w, SECTION_H),
                        )
                    )
                    y += SECTION_H
                    sectioned = True
                card_h = _card_height(card)
                card_view = _CardView.alloc().initWithCard_onSwitch_(
                    card, self._on_switch
                )
                card_view.setFrame_(NSMakeRect(PAD, y, inner_w, card_h))
                if card.get("disabled"):
                    card_view.setAlphaValue_(0.45)

                title = card["title"]
                if card.get("disabled"):
                    title = f"{title}  (disabled)"
                card_view.addSubview_(
                    _label(
                        title,
                        font_title,
                        pal["fg"],
                        NSMakeRect(CARD_PAD + 6, CARD_PAD, inner_w - CARD_PAD * 2 - 52, TITLE_H),
                    )
                )
                if card.get("active"):
                    badge = _label(
                        "active",
                        font_small,
                        pal["accent"],
                        NSMakeRect(inner_w - CARD_PAD - 48, CARD_PAD + 1, 48, TITLE_H),
                        align="right",
                    )
                    card_view.addSubview_(badge)

                subtitle = card.get("subtitle") or ""
                row_y = CARD_PAD + TITLE_H
                if subtitle:
                    card_view.addSubview_(
                        _label(
                            subtitle,
                            font_small,
                            pal["muted"],
                            NSMakeRect(
                                CARD_PAD + 6, row_y, inner_w - CARD_PAD * 2 - 8, SUBTITLE_H
                            ),
                        )
                    )
                    row_y += SUBTITLE_H
                row_y += 6
                if card["windows"]:
                    stale = bool(card.get("needs_relogin"))
                    label_x = CARD_PAD + 6
                    count_x = inner_w - CARD_PAD - COUNT_W
                    pct_x = count_x - COL_GAP - PCT_W
                    bar_x = label_x + LABEL_W
                    bar_w = min(BAR_MAX_W, max(24.0, pct_x - COL_GAP - bar_x))
                    for win in card["windows"]:
                        card_view.addSubview_(
                            _label(
                                win["label"],
                                font_label,
                                pal["muted"],
                                NSMakeRect(label_x, row_y - 2, LABEL_W - 4, ROW_H),
                            )
                        )
                        bar = _BarView.alloc().initWithPct_threshold_stale_(
                            win["pct"],
                            0 if stale else self._threshold(),
                            stale,
                        )
                        bar.setFrame_(
                            NSMakeRect(bar_x, row_y + (ROW_H - BAR_H) / 2 - 2, bar_w, BAR_H)
                        )
                        card_view.addSubview_(bar)
                        pct_color = pal["muted"] if stale else _sev(win["pct"], pal)
                        card_view.addSubview_(
                            _label(
                                f"{win['pct']:.0f}%",
                                font_digits,
                                pct_color,
                                NSMakeRect(pct_x, row_y - 2, PCT_W, ROW_H),
                                align="right",
                            )
                        )
                        suffix = window_suffix(win, stale=stale)
                        card_view.addSubview_(
                            _label(
                                suffix,
                                font_small,
                                pal["muted"],
                                NSMakeRect(count_x, row_y - 2, COUNT_W, ROW_H),
                                align="right",
                            )
                        )
                        row_y += ROW_H
                if card.get("note"):
                    note_color = pal["warn"] if card.get("needs_relogin") else pal["muted"]
                    card_view.addSubview_(
                        _label(
                            card["note"],
                            font_small,
                            note_color,
                            NSMakeRect(CARD_PAD + 6, row_y, inner_w - CARD_PAD * 2 - 8, ROW_H),
                        )
                    )
                    row_y += ROW_H

                root.addSubview_(card_view)
                y += card_h + CARD_GAP

        # Footer
        fy = height - FOOTER_H
        if running_line:
            root.addSubview_(
                _label(
                    running_line,
                    font_small,
                    pal["muted"],
                    NSMakeRect(PAD, fy - RUNNING_LINE_H, inner_w, RUNNING_LINE_H),
                )
            )
        hairline = _FillView.alloc().initWithColor_(pal["hairline"])
        hairline.setFrame_(NSMakeRect(PAD, fy, inner_w, 1))
        root.addSubview_(hairline)

        self._add_button(
            root, "Rotate", NSMakeRect(PAD, fy + 8, 62, 22), self._on_rotate, font_small
        )
        self._add_button(
            root, "Best", NSMakeRect(PAD + 70, fy + 8, 54, 22), self._on_best, font_small
        )
        self._add_button(
            root,
            "Settings",
            NSMakeRect(PAD + 134, fy + 8, 72, 22),
            self._show_settings,
            font_small,
        )
        self._add_button(
            root,
            "More",
            NSMakeRect(PANEL_WIDTH - PAD - 62, fy + 8, 62, 22),
            self._more,
            font_small,
        )

        return root

    def _build_settings(self):
        pal = _colors()
        font_title = NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold)
        font_body = NSFont.systemFontOfSize_weight_(12, NSFontWeightRegular)
        font_small = NSFont.systemFontOfSize_weight_(11, NSFontWeightRegular)
        inner_w = PANEL_WIDTH - PAD * 2
        settings = self._settings() if callable(self._settings) else MenuBarSettings()
        strategy = self._strategy() if callable(self._strategy) else "best"
        try:
            threshold = float(self._threshold())
        except Exception:
            threshold = 0.0
        rows = settings_page_rows(settings, strategy=strategy, threshold=threshold)

        def _choice_lines(row):
            lines = []
            line = []
            x = 0.0
            current = row.get("value")
            for value, lab in row.get("options") or []:
                title = f"✓ {lab}" if value == current else lab
                w = min(inner_w, max(52.0, _button_width(title, font_small)))
                if line and x + w > inner_w:
                    lines.append(line)
                    line = []
                    x = 0.0
                line.append((title, value, w))
                x += w + SETTINGS_BTN_GAP_X
            if line:
                lines.append(line)
            return lines

        def _row_height(row) -> float:
            kind = row.get("kind")
            if kind == "group":
                return SETTINGS_GROUP_H
            if kind == "toggle":
                return SETTINGS_TOGGLE_H
            if kind == "popup":
                return SETTINGS_TOGGLE_H
            if kind == "choice":
                n = max(len(_choice_lines(row)), 1)
                return SETTINGS_CHOICE_LABEL_H + n * (SETTINGS_BTN_H + SETTINGS_BTN_GAP_Y)
            return SETTINGS_TOGGLE_H

        body_h = 0.0
        for row in rows:
            body_h += _row_height(row) + SETTINGS_ROW_GAP
        height = PAD + HEADER_H + body_h + PAD
        root = _RootView.alloc().initWithHover_(self._on_hover)
        root.setFrame_(NSMakeRect(0, 0, PANEL_WIDTH, height))
        root.setMaterial_(NSVisualEffectMaterialMenu)
        root.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
        root.setState_(NSVisualEffectStateActive)

        title = _label("Settings", font_title, pal["fg"], NSMakeRect(0, 0, 80, 20))
        title.sizeToFit()
        ts = title.frame().size
        back_f, title_f = settings_header_frames(
            PAD, (SETTINGS_BACK_W, SETTINGS_BACK_H), (ts.width, ts.height)
        )
        self._add_button(root, "Back", NSMakeRect(*back_f), self._show_main, font_small)
        title.setFrame_(NSMakeRect(*title_f))
        root.addSubview_(title)
        hairline = _FillView.alloc().initWithColor_(pal["hairline"])
        hairline.setFrame_(NSMakeRect(PAD, PAD + HEADER_H - 4, inner_w, 1))
        root.addSubview_(hairline)

        y = PAD + HEADER_H
        for row in rows:
            kind = row.get("kind")
            h = _row_height(row)
            if kind == "group":
                root.addSubview_(
                    _label(
                        row.get("label") or "",
                        font_small,
                        pal["muted"],
                        NSMakeRect(PAD, y + 4, inner_w, SETTINGS_GROUP_H - 4),
                    )
                )
            elif kind == "toggle":
                root.addSubview_(
                    _label(
                        row.get("label") or "",
                        font_body,
                        pal["fg"],
                        NSMakeRect(PAD, y + 5, inner_w - 70, 20),
                    )
                )
                sw = self._alloc_switch(
                    bool(row.get("value")),
                    lambda _s, rid=row["id"]: self._emit_setting(rid, None),
                )
                cs = sw.frame().size
                sw.setFrame_(
                    NSMakeRect(
                        PANEL_WIDTH - PAD - cs.width,
                        y + (h - cs.height) / 2,
                        cs.width,
                        cs.height,
                    )
                )
                root.addSubview_(sw)
            elif kind == "popup":
                root.addSubview_(
                    _label(
                        row.get("label") or "",
                        font_body,
                        pal["fg"],
                        NSMakeRect(PAD, y + 5, inner_w - SETTINGS_POPUP_W - 8, 20),
                    )
                )
                self._add_popup(
                    root,
                    row.get("options") or [],
                    row.get("value"),
                    NSMakeRect(
                        PANEL_WIDTH - PAD - SETTINGS_POPUP_W,
                        y + (h - SETTINGS_POPUP_H) / 2,
                        SETTINGS_POPUP_W,
                        SETTINGS_POPUP_H,
                    ),
                    row["id"],
                    font_small,
                )
            elif kind == "choice":
                root.addSubview_(
                    _label(
                        row.get("label") or "",
                        font_small,
                        pal["muted"],
                        NSMakeRect(PAD, y, inner_w, SETTINGS_CHOICE_LABEL_H),
                    )
                )
                by = y + SETTINGS_CHOICE_LABEL_H
                rid = row["id"]
                for line in _choice_lines(row):
                    x = PAD
                    for title, value, w in line:
                        self._add_button(
                            root,
                            title,
                            NSMakeRect(x, by, w, SETTINGS_BTN_H),
                            lambda _s, i=rid, v=value: self._emit_setting(i, v),
                            font_small,
                        )
                        x += w + SETTINGS_BTN_GAP_X
                    by += SETTINGS_BTN_H + SETTINGS_BTN_GAP_Y
            y += h + SETTINGS_ROW_GAP

        return root
