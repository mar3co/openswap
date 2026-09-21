"""Widget snapshot JSON the macOS WidgetKit extension reads."""

from __future__ import annotations

import json
from pathlib import Path

from openswap import widget_snapshot as ws
from openswap.menubar import SENTINEL_NOTES, panel_windows
from openswap.switcher import USAGE_RELOGIN_REQUIRED

_NOW = 1_000_000.0
_USAGE = {
    "five_hour": {"pct": 42.0, "resets_at": "2001-09-09T02:46:40+00:00"},
    "seven_day": {"pct": 18.0},
}


def _snap():
    return {
        "accounts": [
            (1, "a@x.com", True, _USAGE, _USAGE, "personal", "", False, None),
            (2, "b@x.com", False, "no credentials", None, "", "", True, None),
        ]
    }


def test_build_payload_stringifies_num_and_keeps_windows():
    payload = ws.build_widget_payload(_snap(), now=_NOW)
    assert payload["schema"] == ws.SCHEMA_VERSION
    assert payload["updated_at"] == _NOW
    assert payload["accounts"][0]["num"] == "1"
    assert payload["accounts"][0]["title"] == "personal"
    assert payload["accounts"][0]["active"] is True
    labels = [w["label"] for w in payload["accounts"][0]["windows"]]
    assert labels == ["5h", "7d"]
    assert payload["accounts"][1]["title"] == "personal"
    assert payload["accounts"][1]["subtitle"] == "b@x.com"
    assert payload["accounts"][1]["note"] == "no credentials"
    assert payload["accounts"][1]["windows"] == []
    # Disabled slot 2 is not in the pool; slot 1 is 42% / 18%.
    assert payload["combined"]["five_hour"]["remaining"] == 0.58
    assert payload["combined"]["five_hour"]["total"] == 1
    assert payload["combined"]["five_hour"]["switch_num"] == "1"
    assert payload["combined"]["seven_day"]["remaining"] == 0.82


def test_build_payload_updated_at_is_measurement_not_paint():
    fetched = _NOW - 3600
    snap = {
        "accounts": [
            (1, "a@x.com", True, _USAGE, _USAGE, "personal", "", False, fetched),
        ]
    }
    payload = ws.build_widget_payload(snap, now=_NOW)
    assert payload["updated_at"] == fetched
    assert payload["accounts"][0]["fetched_at"] == fetched


def test_build_payload_updated_at_signed_out_uses_last_good_fetch():
    fetched = _NOW - 7200
    snap = {
        "accounts": [
            (
                1,
                "a@x.com",
                False,
                SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED],
                _USAGE,
                "personal",
                "",
                False,
                fetched,
            ),
        ]
    }
    payload = ws.build_widget_payload(snap, now=_NOW)
    assert payload["updated_at"] == fetched
    for window in payload["accounts"][0]["windows"]:
        assert window["countdown"] is None
        assert window["resets_at_ts"] is None


def test_snapshot_updated_at_prefers_live_over_signed_out():
    accounts = [
        _card(1, "personal", 20, fetched_at=_NOW - 60),
        _card(2, "dead", 10, needs_relogin=True, fetched_at=_NOW - 7200),
    ]
    assert ws.snapshot_updated_at(accounts, _NOW) == _NOW - 60


def test_panel_windows_exposes_resets_at_ts_for_the_widget():
    rows = panel_windows(_USAGE, now=_NOW)
    assert rows[0]["resets_at_ts"] is not None
    assert rows[1]["resets_at_ts"] is None


def test_write_widget_snapshot_atomic(tmp_path: Path):
    dest = tmp_path / "Library" / "Application Support" / "OpenSwap" / "widget-snapshot.json"
    written = ws.write_widget_snapshot(_snap(), now=_NOW, dest=dest)
    assert written == dest
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["accounts"][0]["title"] == "personal"
    assert data["schema"] == 1
    leftovers = list(dest.parent.glob("widget-snapshot.*"))
    assert leftovers == [dest]


def test_default_snapshot_path_under_application_support(tmp_path: Path):
    assert ws.default_snapshot_path(tmp_path) == (
        tmp_path / "Library" / "Application Support" / "OpenSwap" / "widget-snapshot.json"
    )


def test_publish_returns_none_on_write_failure(tmp_path: Path, monkeypatch):
    dest = tmp_path / "widget-snapshot.json"

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(ws, "write_widget_snapshot", _boom)
    assert ws.publish_widget_snapshot(_snap(), dest=dest) is None


def test_notify_and_wake_are_noop_off_darwin(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(ws.sys, "platform", "linux")
    ws.notify_widget_reload()  # must not raise
    ws.wake_widget_host(tmp_path)  # must not raise


def test_default_command_path_under_application_support(tmp_path: Path):
    assert ws.default_command_path(tmp_path) == (
        tmp_path / "Library" / "Application Support" / "OpenSwap" / "widget-command.json"
    )


def test_cleanup_legacy_widget_support_removes_only_known_ipc_files(tmp_path: Path):
    legacy = tmp_path / "Library" / "Application Support" / "cswap"
    legacy.mkdir(parents=True)
    (legacy / ws.SNAPSHOT_FILENAME).write_text("{}", encoding="utf-8")
    (legacy / ws.COMMAND_FILENAME).write_text("{}", encoding="utf-8")
    unrelated = legacy / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    assert ws.cleanup_legacy_widget_support(tmp_path) is True
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not (legacy / ws.SNAPSHOT_FILENAME).exists()
    assert not (legacy / ws.COMMAND_FILENAME).exists()


def test_cleanup_legacy_widget_support_removes_empty_directory(tmp_path: Path):
    legacy = tmp_path / "Library" / "Application Support" / "cswap"
    legacy.mkdir(parents=True)
    (legacy / ws.SNAPSHOT_FILENAME).write_text("{}", encoding="utf-8")

    assert ws.cleanup_legacy_widget_support(tmp_path) is True
    assert not legacy.exists()


def test_parse_switch_command_happy_path():
    assert ws.parse_switch_command({"op": "switch", "num": "2"}) == "2"
    assert ws.parse_switch_command({"op": "switch", "num": 3}) == "3"


def test_parse_switch_command_missing_num():
    assert ws.parse_switch_command({"op": "switch"}) is None
    assert ws.parse_switch_command({"op": "switch", "num": ""}) is None
    assert ws.parse_switch_command({"op": "switch", "num": None}) is None


def test_parse_switch_command_op_not_switch():
    assert ws.parse_switch_command({"op": "reload", "num": "1"}) is None
    assert ws.parse_switch_command({"num": "1"}) is None
    assert ws.parse_switch_command("switch") is None


def test_consume_switch_command_happy_path(tmp_path: Path):
    path = tmp_path / "widget-command.json"
    path.write_text('{"op":"switch","num":"1","at":1.0}', encoding="utf-8")
    assert ws.consume_switch_command(path) == "1"
    assert not path.exists()


def test_consume_switch_command_missing_file(tmp_path: Path):
    path = tmp_path / "no-such-command.json"
    assert ws.consume_switch_command(path) is None
    assert not path.exists()


def test_consume_switch_command_bad_json(tmp_path: Path):
    path = tmp_path / "widget-command.json"
    path.write_text("{not json", encoding="utf-8")
    assert ws.consume_switch_command(path) is None
    assert not path.exists()


def test_consume_switch_command_missing_num(tmp_path: Path):
    path = tmp_path / "widget-command.json"
    path.write_text('{"op":"switch"}', encoding="utf-8")
    assert ws.consume_switch_command(path) is None
    assert not path.exists()


def test_consume_switch_command_op_not_switch(tmp_path: Path):
    path = tmp_path / "widget-command.json"
    path.write_text('{"op":"reload","num":"1"}', encoding="utf-8")
    assert ws.consume_switch_command(path) is None
    assert not path.exists()


def _card(num, title, pct_5h, pct_7d=None, **kwargs):
    windows = [
        {
            "label": "5h",
            "pct": pct_5h,
            "countdown": kwargs.get("countdown_5h"),
            "resets_at_ts": kwargs.get("ts_5h"),
            "ahead": False,
            "maxed": False,
        }
    ]
    if pct_7d is not None:
        windows.append(
            {
                "label": "7d",
                "pct": pct_7d,
                "countdown": kwargs.get("countdown_7d"),
                "resets_at_ts": kwargs.get("ts_7d"),
                "ahead": False,
                "maxed": False,
            }
        )
    card = {
        "num": num,
        "title": title,
        "subtitle": "",
        "active": kwargs.get("active", False),
        "disabled": kwargs.get("disabled", False),
        "note": kwargs.get("note"),
        "needs_relogin": kwargs.get("needs_relogin", False),
        "windows": windows,
    }
    if "fetched_at" in kwargs:
        card["fetched_at"] = kwargs["fetched_at"]
    return card


def test_remaining_fraction_clamps():
    assert ws.remaining_fraction(20) == 0.8
    assert ws.remaining_fraction(0) == 1.0
    assert ws.remaining_fraction(100) == 0.0
    assert ws.remaining_fraction(150) == 0.0
    assert ws.remaining_fraction(-10) == 1.0


def test_combined_window_does_not_average_percentages():
    accounts = [
        _card(1, "personal", 20, 10, ts_5h=_NOW + 7_800),
        _card(2, "Ads Online", 80, 40, ts_5h=_NOW + 14_400),
    ]
    five = ws.combined_window(accounts, "5h", _NOW)
    assert five is not None
    assert five["remaining"] == 1.0
    assert five["total"] == 2
    assert five["switch_num"] == "1"
    assert five["hottest_num"] == "2"
    assert five["hottest_title"] == "Ads Online"
    assert five["next_num"] == "1"
    assert five["next_resets_at_ts"] == _NOW + 7_800
    seven = ws.combined_window(accounts, "7d", _NOW)
    assert seven is not None
    assert seven["remaining"] == 1.5
    assert seven["switch_num"] == "1"
    assert seven["hottest_num"] == "2"


def test_combined_window_skips_disabled_and_relogin():
    accounts = [
        _card(1, "personal", 20, ts_5h=_NOW + 100),
        _card(2, "off", 0, disabled=True, ts_5h=_NOW + 100),
        _card(3, "dead", 0, needs_relogin=True, ts_5h=_NOW + 100),
    ]
    five = ws.combined_window(accounts, "5h", _NOW)
    assert five is not None
    assert five["total"] == 1
    assert five["remaining"] == 0.8
    assert five["switch_num"] == "1"
    assert [s["num"] for s in five["slices"]] == ["1"]


def test_combined_window_empty_when_no_live_slots():
    accounts = [
        _card(1, "off", 10, disabled=True),
        _card(2, "dead", 10, needs_relogin=True),
        {"num": "3", "title": "empty", "disabled": False, "needs_relogin": False, "windows": []},
    ]
    assert ws.combined_window(accounts, "5h", _NOW) is None
    assert ws.build_combined(accounts, _NOW) == {}


def test_combined_window_excludes_stale_or_action_required_capacity():
    accounts = [
        _card(1, "live", 20),
        {**_card(2, "stale", 0), "stale": True},
        {**_card(3, "repair", 0), "action_required": True},
    ]

    combined = ws.combined_window(accounts, "5h", _NOW)

    assert combined is not None
    assert combined["total"] == 1
    assert combined["switch_num"] == "1"


def test_combined_window_ignores_passed_resets():
    accounts = [_card(1, "personal", 50, ts_5h=_NOW - 60)]
    five = ws.combined_window(accounts, "5h", _NOW)
    assert five is not None
    assert five["next_num"] is None
    assert five["next_resets_at_ts"] is None


def test_build_combined_two_healthy_accounts():
    usage_a = {
        "five_hour": {"pct": 20.0, "resets_at": "2001-09-09T03:10:00+00:00"},
        "seven_day": {"pct": 10.0},
    }
    usage_b = {
        "five_hour": {"pct": 80.0, "resets_at": "2001-09-09T05:00:00+00:00"},
        "seven_day": {"pct": 40.0},
    }
    snap = {
        "accounts": [
            (1, "a@x.com", True, usage_a, usage_a, "personal", "", False, None),
            (2, "b@x.com", False, usage_b, usage_b, "Ads Online", "Ads Online", False, None),
        ]
    }
    payload = ws.build_widget_payload(snap, now=_NOW)
    five = payload["combined"]["five_hour"]
    assert five["remaining"] == 1.0
    assert five["total"] == 2
    assert five["switch_num"] == "1"
    assert five["hottest_title"] == "Ads Online"


def test_combined_excludes_codex_cards():
    snap_with_one_claude_and_one_codex = {
        "accounts": [
            (1, "a@x.com", True, _USAGE, _USAGE, "personal", "", False, None),
            ("codex:1", "c@x.com", True, _USAGE, _USAGE, "", "plus", False, None),
        ]
    }
    payload = ws.build_widget_payload(snap_with_one_claude_and_one_codex, now=_NOW)
    assert payload["combined"]["five_hour"]["total"] == 1
    assert [c["num"] for c in payload["accounts"]] == ["1", "codex:1"]
