"""Reset events: per-account usage-window events and where they are delivered.

``reset_events.json`` holds the user's destinations; ``reset_events_state.json``
records what has already fired so a restart does not repeat an event.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from openswap.locking import FileLock
from openswap.settings import atomic_write_json

_logger = logging.getLogger(__name__)

EVENT_TYPES = ("window.started", "window.ending_soon", "window.reset")
WINDOWS = ("five_hour", "seven_day")
KINDS = ("mac", "webhook")

CONFIG_FILENAME = "reset_events.json"
STATE_FILENAME = "reset_events_state.json"
SENT_RETENTION_S = 8 * 86400


def _default_lead_s() -> dict[str, int]:
    return {"five_hour": 1800, "seven_day": 10800}


@dataclass(frozen=True)
class Filter:
    accounts: tuple[str, ...] = ()
    windows: tuple[str, ...] = WINDOWS
    events: tuple[str, ...] = EVENT_TYPES
    lead_s: dict[str, int] = field(default_factory=_default_lead_s)
    min_left_pct: float = 20.0


@dataclass(frozen=True)
class Destination:
    id: str
    kind: str
    enabled: bool
    url: str = ""
    filter: Filter = field(default_factory=Filter)
    last_error: str = ""


@dataclass
class FiredState:
    windows: dict[str, float]
    sent: dict[str, float]


def _default_destinations() -> list[Destination]:
    return [Destination(id="mac", kind="mac", enabled=False, filter=Filter())]


def _read_object(path: Path) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        _logger.warning("Could not read %s (%s); using defaults", path, e)
        return None
    if not isinstance(raw, dict):
        _logger.warning("%s is not a JSON object; using defaults", path)
        return None
    return raw


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _str_tuple(value, allowed: tuple[str, ...] | None = None) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"expected a list of strings, got {value!r}")
    if allowed is not None and not set(value) <= set(allowed):
        raise ValueError(f"unknown values in {value!r}")
    return tuple(value)


def _filter_from_json(raw) -> Filter:
    if not isinstance(raw, dict):
        raise ValueError("filter is not an object")
    default = Filter()
    lead_s = raw.get("leadS", {})
    if not isinstance(lead_s, dict) or not all(
        k in WINDOWS and _is_number(v) and v >= 0 for k, v in lead_s.items()
    ):
        raise ValueError(f"bad leadS {lead_s!r}")
    min_left_pct = raw.get("minLeftPct", default.min_left_pct)
    if not _is_number(min_left_pct):
        raise ValueError(f"bad minLeftPct {min_left_pct!r}")
    return Filter(
        accounts=_str_tuple(raw.get("accounts", list(default.accounts))),
        windows=_str_tuple(raw.get("windows", list(default.windows)), WINDOWS),
        events=_str_tuple(raw.get("events", list(default.events)), EVENT_TYPES),
        lead_s=default.lead_s | {k: int(v) for k, v in lead_s.items()},
        min_left_pct=float(min_left_pct),
    )


def _destination_from_json(raw) -> Destination:
    if not isinstance(raw, dict):
        raise ValueError("destination is not an object")
    dest_id, kind, enabled = raw.get("id"), raw.get("kind"), raw.get("enabled")
    url, last_error = raw.get("url", ""), raw.get("lastError", "")
    if not isinstance(dest_id, str) or not dest_id:
        raise ValueError(f"bad id {dest_id!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if dest_id == "mac" and kind != "mac":
        raise ValueError(f"id 'mac' is reserved for the mac destination, not {kind!r}")
    if not isinstance(enabled, bool):
        raise ValueError(f"bad enabled {enabled!r}")
    if not isinstance(url, str) or not isinstance(last_error, str):
        raise ValueError("url and lastError must be strings")
    return Destination(
        id=dest_id,
        kind=kind,
        enabled=enabled,
        url=url,
        filter=_filter_from_json(raw.get("filter", {})),
        last_error=last_error,
    )


def _destination_to_json(dest: Destination) -> dict:
    f = dest.filter
    return {
        "id": dest.id,
        "kind": dest.kind,
        "enabled": dest.enabled,
        "url": dest.url,
        "filter": {
            "accounts": list(f.accounts),
            "windows": list(f.windows),
            "events": list(f.events),
            "leadS": dict(f.lead_s),
            "minLeftPct": f.min_left_pct,
        },
        "lastError": dest.last_error,
    }


def load_destinations(root: Path) -> list[Destination]:
    path = root / CONFIG_FILENAME
    raw = _read_object(path)
    if raw is None:
        return _default_destinations()
    entries = raw.get("destinations")
    if not isinstance(entries, list):
        _logger.warning("%s has no destinations list; using defaults", path)
        return _default_destinations()
    dests: list[Destination] = []
    for entry in entries:
        try:
            dest = _destination_from_json(entry)
            if any(d.id == dest.id for d in dests):
                raise ValueError(f"duplicate id {dest.id!r}")
        except ValueError as e:
            _logger.warning("Dropping reset-event destination in %s: %s", path, e)
            continue
        dests.append(dest)
    if not any(d.kind == "mac" for d in dests):
        dests = _default_destinations() + dests
    return dests


def _config_lock(root: Path) -> FileLock:
    return FileLock(root / "reset_events.lock")


def _write_destinations(root: Path, dests: list[Destination]) -> None:
    atomic_write_json(
        root / CONFIG_FILENAME,
        {"destinations": [_destination_to_json(d) for d in dests]},
    )


def save_destinations(root: Path, dests: list[Destination]) -> None:
    with _config_lock(root):
        _write_destinations(root, dests)


def update_destinations(
    root: Path, fn: Callable[[list[Destination]], list[Destination]],
) -> list[Destination]:
    """Load, transform, and save under one lock so concurrent writers never
    overwrite each other's changes from a stale copy."""
    with _config_lock(root):
        dests = fn(load_destinations(root))
        _write_destinations(root, dests)
    return dests


def _number_map(value) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {k: float(v) for k, v in value.items() if isinstance(k, str) and _is_number(v)}


def load_state(root: Path) -> FiredState:
    raw = _read_object(root / STATE_FILENAME) or {}
    return FiredState(windows=_number_map(raw.get("windows")), sent=_number_map(raw.get("sent")))


def save_state(root: Path, state: FiredState, now: float | None = None) -> None:
    cutoff = (time.time() if now is None else now) - SENT_RETENTION_S
    state.sent = {k: ts for k, ts in state.sent.items() if ts >= cutoff}
    atomic_write_json(
        root / STATE_FILENAME,
        {"windows": dict(state.windows), "sent": dict(state.sent)},
    )
