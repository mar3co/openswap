"""Click popover for the macOS menu bar — drawn usage bars, not a text menu.

Imported only from ``menubar.run`` after rumps/AppKit are available. The
status item stays a short title; this panel is what opens on click.
"""

from __future__ import annotations

import time
from pathlib import Path

import objc
from AppKit import (
    NSApp,
    NSAppearance,
    NSAppearanceNameAqua,
    NSAppearanceNameDarkAqua,
    NSApplication,
    NSBezierPath,
    NSButton,
    NSButtonTypePushOnPushOff,
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
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSLineBreakByTruncatingTail,
    NSLineBreakByWordWrapping,
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
    NSWorkspace,
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

from openswap.brand_motion import (
    BRAND_MOTION_DURATION,
    brand_mark_centers,
    brand_motion_progress,
)
from openswap.menubar import (
    MAIN_PAGE,
    MenuBarSettings,
    PANEL_WIDTH,
    POPOVER_AUTO_CLOSE_S,
    SETTINGS_PAGE,
    SETTINGS_POPUP_W,
    SETTINGS_SECTION_AUTOMATION,
    SETTINGS_SECTION_GENERAL,
    SETTINGS_SECTIONS,
    panel_accounts,
    provider_cards,
    provider_empty_state,
    login_panel_state,
    provider_shared_copy,
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
SETTINGS_TABS_H = 40.0
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
TAB_H = 40.0
TAB_GAP = 6.0
INFO_LINE_H = 16.0
LOGIN_BRAND_SIZE = 56.0
LOGIN_BRAND_SPACE = 68.0


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
_BRAND_IMAGE = None


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


def _brand_mark(frame, tint):
    """OpenSoft monogram from the packaged brand asset, tinted like text."""
    global _BRAND_IMAGE
    if _BRAND_IMAGE is None:
        path = Path(__file__).with_name("assets") / "opensoft-symbol-64.png"
        _BRAND_IMAGE = NSImage.alloc().initWithContentsOfFile_(str(path))
        if _BRAND_IMAGE is not None:
            _BRAND_IMAGE.setTemplate_(True)
    if _BRAND_IMAGE is None:
        return None
    view = NSImageView.alloc().initWithFrame_(frame)
    view.setImage_(_BRAND_IMAGE)
    view.setImageScaling_(NSImageScaleProportionallyUpOrDown)
    try:
        view.setContentTintColor_(tint)
    except Exception:
        pass
    return view


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


class _BrandMotionView(NSView):
    """One-shot native rendering of the OpenSoft ring-to-mark motion."""

    def initWithFrame_tint_elapsed_(self, frame, tint, elapsed):
        self = objc.super(_BrandMotionView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._tint = tint
        self._elapsed = max(0.0, float(elapsed))
        self._started_at = None
        self._timer = None
        fraction = min(self._elapsed / BRAND_MOTION_DURATION, 1.0)
        self._progress = brand_motion_progress(fraction)
        try:
            self.setAccessibilityElement_(False)
        except Exception:
            pass
        return self

    def isFlipped(self):
        return True

    def _reduce_motion(self) -> bool:
        try:
            return bool(
                NSWorkspace.sharedWorkspace().accessibilityDisplayShouldReduceMotion()
            )
        except Exception:
            return False

    def _stop_timer(self) -> None:
        timer = self._timer
        self._timer = None
        if timer is not None:
            timer.invalidate()

    def viewDidMoveToWindow(self):
        objc.super(_BrandMotionView, self).viewDidMoveToWindow()
        if self.window() is None:
            self._stop_timer()
            return
        if self._reduce_motion() or self._elapsed >= BRAND_MOTION_DURATION:
            self._progress = 1.0
            self.setNeedsDisplay_(True)
            return
        if self._timer is None:
            self._started_at = time.monotonic() - self._elapsed
            self._timer = (
                NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    1.0 / 60.0, self, "tick:", None, True
                )
            )

    def tick_(self, _timer):
        elapsed = time.monotonic() - self._started_at
        fraction = min(elapsed / BRAND_MOTION_DURATION, 1.0)
        self._progress = brand_motion_progress(fraction)
        if fraction >= 1.0:
            self._progress = 1.0
            self._stop_timer()
        self.setNeedsDisplay_(True)

    def viewDidChangeEffectiveAppearance(self):
        objc.super(_BrandMotionView, self).viewDidChangeEffectiveAppearance()
        self.setNeedsDisplay_(True)

    def _half_path(self, center_y: float, *, right: bool):
        bounds = self.bounds()
        side = min(bounds.size.width, bounds.size.height)
        scale = side / 32.0
        origin_x = (bounds.size.width - side) / 2.0
        origin_y = (bounds.size.height - side) / 2.0

        def point(x, y):
            return (origin_x + x * scale, origin_y + y * scale)

        radius = 8.0
        control = radius * 0.5522847498
        direction = 1.0 if right else -1.0
        path = NSBezierPath.bezierPath()
        path.moveToPoint_(point(16.0, center_y - radius))
        path.curveToPoint_controlPoint1_controlPoint2_(
            point(16.0 + direction * radius, center_y),
            point(16.0 + direction * control, center_y - radius),
            point(16.0 + direction * radius, center_y - control),
        )
        path.curveToPoint_controlPoint1_controlPoint2_(
            point(16.0, center_y + radius),
            point(16.0 + direction * radius, center_y + control),
            point(16.0 + direction * control, center_y + radius),
        )
        path.setLineWidth_(4.0 * scale)
        return path

    def drawRect_(self, _rect):
        left_y, right_y = brand_mark_centers(self._progress)
        self._tint.setStroke()
        self._half_path(left_y, right=False).stroke()
        self._half_path(right_y, right=True).stroke()


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
        on_toggle_chatgpt_auto=None,
        on_review_chatgpt_switch=None,
        chatgpt_switch_pending=None,
        on_setting=None,
        settings=None,
        strategy=None,
        has_codex=None,
        codex_enabled=None,
        desktop_status=None,
        account_state=None,
        on_empty_action=None,
        login_state=None,
        on_login_action=None,
    ):
        self._on_switch = on_switch
        self._on_rotate = on_rotate
        self._on_best = on_best
        self._on_toggle_auto = on_toggle_auto
        self._on_toggle_chatgpt_auto = on_toggle_chatgpt_auto
        self._on_review_chatgpt_switch = on_review_chatgpt_switch
        self._chatgpt_switch_pending = chatgpt_switch_pending or (lambda: False)
        self._on_more = on_more
        self._on_setting = on_setting
        self._auto_enabled = auto_enabled
        self._snapshot = snapshot
        self._threshold = threshold
        self._settings = settings
        self._strategy = strategy
        self._has_codex = has_codex
        self._codex_enabled = codex_enabled
        self._desktop_status = desktop_status or (lambda: "Experimental · Switching reopens ChatGPT")
        self._account_state = account_state or (lambda _provider: "ready")
        self._on_empty_action = on_empty_action
        self._login_state = login_state or (lambda: {"stage": "idle"})
        self._on_login_action = on_login_action
        self._login_alias = ""
        self._login_alias_field = None
        self._login_brand_started_at = None
        # Deliberately kept outside close()/reload(): users can inspect a
        # second provider without losing their place when the popover closes.
        self._selected_provider = "claude"
        self._settings_section = SETTINGS_SECTION_GENERAL
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

    def popover_window(self):
        """The popover's window while shown, else None."""
        if not self.is_shown():
            return None
        try:
            return self._popover.contentViewController().view().window()
        except AttributeError:  # no controller or view yet
            return None

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

    def _select_provider(self, provider: str) -> None:
        if provider not in ("claude", "chatgpt"):
            return
        self._selected_provider = provider
        if self.is_shown():
            self.reload()

    def show_login(self) -> None:
        """Open the ChatGPT onboarding surface in the current popover."""
        self._selected_provider = "chatgpt"
        self._page = MAIN_PAGE
        self.reload()

    def _login_action(self, action: str, alias: str | None = None) -> None:
        if self._on_login_action is not None:
            self._on_login_action(action, alias=alias)

    def _save_login(self, field) -> None:
        try:
            self._login_alias = field.stringValue().strip()
        except Exception:
            self._login_alias = ""
        self._login_action("save", self._login_alias or None)

    def _login_state_model(self) -> dict:
        try:
            return login_panel_state(self._login_state())
        except Exception:
            return login_panel_state({"stage": "error", "message": "Sign-in is unavailable."})

    def _login_brand_elapsed(self, stage: str) -> float:
        if stage not in {"starting", "waiting"}:
            self._login_brand_started_at = None
            return 0.0
        now = time.monotonic()
        if self._login_brand_started_at is None:
            self._login_brand_started_at = now
        return max(0.0, now - self._login_brand_started_at)

    def _empty_action(self, provider, action):
        if self._on_empty_action is not None:
            self._on_empty_action(provider, action)

    def _tramp(self, fn) -> _Trampoline:
        t = _Trampoline.alloc().initWithCallback_(fn)
        self._tramps.append(t)
        return t

    def _show_settings(self, _sender=None):
        self._settings_section = (
            SETTINGS_SECTION_AUTOMATION
            if self._selected_provider == "chatgpt"
            else SETTINGS_SECTION_GENERAL
        )
        self._page = SETTINGS_PAGE
        self.reload()

    def _show_main(self, _sender=None):
        self._page = MAIN_PAGE
        self.reload()

    def _select_settings_section(self, section: str) -> None:
        if section not in {value for value, _label in SETTINGS_SECTIONS}:
            return
        self._settings_section = section
        if self.is_shown():
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
        all_cards = panel_accounts(snap)
        provider = self._selected_provider
        cards = provider_cards(all_cards, provider)
        current_login_model = self._login_state_model()
        login_model = current_login_model if provider == "chatgpt" else login_panel_state(None)
        login_view = provider == "chatgpt" and login_model["stage"] != "idle"
        branded_login = login_view and login_model["stage"] in {"starting", "waiting"}
        brand_elapsed = self._login_brand_elapsed(current_login_model["stage"])
        if current_login_model["stage"] == "ready" and self._login_alias_field is not None:
            try:
                self._login_alias = self._login_alias_field.stringValue().strip()
            except Exception:
                pass
        elif current_login_model["stage"] != "ready":
            self._login_alias = ""
        self._login_alias_field = None
        empty = provider_empty_state(provider, self._account_state(provider)) if not cards else None
        if login_view:
            empty = None
        shared_copy = provider_shared_copy(provider)
        hold_line = snap.get("hold_line") or ""
        if provider != "claude" or not self._auto_enabled():
            hold_line = ""
        running_line = (snap.get("running_line") or "") if provider == "claude" else ""
        codex_running_line = (snap.get("codex_running_line") or "") if provider == "chatgpt" else ""
        desktop_status = str(self._desktop_status() or "") if provider == "chatgpt" else ""
        if empty or login_view:
            shared_copy = hold_line = running_line = codex_running_line = desktop_status = ""
        pal = _colors()

        body_h = 0.0
        if login_view:
            body_h = 220.0 if login_model["stage"] == "ready" else (230.0 if len(login_model.get("actions") or []) > 2 else 184.0)
            if branded_login:
                body_h += LOGIN_BRAND_SPACE
        elif not cards:
            body_h = 184.0
        else:
            for card in cards:
                body_h += _card_height(card)
            body_h += CARD_GAP * (len(cards) - 1)

        hold_h = HOLD_LINE_H if hold_line else 0.0
        info_h = INFO_LINE_H if shared_copy else 0.0
        n_running = (1 if running_line else 0) + (1 if codex_running_line else 0)
        running_h = RUNNING_LINE_H * n_running
        status_h = RUNNING_LINE_H if desktop_status else 0.0
        height = PAD + HEADER_H + TAB_H + hold_h + info_h + 4 + body_h + PAD + running_h + status_h + FOOTER_H
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

        # Header: OpenSoft mark + product name on the left, provider-specific
        # automation switch hugging the top-right.
        mark = _brand_mark(NSMakeRect(PAD, PAD + 1, 18, 18), pal["fg"])
        if mark is not None:
            root.addSubview_(mark)
        root.addSubview_(
            _label(
                "OpenSwap",
                font_title,
                pal["fg"],
                NSMakeRect(PAD + (24 if mark is not None else 0), PAD, 86, 20),
            )
        )
        auto_label = _label(
            "Auto-switch",
            font_small,
            pal["muted"],
            NSMakeRect(0, 0, 80, 18),
        )
        auto_label.sizeToFit()
        ls = auto_label.frame().size
        chatgpt_auto = bool(
            getattr(self._settings() if callable(self._settings) else None,
                    "chatgpt_auto_enabled", False)
        )
        auto = self._alloc_switch(
            chatgpt_auto if provider == "chatgpt" else bool(self._auto_enabled()),
            self._on_toggle_chatgpt_auto if provider == "chatgpt" else self._on_toggle_auto,
        )
        cs = auto.frame().size
        lab_f, ctl_f = trailing_header_frames(
            PANEL_WIDTH, PAD, (ls.width, ls.height), (cs.width, cs.height)
        )
        auto_label.setFrame_(NSMakeRect(*lab_f))
        auto.setFrame_(NSMakeRect(*ctl_f))
        if ((provider == "claude" and cards) or
                (provider == "chatgpt" and self._on_toggle_chatgpt_auto is not None)):
            root.addSubview_(auto_label)
            root.addSubview_(auto)

        inner_w = PANEL_WIDTH - PAD * 2
        y = PAD + HEADER_H
        tab_w = (inner_w - TAB_GAP) / 2.0
        for index, (tab_provider, title) in enumerate((("claude", "Claude"), ("chatgpt", "ChatGPT"))):
            tab = self._add_button(
                root,
                title,
                NSMakeRect(PAD + index * (tab_w + TAB_GAP), y, tab_w, TAB_H),
                lambda _sender, p=tab_provider: self._select_provider(p),
                font_small,
            )
            if tab_provider == provider:
                tab.setButtonType_(NSButtonTypePushOnPushOff)
                tab.setState_(1)
                tab.setFont_(font_title)
            else:
                tab.setButtonType_(NSButtonTypePushOnPushOff)
                tab.setState_(0)
                tab.setAlphaValue_(0.68)
        y += TAB_H
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
        if shared_copy:
            root.addSubview_(_label(shared_copy, font_small, pal["muted"], NSMakeRect(PAD, y, inner_w, INFO_LINE_H)))
            y += INFO_LINE_H
        y += 4

        if login_view:
            # Login state is intentionally a compact, replace-in-place panel:
            # the roster never appears alongside an in-flight attempt.
            title_y = y + 14
            body_y = y + 44
            title_align = "left"
            if branded_login:
                brand = _BrandMotionView.alloc().initWithFrame_tint_elapsed_(
                    NSMakeRect(
                        PAD + (inner_w - LOGIN_BRAND_SIZE) / 2.0,
                        y + 10,
                        LOGIN_BRAND_SIZE,
                        LOGIN_BRAND_SIZE,
                    ),
                    pal["fg"],
                    brand_elapsed,
                )
                root.addSubview_(brand)
                title_y = y + 76
                body_y = y + 104
                title_align = "center"
            root.addSubview_(
                _label(
                    login_model["title"],
                    font_title,
                    pal["fg"],
                    NSMakeRect(PAD + 12, title_y, inner_w - 24, 22),
                    align=title_align,
                )
            )
            body = login_model.get("body") or ""
            body_height = 70 if login_model["stage"] == "error" else 34
            if body:
                label = _label(
                    body,
                    font_body,
                    pal["muted"],
                    NSMakeRect(PAD + 12, body_y, inner_w - 24, body_height),
                    align="center" if branded_login else "left",
                )
                label.cell().setUsesSingleLineMode_(False)
                label.cell().setScrollable_(False)
                label.cell().setWraps_(True)
                label.cell().setLineBreakMode_(NSLineBreakByWordWrapping)
                root.addSubview_(label)
            row_y = body_y + body_height if body else (y + 104 if branded_login else y + 48)
            if login_model.get("email"):
                root.addSubview_(_label(login_model["email"], font_body, pal["fg"], NSMakeRect(PAD + 12, row_y, inner_w - 24, 20)))
                row_y += 22
            if login_model.get("plan"):
                root.addSubview_(_label(login_model["plan"], font_small, pal["muted"], NSMakeRect(PAD + 12, row_y, inner_w - 24, 18)))
                row_y += 24
            workspace_id = login_model.get("workspace_id") or login_model.get("account_id")
            if workspace_id:
                workspace = _label(f"Workspace · {workspace_id}", font_small, pal["muted"], NSMakeRect(PAD + 12, row_y, inner_w - 24, 18))
                workspace.setToolTip_("Workspace identifier")
                root.addSubview_(workspace)
                row_y += 22
            if login_model["stage"] == "ready":
                alias_field = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 12, row_y, inner_w - 24, 28))
                alias_field.setPlaceholderString_("Nickname (optional)")
                alias_field.setStringValue_(self._login_alias)
                alias_field.setFont_(font_body)
                alias_field.cell().setUsesSingleLineMode_(True)
                alias_field.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
                root.addSubview_(alias_field)
                self._login_alias_field = alias_field
                row_y += 36
                button_w = (inner_w - 24 - 6) / 2
                save = self._add_button(root, "Save account", NSMakeRect(PAD + 12, row_y, button_w, 40), lambda _s, f=alias_field: self._save_login(f), font_body)
                save.setEnabled_(self._on_login_action is not None)
                cancel = self._add_button(root, "Cancel", NSMakeRect(PAD + 12 + button_w + 6, row_y, button_w, 40), lambda _s: self._login_action("cancel"), font_body)
                cancel.setEnabled_(self._on_login_action is not None)
            else:
                if login_model.get("device_code"):
                    code = _label(login_model["device_code"], font_digits, pal["fg"], NSMakeRect(PAD + 12, row_y, inner_w - 24, 24), align="center")
                    root.addSubview_(code)
                    row_y += 30
                actions = login_model.get("actions") or []
                labels = {"cancel": "Cancel", "copy_link": "Copy link", "retry": "Try again", "device": "Use a code", "open_browser": "Open browser", "copy_code": "Copy code", "start": "Sign in with ChatGPT", "dismiss": "Done"}
                enabled_actions = [a for a in actions if a in labels]
                for index, action in enumerate(enabled_actions):
                    width = (inner_w - 24 - 6) / 2 if len(enabled_actions) > 1 else inner_w - 24
                    x = PAD + 12 + (width + 6) * (index % 2)
                    yy = row_y + (46 * (index // 2))
                    btn = self._add_button(root, labels[action], NSMakeRect(x, yy, width, 40), lambda _s, a=action: self._login_action(a), font_body)
                    if action == "copy_link":
                        btn.setToolTip_("Open this link in another browser profile to use a different account.")
                    elif action == "device":
                        btn.setToolTip_("Device sign-in must be enabled by your account or workspace.")
                    btn.setEnabled_(self._on_login_action is not None)
                if login_model.get("hint"):
                    root.addSubview_(_label(login_model["hint"], font_small, pal["muted"], NSMakeRect(PAD + 12, y + body_h - 22, inner_w - 24, 18)))
        elif not cards:
            root.addSubview_(
                _label(
                    empty["title"], font_title, pal["fg"],
                    NSMakeRect(PAD + 12, y + 14, inner_w - 24, 22),
                )
            )
            for text, offset, h, font in ((empty["body"], 42, 54, font_body),
                                          (empty["hint"], 144, 32, font_small)):
                label = _label(text, font, pal["muted"], NSMakeRect(PAD + 12, y + offset, inner_w - 24, h))
                label.cell().setUsesSingleLineMode_(False)
                label.cell().setScrollable_(False)
                label.cell().setWraps_(True)
                label.cell().setLineBreakMode_(NSLineBreakByWordWrapping)
                root.addSubview_(label)
            if empty["action"]:
                button = self._add_button(
                    root, empty["button"], NSMakeRect(PAD + 12, y + 98, inner_w - 24, 40),
                    lambda _sender, p=provider, a=empty["action"]: self._empty_action(p, a), font_body,
                )
                button.setEnabled_(self._on_empty_action is not None)
                secondary = empty.get("secondary_action")
                if secondary:
                    secondary_btn = self._add_button(
                        root, empty.get("secondary_button", "Save current login"),
                        NSMakeRect(PAD + 12, y + 144, inner_w - 24, 40),
                        lambda _sender, p=provider, a=secondary: self._empty_action(p, a), font_body,
                    )
                    secondary_btn.setEnabled_(self._on_empty_action is not None)
        else:
            for card in cards:
                card_h = _card_height(card)
                card_view = _CardView.alloc().initWithCard_onSwitch_(
                    card, self._on_switch
                )
                card_view.setFrame_(NSMakeRect(PAD, y, inner_w, card_h))
                if card.get("disabled"):
                    card_view.setAlphaValue_(0.45)

                title = card["title"]
                if card.get("disabled"):
                    title = (
                        f"{title}  (CLI only)"
                        if card.get("api_key")
                        else f"{title}  ({'paused' if provider == 'chatgpt' else 'disabled'})"
                    )
                badge_w = 58 if provider == "chatgpt" else 48
                title_w = inner_w - CARD_PAD * 2 - 8
                if card.get("active"):
                    title_w -= badge_w + 8
                card_view.addSubview_(
                    _label(
                        title,
                        font_title,
                        pal["fg"],
                        NSMakeRect(CARD_PAD + 6, CARD_PAD, title_w, TITLE_H),
                    )
                )
                if card.get("active"):
                    badge_text = "selected" if provider == "chatgpt" else "active"
                    badge = _label(
                        badge_text,
                        font_small,
                        pal["accent"],
                        NSMakeRect(inner_w - CARD_PAD - badge_w, CARD_PAD + 1, badge_w, TITLE_H),
                        align="right",
                    )
                    if provider == "chatgpt":
                        badge.setToolTip_(
                            "Selected in the shared credential file. Verify the account in ChatGPT after switching."
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
        extra = RUNNING_LINE_H if (running_line and codex_running_line) else 0.0
        if running_line:
            root.addSubview_(
                _label(
                    running_line,
                    font_small,
                    pal["muted"],
                    NSMakeRect(PAD, fy - RUNNING_LINE_H - extra, inner_w, RUNNING_LINE_H),
                )
            )
        if codex_running_line:
            root.addSubview_(
                _label(
                    codex_running_line,
                    font_small,
                    pal["muted"],
                    NSMakeRect(PAD, fy - RUNNING_LINE_H, inner_w, RUNNING_LINE_H),
                )
            )
        hairline = _FillView.alloc().initWithColor_(pal["hairline"])
        hairline.setFrame_(NSMakeRect(PAD, fy, inner_w, 1))
        root.addSubview_(hairline)

        if provider == "claude" and cards:
            self._add_button(
                root, "Rotate", NSMakeRect(PAD, fy + 8, 62, 22), self._on_rotate, font_small
            )
            self._add_button(
                root, "Best", NSMakeRect(PAD + 70, fy + 8, 54, 22), self._on_best, font_small
            )
        else:
            if desktop_status:
                root.addSubview_(_label(desktop_status, font_small, pal["muted"], NSMakeRect(PAD, fy - running_h - RUNNING_LINE_H, inner_w, RUNNING_LINE_H)))
            if provider == "chatgpt" and cards:
                add = self._add_button(root, "Add account", NSMakeRect(PAD, fy + 8, 90, 22), lambda _s: self._login_action("start"), font_small)
                add.setEnabled_(self._on_login_action is not None)
                if self._on_review_chatgpt_switch is not None and self._chatgpt_switch_pending():
                    self._add_button(root, "Review switch", NSMakeRect(PAD + 96, fy + 8, 100, 22), self._on_review_chatgpt_switch, font_small)
        self._add_button(
            root, "Settings",
            NSMakeRect(PAD + (134 if provider == "claude" and cards else (202 if provider == "chatgpt" and cards and self._chatgpt_switch_pending() else (96 if provider == "chatgpt" and cards else 0))), fy + 8, 72, 22),
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
        font_group = NSFont.systemFontOfSize_weight_(11, NSFontWeightSemibold)
        inner_w = PANEL_WIDTH - PAD * 2
        settings = self._settings() if callable(self._settings) else MenuBarSettings()
        strategy = self._strategy() if callable(self._strategy) else "best"
        try:
            threshold = float(self._threshold())
        except Exception:
            threshold = 0.0
        try:
            has_codex = bool(self._has_codex()) if callable(self._has_codex) else False
        except Exception:
            has_codex = False
        try:
            codex_enabled = (
                bool(self._codex_enabled()) if callable(self._codex_enabled) else True
            )
        except Exception:
            codex_enabled = True
        rows = settings_page_rows(
            settings,
            strategy=strategy,
            threshold=threshold,
            has_codex=has_codex,
            codex_enabled=codex_enabled,
            section=self._settings_section,
        )

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
                return 34.0 if row.get("style") == "hint" else SETTINGS_GROUP_H
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
        height = PAD + HEADER_H + SETTINGS_TABS_H + body_h + PAD
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
        tab_w = (inner_w - TAB_GAP) / len(SETTINGS_SECTIONS)
        for index, (section, label) in enumerate(SETTINGS_SECTIONS):
            tab = self._add_button(
                root,
                label,
                NSMakeRect(PAD + index * (tab_w + TAB_GAP), y, tab_w, SETTINGS_TABS_H),
                lambda _sender, value=section: self._select_settings_section(value),
                font_small,
            )
            tab.setButtonType_(NSButtonTypePushOnPushOff)
            selected = section == self._settings_section
            tab.setState_(1 if selected else 0)
            if selected:
                tab.setFont_(font_group)
            else:
                tab.setAlphaValue_(0.68)
        y += SETTINGS_TABS_H

        for row in rows:
            kind = row.get("kind")
            h = _row_height(row)
            if kind == "group":
                is_hint = row.get("style") == "hint"
                label = _label(
                    row.get("label") or "",
                    font_small if is_hint else font_group,
                    pal["muted"] if is_hint else pal["fg"],
                    NSMakeRect(PAD, y + (2 if is_hint else 4), inner_w, h - 4),
                )
                if is_hint:
                    label.cell().setUsesSingleLineMode_(False)
                    label.cell().setScrollable_(False)
                    label.cell().setWraps_(True)
                    label.cell().setLineBreakMode_(NSLineBreakByWordWrapping)
                root.addSubview_(label)
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
                sw.setEnabled_(not bool(row.get("disabled")))
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
