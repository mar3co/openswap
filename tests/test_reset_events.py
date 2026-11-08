"""Tests for the reset-event config and fired-state files (reset_events.py)."""

from __future__ import annotations

import json
from dataclasses import replace

from openswap.reset_events import (
    Destination,
    FiredState,
    Filter,
    load_destinations,
    load_state,
    save_destinations,
    save_state,
    update_destinations,
)

DEFAULT_MAC = Destination(id="mac", kind="mac", enabled=False, filter=Filter())
DEFAULT = [DEFAULT_MAC]


def _write_config(root, entries):
    (root / "reset_events.json").write_text(json.dumps({"destinations": entries}))


def test_missing_config_gives_disabled_mac_destination(tmp_path):
    dests = load_destinations(tmp_path)

    assert dests == DEFAULT
    assert dests[0].enabled is False


def test_destinations_round_trip(tmp_path):
    dests = [
        Destination(id="mac", kind="mac", enabled=True, filter=Filter()),
        Destination(
            id="hook1",
            kind="webhook",
            enabled=True,
            url="https://example.com/hook",
            filter=Filter(
                accounts=("claude:a@example.com:org-1",),
                windows=("five_hour",),
                events=("window.reset", "window.ending_soon"),
                lead_s={"five_hour": 600, "seven_day": 3600},
                min_left_pct=35.5,
            ),
            last_error="HTTP 500",
        ),
    ]

    save_destinations(tmp_path, dests)

    assert load_destinations(tmp_path) == dests
    raw = json.loads((tmp_path / "reset_events.json").read_text())
    hook = raw["destinations"][1]
    assert hook["lastError"] == "HTTP 500"
    assert hook["filter"]["leadS"] == {"five_hour": 600, "seven_day": 3600}
    assert hook["filter"]["minLeftPct"] == 35.5


def test_unknown_kind_dropped_others_kept(tmp_path):
    good = {"id": "hook1", "kind": "webhook", "enabled": True, "url": "https://example.com/h"}
    _write_config(tmp_path, [
        {"id": "mac", "kind": "mac", "enabled": True},
        {"id": "x", "kind": "pager", "enabled": True},
        {"id": "y", "kind": "webhook", "enabled": "yes"},
        {"id": "z", "kind": "webhook", "enabled": True, "filter": {"windows": ["daily"]}},
        {"id": "w", "kind": "webhook", "enabled": True, "filter": {"leadS": {"five_hour": True}}},
        good,
    ])

    assert load_destinations(tmp_path) == [
        Destination(id="mac", kind="mac", enabled=True, filter=Filter()),
        Destination(id="hook1", kind="webhook", enabled=True, url="https://example.com/h", filter=Filter()),
    ]


def test_partial_lead_s_merges_over_defaults_and_negative_is_dropped(tmp_path):
    _write_config(tmp_path, [
        {"id": "mac", "kind": "mac", "enabled": True, "filter": {"leadS": {"seven_day": 600}}},
        {"id": "neg", "kind": "webhook", "enabled": True, "filter": {"leadS": {"five_hour": -1}}},
    ])

    dests = load_destinations(tmp_path)

    assert [d.id for d in dests] == ["mac"]
    assert dests[0].filter.lead_s == {"five_hour": 1800, "seven_day": 600}


def test_mac_destination_inserted_when_none_survives(tmp_path):
    hook = {"id": "hook1", "kind": "webhook", "enabled": True, "url": "https://example.com/h"}
    _write_config(tmp_path, [
        {"id": "mac", "kind": "mac", "enabled": "on"},
        {"id": "mac", "kind": "webhook", "enabled": True},
        hook,
    ])
    assert load_destinations(tmp_path) == [
        DEFAULT_MAC,
        Destination(id="hook1", kind="webhook", enabled=True, url="https://example.com/h"),
    ]

    _write_config(tmp_path, [{"id": "x", "kind": "pager", "enabled": True}])
    assert load_destinations(tmp_path) == DEFAULT


def test_duplicate_ids_keep_first(tmp_path):
    _write_config(tmp_path, [
        {"id": "mac", "kind": "mac", "enabled": False},
        {"id": "h", "kind": "webhook", "enabled": True, "url": "https://first.example"},
        {"id": "h", "kind": "webhook", "enabled": True, "url": "https://second.example"},
        {"id": "mac", "kind": "mac", "enabled": True},
    ])

    dests = load_destinations(tmp_path)

    assert [(d.id, d.url, d.enabled) for d in dests] == [
        ("mac", "", False), ("h", "https://first.example", True),
    ]


def test_update_destinations_sees_current_file_and_persists(tmp_path):
    hook = Destination(id="h", kind="webhook", enabled=True, url="https://example.com/h")
    save_destinations(tmp_path, [DEFAULT_MAC, hook])
    seen = []

    def record_error(dests):
        seen.append(list(dests))
        return [replace(d, last_error="HTTP 500") if d.id == "h" else d for d in dests]

    result = update_destinations(tmp_path, record_error)

    assert seen == [[DEFAULT_MAC, hook]]
    assert result == [DEFAULT_MAC, replace(hook, last_error="HTTP 500")]
    assert load_destinations(tmp_path) == result


def test_corrupt_config_falls_back_to_default(tmp_path):
    path = tmp_path / "reset_events.json"
    for text in ("{not json", "[]", '{"destinations": "nope"}'):
        path.write_text(text)
        assert load_destinations(tmp_path) == DEFAULT


def test_state_round_trip_prunes_sent_older_than_8_days(tmp_path):
    now = 1_800_000_000.0
    state = FiredState(
        windows={"claude:a@example.com:org-1|five_hour": now + 3600},
        sent={"mac|old": now - 9 * 86400, "mac|recent": now - 86400},
    )

    save_state(tmp_path, state, now=now)
    loaded = load_state(tmp_path)

    assert loaded.windows == state.windows
    assert loaded.sent == {"mac|recent": now - 86400}


def test_missing_or_corrupt_state_is_empty(tmp_path):
    assert load_state(tmp_path) == FiredState(windows={}, sent={})
    (tmp_path / "reset_events_state.json").write_text("{oops")
    assert load_state(tmp_path) == FiredState(windows={}, sent={})
