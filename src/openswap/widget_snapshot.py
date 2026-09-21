"""Usage snapshot the macOS WidgetKit extension reads.

The menu bar extra is a Python LaunchAgent; WidgetKit extensions are a
separate signed Swift .appex and cannot import this package. The extra
writes a JSON file to a stable path under Application Support and pokes
the widget host via a distributed notification so timelines reload.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from openswap.fsutil import replace_with_retry
from openswap.menubar import panel_accounts

SCHEMA_VERSION = 1
SNAPSHOT_FILENAME = "widget-snapshot.json"
COMMAND_FILENAME = "widget-command.json"
RELOAD_NOTIFICATION = "com.opensoft.openswap.widget.reload"
WIDGET_APP_NAME = "OpenSwap.app"
SUPPORT_DIRNAME = "OpenSwap"
LEGACY_SUPPORT_DIRNAME = "cswap"


def _support_dir(home: Path | None = None) -> Path:
    root = home if home is not None else Path.home()
    return root / "Library" / "Application Support" / SUPPORT_DIRNAME


def default_snapshot_path(home: Path | None = None) -> Path:
    """``~/Library/Application Support/OpenSwap/widget-snapshot.json``."""
    return _support_dir(home) / SNAPSHOT_FILENAME


def default_command_path(home: Path | None = None) -> Path:
    """``~/Library/Application Support/OpenSwap/widget-command.json``."""
    return _support_dir(home) / COMMAND_FILENAME


def parse_switch_command(raw: dict) -> str | None:
    """Return slot num if ``op=='switch'`` and ``num`` is a non-empty str/int.

    Never raises on a bad JSON shape.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("op") != "switch":
        return None
    num = raw.get("num")
    if isinstance(num, bool):
        return None
    if isinstance(num, int):
        return str(num)
    if isinstance(num, str) and num:
        return num
    return None


def consume_switch_command(path: Path | None = None) -> str | None:
    """Read, delete, and return a switch slot num.

    Missing file → ``None``. Unreadable or invalid → delete if possible,
    return ``None`` (do not retry a poison file).
    """
    dest = path if path is not None else default_command_path()
    try:
        text = dest.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        _unlink_quiet(dest)
        return None
    _unlink_quiet(dest)
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parse_switch_command(raw)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def widget_app_path(home: Path | None = None) -> Path:
    """Installed host app the WidgetKit extension is embedded in."""
    root = home if home is not None else Path.home()
    return root / "Applications" / WIDGET_APP_NAME


def _window_with_label(card: dict, label: str) -> dict | None:
    for window in card.get("windows") or []:
        if isinstance(window, dict) and window.get("label") == label:
            return window
    return None


def remaining_fraction(pct: float) -> float:
    """One account is one slot. 20% used → 0.8 remaining."""
    return round(min(1.0, max(0.0, (100.0 - float(pct)) / 100.0)), 4)


def combined_window(accounts: list[dict], label: str, now: float) -> dict | None:
    """Remaining capacity for one window across healthy accounts.

    Disabled and signed-out cards are excluded: their last-good bars are
    not live capacity. Percentages are not averaged and 5h is not mixed
    with 7d. ``switch_num`` is the slot with the most remaining.
    """
    slices: list[dict] = []
    for card in accounts:
        if (
            card.get("disabled")
            or card.get("needs_relogin")
            or card.get("action_required")
            or card.get("stale")
        ):
            continue
        window = _window_with_label(card, label)
        if not isinstance(window, dict) or not isinstance(window.get("pct"), (int, float)):
            continue
        pct = float(window["pct"])
        ts = window.get("resets_at_ts")
        if not isinstance(ts, (int, float)):
            ts = None
        slices.append(
            {
                "num": str(card["num"]),
                "title": card.get("title") or f"account {card['num']}",
                "pct": pct,
                "remaining": remaining_fraction(pct),
                "resets_at_ts": ts,
                "countdown": window.get("countdown"),
            }
        )
    if not slices:
        return None
    remaining_sum = round(sum(item["remaining"] for item in slices), 4)
    # max() keeps the earlier slice on a tie; accounts stay in slot order.
    hottest = max(slices, key=lambda item: item["pct"])
    switcher = max(slices, key=lambda item: item["remaining"])
    upcoming = [
        item
        for item in slices
        if isinstance(item.get("resets_at_ts"), (int, float)) and item["resets_at_ts"] > now
    ]
    nxt = min(upcoming, key=lambda item: item["resets_at_ts"]) if upcoming else None
    return {
        "label": label,
        "remaining": remaining_sum,
        "total": len(slices),
        "hottest_title": hottest["title"],
        "hottest_num": hottest["num"],
        "switch_num": switcher["num"],
        "next_num": None if nxt is None else nxt["num"],
        "next_resets_at_ts": None if nxt is None else nxt["resets_at_ts"],
        "next_countdown": None if nxt is None else nxt.get("countdown"),
        "slices": slices,
    }


def build_combined(accounts: list[dict], now: float) -> dict:
    """``five_hour`` / ``seven_day`` combined blocks; omit a key when empty."""
    claude = [c for c in accounts if c.get("provider") != "codex"]
    out: dict = {}
    five = combined_window(claude, "5h", now)
    if five is not None:
        out["five_hour"] = five
    seven = combined_window(claude, "7d", now)
    if seven is not None:
        out["seven_day"] = seven
    return out


def snapshot_updated_at(accounts: list[dict], now: float) -> float:
    """When the bars in this payload were measured, not when the extra painted.

    Extra writes this file on a 1s tick. Stamping paint time made the widget
    footer treat signed-out last-good (and any store-served row) as fresh.
    Live cards contribute their ``fetched_at``; if every card is signed-out
    or disabled, last-good fetch time still wins over paint time.
    """
    live: list[float] = []
    any_fetch: list[float] = []
    for card in accounts:
        ts = card.get("fetched_at")
        if not isinstance(ts, (int, float)):
            continue
        stamp = float(ts)
        any_fetch.append(stamp)
        if (
            not card.get("disabled")
            and not card.get("needs_relogin")
            and not card.get("action_required")
            and not card.get("stale")
        ):
            live.append(stamp)
    if live:
        return max(live)
    if any_fetch:
        return max(any_fetch)
    return now


def build_widget_payload(snapshot: dict, now: float | None = None) -> dict:
    """JSON-friendly cards from a menubar snapshot dict."""
    if now is None:
        now = time.time()
    accounts = []
    for card in panel_accounts(snapshot, now=now):
        item = dict(card)
        item["num"] = str(card["num"])
        accounts.append(item)
    return {
        "schema": SCHEMA_VERSION,
        "updated_at": snapshot_updated_at(accounts, now),
        "accounts": accounts,
        "combined": build_combined(accounts, now),
    }


def write_widget_snapshot(
    snapshot: dict,
    *,
    now: float | None = None,
    dest: Path | None = None,
) -> Path:
    """Atomically write the widget JSON. Parent dirs are created as needed."""
    dest = dest if dest is not None else default_snapshot_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = build_widget_payload(snapshot, now=now)
    fd, tmp_name = tempfile.mkstemp(
        suffix=".json", prefix="widget-snapshot.", dir=str(dest.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        replace_with_retry(tmp_name, dest)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return dest


def notify_widget_reload() -> None:
    """Ask the widget host to reload timelines. No-op off macOS or on failure."""
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSDistributedNotificationCenter

        NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
            RELOAD_NOTIFICATION, None, None, True
        )
    except Exception:
        pass


def wake_widget_host(home: Path | None = None) -> None:
    """Launch the widget host if it is installed, so it can relay reloads."""
    if sys.platform != "darwin":
        return
    app = widget_app_path(home)
    if not app.is_dir():
        return
    try:
        subprocess.Popen(
            ["/usr/bin/open", "-ga", str(app)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def publish_widget_snapshot(
    snapshot: dict,
    *,
    now: float | None = None,
    dest: Path | None = None,
) -> Path | None:
    """Write the snapshot and poke the host. Never raises (display path)."""
    try:
        path = write_widget_snapshot(snapshot, now=now, dest=dest)
        notify_widget_reload()
        return path
    except Exception:
        return None
