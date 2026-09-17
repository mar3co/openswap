"""Pure helpers for the macOS extra. Import-safe without rumps.

The rumps app lives in ``openswap.menubar``, which re-exports these names so
existing tests and the popover keep importing from ``openswap.menubar``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

from openswap import pace
from openswap.exceptions import ClaudeSwitchError, CredentialReadError
from openswap.kickoff import (
    KICKOFF_RETRY_BACKOFF_S,
    invoke_kickoff,
    kickoff_account_eligible,
    kickoff_backoff_active,
    kickoff_is_due,
    kickoff_pass_complete,
    kickoff_time_options,
    kickoff_time_value,
    kickoff_uses_default_login,
    parse_kickoff_time,
)
from openswap.autoswitch import record_manual_switch
from openswap.paths import get_backup_root
from openswap.settings import atomic_write_json
from openswap.engine import SENTINEL_NOTES, USAGE_API_KEY, USAGE_RELOGIN_REQUIRED

REFRESH_CHOICES: tuple[int, ...] = (30, 60, 300)
AUTO_THRESHOLD_CHOICES: tuple[int, ...] = (80, 90, 95, 98)
AUTO_STRATEGY_CHOICES: tuple[tuple[str, str], ...] = (
    ("best", "Most quota left"),
    ("consume-first", "Burn weekly first"),
    ("soonest-5h", "Burn 5-hour first"),
)
AUTO_STRATEGY_HINTS: dict[str, str] = {
    "best": "Picks the account with the most quota left.",
    "consume-first": "Picks the account whose 7-day window resets soonest.",
    "soonest-5h": "Picks the account whose 5-hour session resets soonest.",
}
_HOLD_WINDOW = {
    "consume-first": "weekly",
    "soonest-5h": "5-hour",
}
TITLE_PCT_CHOICES: tuple[str, ...] = ("off", "5h", "7d", "both")
MENUBAR_SETTINGS_FILENAME = "menubar_settings.json"


def menubar_settings_path(backup_root: Path) -> Path:
    """Extra display + kickoff file. Not ``settings.json`` (shared policy)."""
    return backup_root / MENUBAR_SETTINGS_FILENAME


def title_shows_5h(title_pct: str) -> bool:
    return title_pct in ("5h", "both")


def title_shows_7d(title_pct: str) -> bool:
    return title_pct in ("7d", "both")


def combine_title_pct(show_5h: bool, show_7d: bool) -> str:
    """Map the two extra toggles back to the stored ``title_pct`` value."""
    if show_5h and show_7d:
        return "both"
    if show_5h:
        return "5h"
    if show_7d:
        return "7d"
    return "off"
REFRESH_LABELS: dict[int, str] = {30: "30 seconds", 60: "60 seconds", 300: "5 minutes"}
SETTINGS_PAGE = "settings"
MAIN_PAGE = "main"
SETTINGS_SECTION_GENERAL = "general"
SETTINGS_SECTION_AUTOMATION = "automation"
SETTINGS_SECTIONS: tuple[tuple[str, str], ...] = (
    (SETTINGS_SECTION_GENERAL, "General"),
    (SETTINGS_SECTION_AUTOMATION, "Automation"),
)
SWITCH_HISTORY_LIMIT = 10
NOTIFICATION_BUNDLE_ID = "com.opensoft.openswap.menubar"
RELOGIN_CARD_NOTE = "Signed out. Log in with Claude Code, then click this card."
_CLAUDE_PATH_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")


def ensure_notification_identity(
    executable: Path | None = None,
    *,
    platform: str = sys.platform,
) -> Path | None:
    """Ensure rumps can resolve a bundle identifier for notifications.

    Command-line Python tools have no app bundle, so rumps looks for an
    ``Info.plist`` beside the interpreter. uv/pipx reinstalls can recreate that
    environment; repair the tiny plist on every launch when needed.
    """
    if platform != "darwin":
        return None
    if getattr(sys, "frozen", False):
        # Inside a real .app bundle Contents/Info.plist already carries the
        # bundle id, and writing next to the executable would break the
        # code signature.
        return None
    path = (executable or Path(sys.executable)).parent / "Info.plist"
    data: dict = {}
    try:
        if path.exists():
            try:
                loaded = plistlib.loads(path.read_bytes())
            except Exception:
                loaded = None  # unreadable/corrupt — rebuild from scratch
            if isinstance(loaded, dict):
                data = loaded
        changed = False
        if not data.get("CFBundleIdentifier"):
            data["CFBundleIdentifier"] = NOTIFICATION_BUNDLE_ID
            changed = True
        if not data.get("CFBundleName"):
            data["CFBundleName"] = "openswap"
            changed = True
        if changed or not path.exists():
            # atomic: an interrupted write must not leave a half-written plist
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(plistlib.dumps(data))
            os.replace(tmp, path)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        logging.getLogger("openswap").warning(
            "Could not prepare menu-bar notification identity: %s", exc
        )
        return None
    return path


def _json_matches_field(value: object, default: object) -> bool:
    """True when a JSON value is safe to assign onto a MenuBarSettings field.

    ``bool`` is a subclass of ``int``, so ``isinstance(True, int)`` is True.
    A JSON ``true`` must not become ``refresh_interval`` or ``kickoff_hour``.
    """
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, type(default))


@dataclass
class MenuBarSettings:
    """Extra display + kickoff, persisted as ``menubar_settings.json``.

    Auto-switch *policy* (threshold, cooldown, hysteresis) lives in
    ``settings.json`` via ``openswap.settings``, so the CLI and extra share it.
    Engine cooldown / last switch / quarantine live in ``autoswitch_state.json``.
    """

    show_account_name: bool = True
    title_pct: str = "both"  # one of TITLE_PCT_CHOICES
    title_scoped: bool = False  # append per-model weekly limits (e.g. Fable) to the title
    refresh_interval: int = 60
    auto_switch_enabled: bool = False
    # Separate from Claude's live rotation: this only proposes desktop targets.
    chatgpt_auto_enabled: bool = False
    show_icon: bool = False  # optional ✻ in the status-item title; off by default
    confirm_switch: bool = True  # ask before a card click swaps the live login
    kickoff_enabled: bool = False
    kickoff_hour: int = 7
    kickoff_minute: int = 0
    kickoff_last_date: str = ""  # local YYYY-MM-DD of the last kickoff run

    @classmethod
    def load(cls, path: Path) -> "MenuBarSettings":
        """Load settings, falling back to defaults on any problem.

        Unknown keys are ignored; a value whose type doesn't match the field
        default is dropped (that field keeps its default). A missing or
        unparseable file yields all-defaults. ``title_pct`` must be one of
        ``TITLE_PCT_CHOICES``.
        """
        defaults = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return defaults
        if not isinstance(raw, dict):
            return defaults
        kwargs = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if not _json_matches_field(value, getattr(defaults, f.name)):
                continue
            if f.name == "title_pct" and value not in TITLE_PCT_CHOICES:
                continue
            kwargs[f.name] = value
        return cls(**kwargs)

    def save(self, path: Path) -> None:
        """Atomically write settings (0600 file, 0700 parent, through a symlink)."""
        atomic_write_json(path, asdict(self))


def settings_page_rows(
    settings: MenuBarSettings,
    *,
    strategy: str,
    threshold: float,
    has_codex: bool = False,
    codex_enabled: bool = True,
    section: str | None = None,
) -> list[dict]:
    """Rows for the in-popover settings page. No AppKit.

    Each dict: ``{"kind": "toggle"|"choice"|"group"|"popup", "id": str, "label": str, ...}``.
    Choice and popup rows include ``options`` ``(value, label)`` and the current
    ``value``. Toggles include a bool ``value``. Child rows are omitted while
    their parent is off (shared automation policy, kickoff time). Every row is
    tagged with a settings section so AppKit can keep the page compact; callers
    that omit ``section`` receive the complete model for compatibility.

    Claude, ChatGPT desktop selection, and Codex CLI rotation are named
    independently. ChatGPT suggestions and live Codex rotation are mutually
    exclusive in the controller; the model makes that relationship visible.
    """
    general = [
        {
            "kind": "group",
            "style": "section",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "group_menu_bar",
            "label": "Menu bar",
        },
        {
            "kind": "toggle",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "show_account_name",
            "label": "Show account name in menu bar",
            "value": bool(settings.show_account_name),
        },
        {
            "kind": "toggle",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "title_pct_5h",
            "label": "Show 5-hour % in menu bar",
            "value": title_shows_5h(settings.title_pct),
        },
        {
            "kind": "toggle",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "title_pct_7d",
            "label": "Show 7-day % in menu bar",
            "value": title_shows_7d(settings.title_pct),
        },
        {
            "kind": "toggle",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "title_scoped",
            "label": "Show model limits in title",
            "value": bool(settings.title_scoped),
        },
        {
            "kind": "group",
            "style": "section",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "group_behavior",
            "label": "Behavior",
        },
        {
            "kind": "toggle",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "confirm_switch",
            "label": "Confirm before switching",
            "value": bool(settings.confirm_switch),
        },
        {
            "kind": "choice",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "refresh_interval",
            "label": "Refresh interval",
            "options": [(secs, REFRESH_LABELS[secs]) for secs in REFRESH_CHOICES],
            "value": settings.refresh_interval,
        },
        {
            "kind": "group",
            "style": "section",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "group_advanced",
            "label": "Advanced",
        },
        {
            "kind": "toggle",
            "section": SETTINGS_SECTION_GENERAL,
            "id": "show_icon",
            "label": "Show asterisk in menu bar",
            "value": bool(settings.show_icon),
        },
    ]

    automation = [
        {
            "kind": "group",
            "style": "section",
            "section": SETTINGS_SECTION_AUTOMATION,
            "id": "group_claude",
            "label": "Claude Code",
        },
        {
            "kind": "toggle",
            "section": SETTINGS_SECTION_AUTOMATION,
            "id": "auto_switch_enabled",
            "label": "Auto-switch Claude accounts",
            "value": bool(settings.auto_switch_enabled),
        },
    ]
    if has_codex or settings.chatgpt_auto_enabled:
        automation.extend(
            [
                {
                    "kind": "group",
                    "style": "section",
                    "section": SETTINGS_SECTION_AUTOMATION,
                    "id": "group_chatgpt",
                    "label": "ChatGPT",
                },
                {
                    "kind": "toggle",
                    "section": SETTINGS_SECTION_AUTOMATION,
                    "id": "chatgpt_auto_enabled",
                    "label": "Suggest ChatGPT account switches",
                    "value": bool(settings.chatgpt_auto_enabled),
                },
                {
                    "kind": "group",
                    "style": "hint",
                    "section": SETTINGS_SECTION_AUTOMATION,
                    "id": "chatgpt_auto_hint",
                    "label": "Uses Codex quota. You approve every ChatGPT restart.",
                },
            ]
        )
    if settings.auto_switch_enabled or settings.chatgpt_auto_enabled:
        automation.extend(
            [
                {
                    "kind": "group",
                    "style": "section",
                    "section": SETTINGS_SECTION_AUTOMATION,
                    "id": "group_policy",
                    "label": "Shared rotation policy",
                },
                {
                    "kind": "choice",
                    "section": SETTINGS_SECTION_AUTOMATION,
                    "id": "threshold",
                    "label": "Switch at",
                    "options": [(pct, f"{pct}%") for pct in AUTO_THRESHOLD_CHOICES],
                    "value": int(threshold),
                },
                {
                    "kind": "choice",
                    "section": SETTINGS_SECTION_AUTOMATION,
                    "id": "strategy",
                    "label": "Choose the next account by",
                    "options": list(AUTO_STRATEGY_CHOICES),
                    "value": strategy,
                },
                {
                    "kind": "group",
                    "style": "hint",
                    "section": SETTINGS_SECTION_AUTOMATION,
                    "id": "strategy_hint",
                    "label": AUTO_STRATEGY_HINTS.get(
                        strategy, AUTO_STRATEGY_HINTS["best"]
                    ),
                },
            ]
        )
        if has_codex and settings.auto_switch_enabled:
            automation.extend(
                [
                    {
                        "kind": "group",
                        "style": "section",
                        "section": SETTINGS_SECTION_AUTOMATION,
                        "id": "group_codex",
                        "label": "Codex CLI",
                    },
                    {
                        "kind": "toggle",
                        "section": SETTINGS_SECTION_AUTOMATION,
                        "id": "codex_enabled",
                        "label": "Auto-switch Codex CLI accounts",
                        "value": bool(codex_enabled),
                        "disabled": bool(settings.chatgpt_auto_enabled),
                    },
                    {
                        "kind": "group",
                        "style": "hint",
                        "section": SETTINGS_SECTION_AUTOMATION,
                        "id": "codex_auto_hint",
                        "label": (
                            "Turn off ChatGPT suggestions to use live Codex rotation."
                            if settings.chatgpt_auto_enabled
                            else "Runs alongside Claude auto-switch using the shared policy."
                        ),
                    },
                ]
            )

    automation.extend(
        [
            {
                "kind": "group",
                "style": "section",
                "section": SETTINGS_SECTION_AUTOMATION,
                "id": "group_schedule",
                "label": "Claude schedule",
            },
            {
                "kind": "toggle",
                "section": SETTINGS_SECTION_AUTOMATION,
                "id": "kickoff_enabled",
                "label": "Start Claude 5-hour window",
                "value": bool(settings.kickoff_enabled),
            },
        ]
    )
    if settings.kickoff_enabled:
        automation.append(
            {
                "kind": "popup",
                "section": SETTINGS_SECTION_AUTOMATION,
                "id": "kickoff_time",
                "label": "Time",
                "options": kickoff_time_options(
                    settings.kickoff_hour, settings.kickoff_minute
                ),
                "value": kickoff_time_value(
                    settings.kickoff_hour, settings.kickoff_minute
                ),
            }
        )
    rows = general + automation
    if section is not None:
        valid_sections = {value for value, _label in SETTINGS_SECTIONS}
        if section not in valid_sections:
            section = SETTINGS_SECTION_GENERAL
        rows = [row for row in rows if row["section"] == section]
    return rows


STATUS_ICON = "✻"
# AppKit's default title extra is ~10pt per side. With no leading icon that
# empty inset is just gap; compact width keeps ~3pt per side so the hover
# pill still clears the first glyph.
STATUS_ITEM_COMPACT_PAD = 6.0
NS_VARIABLE_STATUS_ITEM_LENGTH = -1.0
# After the pointer leaves the popover (not on open). Click-outside still
# dismisses immediately.
POPOVER_AUTO_CLOSE_S = 12.0
HEADER_TITLE_H = 20.0
HEADER_CONTROL_GAP = 6.0
# NSTextField glyphs sit high in their bounds next to a bezeled button.
HEADER_LABEL_OPTICAL_DY = 3.0
SETTINGS_HEADER_GAP = 8.0
SETTINGS_POPUP_W = 118.0
# Was 312. The extra's popover is the "dropdown"; 390 is +25%.
PANEL_WIDTH = 390.0


@dataclass(frozen=True)
class NotificationCopy:
    """Title / subtitle / body for ``rumps.notification``, in that order."""

    title: str
    subtitle: str = ""
    body: str = ""

    def rumps_args(self) -> tuple[str, str, str]:
        return (self.title, self.subtitle, self.body)


def account_short_name(
    email: str | None = None,
    alias: str | None = None,
    number=None,
) -> str:
    """Glanceable account identity: alias, else email local-part, never ``Account-N (email)``."""
    if alias:
        return str(alias)
    if email:
        return _local_part(str(email))
    if number is not None and str(number) != "":
        return f"account {number}"
    return "unknown"


def account_card_names(email, alias, org_name) -> tuple[str, str]:
    """(title, subtitle) for extra/widget cards.

    Alias wins as title. Otherwise the display tag (org name or
    'personal'). Email is always the subtitle when it differs from title.
    """
    title = alias or (org_name.strip() if org_name else "personal")
    email = email or ""
    subtitle = email if email != title else ""
    return title, subtitle


def _alias_key(number, provider: str = "claude") -> str:
    key = str(number)
    if provider == "codex" and not key.startswith("codex:"):
        return f"codex:{key}"
    return key


def _alias_lookup(
    number,
    email: str | None,
    aliases: dict[str, str] | None,
    *,
    provider: str = "claude",
) -> str | None:
    if not aliases:
        return None
    if number is not None:
        found = aliases.get(_alias_key(number, provider))
        if found:
            return found
    if email:
        return aliases.get(email) or None
    return None


def _name_from_ref(
    ref: dict | None,
    aliases: dict[str, str] | None,
    *,
    provider: str = "claude",
) -> str:
    if not isinstance(ref, dict):
        return "unknown"
    email = ref.get("email")
    number = ref.get("number")
    alias = ref.get("alias") or _alias_lookup(
        number, email, aliases, provider=provider
    )
    return account_short_name(email, alias, number)


def format_local_reset(value: str | None, *, now: datetime | None = None) -> str | None:
    """Turn an ISO-Z / ISO-offset timestamp into a local clock like ``3:42 PM``.

    Adds a weekday when the reset is not today. Returns None when unparseable.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    now_local = (now or datetime.now().astimezone()).astimezone()
    if local.date() != now_local.date():
        return f"{local.strftime('%a')} {clock}"
    return clock


def _abbrev_cwd(cwd: str) -> str:
    """Last two path parts, e.g. ``/Users/x/proj`` → ``x/proj``."""
    p = Path(str(cwd).rstrip("/\\"))
    if p.parent.name:
        return f"{p.parent.name}/{p.name}"
    return p.name or str(cwd)


def format_running_line(sessions, ides) -> str | None:
    """None when both lists empty.

    One session: ``Claude Code is running in {cwd}.``
    Multiple: ``Claude Code is running ({n} sessions).``
    IDE only: ``Claude Code is running in {ide_name}.``
    Prefer session cwd (abbreviate to last two path parts) over listing PIDs.
    Never include pid numbers in the string.
    """
    n = len(sessions or ())
    if n == 1:
        cwd = str(getattr(sessions[0], "cwd", "") or "")
        place = _abbrev_cwd(cwd)
        if place:
            return f"Claude Code is running in {place}."
        return "Claude Code is running."
    if n > 1:
        return f"Claude Code is running ({n} sessions)."
    if ides:
        name = str(getattr(ides[0], "ide_name", "") or "").strip() or "an IDE"
        return f"Claude Code is running in {name}."
    return None


def format_codex_running_line(procs) -> str | None:
    """None when empty.

    One without cwd: ``Codex is running.``
    One with cwd: ``Codex is running in {last two path parts}.``
    Multiple: ``Codex is running ({n} sessions).``
    Never include pid numbers in the string.
    """
    n = len(procs or ())
    if n == 1:
        cwd = str(getattr(procs[0], "cwd", "") or "")
        place = _abbrev_cwd(cwd) if cwd else ""
        if place:
            return f"Codex is running in {place}."
        return "Codex is running."
    if n > 1:
        return f"Codex is running ({n} sessions)."
    return None


def switch_restart_hint(running: bool) -> str:
    """Restart sentence when Claude Code is live; otherwise empty."""
    if running:
        return "Restart Claude Code to apply now, or wait about 30 seconds."
    return ""


def switch_codex_restart_hint(running: bool) -> str:
    """Restart sentence when a Codex TUI is live; otherwise empty."""
    if running:
        return "Restart Codex to apply."
    return ""


def notification_copy_for_event(
    event, aliases: dict[str, str] | None = None, *, running: bool = True
) -> NotificationCopy | None:
    """Glanceable copy for menu-bar notifications, or None when the event is silent.

    Poll / no-switch / sleep / dry-run ticks do not notify. Account identity is
    alias-or-short-name, never ``Account-N (email)``. Trigger jargon is not the
    headline. Exhausted reset times are local-clock, not ISO-Z. Recovery is not
    a CLI command. Switch toasts include the restart sentence only when
    ``running`` is true (a live Claude Code session/IDE lock, or a Codex TUI).
    """
    kind = getattr(event, "kind", None)
    provider = getattr(event, "provider", "claude")
    if kind == "switch":
        if getattr(event, "dry_run", False):
            return None
        dest = _name_from_ref(
            getattr(event, "to_ref", None), aliases, provider=provider
        )
        src_ref = getattr(event, "from_ref", None)
        src = (
            _name_from_ref(src_ref, aliases, provider=provider) if src_ref else None
        )
        parts = []
        if src:
            parts.append(f"Was {src}.")
        if provider == "codex":
            hint = switch_codex_restart_hint(running)
        else:
            hint = switch_restart_hint(running)
        if hint:
            parts.append(hint)
        return NotificationCopy(title=f"Switched to {dest}", body=" ".join(parts))
    if kind == "account-quarantined":
        name = account_short_name(
            getattr(event, "email", None),
            _alias_lookup(
                getattr(event, "number", None),
                getattr(event, "email", None),
                aliases,
                provider=provider,
            ),
            getattr(event, "number", None),
        )
        if provider == "codex":
            body = "Sign in with this account in Codex, then click it in the extra."
        else:
            body = "Sign in with this account in Claude Code, then click it in the extra."
        return NotificationCopy(
            title=f"{name} was paused",
            body=body,
        )
    if kind == "all-exhausted":
        reset = format_local_reset(getattr(event, "earliest_reset_at", None))
        body = f"Earliest reset at {reset}." if reset else "No reset time is known yet."
        return NotificationCopy(title="All accounts are out of usage", body=body)
    if kind == "config-warning":
        message = str(getattr(event, "message", "") or "A setting is not doing anything.")
        return NotificationCopy(title="Settings need a look", body=message)
    return None


def notification_copy_for_manual_switch(
    dest_name: str, *, running: bool = True, provider: str = "claude"
) -> NotificationCopy:
    """Copy after a user-initiated switch; the title names the destination."""
    if provider == "codex":
        body = switch_codex_restart_hint(running)
    else:
        body = switch_restart_hint(running)
    return NotificationCopy(
        title=f"Switched to {dest_name}",
        body=body,
    )


def notification_copy_for_engine_start_failure(message: str) -> NotificationCopy:
    return NotificationCopy(
        title="Auto-switch didn't start",
        body=str(message) or "The auto-switch engine failed to start.",
    )


def notification_copy_for_kickoff(
    results: list[tuple[str, bool, str]],
) -> NotificationCopy | None:
    """Copy after a scheduled 5h kickoff pass. None when nothing was attempted."""
    if not results:
        return None
    ok = [name for name, success, _err in results if success]
    bad = [(name, err) for name, success, err in results if not success]
    if ok and not bad:
        if len(ok) == 1:
            title = f"Started {ok[0]}'s 5-hour window"
        else:
            title = "Started 5-hour windows"
        body = "Pinged " + ", ".join(ok) + "."
    elif ok and bad:
        title = "Started some 5-hour windows"
        failed = ", ".join(name for name, _err in bad)
        body = f"Started {', '.join(ok)}. Couldn't reach {failed}."
    else:
        title = "Couldn't start 5-hour windows"
        body = "; ".join(
            f"{name}: {err}" if err else name for name, err in bad
        )[:240]
    return NotificationCopy(title=title, body=body)


def notification_copy_for_relogin(name: str) -> NotificationCopy:
    return NotificationCopy(
        title=f"{name} signed out",
        body="Log in with Claude Code, then click that account in the extra.",
    )


def notification_copy_for_relogin_captured(name: str) -> NotificationCopy:
    return NotificationCopy(
        title=f"{name} is signed in again",
        body="Credentials updated.",
    )


@dataclass(frozen=True)
class ReloginClickPlan:
    """What a signed-out card click should do. Never captures the wrong org."""

    kind: str  # capture | open_login | confirm_open_login
    slot_name: str
    login_email: str
    live_name: str | None = None


def display_needs_relogin(display) -> bool:
    return display == SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED]


def extra_note_for_display(display) -> str | None:
    """Popover note: extra copy for signed-out, otherwise the sentinel string."""
    if display_needs_relogin(display):
        return RELOGIN_CARD_NOTE
    if isinstance(display, str):
        return display
    return None


def relogin_slot_nums(snapshot: dict) -> set[str]:
    return {
        str(row[0])
        for row in snapshot.get("accounts") or []
        if display_needs_relogin(row[3])
    }


def newly_relogin_slots(prev: set[str], curr: set[str]) -> set[str]:
    return set(curr) - set(prev)


def slot_identity_from_sequence(sequence: dict | None, num) -> tuple[str, str] | None:
    """``(email, organizationUuid)`` for a managed slot, or None."""
    acc = ((sequence or {}).get("accounts") or {}).get(str(num)) or {}
    email = acc.get("email") or ""
    if not email:
        return None
    return email, acc.get("organizationUuid") or ""


def matching_relogin_slot(
    live: tuple[str, str] | None,
    identities: dict[str, tuple[str, str]],
    relogin_nums: set[str],
) -> str | None:
    """Slot whose identity equals the live login and is currently signed out.

    Email is not enough: two orgs can share an address.
    """
    if live is None:
        return None
    for num in relogin_nums:
        if identities.get(str(num)) == live:
            return str(num)
    return None


def plan_relogin_click(
    *,
    live: tuple[str, str] | None,
    slot: tuple[str, str] | None,
    slot_name: str,
    live_name: str | None,
) -> ReloginClickPlan | None:
    """Decide capture vs open-login. Capture only when email and org both match."""
    if slot is None:
        return None
    email = slot[0]
    if live is not None and live == slot:
        return ReloginClickPlan("capture", slot_name, email, live_name)
    if live is None:
        return ReloginClickPlan("open_login", slot_name, email, None)
    return ReloginClickPlan("confirm_open_login", slot_name, email, live_name)


def relogin_wrong_account_title(plan: ReloginClickPlan) -> str:
    """Alert title: name the slot they clicked, not the live login."""
    return f"Sign in as {plan.slot_name}?"


def relogin_wrong_account_message(plan: ReloginClickPlan) -> str:
    """Alert body: Claude Code's current login changes; the saved slot stays."""
    live_name = plan.live_name or "another account"
    return (
        f"Claude Code is using {live_name} right now. "
        f"Your saved {live_name} account is not removed. "
        f"Continue to sign in as {plan.slot_name}?"
    )


def relogin_login_opened_message(slot_name: str) -> str:
    return (
        f"Sign in as {slot_name} in the Terminal window. After that, click this "
        "card again, or wait and the extra will capture it."
    )


def resolve_claude_bin(
    which=None,
    extra_dirs: tuple[str, ...] = _CLAUDE_PATH_DIRS,
) -> str | None:
    which_fn = shutil.which if which is None else which
    found = which_fn("claude")
    if found:
        return found
    for folder in extra_dirs:
        candidate = Path(os.path.expanduser(folder)) / "claude"
        if candidate.is_file():
            return str(candidate)
    return None


LOGIN_COMMAND_NAME = "login.command"


def build_login_command_text(claude_bin: str, email: str) -> str:
    """Shell body for a ``.command`` file that runs ``claude auth login``."""
    cmd = (
        f"{shlex.quote(claude_bin)} auth login --claudeai --email {shlex.quote(email)}"
    )
    return f"#!/bin/bash\nexec {cmd}\n"


def write_login_command(claude_bin: str, email: str, path: Path) -> Path:
    """Write an executable login ``.command`` at ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_login_command_text(claude_bin, email), encoding="utf-8")
    path.chmod(0o700)
    return path


def launch_claude_login(
    email: str,
    *,
    which=None,
    run=None,
    command_path: Path | None = None,
) -> str:
    """Open Terminal on ``claude auth login --email``. Returns the shell command.

    The extra has no TTY, so the OAuth flow cannot run inside this process.
    Launch Services opens a ``.command`` file (Terminal's document type) instead
    of Apple Events, so macOS does not ask to let Python control Terminal.
    """
    run_fn = subprocess.run if run is None else run
    claude_bin = resolve_claude_bin(which=which)
    if not claude_bin:
        raise ClaudeSwitchError(
            "'claude' was not found. Install Claude Code, then try again."
        )
    dest = command_path or (get_backup_root() / LOGIN_COMMAND_NAME)
    write_login_command(claude_bin, email, dest)
    result = run_fn(
        ["open", str(dest)],
        capture_output=True,
        text=True,
        check=False,
    )
    if getattr(result, "returncode", 1) != 0:
        cmd = (
            f"{shlex.quote(claude_bin)} auth login --claudeai --email "
            f"{shlex.quote(email)}"
        )
        err = (getattr(result, "stderr", None) or "").strip()
        extra = f" ({err})" if err else ""
        raise ClaudeSwitchError(
            f"Couldn't open Terminal{extra}. Run this yourself: {cmd}"
        )
    return f"{claude_bin} auth login --claudeai --email {email}"


# ---- pure display helpers (operate on the usage-window dict shape produced by
# ---- oauth.build_usage_result / stored in UsageEntry.last_good) --------------

def tightest_pct(usage: dict | str | None) -> float | None:
    """Highest 5h/7d utilization percentage, or None if unknown.

    Surfaces the binding window's utilization for display. Spend is excluded —
    it isn't a rate-limit window.
    """
    if not isinstance(usage, dict):
        return None
    pcts = [
        window["pct"]
        for window in (usage.get("five_hour"), usage.get("seven_day"))
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def _window_pct(usage: dict | str | None, key: str) -> float | None:
    """Utilization pct for a usage window (``five_hour``/``seven_day``), or None."""
    if isinstance(usage, dict):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            return float(window["pct"])
    return None


def _resets_at_ts(window: dict | str | None) -> float:
    """POSIX timestamp of a usage window's ``resets_at``; inf if missing/bad."""
    if isinstance(window, dict):
        ra = window.get("resets_at")
        if isinstance(ra, str):
            try:
                return datetime.fromisoformat(ra).timestamp()
            except ValueError:
                pass
    return float("inf")


def _live_countdown(window: dict | str | None, now: float) -> str | None:
    """Time until a usage window resets, computed live from ``resets_at``.

    The cached usage dict's ``countdown`` string is frozen at fetch time, so a
    stale (e.g. last-known-good) entry would show a wrong remaining time. Deriving
    it from the absolute ``resets_at`` keeps it correct between/without refetches.
    Returns ``None`` when there's no ``resets_at`` or it has already passed.
    """
    ts = _resets_at_ts(window)
    if ts == float("inf"):
        return None
    remaining = int(ts - now)
    if remaining <= 0:
        return None
    days, rem = divmod(remaining, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


_WEEKLY_PERIOD_S = 7 * 86400  # weekly limits reset on a fixed 7-day cadence


def _rolled_weekly_window(window: dict | None, now: float) -> dict | None:
    """A weekly window with a passed reset advanced to its next 7-day boundary.

    Weekly limits reset on a fixed weekly cadence, so once the stored
    ``resets_at`` is in the past we know the window rolled over — the stored pct
    belongs to a window that no longer exists. Return a copy reflecting the reset
    state (``pct`` 0, ``resets_at`` advanced to the next future boundary) so the
    menu bar shows the reset from the static schedule alone, without waiting to
    spend tokens on a fresh fetch. Missing/future/unparseable windows are
    returned unchanged.
    """
    if not isinstance(window, dict):
        return window
    ts = _resets_at_ts(window)
    if ts == float("inf") or ts > now:
        return window
    missed = int((now - ts) // _WEEKLY_PERIOD_S) + 1
    new_ts = ts + missed * _WEEKLY_PERIOD_S
    rolled = dict(window)
    rolled["pct"] = 0.0
    rolled["resets_at"] = datetime.fromtimestamp(new_ts, tz=timezone.utc).isoformat()
    rolled.pop("countdown", None)  # recomputed live from the rolled resets_at
    rolled.pop("clock", None)
    return rolled


def usage_summary(
    usage: dict | str | None, now: float | None = None, fetched_at: float | None = None
) -> str:
    """One-line usage summary for an account row (reset countdown computed live).

    ``fetched_at`` is the underlying measurement's fetch time (may be older
    than ``now`` when serving last-good data) — used only to flag a weekly
    window that's meaningfully ahead of pace (issue #125), never the 5h one.
    """
    if isinstance(usage, str):
        return usage
    if usage is None:
        return "usage unavailable"
    if now is None:
        now = time.time()
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        pace_result = None
        if key == "seven_day":
            window = _rolled_weekly_window(window, now)  # reflect a passed weekly reset
            # Pace against the rolled window, not the raw one: a stale window
            # rolled to 0% has no current-cycle data to compare against, so
            # its (correctly zeroed) pct naturally never reads as "ahead" —
            # computing pace pre-roll would otherwise pair last cycle's high
            # pct with this cycle's freshly-reset 0% display.
            pace_result = pace.compute_pace(window, fetched_at=fetched_at)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            seg = f"{label} {window['pct']:.0f}%"
            if key == "seven_day" and pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"  # time until this window resets
            parts.append(seg)
    # Per-model weekly limits (e.g. Fable), from the usage API's ``limits`` array.
    for window in usage.get("scoped") or []:
        window = _rolled_weekly_window(window, now)  # weekly cadence, same roll-forward
        pace_result = pace.compute_pace(window, fetched_at=fetched_at)  # against the rolled window, see above
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
            seg = f"{window['name']} {window['pct']:.0f}%"
            if window["pct"] >= 100:
                seg += " (!)"  # maxed model — the usual reason to switch
            elif pace_result and pace_result.ahead:
                seg += " (ahead)"
            countdown = _live_countdown(window, now)
            if countdown:
                seg += f" ({countdown})"
            parts.append(seg)
    spend = usage.get("spend")
    if isinstance(spend, dict) and isinstance(spend.get("pct"), (int, float)):
        parts.append(f"$ {spend['pct']:.0f}%")
    return " · ".join(parts) if parts else "usage unavailable"


def format_account_label(
    num,
    email: str,
    usage: dict | str | None,
    now: float | None = None,
    alias: str | None = None,
    disabled: bool = False,
    fetched_at: float | None = None,
) -> str:
    """Build one account row's menu label."""
    label = f"{alias}  ({email})" if alias else email
    marker = "  (disabled)" if disabled else ""
    return f"{num}  {label}{marker}  {usage_summary(usage, now, fetched_at)}"


def panel_windows(
    usage: dict | str | None,
    now: float | None = None,
    fetched_at: float | None = None,
) -> list[dict]:
    """Usage windows for the popover (drawn bars, not the status-item title).

    Each item is ``{label, pct, countdown, resets_at_ts, ahead, maxed}``.
    Sentinel strings and missing usage produce an empty list — the popover
    shows ``note`` instead. ``resets_at_ts`` is a POSIX timestamp so the
    macOS widget can recompute the countdown between extra refreshes.
    """
    if not isinstance(usage, dict):
        return []
    if now is None:
        now = time.time()
    rows: list[dict] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        window = usage.get(key)
        ahead = False
        if key == "seven_day":
            window = _rolled_weekly_window(window, now)
            result = pace.compute_pace(window, fetched_at=fetched_at)
            ahead = bool(result and result.ahead)
        if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)):
            rows.append(_window_row(label, window, ahead=ahead, maxed=False, now=now))
    for window in usage.get("scoped") or []:
        window = _rolled_weekly_window(window, now)
        if not (
            isinstance(window, dict)
            and isinstance(window.get("pct"), (int, float))
            and window.get("name")
        ):
            continue
        result = pace.compute_pace(window, fetched_at=fetched_at)
        pct = float(window["pct"])
        rows.append(
            _window_row(
                str(window["name"]),
                window,
                ahead=bool(result and result.ahead) and pct < 100,
                maxed=pct >= 100,
                now=now,
            )
        )
    return rows


def _window_row(
    label: str, window: dict, *, ahead: bool, maxed: bool, now: float
) -> dict:
    ts = _resets_at_ts(window)
    return {
        "label": label,
        "pct": float(window["pct"]),
        "countdown": _live_countdown(window, now),
        "resets_at_ts": None if ts == float("inf") else ts,
        "ahead": ahead,
        "maxed": maxed,
    }


def resolve_popover_theme(
    *,
    app_appearance_name: str | None,
    interface_style: str | None,
) -> str:
    """Dark vs light for the menu-bar popover.

    Status items follow the menu bar, which tints with the wallpaper and can
    stay Aqua while System Settings → Appearance is Dark. The popover should
    follow Dark Mode instead. ``AppleInterfaceStyle`` is the live system
    value (unset in Light, ``Dark`` in Dark, including while auto-switching).
    The app appearance is the fallback when that default is missing.
    """
    if (interface_style or "").lower() == "dark":
        return "dark"
    name = str(app_appearance_name or "")
    if "Dark" in name:
        return "dark"
    return "light"


def panel_accounts(snapshot: dict, now: float | None = None) -> list[dict]:
    """Account cards for the popover, from the menubar snapshot dict."""
    if now is None:
        now = time.time()
    cards = []
    for row in snapshot.get("accounts") or []:
        num, email, is_active, display, last_good, alias, org_name, disabled, fetched_at = row
        needs_relogin = display_needs_relogin(display)
        note = extra_note_for_display(display)
        usage = display if isinstance(display, dict) else last_good
        title, subtitle = account_card_names(email, alias, org_name)
        is_codex = str(num).startswith("codex:")
        if is_codex:
            title = f"Codex · {title}"
        as_of = now
        if needs_relogin and isinstance(fetched_at, (int, float)):
            as_of = float(fetched_at)
        windows = panel_windows(
            usage if isinstance(usage, dict) else None, as_of, fetched_at
        )
        if needs_relogin:
            # Last-good bars stay, but the reset clock is not live: ticking
            # countdown / wall-clock weekly roll would paint stale quota as
            # a fresh measurement (issue #4).
            windows = [
                {**win, "countdown": None, "resets_at_ts": None}
                for win in windows
            ]
        is_api_key = (
            (snapshot.get("kinds") or {}).get(str(num)) == "api_key"
            or display == USAGE_API_KEY
        )
        cards.append(
            {
                "num": num,
                "title": title,
                "subtitle": subtitle,
                "active": bool(is_active),
                "disabled": bool(disabled),
                "api_key": is_api_key,
                "note": note,
                "needs_relogin": needs_relogin,
                "fetched_at": fetched_at,
                "windows": windows,
                "provider": "codex" if is_codex else "claude",
            }
        )
    return cards


def provider_cards(cards: list[dict], provider: str) -> list[dict]:
    """Filter popover cards by UI provider tab.

    The ChatGPT tab intentionally reuses the Codex roster: those credentials
    are shared, while the visible copy makes that relationship explicit.
    """
    if provider not in ("claude", "chatgpt"):
        return []
    if provider == "claude":
        return [card for card in cards if card.get("provider", "claude") == "claude"]
    result = []
    for card in cards:
        if card.get("provider") != "codex":
            continue
        shown = dict(card)
        if str(shown.get("title", "")).startswith("Codex · "):
            shown["title"] = str(shown["title"])[len("Codex · ") :]
        shown["provider"] = "chatgpt"
        # Desktop-only restrictions must not change widget/CLI behavior.
        if shown.get("api_key"):
            shown["disabled"] = True
            shown["note"] = "CLI-only · Switch API-key accounts with Codex CLI."
        result.append(shown)
    return result


def provider_empty_copy(provider: str) -> str:
    if provider == "chatgpt":
        return "No shared ChatGPT / Codex accounts"
    return "No managed Claude accounts"


def provider_empty_state(provider: str, state: str = "ready") -> dict:
    """Actionable empty copy; never mistake a failed read for an empty roster."""
    name = "ChatGPT" if provider == "chatgpt" else "Claude"
    if state == "loading":
        return {"title": f"Loading {name} accounts…",
                "body": "Checking your saved logins. This may take a moment.",
                "action": None, "button": "", "hint": "Your current login stays unchanged."}
    if state == "error":
        return {"title": f"Couldn’t load {name} accounts",
                "body": "OpenSwap couldn’t read the account list. Try again; OpenSwap hasn’t changed your saved logins.",
                "action": "retry", "button": "Try again", "hint": "Retrying does not switch accounts."}
    if state == "unavailable":
        return {"title": f"{name} accounts unavailable",
                "body": "The shared Codex account store isn’t available in this session. Restart OpenSwap to reconnect it.",
                "action": None, "button": "", "hint": "Your current login stays unchanged."}
    if provider == "chatgpt":
        return {
            "title": "Add a ChatGPT account",
            "body": "Add an account without signing out.",
            "action": "start",
            "button": "Sign in with ChatGPT",
            "secondary_action": "capture",
            "secondary_button": "Save current login",
            "hint": "",
        }
    body = "Sign in to Claude Code, then save your login."
    return {"title": f"Add your first {name} account", "body": body,
            "action": "add", "button": "Add current login",
            "hint": "Saves the login to OpenSwap. Does not switch accounts."}


def provider_shared_copy(provider: str) -> str:
    if provider == "chatgpt":
        return "Shared with Codex · Codex usage"
    return ""


def login_panel_state(state: dict | None) -> dict:
    """Return safe, concise native-view data for the browser login lifecycle.

    This deliberately accepts backend-shaped dictionaries but only exposes
    display-safe fields. In particular, URLs are represented by ``has_url``;
    the URL itself never becomes public UI state.
    """
    raw = state if isinstance(state, dict) else {}
    stage = str(raw.get("stage") or "idle").lower()
    if stage not in {"idle", "starting", "waiting", "ready", "error", "cancelled", "saving", "cancelling", "saved"}:
        stage = "error"
    email = str(raw.get("email") or "").strip()
    plan = str(raw.get("plan") or "").strip()
    account_id = str(raw.get("account_id") or "").strip()
    workspace_id = str(raw.get("workspace_id") or "").strip()
    message = str(raw.get("message") or "").strip().replace("\n", " ")
    message = re.sub(r"https?://\S+|\b(?:sk|auth)_[A-Za-z0-9_-]+", "", message).strip()
    message = re.sub(r"\s{2,}", " ", message)[:180]
    code = str(raw.get("device_code") or "").strip()
    mode = str(raw.get("mode") or "browser").lower()
    result = {
        "stage": stage,
        "email": email,
        "plan": plan,
        "account_id": account_id,
        "workspace_id": workspace_id,
        "message": message,
        "has_url": bool(raw.get("has_url")),
        "device_code": code[:64],
        "mode": mode,
        "hint": "",
        "title": "",
        "body": "",
        "actions": [],
    }
    if stage == "starting":
        result.update(title="Preparing sign-in", body="", actions=["cancel"])
    elif stage == "waiting":
        if mode == "device":
            actions = (["open_browser"] if result["has_url"] else []) + ["cancel"]
            if code:
                actions.append("copy_code")
            if result["has_url"]:
                actions.append("copy_link")
            result.update(title="Waiting for sign-in…", body="Enter this code in your browser." if code else "Preparing your code…", actions=actions)
        else:
            actions = (["open_browser"] if result["has_url"] else []) + ["cancel"]
            if result["has_url"]:
                actions.append("copy_link")
            actions.append("device")
            result.update(title="Waiting for sign-in…", body="Finish in your browser.", actions=actions)
    elif stage == "ready":
        result.update(title="Save this account?", body="", actions=["save", "cancel"])
    elif stage == "error":
        result.update(title="Sign-in didn’t finish", body=message or "Try again.", actions=["retry", "cancel"] + ([] if mode == "device" else ["device"]))
    elif stage == "cancelled":
        result.update(title="Sign-in cancelled", body="", actions=["start", "dismiss"])
    elif stage == "saved":
        result.update(title="Account saved", body="Ready to use.", actions=["dismiss"])
    elif stage == "saving":
        result.update(title="Saving account…", body="", actions=[])
    elif stage == "cancelling":
        result.update(title="Cancelling sign-in…", body="", actions=[])
    return result


# Descriptive alias for callers that prefer the model's role to its layout.
login_state_panel = login_panel_state


def hold_event_update(current, event):
    """Keep the last engine hold event; a real switch clears it."""
    kind = getattr(event, "kind", None)
    if kind in ("no-switch", "all-exhausted"):
        return event
    if kind == "switch":
        return None
    return current


def hold_event_for_snapshot(event, *, hold_slot, active_num):
    """Drop a cached hold that belongs to a different live slot.

    ``hold_slot=""`` is a real bind (no active account this tick). ``None``
    means the slot is unknown, so the event is kept.
    """
    if event is None:
        return None
    if hold_slot is None:
        return event
    if str(active_num or "") != str(hold_slot):
        return None
    return event


def poll_tick_slot(event) -> str | None:
    """Decision-time slot from a PollEvent; None if this event is not a poll."""
    if getattr(event, "kind", None) != "poll":
        return None
    active = getattr(event, "active", None) or {}
    num = active.get("number")
    return str(num) if num is not None else ""


def hold_cache_after_event(hold_event, hold_slot, tick_slot, event):
    """Advance the extra's hold cache for one engine callback.

    Poll events record the tick's decision-time slot and must not clear it
    when there is not yet a cached hold. Only a switch clears the cache.
    Codex events never update the Claude hold line.
    """
    if getattr(event, "provider", "claude") != "claude":
        return hold_event, hold_slot, tick_slot
    poll_slot = poll_tick_slot(event)
    if poll_slot is not None:
        tick_slot = poll_slot
    prev = hold_event
    new = hold_event_update(prev, event)
    if getattr(event, "kind", None) == "switch":
        return None, None, None
    if new is not None and new is not prev:
        hold_slot = tick_slot if tick_slot is not None else ""
    return new, hold_slot, tick_slot


def extra_hold_line(
    *,
    auto_enabled: bool,
    event,
    active_title: str | None,
    strategy: str,
) -> str | None:
    """Return popover hold copy only when auto-switch is running."""
    if not auto_enabled:
        return None
    return hold_line_from_event(
        event, active_title=active_title, strategy=strategy
    )


def hold_panel_reload_plan(
    *, copy_changed: bool, pending: bool, left_mouse_down: bool
) -> tuple[bool, bool]:
    """Whether to rebuild the open extra for a hold-copy change.

    Returns ``(reload_now, pending)``. A 1s tick must not rebuild under a
    card click: ``mouseUp_`` fires the switch, and replacing the view tree
    first swallows it. Defer until the left button is up.
    """
    pending = pending or copy_changed
    if not pending:
        return False, False
    if left_mouse_down:
        return False, True
    return True, False


def hold_line_from_event(
    event,
    *,
    active_title: str | None = None,
    strategy: str = "best",
) -> str | None:
    """One glanceable sentence from an engine event. Extra does not re-rank."""
    if event is None:
        return None
    kind = getattr(event, "kind", None)
    if kind == "all-exhausted":
        return "All accounts are out of usage."
    if kind != "no-switch":
        return None
    reason = getattr(event, "reason", "") or ""
    detail = str(getattr(event, "detail", "") or "").rstrip(".")
    title = (active_title or "").strip() or "this account"
    window = _HOLD_WINDOW.get(strategy)
    if reason == "already-consuming-soonest":
        # Engine uses this for both "active is soonest" and "sooner peers
        # have no room"; do not claim the active account resets first.
        if window:
            return f"Holding on {title}: no sooner {window} reset with room."
        return f"Holding on {title}: no sooner reset with room."
    if reason == "below-threshold":
        return f"Holding: {detail}." if detail else "Holding: below switch threshold."
    if reason == "cooldown":
        return "Holding: cooldown after last switch."
    if reason == "reset-unknown":
        if window:
            return f"Holding: {window} reset time is unknown."
        return "Holding: reset time is unknown."
    if reason == "no-comparison":
        return "Holding: no candidate has readable usage."
    if reason == "no-candidates":
        return "Holding: no other accounts to switch to."
    if reason == "no-qualifying-candidate":
        return "Holding: no better account yet."
    if reason == "active-api-key":
        return "Holding: API-key accounts have no quota to watch."
    if reason == "active-idle":
        return "Holding: Claude Code is idle."
    if reason == "active-usage-unknown":
        return (
            f"Holding: waiting for usage ({detail})."
            if detail
            else "Holding: waiting for usage."
        )
    if reason == "unmanaged-active-account":
        return "Holding: live login is not a managed account."
    if reason == "no-active-account":
        return "Holding: no active account."
    if reason == "no-viable-target":
        return "Holding: no viable target."
    if reason == "stale-usage":
        return "Holding: usage is stale."
    if reason:
        return f"Holding: {reason.replace('-', ' ')}."
    return None


def _local_part(email: str, limit: int = 12) -> str:
    """Email text before '@', truncated with a trailing '*' marker."""
    local = email.split("@", 1)[0]
    if len(local) > limit:
        return local[: limit - 1] + "*"
    return local


def format_title(
    active_email: str | None,
    active_usage: dict | str | None,
    settings: MenuBarSettings,
    now: float | None = None,
    alias: str | None = None,
    org_name: str | None = None,
) -> str:
    """Build the menu-bar title from the active account and settings.

    Never empty: an unmanaged live login, or every title toggle off, shows the
    icon, because a blank status item is indistinguishable from a crashed one.
    """
    if active_email is None:
        return STATUS_ICON
    if now is None:
        now = time.time()
    segments: list[str] = []
    if settings.show_account_name:
        org = org_name.strip() if org_name else ""
        segments.append(alias or org or _local_part(active_email))
    if title_shows_5h(settings.title_pct):
        p = _window_pct(active_usage, "five_hour")
        if p is not None:
            segments.append(f"{p:.0f}%")
    if title_shows_7d(settings.title_pct):
        seven = active_usage.get("seven_day") if isinstance(active_usage, dict) else None
        seven = _rolled_weekly_window(seven, now)  # reflect a passed weekly reset
        p = seven["pct"] if isinstance(seven, dict) and isinstance(seven.get("pct"), (int, float)) else None
        if p is not None:
            segments.append(f"{p:.0f}%")
    if settings.title_scoped and isinstance(active_usage, dict):
        # Per-model weekly limits (e.g. Fable), same shape/roll-forward as the
        # dropdown rows; named so multiple scoped models stay distinguishable.
        for window in active_usage.get("scoped") or []:
            window = _rolled_weekly_window(window, now)
            if isinstance(window, dict) and isinstance(window.get("pct"), (int, float)) and window.get("name"):
                segments.append(f"{window['name']} {window['pct']:.0f}%")
    text = " · ".join(segments)
    if settings.show_icon and text:
        return f"{STATUS_ICON} {text}"
    return text or STATUS_ICON


def should_confirm_switch(enabled: bool, *, is_active: bool) -> bool:
    """A click on the already-active card is a no-op and needs no dialog."""
    return bool(enabled) and not is_active


def switch_confirm_copy(
    dest_name: str, *, live_name: str | None, app: str = "Claude Code"
) -> tuple[str, str]:
    """Title and body for the dialog shown before a card click switches."""
    title = f"Switch to {dest_name}?"
    if live_name:
        body = f"{app} is signed in as {live_name}. Switch it to {dest_name}?"
    else:
        body = f"Sign {app} in as {dest_name}?"
    return title, body


STRATEGY_CONFIRM_TITLES = {
    None: "Rotate to the next account?",
    "best": "Switch to the account with the most headroom?",
    "next-available": "Switch to the next available account?",
}


def strategy_confirm_copy(
    strategy: str | None, *, live_name: str | None, app: str = "Claude Code"
) -> tuple[str, str]:
    """Dialog for Rotate / Best / Next available, whose destination is only
    known after the switch runs."""
    title = STRATEGY_CONFIRM_TITLES.get(strategy, "Switch accounts?")
    body = f"{app} is signed in as {live_name}." if live_name else ""
    return title, body


_ROSTER_VOLATILE_KEYS = ("activeAccountNumber", "lastUpdated")


def _roster_digest(path) -> str | None:
    """The index file's content minus the keys every switch rewrites."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    stable = {k: v for k, v in data.items() if k not in _ROSTER_VOLATILE_KEYS}
    return json.dumps(stable, sort_keys=True)


def store_roster_changed(paths, seen: dict) -> bool:
    """True when a store index file's roster (slots, aliases, disabled flags)
    differs from the last call.

    ``seen`` maps path to ``(stamp, digest)`` and is updated in place, where
    stamp is (mtime_ns, size, inode): the writers replace the file
    atomically, so a rewrite inside one coarse timestamp tick still changes
    the inode. The first call only primes it, so startup does not
    trigger a refresh. The file is parsed only when its stamp moved. Switches
    rewrite the index too,
    but only its volatile keys, which the digest ignores: the active-slot
    watcher already covers those and the app refreshes itself after its own
    actions. A file appearing, vanishing, or turning unparseable counts as a
    change.
    """
    changed = False
    for path in paths:
        key = str(path)
        try:
            st = path.stat()
            stamp: tuple[int, int, int] | None = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            stamp = None
        previous = seen.get(key)
        if previous is not None and previous[0] == stamp:
            continue
        digest = _roster_digest(path) if stamp is not None else None
        if previous is not None and (
            previous[1] != digest or (previous[0] is None) != (stamp is None)
        ):
            changed = True
        seen[key] = (stamp, digest)
    return changed


def window_suffix(win: dict, *, stale: bool) -> str:
    """Right-hand note on a card row: the reset countdown when there is one,
    else "max" or "ahead" for a live measurement. The red 100% already says
    maxed, so the countdown wins."""
    suffix = win.get("countdown") or ""
    if suffix or stale:
        return suffix
    if win.get("maxed"):
        return "max"
    if win.get("ahead"):
        return "ahead"
    return ""


def alias_edit(text: str, current: str | None) -> tuple[str, str | None]:
    """Decide what a rename prompt's answer means: set, clear, or noop."""
    value = text.strip()
    if not value:
        return ("clear", None) if current else ("noop", None)
    if value == current:
        return ("noop", None)
    return ("set", value)


def status_item_length(title_width: float, *, compact: bool) -> float:
    """Width for the status extra. Compact (icon off) drops the ~10pt insets."""
    if not compact or title_width <= 0:
        return NS_VARIABLE_STATUS_ITEM_LENGTH
    return float(math.ceil(title_width + STATUS_ITEM_COMPACT_PAD))


def trailing_header_frames(
    panel_width: float,
    pad: float,
    label_wh: tuple[float, float],
    control_wh: tuple[float, float],
    *,
    title_h: float = HEADER_TITLE_H,
    gap: float = HEADER_CONTROL_GAP,
) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    """Pin a label+control pair to the top-right of the popover header.

    Sizes are ``(width, height)``. Returns ``(label_frame, control_frame)``
    as ``(x, y, w, h)`` in a flipped view (origin at the top-left). The
    control's trailing edge sits ``pad`` from the panel's right edge; both
    are vertically centered on the title row.
    """
    lw, lh = label_wh
    cw, ch = control_wh
    x = panel_width - pad - (lw + gap + cw)
    mid_y = pad + title_h / 2.0
    ly = max(pad, mid_y - lh / 2.0)
    cy = max(pad, mid_y - ch / 2.0)
    return (x, ly, lw, lh), (x + lw + gap, cy, cw, ch)


def settings_header_frames(
    pad: float,
    back_wh: tuple[float, float],
    title_wh: tuple[float, float],
    *,
    gap: float = SETTINGS_HEADER_GAP,
    optical_dy: float = HEADER_LABEL_OPTICAL_DY,
) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    """Back button + Settings title, sharing the button's vertical center.

    Returns ``(back_frame, title_frame)`` as ``(x, y, w, h)`` in a flipped
    view. ``optical_dy`` nudges the title down so NSTextField glyphs line up
    with the bezeled button label.
    """
    bw, bh = back_wh
    tw, th = title_wh
    mid = pad + bh / 2.0
    ty = mid - th / 2.0 + optical_dy
    return (pad, pad, bw, bh), (pad + bw + gap, ty, tw, th)


def format_usage_log(email: str, usage: dict | str | None) -> str | None:
    """A log line of an account's session (5h) and weekly (7d) limits.

    Uses each window's absolute reset ``clock`` rather than a live countdown,
    since log lines are already timestamped. Returns ``None`` when no numeric
    window is available (sentinels, ``None``, or spend-only) so callers can skip
    logging nothing.
    """
    parts: list[str] = []
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = _window_pct(usage, key)
        if pct is None:
            continue
        window = usage.get(key)  # a dict — _window_pct found a numeric pct in it
        clock = window.get("clock") if isinstance(window, dict) else None
        seg = f"{label} {pct:.0f}%"
        if clock:
            seg += f" (resets {clock})"
        parts.append(seg)
    if not parts:
        return None
    return f"usage {email}: " + " · ".join(parts)


def _usage_log_key(usage: dict | str | None) -> tuple[float | None, float | None]:
    """De-dupe key for usage logging: the (5h, 7d) percentages only.

    Reset clocks change every refresh; keying on the percentages means an idle
    account isn't re-logged every cycle.
    """
    return (_window_pct(usage, "five_hour"), _window_pct(usage, "seven_day"))


_SWITCH_LOG_RE = re.compile(r"Switched from account (\d+) to (\d+)")


def parse_switch_history(log_text: str, limit: int = SWITCH_HISTORY_LIMIT) -> list[str]:
    """Recent account switches from the log, most-recent first.

    Reads the ``Switched from account X to Y`` lines the switcher logs and pairs
    each with its timestamp (trimmed to the minute). Returns at most ``limit``
    entries like ``"3 → 1   2026-06-27 02:06"``. Any unparseable line is skipped.
    """
    out: list[str] = []
    for line in log_text.splitlines():
        m = _SWITCH_LOG_RE.search(line)
        if not m:
            continue
        stamp = line.split(" - ", 1)[0].strip()[:16]  # "YYYY-MM-DD HH:MM"
        out.append(f"{m.group(1)} → {m.group(2)}   {stamp}")
    return out[-limit:][::-1]


def _account_display_usage(entry) -> dict | str | None:
    """Menu-display usage for a ``UsageEntry``.

    A human-readable note for a sentinel state (token expired / API key /
    keychain unavailable), otherwise the last-good measurement dict, otherwise
    ``None``.
    """
    if entry.sentinel:
        return SENTINEL_NOTES.get(entry.sentinel, entry.sentinel)
    return entry.last_good


EMPTY_SNAPSHOT: dict = {
    "accounts": [],
    "active_email": None,
    "active_num": None,
    "active_usage": None,
    "active_last_good": None,
    "active_fetched_at": None,
    "active_alias": None,
    "active_org": None,
    "identities": {},
    "kinds": {},
    "codex_active_num": None,
}


def _adapt_snapshot(snap, codex_snap=None) -> dict:
    """Adapt an ``AccountsSnapshot`` to the menu bar's render dict.

    Shape: ``{"accounts": [(num, email, is_active, display_usage, last_good, alias, org_name, disabled, fetched_at), ...],
    "active_email": str | None, "active_num": str | None,
    "active_usage": dict | str | None, "active_last_good": dict | None,
    "active_fetched_at": float | None,
    "active_alias": str | None, "active_org": str | None}``. The snapshot itself is produced by
    ``SnapshotSource`` (the paced read path), so this is a pure transform — no
    fetching, no I/O. Per-account ``fetched_at`` is the underlying
    measurement's fetch time (pace marker, signed-out freeze, widget age).
    """
    accounts = []
    active_email = None
    active_num = None
    active_usage = None
    active_last_good = None
    active_fetched_at = None
    active_alias = None
    active_org = None
    identities: dict[str, tuple[str, str]] = {}
    kinds: dict[str, str] = {}
    for acc in snap.accounts:
        display = _account_display_usage(acc.usage)
        org_name = getattr(acc, "org_name", "") or ""
        accounts.append(
            (
                acc.number, acc.email, acc.is_active, display, acc.usage.last_good,
                acc.alias, org_name, acc.disabled, acc.usage.fetched_at,
            )
        )
        identities[str(acc.number)] = (
            acc.email, getattr(acc, "org_uuid", "") or "",
        )
        kinds[str(acc.number)] = getattr(acc, "kind", "oauth")
        if acc.is_active:
            active_email, active_usage, active_alias = acc.email, display, acc.alias
            active_org = org_name
            active_num = str(acc.number)
            active_last_good = acc.usage.last_good
            active_fetched_at = acc.usage.fetched_at
    codex_active_num = None
    if codex_snap is not None:
        for acc in codex_snap.accounts:
            display = _account_display_usage(acc.usage)
            org_name = getattr(acc, "org_name", "") or ""
            num = f"codex:{acc.number}"
            accounts.append(
                (
                    num, acc.email, acc.is_active, display, acc.usage.last_good,
                    acc.alias, org_name, acc.disabled, acc.usage.fetched_at,
                )
            )
            identities[num] = (
                acc.email, getattr(acc, "org_uuid", "") or "",
            )
            kinds[num] = getattr(acc, "kind", "oauth")
            if acc.is_active:
                codex_active_num = str(acc.number)
    return {
        "accounts": accounts,
        "active_email": active_email,
        "active_num": active_num,
        "active_usage": active_usage,
        "active_last_good": active_last_good,
        "active_fetched_at": active_fetched_at,
        "active_alias": active_alias,
        "active_org": active_org,
        "identities": identities,
        "kinds": kinds,
        "codex_active_num": codex_active_num,
    }


def title_usage(snapshot: dict) -> dict | str | None:
    """Usage dict for the extra title.

    A sentinel note on the active slot still has ``last_good``. Percentages
    should follow that instead of going blank.
    """
    usage = snapshot.get("active_usage")
    if isinstance(usage, dict):
        return usage
    last = snapshot.get("active_last_good")
    if isinstance(last, dict):
        return last
    return usage


def title_clock(snapshot: dict, now: float | None = None) -> float:
    """Clock ``format_title`` should use for last-good vs live measurements.

    Sentinel active slots still title from ``last_good``. Rolling those
    windows against wall-clock ``now`` zeros a weekly bar that we have not
    re-fetched, which looks like a fresh 0%. Freeze at ``active_fetched_at``.
    """
    if now is None:
        now = time.time()
    if isinstance(snapshot.get("active_usage"), str):
        fetched = snapshot.get("active_fetched_at")
        if isinstance(fetched, (int, float)):
            return float(fetched)
    return now


def should_notify_manual_switch(result: dict | None) -> bool:
    """Toast only when credentials actually moved, not on already-active."""
    return bool(result and result.get("switched"))


def should_dismiss_panel_after_switch(result: dict | None) -> bool:
    """Close the popover after a handled click; keep it open on error."""
    return result is not None


def live_slot_changed(snapshot: dict, live_num: str | int | None) -> bool:
    """True when the live slot differs from the snapshot, even if emails match."""
    snap = snapshot.get("active_num")
    snap_s = str(snap) if snap is not None else None
    live_s = str(live_num) if live_num is not None else None
    return snap_s != live_s


def codex_live_slot_changed(snapshot: dict, live_num: str | int | None) -> bool:
    """True when the live Codex slot differs from the snapshot."""
    snap = snapshot.get("codex_active_num")
    snap_s = str(snap) if snap is not None else None
    live_s = str(live_num) if live_num is not None else None
    return snap_s != live_s


def codex_restart_hint() -> str:
    return switch_codex_restart_hint(True)
