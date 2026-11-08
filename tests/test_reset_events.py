"""Tests for the reset-event config and fired-state files (reset_events.py)."""

from __future__ import annotations

import json

from openswap.reset_events import (
    Destination,
    FiredState,
    Filter,
    load_destinations,
    load_state,
    save_destinations,
    save_state,
)

DEFAULT = [Destination(id="mac", kind="mac", enabled=False, filter=Filter())]


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
    (tmp_path / "reset_events.json").write_text(json.dumps({
        "destinations": [
            {"id": "x", "kind": "pager", "enabled": True},
            {"id": "y", "kind": "webhook", "enabled": "yes"},
            {"id": "z", "kind": "webhook", "enabled": True, "filter": {"windows": ["daily"]}},
            {"id": "w", "kind": "webhook", "enabled": True, "filter": {"leadS": {"five_hour": True}}},
            good,
        ],
    }))

    assert load_destinations(tmp_path) == [
        Destination(id="hook1", kind="webhook", enabled=True, url="https://example.com/h", filter=Filter()),
    ]


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
