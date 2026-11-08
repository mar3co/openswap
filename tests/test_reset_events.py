"""Tests for reset events: config and state files, detection, payload and signature."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from openswap.reset_events import (
    AccountReading,
    Destination,
    DestinationsUnreadable,
    Filter,
    FiredState,
    ResetEvent,
    detect,
    load_destinations,
    load_destinations_checked,
    load_state,
    load_state_checked,
    payload,
    readings_from_snapshot,
    save_destinations,
    save_state,
    signature_header,
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


@pytest.mark.parametrize("text", [
    "{not json",
    json.dumps({"destinations": "nope"}),
    json.dumps({"destinations": [
        {"id": "mac", "kind": "mac", "enabled": True},
        {"id": "h", "kind": "webhook", "enabled": "yes", "url": "https://example.com/h"},
    ]}),
])
def test_update_destinations_refuses_to_overwrite_a_degraded_file(tmp_path, text):
    config = tmp_path / "reset_events.json"
    config.write_text(text)

    with pytest.raises(DestinationsUnreadable):
        update_destinations(tmp_path, lambda dests: dests)

    assert config.read_text() == text


def test_update_destinations_starts_from_defaults_when_file_is_missing(tmp_path):
    assert update_destinations(tmp_path, lambda dests: dests) == DEFAULT


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
        emitted={"evt_old": now - 9 * 86400, "evt_recent": now - 86400},
    )

    save_state(tmp_path, state, now=now)
    loaded = load_state(tmp_path)

    assert loaded.windows == state.windows
    assert loaded.sent == {"mac|recent": now - 86400}
    assert loaded.emitted == {"evt_recent": now - 86400}


def test_missing_or_corrupt_state_is_empty(tmp_path):
    assert load_state(tmp_path) == FiredState(windows={}, sent={})
    (tmp_path / "reset_events_state.json").write_text("{oops")
    assert load_state(tmp_path) == FiredState(windows={}, sent={})


T = 1_800_000_000.0
WEEK = 7 * 86400
KEY = "claude:a@example.com:org-1"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def reading(window_ts=T, pct=40.0, live=True, fetched_at=None, window="five_hour",
            email="a@example.com", scoped=None):
    usage = {window: {"pct": pct, "resets_at": _iso(window_ts)}}
    if scoped is not None:
        usage["scoped"] = scoped
    return AccountReading(
        provider="claude", email=email, org_uuid="org-1", label="Work", org_name="Acme",
        is_active=True, live=live, usage=usage, fetched_at=fetched_at,
    )


def hook(dest_id="h", enabled=True, **filter_kw):
    return Destination(id=dest_id, kind="webhook", enabled=enabled,
                       url="https://example.com/h", filter=Filter(**filter_kw))


def empty_state():
    return FiredState(windows={}, sent={})


def types(results):
    return [e.type for _, e in results]


def test_first_observation_seeds_without_started():
    state = empty_state()

    assert detect([reading()], [hook()], state, now=T - 4 * 3600) == []
    assert state.windows == {f"{KEY}|five_hour": T}


def test_later_resets_at_from_live_reading_fires_started_once():
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})
    later = T + 5 * 3600
    now = T + 60

    out = detect([reading(window_ts=later, pct=2.0)], [hook(events=("window.started",))], state, now)

    assert len(out) == 1
    event = out[0][1]
    assert (event.type, event.window, event.resets_at, event.used_pct) == (
        "window.started", "five_hour", later, 2.0)
    assert state.windows[f"{KEY}|five_hour"] == later
    assert detect([reading(window_ts=later, pct=2.0)], [hook(events=("window.started",))],
                  state, now) == []


def test_started_not_fired_for_weekly():
    state = FiredState(windows={f"{KEY}|seven_day": T}, sent={})

    out = detect([reading(window_ts=T + WEEK, window="seven_day")],
                 [hook(events=("window.started",))], state, now=T - 60)

    assert out == []
    assert state.windows[f"{KEY}|seven_day"] == T + WEEK


def test_started_never_from_last_known_good():
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    out = detect([reading(window_ts=T + 5 * 3600, live=False)],
                 [hook(events=("window.started",))], state, now=T - 60)

    assert out == []
    assert state.windows[f"{KEY}|five_hour"] == T


def test_reset_fires_from_state_without_fetch():
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    out = detect([reading(window_ts=T, live=False)], [hook()], state, now=T + 1)

    assert types(out) == ["window.reset"]
    assert out[0][1].resets_at == T
    assert out[0][1].used_pct == 0
    assert detect([reading(window_ts=T, live=False)], [hook()], state, now=T + 2) == []


def test_reset_not_emitted_after_seven_days():
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    assert detect([reading(window_ts=T, live=False)], [hook()], state, now=T + WEEK) == []


def test_weekly_state_rolls_to_next_boundary_after_reset():
    k = f"{KEY}|seven_day"
    state = FiredState(windows={k: T}, sent={})

    out = detect([reading(window_ts=T, window="seven_day", live=False)], [hook()], state, now=T + 1)

    assert types(out) == ["window.reset"]
    assert state.windows[k] == T + WEEK

    state = FiredState(windows={k: T}, sent={})
    assert detect([reading(window_ts=T, window="seven_day", live=False)], [hook()], state,
                  now=T + 3 * WEEK + 5) == []
    assert state.windows[k] == T + 4 * WEEK


def test_sleep_past_reset_gives_reset_not_ending_soon():
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    out = detect([reading(window_ts=T, live=False)], [hook(lead_s={"five_hour": 7200})],
                 state, now=T + 3600)

    assert types(out) == ["window.reset"]


def test_ending_soon_respects_lead_and_min_left():
    dest = hook(events=("window.ending_soon",), lead_s={"five_hour": 1800})
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    assert detect([reading(pct=60.0)], [dest], state, now=T - 1900) == []
    assert detect([reading(pct=85.0)], [dest], state, now=T - 1700) == []
    out = detect([reading(pct=60.0)], [dest], state, now=T - 1700)

    assert types(out) == ["window.ending_soon"]
    assert (out[0][1].resets_at, out[0][1].used_pct) == (T, 60.0)


def test_ending_soon_per_destination_independent():
    early = hook("early", events=("window.ending_soon",), lead_s={"five_hour": 3600})
    late = hook("late", events=("window.ending_soon",), lead_s={"five_hour": 600})
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})
    got = []

    for now in (T - 4000, T - 3500, T - 3000, T - 500, T - 100):
        got += [(d.id, now) for d, _ in detect([reading()], [early, late], state, now)]

    assert got == [("early", T - 3500), ("late", T - 500)]


def test_ending_soon_from_previous_window_numbers_skipped():
    state = FiredState(windows={f"{KEY}|five_hour": T + 5 * 3600}, sent={})

    out = detect([reading(window_ts=T, live=False)],
                 [hook(events=("window.ending_soon",), lead_s={"five_hour": 7200})],
                 state, now=T + 60)

    assert out == []


def test_disabled_destination_gets_nothing():
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    assert detect([reading()], [hook(enabled=False)], state, now=T + 1) == []
    assert state.sent == {}


def test_account_filter_limits_events():
    other = reading(email="b@example.com")
    state = FiredState(windows={f"{KEY}|five_hour": T, "claude:b@example.com:org-1|five_hour": T},
                       sent={})

    out = detect([reading(), other], [hook(accounts=(KEY,))], state, now=T + 1)

    assert [e.reading.key for _, e in out] == [KEY]


def test_event_id_identical_across_states():
    state_a = FiredState(windows={f"{KEY}|five_hour": T}, sent={})
    state_b = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    [(_, a)] = detect([reading()], [hook("one")], state_a, now=T + 1)
    [(_, b)] = detect([reading()], [hook("two")], state_b, now=T + 1)

    expected = "evt_" + hashlib.sha256(
        f"claude|a@example.com|org-1|five_hour|{_iso(T).replace('+00:00', 'Z')}|window.reset"
        .encode()).hexdigest()[:24]
    assert a.id == b.id == expected


def test_readings_skip_rows_without_usage():
    good = {"five_hour": {"pct": 10.0, "resets_at": _iso(T)}}
    snapshot = {
        "accounts": [
            ("1", "a@example.com", True, "Rate limited", None, "Work", "Acme", False, None),
            ("2", "b@example.com", False, "Rate limited", good, None, "", False, T - 30),
            ("codex:1", "c@example.com", False, good, None, None, "", False, T - 10),
        ],
        "identities": {"1": ("a@example.com", "org-1"), "2": ("b@example.com", "org-2"),
                       "codex:1": ("c@example.com", "")},
    }

    got = readings_from_snapshot(snapshot)

    assert got == [
        AccountReading(provider="claude", email="b@example.com", org_uuid="org-2",
                       label="b@example.com", org_name="", is_active=False, live=False,
                       usage=good, fetched_at=T - 30),
        AccountReading(provider="codex", email="c@example.com", org_uuid="",
                       label="c@example.com", org_name="", is_active=False, live=True,
                       usage=good, fetched_at=T - 10),
    ]


def test_payload_shape():
    scoped = ({"name": "Fable", "used_pct": 100.0, "resets_at": "2027-01-15T08:00:00Z"},)
    event = ResetEvent(type="window.ending_soon", window="seven_day",
                       reading=reading(fetched_at=T - 600), resets_at=T,
                       used_pct=61.0, scoped=scoped)

    body = payload(event, mac="Test Mac", now=T - 7200.5)

    assert set(body) == {
        "id", "type", "provider", "account", "window", "resets_at", "seconds_until_reset",
        "used_pct", "remaining_pct", "is_active_account", "scoped_limits", "measured_at",
        "observed_at", "mac",
    }
    assert body["id"] == event.id
    assert body["account"] == {"label": "Work", "email": "a@example.com", "org": "Acme"}
    assert body["resets_at"] == "2027-01-15T08:00:00Z"
    assert body["seconds_until_reset"] == 7200
    assert body["remaining_pct"] == 39.0
    assert body["measured_at"] == "2027-01-15T07:50:00Z"
    assert body["observed_at"] == "2027-01-15T05:59:59Z"
    assert payload(replace(event, reading=reading(fetched_at=T - 600.7)), mac="m",
                   now=T)["measured_at"] == "2027-01-15T07:49:59Z"
    assert body["scoped_limits"] == list(scoped)
    assert body["is_active_account"] is True
    assert payload(event, mac="m", now=T + 5, test=True)["test"] is True
    assert payload(event, mac="m", now=T + 5)["seconds_until_reset"] == 0
    no_fetch = replace(event, reading=reading(fetched_at=None))
    assert payload(no_fetch, mac="m", now=T)["measured_at"] is None


def test_detect_maps_scoped_limits():
    scoped = [{"name": "Fable", "pct": 100.0, "resets_at": "2027-01-15T08:00:00+00:00",
               "countdown": "1h", "clock": "08:00"}, {"name": "Bad"}]
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    [(_, event)] = detect([reading(scoped=scoped)], [hook()], state, now=T + 1)

    assert event.scoped == (
        {"name": "Fable", "used_pct": 100.0, "resets_at": "2027-01-15T08:00:00Z"},)


def test_signature_header_matches_reference():
    body = b'{"a": 1}'
    expected = hmac.new(b"s", b"1700000000." + body, hashlib.sha256).hexdigest()

    assert signature_header("s", body, 1700000000) == f"t=1700000000,v1={expected}"


def test_reset_not_delivered_to_destination_added_later():
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    assert types(detect([reading(live=False)], [hook("h")], state, now=T + 1)) == ["window.reset"]
    assert detect([reading(live=False)], [hook("h"), hook("new")], state, now=T + 3 * 86400) == []


def test_weekly_anchor_moved_earlier_corrects_state_without_started():
    k = f"{KEY}|seven_day"
    state = FiredState(windows={k: T + WEEK}, sent={})

    out = detect([reading(window_ts=T + WEEK - 3600, window="seven_day")],
                 [hook(events=("window.started",))], state, now=T)

    assert out == []
    assert state.windows[k] == T + WEEK - 3600


def test_partial_lead_s_filter_gets_defaults():
    assert Filter(lead_s={"five_hour": 600}).lead_s == {"five_hour": 600, "seven_day": 10800}


def test_notification_copy_for_each_event_type():
    from openswap.menubar_display import notification_copy_for_reset_event
    from openswap.oauth import reset_clock_string

    r = reading()
    started = ResetEvent("window.started", "five_hour", r, T, 38.0, ())
    now = T - 5 * 3600
    clock = reset_clock_string(datetime.fromtimestamp(T, timezone.utc),
                               datetime.fromtimestamp(now, timezone.utc))
    copy = notification_copy_for_reset_event(started, now)
    assert (copy.title, copy.body) == ("Work: 5-hour window started", f"62% left · resets {clock}")

    ending = ResetEvent("window.ending_soon", "seven_day", r, T, 61.0, ())
    copy = notification_copy_for_reset_event(ending, T - (3 * 3600 + 5 * 60))
    assert (copy.title, copy.body) == ("Work: weekly ends in 3h 5m", "39% left")

    for now in (T - 59, T - 0.5, T + 1):
        copy = notification_copy_for_reset_event(ending, now)
        assert copy.title == "Work: weekly ends in under a minute"

    reset = ResetEvent("window.reset", "five_hour", r, T, 0.0, ())
    copy = notification_copy_for_reset_event(reset, T + 10)
    assert (copy.title, copy.body) == ("Work: 5-hour reset", "Full quota available")


FIVE_HOUR_BOUNDARY = "2026-10-30T02:20:00"
SEVEN_DAY_BOUNDARY = "2026-11-04T04:00:00"


def _api_reading(fraction, window="five_hour", boundary=FIVE_HOUR_BOUNDARY):
    usage = {window: {"pct": 40.0, "resets_at": f"{boundary}.{fraction}+00:00"}}
    return AccountReading(
        provider="claude", email="a@example.com", org_uuid="org-1", label="Work", org_name="Acme",
        is_active=True, live=True, usage=usage, fetched_at=None,
    )


@pytest.mark.parametrize("window, boundary", [
    ("five_hour", FIVE_HOUR_BOUNDARY), ("seven_day", SEVEN_DAY_BOUNDARY),
])
def test_resets_at_microsecond_jitter_gives_no_started_and_one_ending_soon(window, boundary):
    whole = datetime.fromisoformat(f"{boundary}+00:00").timestamp()
    dests = [hook("one"), hook("two")]
    state = empty_state()
    out = []

    for i, fraction in enumerate(("472302", "512000", "600000", "300000", "900000")):
        out += detect([_api_reading(fraction, window, boundary)], dests, state,
                      now=whole - 1200 + i * 60)

    assert sorted((d.id, e.type) for d, e in out) == [
        ("one", "window.ending_soon"), ("two", "window.ending_soon")]
    assert state.windows == {f"{KEY}|{window}": whole}
    assert out[0][1].resets_at_iso == f"{boundary}Z"


def test_event_id_identical_for_fractions_of_the_same_boundary():
    whole = datetime.fromisoformat(f"{FIVE_HOUR_BOUNDARY}+00:00").timestamp()

    [(_, a)] = detect([_api_reading("472302")], [hook()], empty_state(), now=whole - 600)
    [(_, b)] = detect([_api_reading("900000")], [hook()], empty_state(), now=whole - 600)

    assert a.id == b.id


def test_scoped_resets_at_truncated_to_whole_seconds():
    scoped = [{"name": "Fable", "pct": 10.0, "resets_at": f"{SEVEN_DAY_BOUNDARY}.472466+00:00"}]
    state = FiredState(windows={f"{KEY}|five_hour": T}, sent={})

    [(_, event)] = detect([reading(scoped=scoped)], [hook()], state, now=T + 1)

    assert event.scoped[0]["resets_at"] == f"{SEVEN_DAY_BOUNDARY}Z"


@pytest.mark.parametrize("usage", [
    {"five_hour": {"pct": 0}, "seven_day": {"pct": 0}},
    {},
])
def test_reset_fires_when_reading_has_no_resets_at(usage):
    state = FiredState(windows={f"{KEY}|five_hour": T, f"{KEY}|seven_day": T}, sent={})
    idle = replace(reading(), usage=usage)

    out = detect([idle], [hook()], state, now=T + 1)

    assert sorted((e.type, e.window, e.resets_at) for _, e in out) == [
        ("window.reset", "five_hour", T), ("window.reset", "seven_day", T)]
    assert state.windows[f"{KEY}|seven_day"] == T + WEEK


def test_five_hour_anchor_moved_earlier_corrects_state_without_started():
    k = f"{KEY}|five_hour"
    state = FiredState(windows={k: T + 5 * 3600}, sent={})

    out = detect([reading(window_ts=T + 4 * 3600)], [hook(events=("window.started",))],
                 state, now=T)

    assert out == []
    assert state.windows[k] == T + 4 * 3600


@pytest.mark.parametrize("filter_text", [
    '{"leadS": {"five_hour": Infinity}}',
    '{"leadS": {"seven_day": NaN}}',
    '{"leadS": {"five_hour": 2592001}}',
    '{"minLeftPct": NaN}',
    '{"minLeftPct": Infinity}',
    '{"minLeftPct": 1e308}',
    '{"minLeftPct": -1}',
    '{"minLeftPct": 100.5}',
])
def test_non_finite_or_out_of_range_filter_numbers_drop_the_destination(tmp_path, filter_text):
    (tmp_path / "reset_events.json").write_text(
        '{"destinations": [{"id": "mac", "kind": "mac", "enabled": true},'
        ' {"id": "h", "kind": "webhook", "enabled": true, "url": "https://example.com/h",'
        f' "filter": {filter_text}}}]}}'
    )

    dests, problem = load_destinations_checked(tmp_path)

    assert [d.id for d in dests] == ["mac"]
    assert problem is not None


def test_filter_numbers_at_the_limits_are_kept(tmp_path):
    _write_config(tmp_path, [{"id": "mac", "kind": "mac", "enabled": True,
                              "filter": {"leadS": {"five_hour": 2592000}, "minLeftPct": 100}}])

    assert load_destinations_checked(tmp_path) == (
        [Destination(id="mac", kind="mac", enabled=True,
                     filter=Filter(lead_s={"five_hour": 2592000}, min_left_pct=100.0))],
        None,
    )


def test_load_destinations_checked_reports_problem_with_path(tmp_path):
    assert load_destinations_checked(tmp_path) == (DEFAULT, None)

    path = tmp_path / "reset_events.json"
    for text in ("{not json", "[]", '{"destinations": "nope"}',
                 '{"destinations": [{"id": "x", "kind": "pager", "enabled": true}]}'):
        path.write_text(text)
        dests, problem = load_destinations_checked(tmp_path)
        assert dests == DEFAULT
        assert str(path) in problem and "could not be read in full" in problem


def test_load_state_checked_reports_corrupt_state(tmp_path):
    assert load_state_checked(tmp_path) == (FiredState(windows={}, sent={}), None)

    path = tmp_path / "reset_events_state.json"
    path.write_text("{oops")
    state, problem = load_state_checked(tmp_path)

    assert state == FiredState(windows={}, sent={})
    assert str(path) in problem


HUGE_INT = "9" * 400


@pytest.mark.parametrize("filter_text", [
    f'{{"leadS": {{"five_hour": {HUGE_INT}}}}}',
    f'{{"minLeftPct": {HUGE_INT}}}',
], ids=["leadS", "minLeftPct"])
def test_huge_integer_filter_numbers_drop_the_destination(tmp_path, filter_text):
    (tmp_path / "reset_events.json").write_text(
        '{"destinations": [{"id": "mac", "kind": "mac", "enabled": true},'
        ' {"id": "h", "kind": "webhook", "enabled": true, "url": "https://example.com/h",'
        f' "filter": {filter_text}}}]}}'
    )

    dests, problem = load_destinations_checked(tmp_path)

    assert [d.id for d in dests] == ["mac"]
    assert problem is not None


def test_integer_literal_over_the_json_digit_limit_is_reported_unreadable(tmp_path):
    (tmp_path / "reset_events.json").write_text(f'{{"destinations": {"9" * 5000}}}')
    (tmp_path / "reset_events_state.json").write_text(f'{{"windows": {"9" * 5000}}}')

    dests, problem = load_destinations_checked(tmp_path)
    state, state_problem = load_state_checked(tmp_path)

    assert dests == DEFAULT and "could not be read in full" in problem
    assert state == FiredState(windows={}, sent={}) and state_problem is not None


def test_state_skips_numbers_that_overflow_or_are_not_finite(tmp_path):
    (tmp_path / "reset_events_state.json").write_text(
        f'{{"windows": {{"a": {HUGE_INT}, "b": NaN, "c": Infinity, "d": 5}}, "sent": {{}}}}'
    )

    state, problem = load_state_checked(tmp_path)

    assert state.windows == {"d": 5.0}
    assert problem is None
