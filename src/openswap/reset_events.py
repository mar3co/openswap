"""Reset events: per-account usage-window events and where they are delivered.

``reset_events.json`` holds the user's destinations; ``reset_events_state.json``
records what has already fired so a restart does not repeat an event.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from openswap.locking import FileLock
from openswap.poll_policy import parse_reset_ts
from openswap.settings import atomic_write_json

EVENT_TYPES = ("window.started", "window.ending_soon", "window.reset")
WINDOWS = ("five_hour", "seven_day")
KINDS = ("mac", "webhook")

CONFIG_FILENAME = "reset_events.json"
STATE_FILENAME = "reset_events_state.json"
SENT_RETENTION_S = 8 * 86400
WEEK_S = 7 * 86400
MAX_LEAD_S = 30 * 86400


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
    last_failure: str = ""


@dataclass
class FiredState:
    windows: dict[str, float]
    sent: dict[str, float]
    emitted: dict[str, float] = field(default_factory=dict)


class DestinationsUnreadable(ValueError):
    """``reset_events.json`` could not be read in full, so writing it back would lose data."""


def _default_destinations() -> list[Destination]:
    return [Destination(id="mac", kind="mac", enabled=False, filter=Filter())]


def _read_object(path: Path) -> tuple[dict | None, str | None]:
    """The JSON object in ``path`` (None when missing or unreadable), and why it was unreadable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, RecursionError) as e:
        return None, str(e)
    if not isinstance(raw, dict):
        return None, "not a JSON object"
    return raw, None


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_in(value, low: float, high: float) -> bool:
    return _is_number(value) and low <= value <= high


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
        k in WINDOWS and _is_finite_in(v, 0, MAX_LEAD_S) for k, v in lead_s.items()
    ):
        raise ValueError(f"bad leadS {lead_s!r}")
    min_left_pct = raw.get("minLeftPct", default.min_left_pct)
    if not _is_finite_in(min_left_pct, 0, 100):
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
    url, last_error, last_failure = raw.get("url", ""), raw.get("lastError", ""), raw.get("lastFailure", "")
    if not isinstance(dest_id, str) or not dest_id:
        raise ValueError(f"bad id {dest_id!r}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if dest_id == "mac" and kind != "mac":
        raise ValueError(f"id 'mac' is reserved for the mac destination, not {kind!r}")
    if not isinstance(enabled, bool):
        raise ValueError(f"bad enabled {enabled!r}")
    if not all(isinstance(v, str) for v in (url, last_error, last_failure)):
        raise ValueError("url, lastError and lastFailure must be strings")
    return Destination(
        id=dest_id,
        kind=kind,
        enabled=enabled,
        url=url,
        filter=_filter_from_json(raw.get("filter", {})),
        last_error=last_error,
        last_failure=last_failure,
    )


def destination_to_json(dest: Destination) -> dict:
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
        "lastFailure": dest.last_failure,
    }


def _destinations_from_json(entries: list) -> tuple[list[Destination], list[str]]:
    """The destinations that parse, and why each other entry was dropped."""
    dests: list[Destination] = []
    dropped: list[str] = []
    for entry in entries:
        try:
            dest = _destination_from_json(entry)
            if any(d.id == dest.id for d in dests):
                raise ValueError(f"duplicate id {dest.id!r}")
        except ValueError as e:
            dropped.append(str(e))
            continue
        dests.append(dest)
    return dests, dropped


def _unreadable(path: Path, reason: str) -> str:
    return f"Reset events config could not be read in full ({reason}); fix or remove it: {path}"


def load_destinations_checked(root: Path) -> tuple[list[Destination], str | None]:
    """The destinations, and what in ``reset_events.json`` could not be read
    (None when nothing was dropped)."""
    path = root / CONFIG_FILENAME
    raw, problem = _read_object(path)
    if raw is None:
        return _default_destinations(), None if problem is None else _unreadable(path, problem)
    entries = raw.get("destinations")
    if not isinstance(entries, list):
        return _default_destinations(), _unreadable(path, "no destinations list")
    dests, dropped = _destinations_from_json(entries)
    if not any(d.kind == "mac" for d in dests):
        dests = _default_destinations() + dests
    return dests, _unreadable(path, "; ".join(dropped)) if dropped else None


def load_destinations(root: Path) -> list[Destination]:
    return load_destinations_checked(root)[0]


def _config_lock(root: Path) -> FileLock:
    return FileLock(root / "reset_events.lock")


def _write_destinations(root: Path, dests: list[Destination]) -> None:
    """Raises ValueError, writing nothing, for anything the loader would drop."""
    entries = [destination_to_json(d) for d in dests]
    _, dropped = _destinations_from_json(entries)
    if dropped:
        raise ValueError(f"Invalid reset events destination: {'; '.join(dropped)}")
    atomic_write_json(root / CONFIG_FILENAME, {"destinations": entries})


def save_destinations(root: Path, dests: list[Destination]) -> None:
    with _config_lock(root):
        _write_destinations(root, dests)


def update_destinations(
    root: Path, fn: Callable[[list[Destination]], list[Destination]],
) -> list[Destination]:
    """Load, transform, and save under one lock so concurrent writers never
    overwrite each other's changes from a stale copy. Nothing is written when
    ``fn`` changes nothing.

    Raises :class:`DestinationsUnreadable`, writing nothing, when the file
    could not be read in full."""
    with _config_lock(root):
        dests, problem = load_destinations_checked(root)
        if problem:
            raise DestinationsUnreadable(problem)
        updated = fn(dests)
        if updated != dests:
            _write_destinations(root, updated)
    return updated


def with_delivery_result(dest: Destination, error: str, now: float) -> Destination:
    """``dest`` after a delivery: ``last_error`` is the latest result, and
    ``last_failure`` keeps the latest failure even after a later success."""
    if not error:
        return replace(dest, last_error="")
    return replace(dest, last_error=error, last_failure=f"{_iso_z(int(now))} {error}")


def _finite_float(value) -> float | None:
    try:
        f = float(value)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _number_map(value) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    return {
        k: f for k, v in value.items()
        if isinstance(k, str) and _is_number(v) and (f := _finite_float(v)) is not None
    }


def load_state_checked(root: Path) -> tuple[FiredState, str | None]:
    """The fired state (empty when missing or unreadable), and why it was unreadable."""
    path = root / STATE_FILENAME
    raw, problem = _read_object(path)
    raw = raw or {}
    state = FiredState(
        windows=_number_map(raw.get("windows")),
        sent=_number_map(raw.get("sent")),
        emitted=_number_map(raw.get("emitted")),
    )
    return state, None if problem is None else f"{path}: {problem}"


def load_state(root: Path) -> FiredState:
    return load_state_checked(root)[0]


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


def _reset_ts(value) -> float | None:
    """A ``resets_at`` in whole seconds: the API stamps per-request microseconds on it."""
    ts = parse_reset_ts(value) if isinstance(value, str) else None
    return None if ts is None else float(int(ts))


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
        ts = _reset_ts(entry.get("resets_at"))
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
        k = f"{reading.key}|{window}"
        known = state.windows.get(k)
        if known is not None and known <= now:
            if now < known + WEEK_S:
                events.append(ResetEvent("window.reset", window, reading, known, 0.0, scoped))
            if window == "seven_day":
                known += (int((now - known) // WEEK_S) + 1) * WEEK_S
                state.windows[k] = known
        data = reading.usage.get(window)
        if not isinstance(data, dict) or not _is_number(data.get("pct")):
            continue
        ts = _reset_ts(data.get("resets_at"))
        if ts is None:
            continue
        pct = float(data["pct"])
        if reading.live and ts > now and ts != known:
            if known is not None and ts > known + 60 and window == "five_hour" and pct < 100:
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
    match when first emitted, so a destination enabled later never gets an
    earlier moment. A reset can still arrive up to 7 days late, after the
    Mac was off or asleep, to the destinations enabled at that point.
    ``ending_soon`` is tracked per destination instead, since each
    destination has its own lead time."""
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
