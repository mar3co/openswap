"""Reset events: per-account usage-window events and where they are delivered.

``reset_events.json`` holds the user's destinations; ``reset_events_state.json``
records what has already fired so a restart does not repeat an event.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
WEEK_S = 7 * 86400


def _default_lead_s() -> dict[str, int]:
    return {"five_hour": 1800, "seven_day": 10800}


@dataclass(frozen=True)
class Filter:
    accounts: tuple[str, ...] = ()
    windows: tuple[str, ...] = WINDOWS
    events: tuple[str, ...] = EVENT_TYPES
    lead_s: dict[str, int] = field(default_factory=_default_lead_s)
    min_left_pct: float = 20.0

    def __post_init__(self):
        object.__setattr__(self, "lead_s", _default_lead_s() | self.lead_s)


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
    emitted: dict[str, float] = field(default_factory=dict)


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
        lead_s={k: int(v) for k, v in lead_s.items()},
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
    return FiredState(
        windows=_number_map(raw.get("windows")),
        sent=_number_map(raw.get("sent")),
        emitted=_number_map(raw.get("emitted")),
    )


def save_state(root: Path, state: FiredState, now: float | None = None) -> None:
    cutoff = (time.time() if now is None else now) - SENT_RETENTION_S
    state.sent = {k: ts for k, ts in state.sent.items() if ts >= cutoff}
    state.emitted = {k: ts for k, ts in state.emitted.items() if ts >= cutoff}
    atomic_write_json(
        root / STATE_FILENAME,
        {"windows": dict(state.windows), "sent": dict(state.sent), "emitted": dict(state.emitted)},
    )


@dataclass(frozen=True)
class AccountReading:
    provider: str
    email: str
    org_uuid: str
    label: str
    org_name: str
    is_active: bool
    live: bool
    usage: dict | None
    fetched_at: float | None

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.email}:{self.org_uuid}"


def _iso_z(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class ResetEvent:
    type: str
    window: str
    reading: AccountReading
    resets_at: float
    used_pct: float
    scoped: tuple[dict, ...]

    @property
    def resets_at_iso(self) -> str:
        return _iso_z(self.resets_at)

    @property
    def id(self) -> str:
        r = self.reading
        raw = f"{r.provider}|{r.email}|{r.org_uuid}|{self.window}|{self.resets_at_iso}|{self.type}"
        return "evt_" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def readings_from_snapshot(snapshot: dict) -> list[AccountReading]:
    identities = snapshot.get("identities") or {}
    readings = []
    for num, email, is_active, display, last_good, alias, org_name, _disabled, fetched_at in (
        snapshot.get("accounts") or []
    ):
        live = isinstance(display, dict)
        usage = display if live else last_good if isinstance(last_good, dict) else None
        if usage is None:
            continue
        _, org_uuid = identities.get(str(num)) or ("", "")
        readings.append(AccountReading(
            provider="codex" if str(num).startswith("codex:") else "claude",
            email=email,
            org_uuid=org_uuid,
            label=alias or email,
            org_name=org_name or "",
            is_active=bool(is_active),
            live=live,
            usage=usage,
            fetched_at=fetched_at,
        ))
    return readings


def _scoped(usage: dict) -> tuple[dict, ...]:
    out = []
    for entry in usage.get("scoped") or []:
        if not isinstance(entry, dict) or not entry.get("name") or not _is_number(entry.get("pct")):
            continue
        ts = _parse_ts(entry.get("resets_at"))
        out.append({
            "name": entry["name"],
            "used_pct": float(entry["pct"]),
            "resets_at": None if ts is None else _iso_z(ts),
        })
    return tuple(out)


def _window_events(reading: AccountReading, state: FiredState, now: float) -> list[ResetEvent]:
    events = []
    scoped = _scoped(reading.usage)
    for window in WINDOWS:
        data = reading.usage.get(window)
        if not isinstance(data, dict) or not _is_number(data.get("pct")):
            continue
        ts = _parse_ts(data.get("resets_at"))
        if ts is None:
            continue
        pct = float(data["pct"])
        k = f"{reading.key}|{window}"
        known = state.windows.get(k)
        if known is not None and known <= now:
            if now < known + WEEK_S:
                events.append(ResetEvent("window.reset", window, reading, known, 0.0, scoped))
            if window == "seven_day":
                known += (int((now - known) // WEEK_S) + 1) * WEEK_S
                state.windows[k] = known
        if reading.live and ts > now and ts != known:
            if known is not None and ts > known and window == "five_hour" and pct < 100:
                events.append(ResetEvent("window.started", window, reading, ts, pct, scoped))
            state.windows[k] = ts
        if ts > now:
            events.append(ResetEvent("window.ending_soon", window, reading, ts, pct, scoped))
    return events


def matches(dest: Destination, event: ResetEvent, now: float) -> bool:
    f = dest.filter
    if f.accounts and event.reading.key not in f.accounts:
        return False
    if event.window not in f.windows or event.type not in f.events:
        return False
    if event.type == "window.ending_soon":
        return (
            0 < event.resets_at - now <= f.lead_s[event.window]
            and 100 - event.used_pct >= f.min_left_pct
        )
    return True


def detect(
    readings: list[AccountReading], dests: list[Destination], state: FiredState, now: float,
) -> list[tuple[Destination, ResetEvent]]:
    """Update window tracking in ``state`` and return each (destination, event)
    not yet sent; records what it returns in ``state.sent``.

    ``started`` and ``reset`` are moments: they go only to destinations that
    match when first emitted, so a destination enabled later never gets a
    days-old reset. ``ending_soon`` is tracked per destination instead,
    since each destination has its own lead time."""
    enabled = [d for d in dests if d.enabled]
    out = []
    for reading in readings:
        for event in _window_events(reading, state, now):
            if event.type != "window.ending_soon":
                if event.id in state.emitted:
                    continue
                state.emitted[event.id] = now
            for dest in enabled:
                sent_key = f"{dest.id}|{event.id}"
                if sent_key not in state.sent and matches(dest, event, now):
                    state.sent[sent_key] = now
                    out.append((dest, event))
    return out


def payload(event: ResetEvent, *, mac: str, now: float, test: bool = False) -> dict:
    r = event.reading
    body = {
        "id": event.id,
        "type": event.type,
        "provider": r.provider,
        "account": {"label": r.label, "email": r.email, "org": r.org_name},
        "window": event.window,
        "resets_at": event.resets_at_iso,
        "seconds_until_reset": max(0, int(event.resets_at - now)),
        "used_pct": event.used_pct,
        "remaining_pct": round(100 - event.used_pct, 1),
        "is_active_account": r.is_active,
        "scoped_limits": list(event.scoped),
        "measured_at": None if r.fetched_at is None else _iso_z(int(r.fetched_at)),
        "observed_at": _iso_z(int(now)),
        "mac": mac,
    }
    if test:
        body["test"] = True
    return body


def signature_header(secret: str, body: bytes, t: int) -> str:
    digest = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"
