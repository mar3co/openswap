"""Tests for the menu bar module.

These tests never import or run rumps/AppKit. They exercise the pure helpers
(settings store, title/label formatting, usage/snapshot adapters, log parsing)
only — the auto-switch engine itself lives in ``openswap.autoswitch`` and is
tested there.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import plistlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar
from openswap.autoswitch import (
    AllExhaustedEvent,
    ConfigWarningEvent,
    NoSwitchEvent,
    PollEvent,
    QuarantineEvent,
    SleepEvent,
    SwitchEvent,
)
from openswap.exceptions import ClaudeSwitchError
from openswap.switcher import (
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_RELOGIN_REQUIRED,
)


# --- notification identity -----------------------------------------------------

def test_notification_identity_creates_and_preserves_info_plist(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    info = executable.parent / "Info.plist"
    info.write_bytes(plistlib.dumps({"ExistingKey": "kept"}))

    result = menubar.ensure_notification_identity(executable, platform="darwin")

    assert result == info
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundleIdentifier"] == "com.opensoft.openswap.menubar"
    assert data["CFBundleName"] == "openswap"
    assert data["ExistingKey"] == "kept"


def test_notification_identity_heals_corrupt_info_plist(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    info = executable.parent / "Info.plist"
    # truncated XML plist: plistlib raises ExpatError, not InvalidFileException
    info.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b'<plist version="1.0"><dict><key>CFBundle'
    )

    result = menubar.ensure_notification_identity(executable, platform="darwin")

    assert result == info
    data = plistlib.loads(info.read_bytes())
    assert data["CFBundleIdentifier"] == "com.opensoft.openswap.menubar"
    assert data["CFBundleName"] == "openswap"
    assert not (executable.parent / "Info.plist.tmp").exists()


def test_notification_identity_is_noop_off_macos(tmp_path: Path):
    executable = tmp_path / "bin" / "python3"
    assert menubar.ensure_notification_identity(
        executable, platform="linux"
    ) is None
    assert not (executable.parent / "Info.plist").exists()


def test_notification_identity_is_noop_when_frozen(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    executable = tmp_path / "OpenSwap"
    result = menubar.ensure_notification_identity(executable, platform="darwin")
    assert result is None
    assert not (tmp_path / "Info.plist").exists()


# --- settings ------------------------------------------------------------------

def test_settings_defaults_when_file_missing(tmp_path: Path):
    s = menubar.MenuBarSettings.load(tmp_path / "nope.json")
    assert s.show_account_name is True
    assert s.menu_bar_provider == "claude"
    assert s.title_pct == "both"
    assert s.refresh_interval == 60
    assert s.auto_switch_enabled is False
    assert s.chatgpt_auto_enabled is False
    assert s.kickoff_enabled is False


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(
        show_account_name=False,
        title_pct="5h",
        refresh_interval=300,
        auto_switch_enabled=True,
        chatgpt_auto_enabled=True,
        menu_bar_provider="both",
    )
    original.save(path)
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded == original


def test_settings_corrupt_file_falls_back_to_defaults(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    s = menubar.MenuBarSettings.load(path)
    assert s == menubar.MenuBarSettings()


def test_settings_ignores_unknown_and_bad_types(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(
        json.dumps(
            {
                "refresh_interval": "fast",
                "bogus": 1,
                "show_account_name": False,
                "kickoff_hour": True,
            }
        ),
        encoding="utf-8",
    )
    s = menubar.MenuBarSettings.load(path)
    # bad-typed refresh_interval falls back to default; valid bool is kept
    assert s.refresh_interval == 60
    assert s.show_account_name is False
    # bool is a subclass of int: JSON true must not become kickoff_hour=1
    assert s.kickoff_hour == 7


def test_settings_invalid_title_pct_falls_back_to_default(tmp_path: Path):
    path = menubar.menubar_settings_path(tmp_path)
    path.write_text(json.dumps({"title_pct": "nope", "confirm_switch": False}), encoding="utf-8")
    s = menubar.MenuBarSettings.load(path)
    assert s.title_pct == "both"
    assert s.confirm_switch is False


@pytest.mark.parametrize("provider", ["claude", "chatgpt", "both", "logo", "unknown", True])
def test_menu_bar_provider_settings_validation(tmp_path: Path, provider):
    path = tmp_path / "menubar_settings.json"
    path.write_text(json.dumps({"menu_bar_provider": provider}), encoding="utf-8")
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded.menu_bar_provider == (
        provider if provider in ("claude", "chatgpt", "both", "logo") else "claude"
    )


@pytest.mark.parametrize("provider", ["claude", "chatgpt", "both", "logo"])
def test_menu_bar_display_controls_follow_provider(provider):
    settings = menubar.MenuBarSettings(menu_bar_provider=provider, title_scoped=True)
    rows = menubar.settings_page_rows(
        settings, strategy="best", threshold=90, section="general"
    )
    by_id = {row["id"]: row for row in rows}
    picker = by_id["menu_bar_provider"]
    assert picker["label"] == "Show"
    assert picker["kind"] == "popup"
    assert picker["value"] == provider
    assert picker["options"] == [
        ("claude", "Claude"), ("chatgpt", "ChatGPT"),
        ("both", "Both"), ("logo", "Logo only"),
    ]
    assert ("title_scoped" in by_id) == (provider in ("claude", "both"))
    for rid, label in [
        ("show_account_name", "Account name"),
        ("title_pct_5h", "5-hour usage"),
        ("title_pct_7d", "7-day usage"),
    ]:
        assert (rid in by_id) == (provider != "logo")
        if rid in by_id:
            assert by_id[rid]["label"] == label
    hints = " ".join(row["label"] for row in rows if row.get("style") == "hint")
    if provider in ("chatgpt", "both"):
        assert "Codex" in hints and "message limits" in hints and "unverified" in hints
    # Hiding a control must not erase the user's choice.
    assert settings.title_scoped is True


def test_menubar_settings_path_constant(tmp_path: Path):
    assert menubar.MENUBAR_SETTINGS_FILENAME == "menubar_settings.json"
    assert menubar.menubar_settings_path(tmp_path) == tmp_path / "menubar_settings.json"


def test_menubar_run_uses_settings_path_helper():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    run = text[text.index("def run") : text.index("class MenuBarApp")]
    assert "menubar_settings_path" in run
    assert '"menubar_settings.json"' not in run


def test_settings_save_writes_through_symlink(tmp_path: Path):
    repo = tmp_path / "repo"
    live = tmp_path / "live"
    repo.mkdir()
    live.mkdir()
    tracked = repo / "menubar_settings.json"
    tracked.write_text(json.dumps({"show_account_name": True}), encoding="utf-8")
    link = live / "menubar_settings.json"
    link.symlink_to(tracked)

    menubar.MenuBarSettings(show_account_name=False, title_pct="5h").save(link)

    assert link.is_symlink()
    loaded = menubar.MenuBarSettings.load(tracked)
    assert loaded.show_account_name is False
    assert loaded.title_pct == "5h"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_settings_save_hardens_mode(tmp_path: Path):
    path = menubar.menubar_settings_path(tmp_path)
    menubar.MenuBarSettings(show_account_name=False).save(path)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (tmp_path.stat().st_mode & 0o777) == 0o700


def test_auto_strategy_choices_match_core_settings():
    from openswap.settings import SETTING_SPECS

    spec = SETTING_SPECS["autoswitch.strategy"]
    values = tuple(value for value, _label in menubar.AUTO_STRATEGY_CHOICES)
    assert values == spec.choices
    labels = {value: label for value, label in menubar.AUTO_STRATEGY_CHOICES}
    assert "weekly" in labels["consume-first"].lower()
    assert "5-hour" in labels["soonest-5h"].lower()
    assert "quota" in labels["best"].lower()


def test_settings_page_constants():
    assert menubar.SETTINGS_PAGE == "settings"
    assert menubar.MAIN_PAGE == "main"
    assert menubar.SETTINGS_SECTIONS == (
        ("general", "General"),
        ("automation", "Automation"),
    )


def test_settings_page_sections_separate_display_from_provider_automation():
    settings = menubar.MenuBarSettings(
        auto_switch_enabled=True,
        chatgpt_auto_enabled=False,
    )
    general = menubar.settings_page_rows(
        settings,
        strategy="best",
        threshold=90,
        has_codex=True,
        section=menubar.SETTINGS_SECTION_GENERAL,
    )
    automation = menubar.settings_page_rows(
        settings,
        strategy="best",
        threshold=90,
        has_codex=True,
        section=menubar.SETTINGS_SECTION_AUTOMATION,
    )

    general_ids = {row["id"] for row in general}
    automation_by_id = {row["id"]: row for row in automation}
    assert "show_account_name" in general_ids
    assert "auto_switch_enabled" not in general_ids
    assert automation_by_id["auto_switch_enabled"]["label"] == (
        "Auto-switch Claude accounts"
    )
    assert automation_by_id["chatgpt_auto_enabled"]["label"] == (
        "Suggest ChatGPT account switches"
    )
    assert automation_by_id["codex_enabled"]["label"] == (
        "Auto-switch Codex CLI accounts"
    )
    assert automation_by_id["group_policy"]["label"] == "Shared rotation policy"
    assert all(row["section"] == menubar.SETTINGS_SECTION_GENERAL for row in general)
    assert all(
        row["section"] == menubar.SETTINGS_SECTION_AUTOMATION
        for row in automation
    )


def test_settings_page_rows_include_required_ids_and_values():
    from openswap.kickoff import kickoff_time_options, kickoff_time_value

    s = menubar.MenuBarSettings(
        show_account_name=False,
        title_pct="5h",
        title_scoped=True,
        refresh_interval=30,
        auto_switch_enabled=True,
        kickoff_enabled=True,
        kickoff_hour=19,
        kickoff_minute=30,
    )
    rows = menubar.settings_page_rows(s, strategy="soonest-5h", threshold=95)
    by_id = {row["id"]: row for row in rows}
    required = (
        "show_account_name",
        "title_pct_5h",
        "title_pct_7d",
        "title_scoped",
        "refresh_interval",
        "auto_switch_enabled",
        "threshold",
        "strategy",
        "strategy_hint",
        "kickoff_enabled",
        "kickoff_time",
    )
    for rid in required:
        assert rid in by_id

    assert by_id["show_account_name"]["kind"] == "toggle"
    assert by_id["show_account_name"]["value"] is False
    assert by_id["title_scoped"]["kind"] == "toggle"
    assert by_id["title_scoped"]["value"] is True
    assert by_id["auto_switch_enabled"]["value"] is True
    assert by_id["kickoff_enabled"]["value"] is True
    assert "show_icon" not in by_id
    assert "group_advanced" not in by_id

    assert by_id["title_pct_5h"]["kind"] == "toggle"
    assert by_id["title_pct_5h"]["value"] is True
    assert by_id["title_pct_7d"]["kind"] == "toggle"
    assert by_id["title_pct_7d"]["value"] is False
    assert by_id["refresh_interval"]["kind"] == "choice"
    assert by_id["refresh_interval"]["value"] == 30
    assert by_id["refresh_interval"]["options"] == [
        (secs, menubar.REFRESH_LABELS[secs]) for secs in menubar.REFRESH_CHOICES
    ]
    assert by_id["threshold"]["kind"] == "choice"
    assert by_id["threshold"]["value"] == 95
    assert by_id["threshold"]["options"] == [
        (pct, f"{pct}%") for pct in menubar.AUTO_THRESHOLD_CHOICES
    ]
    assert by_id["strategy"]["kind"] == "choice"
    assert by_id["strategy"]["value"] == "soonest-5h"
    assert by_id["strategy"]["options"] == list(menubar.AUTO_STRATEGY_CHOICES)
    assert by_id["strategy_hint"]["kind"] == "group"
    assert "5-hour" in by_id["strategy_hint"]["label"]
    assert by_id["kickoff_time"]["kind"] == "popup"
    assert by_id["kickoff_time"]["label"] == "Time"
    assert by_id["kickoff_time"]["value"] == kickoff_time_value(19, 30)
    assert by_id["kickoff_time"]["options"] == kickoff_time_options(19, 30)
    assert "action_id" not in by_id["kickoff_time"]


def test_settings_page_hides_autoswitch_policy_when_disabled():
    off = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=False),
        strategy="best",
        threshold=90,
    )
    ids_off = [row["id"] for row in off]
    assert "auto_switch_enabled" in ids_off
    assert "threshold" not in ids_off
    assert "strategy" not in ids_off

    on = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=True),
        strategy="best",
        threshold=90,
    )
    ids_on = [row["id"] for row in on]
    assert ids_on.index("auto_switch_enabled") < ids_on.index("threshold")
    assert ids_on.index("threshold") < ids_on.index("strategy")
    assert ids_on.index("strategy") < ids_on.index("strategy_hint")
    assert ids_on.index("strategy_hint") < ids_on.index("kickoff_enabled")
    assert "strategy_hint" not in ids_off
    assert "codex_enabled" not in ids_off
    assert "codex_enabled" not in ids_on


def test_chatgpt_auto_is_persisted_independently_and_exposes_policy():
    settings = menubar.MenuBarSettings(
        auto_switch_enabled=False, chatgpt_auto_enabled=True
    )
    rows = menubar.settings_page_rows(
        settings, strategy="best", threshold=90, has_codex=True,
        codex_enabled=False,
    )
    ids = [row["id"] for row in rows]
    assert settings.auto_switch_enabled is False
    assert settings.chatgpt_auto_enabled is True
    assert "threshold" in ids
    assert "strategy" in ids
    assert "codex_enabled" not in ids


def test_settings_page_shows_codex_enabled_when_auto_on_and_has_codex():
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=True),
        strategy="best",
        threshold=90,
        has_codex=True,
        codex_enabled=True,
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id["codex_enabled"]["kind"] == "toggle"
    assert by_id["codex_enabled"]["label"] == "Auto-switch Codex CLI accounts"
    assert by_id["codex_enabled"]["value"] is True
    ids = [row["id"] for row in rows]
    assert ids.index("auto_switch_enabled") < ids.index("codex_enabled")
    assert ids.index("strategy_hint") < ids.index("codex_enabled")
    assert ids.index("codex_enabled") < ids.index("kickoff_enabled")

    off_value = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=True),
        strategy="best",
        threshold=90,
        has_codex=True,
        codex_enabled=False,
    )
    assert {row["id"]: row for row in off_value}["codex_enabled"]["value"] is False


def test_settings_page_hides_codex_enabled_when_auto_off():
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=False),
        strategy="best",
        threshold=90,
        has_codex=True,
        codex_enabled=True,
    )
    ids = [row["id"] for row in rows]
    assert "auto_switch_enabled" in ids
    assert "codex_enabled" not in ids


def test_settings_page_hides_codex_enabled_without_codex():
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=True),
        strategy="best",
        threshold=90,
    )
    assert "codex_enabled" not in [row["id"] for row in rows]


def test_on_setting_handles_codex_enabled():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    body = text[text.index("def _on_setting") : text.index("def _popup_overflow")]
    assert 'row_id == "codex_enabled"' in body
    assert "autoswitch.codexEnabled" in body
    assert "set_setting" in body
    assert "_codex_engine" in body
    assert "_ensure_codex_engine" in body
    assert "_stop_engine" not in body


def test_ensure_codex_engine_honors_codex_enabled():
    import inspect
    src = inspect.getsource(menubar.run)
    body = src[src.index("def _ensure_codex_engine") : src.index("def _start_engine")]
    assert "codex_enabled" in body
    assert "load_settings" in body


def test_panel_settings_pass_has_codex_and_codex_enabled():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    attach = text[text.index("def _attach_panel_once") : text.index("def _on_setting")]
    assert "has_codex" in attach
    assert "codex_enabled" in attach
    panel = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    build = panel[panel.index("def _build_settings") :]
    assert "has_codex" in build
    assert "codex_enabled" in build
    assert "settings_page_rows" in build


def test_settings_page_hides_kickoff_time_when_disabled():
    off = menubar.settings_page_rows(
        menubar.MenuBarSettings(kickoff_enabled=False),
        strategy="best",
        threshold=90,
    )
    ids_off = [row["id"] for row in off]
    assert "kickoff_enabled" in ids_off
    assert "kickoff_hint" in ids_off
    assert "kickoff_time" not in ids_off
    off_by_id = {row["id"]: row for row in off}
    assert off_by_id["group_schedule"]["label"] == "Window kickoff"
    assert off_by_id["kickoff_enabled"]["label"] == (
        "Start available 5-hour windows"
    )
    assert "report a 5-hour limit" in off_by_id["kickoff_hint"]["label"]

    on = menubar.settings_page_rows(
        menubar.MenuBarSettings(kickoff_enabled=True),
        strategy="best",
        threshold=90,
    )
    ids_on = [row["id"] for row in on]
    assert ids_on.index("kickoff_enabled") < ids_on.index("kickoff_time")
    assert ids_on.index("group_schedule") < ids_on.index("kickoff_enabled")


@pytest.mark.parametrize("title_pct", ["off", "both"])
def test_settings_page_keeps_scoped_title_regardless_of_pct(title_pct):
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(title_pct=title_pct, title_scoped=True),
        strategy="best",
        threshold=90,
    )
    ids = [row["id"] for row in rows]
    assert ids.index("title_pct_5h") < ids.index("title_scoped")
    assert ids.index("title_pct_7d") < ids.index("title_scoped")
    assert ids.index("title_scoped") < ids.index("refresh_interval")


def test_combine_title_pct_round_trips_the_two_toggles():
    assert menubar.combine_title_pct(False, False) == "off"
    assert menubar.combine_title_pct(True, False) == "5h"
    assert menubar.combine_title_pct(False, True) == "7d"
    assert menubar.combine_title_pct(True, True) == "both"
    assert menubar.title_shows_5h("5h") and not menubar.title_shows_7d("5h")
    assert menubar.title_shows_7d("7d") and not menubar.title_shows_5h("7d")
    assert menubar.title_shows_5h("both") and menubar.title_shows_7d("both")
    assert not menubar.title_shows_5h("off") and not menubar.title_shows_7d("off")


_USAGE = {
    "five_hour": {"pct": 42.0},
    "seven_day": {"pct": 18.0},
    "spend": {"pct": 30.0, "used": 3.0, "limit": 10.0},
}


# --- usage display helpers -----------------------------------------------------

def test_tightest_pct_uses_max_window():
    assert menubar.tightest_pct(_USAGE) == 42.0


def test_tightest_pct_none_for_non_dict_or_empty():
    assert menubar.tightest_pct("no credentials") is None
    assert menubar.tightest_pct(None) is None
    assert menubar.tightest_pct({"spend": {"pct": 90.0}}) is None  # no 5h/7d


def test_usage_summary_dict():
    assert menubar.usage_summary(_USAGE) == "5h 42% · 7d 18% · $ 30%"


def test_usage_summary_partial_windows():
    assert menubar.usage_summary({"five_hour": {"pct": 5.0}}) == "5h 5%"


def test_usage_summary_includes_scoped_model_limits():
    # Per-model weekly limits (e.g. Fable) come through as usage["scoped"], after
    # 5h/7d and before spend.
    usage = {
        "five_hour": {"pct": 82.0},
        "seven_day": {"pct": 12.0},
        "scoped": [{"name": "Fable", "pct": 4.0}],
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage) == "5h 82% · 7d 12% · Fable 4% · $ 30%"


def test_usage_summary_scoped_over_limit_marker():
    usage = {"scoped": [{"name": "Fable", "pct": 100.0}]}
    assert menubar.usage_summary(usage) == "Fable 100% (!)"


def test_usage_summary_scoped_multiple_and_countdown():
    usage = {
        "scoped": [
            {"name": "Fable", "pct": 4.0, "resets_at": _iso(2 * 3600)},
            {"name": "Opus", "pct": 55.0},
        ],
    }
    assert menubar.usage_summary(usage, _NOW) == "Fable 4% (2h 0m) · Opus 55%"


def test_usage_summary_string_sentinel_passthrough():
    assert menubar.usage_summary("no credentials") == "no credentials"


def test_usage_summary_none():
    assert menubar.usage_summary(None) == "usage unavailable"


def test_usage_summary_seven_day_ahead_of_pace_marker():
    # 1 day elapsed of the week, 50% used -> far ahead of the ~14% expected.
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert out == "7d 50% (ahead) (6d 0h)"


def test_usage_summary_five_hour_never_shows_pace_marker():
    usage = {"five_hour": {"pct": 90.0, "resets_at": _iso(4 * 3600)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert "ahead" not in out


def test_usage_summary_scoped_ahead_of_pace_marker():
    usage = {"scoped": [{"name": "Fable", "pct": 50.0, "resets_at": _iso(6 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert out == "Fable 50% (ahead) (6d 0h)"


def test_usage_summary_maxed_scoped_marker_wins_over_pace():
    # At/over the limit shows "(!)" — the more urgent signal — not "(ahead)".
    usage = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(6 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW)
    assert "(!)" in out
    assert "ahead" not in out


def test_usage_summary_no_pace_marker_without_fetched_at():
    usage = {"seven_day": {"pct": 50.0, "resets_at": _iso(6 * 86400)}}
    out = menubar.usage_summary(usage, _NOW)
    assert "ahead" not in out


def test_usage_summary_no_pace_marker_on_window_rolled_to_zero():
    # A weekly window whose resets_at has already passed (stale cache, not
    # refetched since the actual reset) is rolled to a display pct of 0% —
    # pace must be computed against that rolled 0%, not the raw stale pct,
    # or the display would show "7d 0% (ahead)" (a marker paired with a
    # percentage it doesn't correspond to).
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-3 * 86400)}}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW - 4 * 86400)
    assert "ahead" not in out
    assert "7d 0%" in out


def test_usage_summary_scoped_no_pace_marker_on_window_rolled_to_zero():
    usage = {"scoped": [{"name": "Fable", "pct": 95.0, "resets_at": _iso(-3 * 86400)}]}
    out = menubar.usage_summary(usage, _NOW, fetched_at=_NOW - 4 * 86400)
    assert "ahead" not in out
    assert "Fable 0%" in out


def test_format_account_label():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE)
    assert label == "2  loc@papaya.asia  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_with_alias():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE, alias="dev")
    assert label == "2  dev  (loc@papaya.asia)  5h 42% · 7d 18% · $ 30%"


def test_format_account_label_disabled_marker():
    label = menubar.format_account_label(2, "loc@papaya.asia", _USAGE, disabled=True)
    assert label == "2  loc@papaya.asia  (disabled)  5h 42% · 7d 18% · $ 30%"


def test_resolve_popover_theme_follows_system_dark_mode():
    assert menubar.resolve_popover_theme(
        app_appearance_name="NSAppearanceNameAqua",
        interface_style="Dark",
    ) == "dark"
    assert menubar.resolve_popover_theme(
        app_appearance_name="NSAppearanceNameDarkAqua",
        interface_style=None,
    ) == "dark"
    assert menubar.resolve_popover_theme(
        app_appearance_name="NSAppearanceNameAccessibilityHighContrastDarkAqua",
        interface_style=None,
    ) == "dark"


def test_resolve_popover_theme_light_when_system_is_light():
    assert menubar.resolve_popover_theme(
        app_appearance_name="NSAppearanceNameAqua",
        interface_style=None,
    ) == "light"
    assert menubar.resolve_popover_theme(
        app_appearance_name=None,
        interface_style=None,
    ) == "light"


def test_panel_windows_from_usage():
    rows = menubar.panel_windows(_USAGE)
    assert [r["label"] for r in rows] == ["5h", "7d"]
    assert rows[0]["pct"] == 42.0
    assert rows[1]["pct"] == 18.0
    assert rows[0]["maxed"] is False


def test_panel_windows_includes_scoped_maxed():
    usage = {**_USAGE, "scoped": [{"name": "Fable", "pct": 100.0}]}
    rows = menubar.panel_windows(usage)
    assert rows[-1]["label"] == "Fable"
    assert rows[-1]["pct"] == 100.0
    assert rows[-1]["maxed"] is True


def test_panel_windows_sentinel_or_missing_is_empty():
    assert menubar.panel_windows("no credentials") == []
    assert menubar.panel_windows(None) == []


def test_panel_windows_omits_five_hour_when_plan_reports_weekly_only():
    rows = menubar.panel_windows({"seven_day": {"pct": 46.0}})
    assert [row["label"] for row in rows] == ["7d"]


def test_panel_accounts_prefers_alias_and_keeps_note():
    # (num, email, is_active, display, last_good, alias, org_name, disabled, fetched_at)
    snap = {
        "accounts": [
            (1, "a@x.com", True, _USAGE, _USAGE, "personal", "", False, None),
            (2, "b@x.com", False, "no credentials", None, "", "", True, None),
            (3, "c@x.com", False, _USAGE, _USAGE, "", "Ads Online", False, None),
        ]
    }
    cards = menubar.panel_accounts(snap)
    assert cards[0]["title"] == "personal"
    assert "a@x.com" in cards[0]["subtitle"]
    assert cards[0]["active"] is True
    assert [w["label"] for w in cards[0]["windows"]] == ["5h", "7d"]
    assert cards[1]["title"] == "personal"
    assert cards[1]["subtitle"] == "b@x.com"
    assert cards[1]["note"] == "no credentials"
    assert cards[1]["disabled"] is True
    assert cards[1]["windows"] == []
    assert cards[2]["title"] == "Ads Online"
    assert cards[2]["subtitle"] == "c@x.com"


def test_panel_accounts_uses_last_good_when_display_is_a_sentinel():
    snap = {
        "accounts": [
            (
                2,
                "a@x.com",
                True,
                menubar.SENTINEL_NOTES[USAGE_FOREIGN_CREDENTIAL],
                _USAGE,
                "adsonline",
                "Ads Online",
                False,
                None,
            ),
        ]
    }
    cards = menubar.panel_accounts(snap)
    assert cards[0]["note"] == menubar.SENTINEL_NOTES[USAGE_FOREIGN_CREDENTIAL]
    assert [w["label"] for w in cards[0]["windows"]] == ["5h", "7d"]
    assert cards[0]["windows"][0]["pct"] == 42.0


# --- auto-switch hold line (engine events, not extra ranking) ------------------


def test_hold_panel_reload_plan_defers_while_left_mouse_down():
    assert menubar.hold_panel_reload_plan(
        copy_changed=False, pending=False, left_mouse_down=False
    ) == (False, False)
    assert menubar.hold_panel_reload_plan(
        copy_changed=True, pending=False, left_mouse_down=False
    ) == (True, False)
    assert menubar.hold_panel_reload_plan(
        copy_changed=True, pending=False, left_mouse_down=True
    ) == (False, True)
    assert menubar.hold_panel_reload_plan(
        copy_changed=False, pending=True, left_mouse_down=True
    ) == (False, True)
    assert menubar.hold_panel_reload_plan(
        copy_changed=False, pending=True, left_mouse_down=False
    ) == (True, False)
    assert menubar.hold_panel_reload_plan(
        copy_changed=True, pending=True, left_mouse_down=False
    ) == (True, False)
    assert menubar.hold_panel_reload_plan(
        copy_changed=False, pending=False, left_mouse_down=True
    ) == (False, False)


def test_hold_event_update_keeps_no_switch_and_clears_on_switch():
    held = NoSwitchEvent(reason="cooldown")
    assert menubar.hold_event_update(None, held) is held
    exhausted = AllExhaustedEvent(earliest_reset_at=None)
    assert menubar.hold_event_update(held, exhausted) is exhausted
    poll = PollEvent(active=None, headroom={}, threshold=90.0)
    assert menubar.hold_event_update(exhausted, poll) is exhausted
    switched = SwitchEvent(trigger="proactive", from_ref=None, to_ref=None)
    assert menubar.hold_event_update(exhausted, switched) is None


def test_hold_event_for_snapshot_hides_hold_from_another_slot():
    ev = NoSwitchEvent(reason="cooldown")
    assert menubar.hold_event_for_snapshot(
        ev, hold_slot="1", active_num="2"
    ) is None
    assert menubar.hold_event_for_snapshot(
        ev, hold_slot="1", active_num="1"
    ) is ev
    assert menubar.hold_event_for_snapshot(
        ev, hold_slot=None, active_num="2"
    ) is ev
    assert menubar.hold_event_for_snapshot(
        None, hold_slot="1", active_num="1"
    ) is None
    assert menubar.hold_event_for_snapshot(
        ev, hold_slot="", active_num="1"
    ) is None
    assert menubar.hold_event_for_snapshot(
        ev, hold_slot="", active_num=None
    ) is ev


def test_poll_tick_slot_uses_poll_active_not_live_login():
    poll = PollEvent(
        active={"number": 2, "email": "b@x.com"},
        headroom={},
        threshold=90.0,
    )
    assert menubar.poll_tick_slot(poll) == "2"
    empty = PollEvent(active=None, headroom={}, threshold=90.0)
    assert menubar.poll_tick_slot(empty) == ""
    assert menubar.poll_tick_slot(NoSwitchEvent(reason="cooldown")) is None


def test_hold_cache_after_event_poll_then_hold_keeps_tick_slot():
    poll = PollEvent(
        active={"number": 1, "email": "a@x.com"},
        headroom={},
        threshold=90.0,
    )
    held = NoSwitchEvent(reason="cooldown")
    ev, slot, tick = menubar.hold_cache_after_event(None, None, None, poll)
    assert ev is None
    assert slot is None
    assert tick == "1"
    ev, slot, tick = menubar.hold_cache_after_event(ev, slot, tick, held)
    assert ev is held
    assert slot == "1"
    assert tick == "1"
    assert menubar.hold_event_for_snapshot(
        ev, hold_slot=slot, active_num="1"
    ) is held
    empty = PollEvent(active=None, headroom={}, threshold=90.0)
    none_hold = NoSwitchEvent(reason="no-active-account")
    ev, slot, tick = menubar.hold_cache_after_event(None, None, None, empty)
    assert tick == ""
    ev, slot, tick = menubar.hold_cache_after_event(ev, slot, tick, none_hold)
    assert slot == ""
    assert menubar.hold_event_for_snapshot(
        ev, hold_slot=slot, active_num=None
    ) is none_hold
    assert menubar.hold_event_for_snapshot(
        ev, hold_slot=slot, active_num="1"
    ) is None
    switched = SwitchEvent(trigger="proactive", from_ref=None, to_ref=None)
    ev, slot, tick = menubar.hold_cache_after_event(held, "1", "1", switched)
    assert ev is None
    assert slot is None
    assert tick is None


def test_extra_hold_line_none_when_auto_off_or_no_event():
    ev = NoSwitchEvent(reason="cooldown")
    assert menubar.extra_hold_line(
        auto_enabled=False, event=ev, active_title="personal", strategy="best"
    ) is None
    assert menubar.extra_hold_line(
        auto_enabled=True, event=None, active_title="personal", strategy="best"
    ) is None
    poll = PollEvent(active=None, headroom={}, threshold=90.0)
    assert menubar.extra_hold_line(
        auto_enabled=True, event=poll, active_title="personal", strategy="best"
    ) is None


def test_hold_line_from_event_uses_engine_reason_not_peer_rank():
    soonest = NoSwitchEvent(reason="already-consuming-soonest")
    assert menubar.hold_line_from_event(
        soonest, active_title="personal", strategy="consume-first"
    ) == "Holding on personal: no sooner weekly reset with room."
    assert menubar.hold_line_from_event(
        soonest, active_title="personal", strategy="soonest-5h"
    ) == "Holding on personal: no sooner 5-hour reset with room."
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="cooldown"),
        active_title="Ads Online",
        strategy="soonest-5h",
    ) == "Holding: cooldown after last switch."
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="below-threshold", detail="10% < 90%"),
        active_title="personal",
        strategy="best",
    ) == "Holding: 10% < 90%."
    assert menubar.hold_line_from_event(
        AllExhaustedEvent(earliest_reset_at=None)
    ) == "All accounts are out of usage."
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="already-active")
    ) == "Holding: already active."


def test_extra_hold_line_passes_through_engine_copy_when_auto_on():
    ev = NoSwitchEvent(reason="cooldown")
    assert menubar.extra_hold_line(
        auto_enabled=True, event=ev, active_title="personal", strategy="best"
    ) == "Holding: cooldown after last switch."
    assert menubar.extra_hold_line(
        auto_enabled=True,
        event=AllExhaustedEvent(earliest_reset_at=None),
        active_title="personal",
        strategy="best",
    ) == "All accounts are out of usage."
    soonest = NoSwitchEvent(reason="already-consuming-soonest")
    assert menubar.extra_hold_line(
        auto_enabled=True,
        event=soonest,
        active_title="personal",
        strategy="consume-first",
    ) == "Holding on personal: no sooner weekly reset with room."


def test_hold_line_from_event_reset_unknown_names_window():
    unk = NoSwitchEvent(reason="reset-unknown")
    assert menubar.hold_line_from_event(
        unk, strategy="consume-first"
    ) == "Holding: weekly reset time is unknown."
    assert menubar.hold_line_from_event(
        unk, strategy="soonest-5h"
    ) == "Holding: 5-hour reset time is unknown."
    assert menubar.hold_line_from_event(
        unk, strategy="best"
    ) == "Holding: reset time is unknown."


def test_settings_strategy_hint_matches_selected_strategy():
    consume = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=True),
        strategy="consume-first",
        threshold=90,
    )
    soonest = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=True),
        strategy="soonest-5h",
        threshold=90,
    )
    best = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=True),
        strategy="best",
        threshold=90,
    )
    by_consume = {row["id"]: row for row in consume}
    by_soonest = {row["id"]: row for row in soonest}
    by_best = {row["id"]: row for row in best}
    assert "7-day" in by_consume["strategy_hint"]["label"]
    assert "5-hour" in by_soonest["strategy_hint"]["label"]
    assert by_consume["strategy_hint"]["label"] != by_soonest["strategy_hint"]["label"]
    best_hint = by_best["strategy_hint"]["label"].lower()
    assert "quota" in best_hint
    assert "7-day" not in best_hint
    assert "5-hour" not in best_hint


# --- usage logging -------------------------------------------------------------

def test_format_usage_log_full():
    usage = {
        "five_hour": {"pct": 35.0, "clock": "06:59"},
        "seven_day": {"pct": 55.0, "clock": "Jun 29 21:59"},
    }
    assert menubar.format_usage_log("a@x.com", usage) == (
        "usage a@x.com: 5h 35% (resets 06:59) · 7d 55% (resets Jun 29 21:59)"
    )


def test_format_usage_log_without_clock():
    usage = {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 12.0}}
    assert menubar.format_usage_log("a@x.com", usage) == "usage a@x.com: 5h 0% · 7d 12%"


def test_format_usage_log_partial_window():
    usage = {"seven_day": {"pct": 12.0, "clock": "Jul 3"}}
    assert menubar.format_usage_log("a@x.com", usage) == "usage a@x.com: 7d 12% (resets Jul 3)"


def test_format_usage_log_none_when_no_numeric_window():
    assert menubar.format_usage_log("a@x.com", None) is None
    assert menubar.format_usage_log("a@x.com", "rate limited") is None
    assert menubar.format_usage_log("a@x.com", {"spend": {"pct": 5.0}}) is None


def test_usage_log_key_ignores_clock_tracks_pct():
    u1 = {"five_hour": {"pct": 35.0, "clock": "06:59"}, "seven_day": {"pct": 55.0}}
    u2 = {"five_hour": {"pct": 35.0, "clock": "07:59"}, "seven_day": {"pct": 55.0}}
    u3 = {"five_hour": {"pct": 36.0}, "seven_day": {"pct": 55.0}}
    assert menubar._usage_log_key(u1) == menubar._usage_log_key(u2)  # clock-only change
    assert menubar._usage_log_key(u1) != menubar._usage_log_key(u3)  # pct change
    assert menubar._usage_log_key(None) == (None, None)


# --- title ---------------------------------------------------------------------

def test_format_title_name_and_5h():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "loc · 42%"


def test_format_title_prefers_alias_over_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s, alias="dev") == "dev"


def test_format_title_prefers_alias_then_org_then_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title(
        "loc@papaya.asia", _USAGE, s, alias="dev", org_name="Ads Online"
    ).startswith("dev")
    assert menubar.format_title(
        "loc@papaya.asia", _USAGE, s, org_name="Ads Online"
    ) == "Ads Online"
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "loc"


def test_format_title_name_only_when_pct_off():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "loc"


def test_format_title_5h_only():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="5h")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "42%"


def test_format_title_7d_only():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "18%"


def test_format_title_both_windows():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "42% · 18%"


def test_format_title_both_windows_with_name():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == "loc · 42% · 18%"


def test_format_title_leaves_logo_only_when_name_and_pct_off():
    # The native image stays visible even when there is no title text.
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    assert menubar.format_title("loc@papaya.asia", _USAGE, s) == ""
    assert "%" not in menubar.format_title(
        "loc@papaya.asia", {"five_hour": {"pct": 0.0}, "seven_day": {"pct": 0.0}}, s
    )


def test_format_title_scoped_appends_model_limits():
    # title_pct="off" + title_scoped gives a title tracking only the scoped model
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off", title_scoped=True)
    usage = {**_USAGE, "scoped": [{"name": "Fable", "pct": 55.0}]}
    assert menubar.format_title("loc@papaya.asia", usage, s) == "loc · Fable 55%"


def test_format_title_scoped_after_windows_multiple_models():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both", title_scoped=True)
    usage = {
        **_USAGE,
        "scoped": [{"name": "Fable", "pct": 55.0}, {"name": "Opus", "pct": 7.0}],
    }
    assert menubar.format_title("loc@papaya.asia", usage, s) == (
        "42% · 18% · Fable 55% · Opus 7%"
    )


def test_format_title_scoped_off_by_default():
    # default settings ignore scoped windows entirely
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="off")
    usage = {**_USAGE, "scoped": [{"name": "Fable", "pct": 55.0}]}
    assert not s.title_scoped
    assert menubar.format_title("loc@papaya.asia", usage, s) == ""


def test_format_title_leaves_logo_only_when_no_active_account():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="both")
    assert menubar.format_title(None, None, s) == ""


def test_kickoff_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(
        kickoff_enabled=True,
        kickoff_hour=4,
        kickoff_minute=30,
        kickoff_last_date="2026-09-05",
    )
    original.save(path)
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded.kickoff_enabled is True
    assert loaded.kickoff_hour == 4
    assert loaded.kickoff_minute == 30
    assert loaded.kickoff_last_date == "2026-09-05"


@pytest.mark.parametrize("legacy_value", [True, False])
def test_settings_drop_legacy_show_icon(tmp_path: Path, legacy_value):
    path = tmp_path / "menubar_settings.json"
    path.write_text(json.dumps({"show_icon": legacy_value, "title_pct": "5h"}), encoding="utf-8")
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded.title_pct == "5h"
    assert not hasattr(loaded, "show_icon")
    loaded.save(path)
    assert "show_icon" not in json.loads(path.read_text(encoding="utf-8"))


def test_trailing_header_frames_hug_the_right_edge():
    label, control = menubar.trailing_header_frames(
        312.0, 12.0, (67.5, 14.0), (54.0, 24.0)
    )
    assert control[0] + control[2] == 300.0  # panel width minus pad
    assert label[0] + label[2] + menubar.HEADER_CONTROL_GAP == control[0]
    assert label[0] > 12.0
    assert control[1] == 12.0  # taller than the title row, clamped to pad


def test_settings_header_frames_center_title_on_back():
    back, title = menubar.settings_header_frames(12.0, (64.0, 22.0), (54.0, 16.0))
    assert back == (12.0, 12.0, 64.0, 22.0)
    assert title[0] == 12.0 + 64.0 + menubar.SETTINGS_HEADER_GAP
    back_mid = back[1] + back[3] / 2.0
    title_mid = title[1] + title[3] / 2.0
    assert title_mid == back_mid + menubar.HEADER_LABEL_OPTICAL_DY


def test_popover_is_a_quarter_wider():
    assert menubar.PANEL_WIDTH == 390.0


def test_popover_auto_close_delay_is_a_few_seconds():
    assert 8.0 <= menubar.POPOVER_AUTO_CLOSE_S <= 30.0


def test_panel_wires_trailing_autoswitch_and_auto_close():
    text = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    assert "trailing_header_frames" in text
    assert "POPOVER_AUTO_CLOSE_S" in text
    assert "addGlobalMonitorForEventsMatchingMask" in text
    assert "NSSwitch" in text
    assert "NSPopoverBehaviorApplicationDefined" in text
    more = text[text.index("def _more") : text.index("def _tramp")]
    assert "self.close()" not in more
    assert "self._on_more(sender)" in more


def test_card_view_accepts_the_first_click():
    text = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    card = text[text.index("class _CardView") : text.index("class _RootView")]
    assert "def acceptsFirstMouse_" in card


def test_rebuild_menu_does_not_reload_an_open_popover():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    rebuild = text[text.index("def rebuild_menu") : text.index("def _add_menu")]
    assert "self._panel.reload()" not in rebuild
    assert "_reload_main_panel_if_shown" not in rebuild
    sync = text[text.index("def on_sync_tick") : text.index("def _detect_active_change")]
    assert "self._panel.reload()" not in sync
    assert "_reload_main_panel_if_shown" not in sync
    assert "_apply_hold_line" in sync
    assert "_settings_menu" not in rebuild
    assert "self._add_menu(rumps)" in rebuild
    assert "self._history_menu(rumps)" in rebuild
    assert 'rumps.MenuItem("Quit"' in rebuild


def test_on_setting_reloads_settings_page_not_rebuild_menu():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    body = text[text.index("def _on_setting") : text.index("def _popup_overflow")]
    for name in (
        "on_toggle_name",
        "on_toggle_title_5h",
        "on_toggle_title_7d",
        "on_toggle_scoped",
        "_make_interval",
        "on_toggle_autoswitch",
        "_make_threshold",
        "_make_strategy",
        "on_toggle_kickoff",
        "on_kickoff_time",
    ):
        assert name in body
    assert "SETTINGS_PAGE" in body
    assert "reload()" in body
    rebuild = text[text.index("def rebuild_menu") : text.index("def _add_menu")]
    assert "self._panel.reload()" not in rebuild


def test_kickoff_popup_holds_overflow_like_more():
    panel = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    popup = panel[panel.index("class _PopupButton") : panel.index("class _Trampoline")]
    assert "def mouseDown_" in popup
    assert "_hold_overflow(True)" in popup
    assert "_hold_overflow(False)" in popup
    more = panel[panel.index("def _more") : panel.index("def _tramp")]
    assert "_hold_overflow(True)" in more
    setting = Path(menubar.__file__).read_text(encoding="utf-8")
    body = setting[setting.index("def _on_setting") : setting.index("def _popup_overflow")]
    kickoff = body[body.index("kickoff_time") :]
    assert "return" in kickoff[: kickoff.index("else:")]


def test_panel_settings_page_does_not_set_menu_open():
    text = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    assert "settings_page_rows" in text
    assert "SETTINGS_PAGE" in text
    assert "MAIN_PAGE" in text
    assert '"Settings"' in text
    assert "Change…" not in text
    assert "NSPopUpButton" in text
    assert "class _PopupButton" in text
    assert "settings_header_frames" in text
    assert "SETTINGS_POPUP_W" in text
    assert "PANEL_WIDTH" in text
    assert "setMinimumWidth_" in text
    show = text[text.index("def _show_settings") : text.index("def _show_main")]
    assert "SETTINGS_PAGE" in show
    assert "self.reload()" in show
    assert "_menu_open" not in show
    back = text[text.index("def _show_main") : text.index("def _emit_setting")]
    assert "MAIN_PAGE" in back
    assert "self.reload()" in back
    assert "_menu_open" not in back
    assert '"Back"' in text
    attach = Path(menubar.__file__).read_text(encoding="utf-8")
    ctor = attach[attach.index("self._panel = MenuBarPanel") : attach.index("self._panel.attach")]
    assert "on_setting=" in ctor
    assert "settings=" in ctor
    assert "strategy=" in ctor


def test_apply_hold_line_reloads_open_main_panel_only_when_copy_changes():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    apply = text[text.index("def _apply_hold_line") : text.index("def _drain_engine_events")]
    assert 'snap["hold_line"] = line' in apply
    assert "self._reload_main_panel_if_shown()" in apply
    assert "self._panel.reload()" not in apply
    assert "panel.reload()" not in apply
    assert "hold_panel_reload_plan" in apply
    assert "pressedMouseButtons() & 1" in apply
    assert "changed or self._hold_reload_pending" in apply
    assert apply.index("changed or self._hold_reload_pending") < apply.index(
        "pressedMouseButtons"
    )
    assert "reload_now, self._hold_reload_pending = hold_panel_reload_plan" in apply
    assert "if reload_now:" in apply
    assert apply.index("if reload_now:") < apply.index(
        "self._reload_main_panel_if_shown()"
    )
    assert apply.count("return") == 1
    assert "self.snapshot is not snap" in apply
    assert apply.index("self.snapshot is not snap") < apply.index(
        "hold_panel_reload_plan"
    )
    assert apply.index('snap["hold_line"] = line') < apply.index(
        "self._reload_main_panel_if_shown()"
    )
    assert apply.index("hold_panel_reload_plan") < apply.index(
        "self._reload_main_panel_if_shown()"
    )
    reload_fn = text[
        text.index("def _reload_main_panel_if_shown") : text.index("def _stop_engine")
    ]
    assert "== MAIN_PAGE" in reload_fn
    assert "SETTINGS_PAGE" not in reload_fn
    assert "is_shown()" in reload_fn
    assert "panel.reload()" in reload_fn


def test_manual_switch_uses_json_stamps_cooldown_and_alerts_in_front():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    finish = text[
        text.index("def _finish_manual_switch") : text.index("def _notify(")
    ]
    assert "_clear_hold_event" in finish
    assert "_apply_hold_line" in finish
    assert "record_manual_switch" in text
    assert "json_output=True" in text
    assert "should_notify_manual_switch" in text
    assert "should_dismiss_panel_after_switch" in text
    assert "activateIgnoringOtherApps_" in text
    assert "live_slot_changed" in text
    from_panel = text[
        text.index("def _on_account_click") : text.index("def _repair_relogin")
    ]
    assert "json_output=True" in from_panel
    assert "close_panel=close_panel" in from_panel
    assert "MenuBarPanel" in text
    assert "close_panel=True" in text


def test_widget_tap_is_consumed_on_sync_tick_without_closing_panel():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    sync = text[text.index("def on_sync_tick") : text.index("def _detect_active_change")]
    assert "consume_switch_command" in text
    assert "_consume_widget_command" in sync or "consume_switch_command" in sync
    from_widget = text[
        text.index("def _switch_from_widget") : text.index("def _slot_needs_relogin")
    ]
    assert "close_panel=False" in from_widget
    assert "_on_account_click" in from_widget
    assert "record_manual_switch" in text


def test_should_notify_manual_switch_only_when_switched():
    assert menubar.should_notify_manual_switch({"switched": True}) is True
    assert menubar.should_notify_manual_switch({"switched": False, "reason": "already-active"}) is False
    assert menubar.should_notify_manual_switch(None) is False
    assert menubar.should_notify_manual_switch({}) is False


def test_should_dismiss_panel_after_switch_stays_open_on_error():
    assert menubar.should_dismiss_panel_after_switch({"switched": True}) is True
    assert menubar.should_dismiss_panel_after_switch({"switched": False}) is True
    assert menubar.should_dismiss_panel_after_switch(None) is False


def test_live_slot_changed_sees_org_switch_with_the_same_email():
    snap = {"active_num": "1", "active_email": "gomryo@gmail.com"}
    assert menubar.live_slot_changed(snap, "2") is True
    assert menubar.live_slot_changed(snap, "1") is False
    assert menubar.live_slot_changed(snap, None) is True
    assert menubar.live_slot_changed({"active_num": None}, None) is False


def test_rebuild_menu_fits_status_item_with_permanent_logo():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    assert "format_menu_bar_title(self.snapshot, self.settings)" in text
    assert "self._fit_status_item(title)" in text
    assert "show_icon" not in text
    panel = Path(menubar.__file__).resolve().parent / "menubar_panel.py"
    body = panel.read_text(encoding="utf-8")
    assert "def fit_status_item" in body
    assert "button.setTitle_" in body
    assert "button.setImage_(icon)" in body
    assert "NSImageLeft if shown else NSImageOnly" in body


def test_kickoff_is_wired_from_menubar_sync_tick():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    assert "self._maybe_kickoff()" in text
    assert "self._drain_kickoff_results()" in text
    assert any(
        row["id"] == "kickoff_enabled"
        and "Start available 5-hour windows" in row["label"]
        for row in menubar.settings_page_rows(
            menubar.MenuBarSettings(), strategy="best", threshold=90
        )
    )
    assert "kickoff_last_date" in text
    assert "five_hour_pct" not in text
    assert "usage=last_good if isinstance(last_good, dict) else None" in text
    sync = text[text.index("def on_sync_tick") : text.index("def _detect_active_change")]
    assert sync.index("self._drain_kickoff_results()") < sync.index(
        "self._maybe_kickoff()"
    )


def test_maybe_kickoff_does_not_stamp_last_date_before_the_worker():
    """A failed morning must remain due; last_date is recorded only after success."""
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    maybe = text[
        text.index("def _maybe_kickoff") : text.index("def _run_kickoff")
    ]
    drain = text[text.index("def _drain_kickoff_results") :]
    drain = drain[: drain.index("\n        def ", 1)]
    assert "kickoff_last_date =" not in maybe
    assert ".save(" not in maybe
    assert "kickoff_backoff_active" in maybe
    assert "kickoff_pass_complete" in drain
    assert "kickoff_last_date =" in drain


def test_kickoff_skips_setup_session_for_the_live_default_login():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    run = text[text.index("def _run_kickoff") : text.index("def _drain_kickoff_results")]
    assert "kickoff_uses_default_login" in run
    assert "setup_session" in run
    assert "invoke_kickoff()" in run or "invoke_kickoff(None)" in run


def test_codex_active_kickoff_pings_engine_home():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    run = text[text.index("def _run_kickoff") : text.index("def _drain_kickoff_results")]
    assert "invoke_codex_kickoff(self.codex.home)" in run
    assert "kickoff_account_eligible" in run


def test_format_title_truncates_long_local_part():
    s = menubar.MenuBarSettings(show_account_name=True, title_pct="off")
    title = menubar.format_title("averylonglocalpart@example.com", None, s)
    assert title == "averylonglo*"  # 12 chars: 11 letters + asterisk marker


def test_format_title_both_drops_unavailable_windows():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    assert menubar.format_title("loc@x.com", "no credentials", s) == ""


def test_format_title_both_keeps_available_window():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="both")
    # only 5h present -> 7d dropped, no trailing separator
    assert menubar.format_title("loc@x.com", {"five_hour": {"pct": 9.0}}, s) == "9%"


# --- reset-time helpers --------------------------------------------------------

def test_resets_at_ts_orders_and_handles_missing():
    early = {"resets_at": "2026-06-24T07:00:00+00:00"}
    late = {"resets_at": "2026-06-26T07:00:00+00:00"}
    assert menubar._resets_at_ts(early) < menubar._resets_at_ts(late)
    assert menubar._resets_at_ts({"pct": 5.0}) == float("inf")   # no resets_at
    assert menubar._resets_at_ts({"resets_at": "garbage"}) == float("inf")
    assert menubar._resets_at_ts(None) == float("inf")


_NOW = 1_000_000.0


def _iso(delta_s):  # ISO-8601 for _NOW + delta_s, UTC
    return _dt.datetime.fromtimestamp(_NOW + delta_s, _dt.timezone.utc).isoformat()


def test_live_countdown_formats_from_resets_at():
    assert menubar._live_countdown({"resets_at": _iso(9 * 3600 + 5 * 60)}, _NOW) == "9h 5m"
    assert menubar._live_countdown({"resets_at": _iso(86400 + 19 * 3600)}, _NOW) == "1d 19h"
    assert menubar._live_countdown({"resets_at": _iso(34 * 60)}, _NOW) == "34m"


def test_live_countdown_none_when_passed_or_missing():
    assert menubar._live_countdown({"resets_at": _iso(-60)}, _NOW) is None   # already reset
    assert menubar._live_countdown({"pct": 5.0}, _NOW) is None               # no resets_at
    assert menubar._live_countdown("no credentials", _NOW) is None


def test_usage_summary_live_countdown_from_resets_at():
    usage = {
        "five_hour": {"pct": 42.0, "resets_at": _iso(2 * 3600 + 33 * 60)},
        "seven_day": {"pct": 18.0, "resets_at": _iso(86400 + 19 * 3600)},
        "spend": {"pct": 30.0},
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 42% (2h 33m) · 7d 18% (1d 19h) · $ 30%"


def test_usage_summary_omits_countdown_when_passed_or_missing():
    # 5h reset already passed (stale data) -> omit; 7d has no resets_at -> omit
    usage = {"five_hour": {"pct": 53.0, "resets_at": _iso(-60)}, "seven_day": {"pct": 8.0}}
    assert menubar.usage_summary(usage, _NOW) == "5h 53% · 7d 8%"


# --- switch-history log parsing ------------------------------------------------

_SWITCH_LOG = (
    "2026-06-27 00:57:50,178 - INFO - Switched from account 1 to 3\n"
    "2026-06-27 02:06:21,302 - INFO - usage a@x.com: 5h 10%\n"
    "2026-06-27 02:10:00,000 - INFO - Switched from account 3 to 1\n"
)


def test_parse_switch_history_most_recent_first():
    assert menubar.parse_switch_history(_SWITCH_LOG) == [
        "3 → 1   2026-06-27 02:10",
        "1 → 3   2026-06-27 00:57",
    ]


def test_parse_switch_history_respects_limit():
    lines = "\n".join(
        f"2026-06-27 0{i}:00:00,000 - INFO - Switched from account 1 to 2"
        for i in range(1, 6)
    )
    out = menubar.parse_switch_history(lines, limit=2)
    assert len(out) == 2
    assert out[0] == "1 → 2   2026-06-27 05:00"  # newest first


def test_parse_switch_history_empty_or_no_matches():
    assert menubar.parse_switch_history("") == []
    assert menubar.parse_switch_history("nothing relevant here") == []


# --- snapshot adapter (fakes for AccountsSnapshot / UsageEntry) -----------------

class _FakeEntry:
    def __init__(self, sentinel=None, last_good=None, fetched_at=None):
        self.sentinel = sentinel
        self.last_good = last_good
        self.fetched_at = fetched_at


class _FakeAcct:
    def __init__(self, number, email, is_active, usage, alias="", disabled=False, org_name=""):
        self.number = number
        self.email = email
        self.is_active = is_active
        self.usage = usage
        self.alias = alias
        self.disabled = disabled
        self.org_name = org_name


class _FakeSnap:
    def __init__(self, accounts):
        self.accounts = accounts


def test_account_display_usage_sentinel_note_last_good_or_none():
    assert menubar._account_display_usage(
        _FakeEntry(sentinel=USAGE_API_KEY)
    ) == menubar.SENTINEL_NOTES[USAGE_API_KEY]
    lg = {"five_hour": {"pct": 5.0}}
    assert menubar._account_display_usage(_FakeEntry(last_good=lg)) == lg
    assert menubar._account_display_usage(_FakeEntry()) is None


def test_adapt_snapshot_shape_and_active_selection():
    # _adapt_snapshot is a pure transform of an AccountsSnapshot (the fetch
    # pacing now lives in SnapshotSource, tested separately).
    lg = {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 20.0}}
    accts = [
        _FakeAcct("1", "a@x.com", True, _FakeEntry(last_good=lg, fetched_at=123.0)),
        _FakeAcct("2", "b@x.com", False, _FakeEntry(sentinel=USAGE_API_KEY), disabled=True),
    ]
    snap = menubar._adapt_snapshot(_FakeSnap(accts))
    assert snap["active_email"] == "a@x.com"
    assert snap["active_num"] == "1"
    assert snap["active_usage"] == lg
    assert snap["active_last_good"] == lg
    assert snap["active_fetched_at"] == 123.0
    assert snap["active_alias"] == ""
    assert snap["active_org"] == ""
    # (num, email, is_active, display, last_good, alias, org_name, disabled, fetched_at)
    assert len(snap["accounts"][0]) == 9
    assert snap["accounts"][0] == ("1", "a@x.com", True, lg, lg, "", "", False, 123.0)
    # sentinel account: display is the human note, last_good/fetched_at are None; disabled carried through
    assert snap["accounts"][1] == (
        "2", "b@x.com", False, menubar.SENTINEL_NOTES[USAGE_API_KEY], None, "", "", True, None,
    )


def test_adapt_snapshot_empty():
    assert menubar._adapt_snapshot(_FakeSnap([])) == menubar.EMPTY_SNAPSHOT


def test_title_usage_falls_back_to_last_good_on_sentinel():
    lg = {"five_hour": {"pct": 13.0}, "scoped": [{"name": "Fable", "pct": 23.0}]}
    note = menubar.SENTINEL_NOTES[USAGE_FOREIGN_CREDENTIAL]
    snap = {
        **menubar.EMPTY_SNAPSHOT,
        "active_email": "a@x.com",
        "active_usage": note,
        "active_last_good": lg,
        "active_alias": "adsonline",
    }
    assert menubar.title_usage(snap) == lg
    s = menubar.MenuBarSettings(
        show_account_name=True, title_pct="5h", title_scoped=True
    )
    assert menubar.format_title(
        snap["active_email"], menubar.title_usage(snap), s, alias="adsonline"
    ) == "adsonline · 13% · Fable 23%"
    assert menubar.title_usage({"active_usage": lg}) == lg
    assert menubar.title_usage(menubar.EMPTY_SNAPSHOT) is None


def test_adapt_snapshot_keeps_last_good_when_active_is_sentinel():
    lg = {"five_hour": {"pct": 0.0}}
    accts = [
        _FakeAcct(
            "2",
            "a@x.com",
            True,
            _FakeEntry(
                sentinel=USAGE_FOREIGN_CREDENTIAL, last_good=lg, fetched_at=55.0
            ),
            alias="adsonline",
        ),
    ]
    snap = menubar._adapt_snapshot(_FakeSnap(accts))
    assert snap["active_usage"] == menubar.SENTINEL_NOTES[USAGE_FOREIGN_CREDENTIAL]
    assert snap["active_last_good"] == lg
    assert snap["active_fetched_at"] == 55.0
    assert menubar.title_usage(snap) == lg


def test_title_clock_freezes_sentinel_last_good():
    note = menubar.SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED]
    snap = {
        **menubar.EMPTY_SNAPSHOT,
        "active_usage": note,
        "active_fetched_at": 123.0,
    }
    assert menubar.title_clock(snap, now=999.0) == 123.0
    live = {**menubar.EMPTY_SNAPSHOT, "active_usage": {"five_hour": {"pct": 1.0}}}
    assert menubar.title_clock(live, now=999.0) == 999.0
    assert menubar.title_clock(menubar.EMPTY_SNAPSHOT, now=999.0) == 999.0


# --- weekly reset roll-forward (static 7-day cadence) --------------------------

def test_rolled_weekly_window_advances_passed_reset():
    w = {"pct": 95.0, "resets_at": _iso(-3 * 86400), "countdown": "stale", "clock": "old"}
    rolled = menubar._rolled_weekly_window(w, _NOW)
    assert rolled["pct"] == 0.0  # the window objectively rolled over
    assert abs(menubar._resets_at_ts(rolled) - (_NOW + 4 * 86400)) < 1
    assert "countdown" not in rolled and "clock" not in rolled  # stale strings dropped


def test_rolled_weekly_window_advances_multiple_missed_weeks():
    w = {"pct": 80.0, "resets_at": _iso(-10 * 86400)}  # two boundaries crossed
    rolled = menubar._rolled_weekly_window(w, _NOW)
    assert abs(menubar._resets_at_ts(rolled) - (_NOW + 4 * 86400)) < 1


def test_rolled_weekly_window_leaves_future_or_unknown_untouched():
    future = {"pct": 42.0, "resets_at": _iso(2 * 86400)}
    assert menubar._rolled_weekly_window(future, _NOW) is future
    no_reset = {"pct": 42.0}
    assert menubar._rolled_weekly_window(no_reset, _NOW) is no_reset
    assert menubar._rolled_weekly_window(None, _NOW) is None


def test_usage_summary_reflects_passed_weekly_reset():
    # 7d reset a day ago: show it as reset (0%) with the next weekly boundary,
    # from the static schedule alone. 5h is untouched (dynamic session window).
    usage = {
        "five_hour": {"pct": 10.0},
        "seven_day": {"pct": 95.0, "resets_at": _iso(-86400)},
    }
    assert menubar.usage_summary(usage, _NOW) == "5h 10% · 7d 0% (6d 0h)"


def test_usage_summary_scoped_reflects_passed_weekly_reset():
    usage = {"scoped": [{"name": "Fable", "pct": 100.0, "resets_at": _iso(-86400)}]}
    # rolled to 0% → the over-limit "(!)" marker is gone too
    assert menubar.usage_summary(usage, _NOW) == "Fable 0% (6d 0h)"


def test_format_title_reflects_passed_weekly_reset():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-86400)}}
    assert menubar.format_title("a@x.com", usage, s, _NOW) == "0%"


def test_format_title_sentinel_last_good_does_not_roll():
    s = menubar.MenuBarSettings(show_account_name=False, title_pct="7d")
    usage = {"seven_day": {"pct": 95.0, "resets_at": _iso(-86400)}}
    fetched = _NOW - 3 * 86400
    assert menubar.format_title("a@x.com", usage, s, fetched) == "95%"


# --- notification copy --------------------------------------------------------

def _combined(copy: menubar.NotificationCopy) -> str:
    return f"{copy.title}\n{copy.subtitle}\n{copy.body}"


def test_switch_notification_uses_alias_not_account_n_or_trigger_jargon():
    ev = SwitchEvent(
        trigger="proactive",
        from_ref={"number": 1, "email": "a@x.com"},
        to_ref={"number": 2, "email": "b@x.com"},
    )
    copy = menubar.notification_copy_for_event(
        ev, aliases={"1": "personal", "2": "adsonline", "a@x.com": "personal", "b@x.com": "adsonline"}
    )
    assert copy is not None
    assert copy.title == "Switched to adsonline"
    text = _combined(copy)
    assert "personal" in copy.body
    assert "Account-2 (" not in text
    assert "Account-1 (" not in text
    assert "proactive" not in text.lower()
    assert "at-limit" not in text.lower()
    assert "openswap" not in text.lower()


def test_switch_notification_falls_back_to_email_local_part():
    ev = SwitchEvent(
        trigger="at-limit",
        from_ref={"number": 1, "email": "loc@papaya.asia"},
        to_ref={"number": 2, "email": "ads@example.com"},
    )
    copy = menubar.notification_copy_for_event(ev)
    assert copy is not None
    assert copy.title == "Switched to ads"
    text = _combined(copy)
    assert "Account-2 (" not in text
    assert "at-limit" not in text.lower()
    assert "proactive" not in text.lower()


def test_dry_run_switch_does_not_notify():
    ev = SwitchEvent(
        trigger="proactive",
        from_ref={"number": 1, "email": "a@x.com"},
        to_ref={"number": 2, "email": "b@x.com"},
        dry_run=True,
    )
    assert menubar.notification_copy_for_event(ev) is None


def test_manual_switch_notification_names_destination():
    copy = menubar.notification_copy_for_manual_switch("adsonline")
    assert copy.title == "Switched to adsonline"
    assert "Account-" not in _combined(copy)
    assert "openswap --add-account" not in copy.body


def test_format_running_line_empty_is_none():
    assert menubar.format_running_line([], []) is None
    copy = menubar.notification_copy_for_manual_switch("Ads Online", running=False)
    assert "Restart Claude Code" not in copy.body


def test_format_running_line_one_session_uses_cwd_not_pid():
    sess = SimpleNamespace(pid=424242, cwd="/Users/x/proj")
    line = menubar.format_running_line([sess], [])
    assert line is not None
    assert "proj" in line
    assert "424242" not in line
    assert "pid" not in line.lower()


def test_format_running_line_multiple_sessions_counts_without_pids():
    sessions = [
        SimpleNamespace(pid=11111, cwd="/a/one"),
        SimpleNamespace(pid=22222, cwd="/b/two"),
    ]
    line = menubar.format_running_line(sessions, [])
    assert line == "Claude Code is running (2 sessions)."
    assert "11111" not in line
    assert "22222" not in line


def test_format_running_line_ide_only_uses_ide_name():
    ide = SimpleNamespace(pid=33333, ide_name="Cursor")
    line = menubar.format_running_line([], [ide])
    assert line == "Claude Code is running in Cursor."
    assert "33333" not in line


def test_format_running_line_prefers_session_cwd_over_ide():
    sess = SimpleNamespace(pid=9, cwd="/Users/x/proj")
    ide = SimpleNamespace(pid=8, ide_name="Cursor")
    line = menubar.format_running_line([sess], [ide])
    assert "proj" in line
    assert "Cursor" not in line


def test_switch_restart_hint_empty_when_not_running():
    assert menubar.switch_restart_hint(True) == (
        "Restart Claude Code to apply now, or wait about 30 seconds."
    )
    assert menubar.switch_restart_hint(False) == ""


def test_manual_switch_notification_restart_depends_on_running():
    off = menubar.notification_copy_for_manual_switch("Ads Online", running=False)
    assert "Restart Claude Code" not in off.body
    on = menubar.notification_copy_for_manual_switch("Ads Online", running=True)
    assert "Restart Claude Code" in on.body
    assert "30 seconds" in on.body
    default = menubar.notification_copy_for_manual_switch("Ads Online")
    assert "Restart Claude Code" in default.body
    assert "30 seconds" in default.body


def test_switch_event_notification_passes_running_through():
    ev = SwitchEvent(
        trigger="proactive",
        from_ref={"number": 1, "email": "a@x.com"},
        to_ref={"number": 2, "email": "b@x.com"},
    )
    off = menubar.notification_copy_for_event(ev, running=False)
    assert off is not None
    assert "Restart Claude Code" not in off.body
    on = menubar.notification_copy_for_event(ev, running=True)
    assert "Restart Claude Code" in on.body
    assert "30 seconds" in on.body
    default = menubar.notification_copy_for_event(ev)
    assert "Restart Claude Code" in default.body


def test_quarantine_notification_has_no_cli_recovery_command():
    ev = QuarantineEvent(number="2", email="ads@example.com", reason="invalid_grant")
    copy = menubar.notification_copy_for_event(ev, aliases={"2": "adsonline"})
    assert copy is not None
    text = _combined(copy)
    assert "adsonline" in copy.title
    assert "Account-2 (" not in text
    assert "openswap --add-account" not in text
    assert "openswap --add-account --slot" not in text


def test_all_exhausted_notification_uses_local_clock_not_iso_z():
    iso = "2026-09-05T15:42:00Z"
    ev = AllExhaustedEvent(earliest_reset_at=iso)
    copy = menubar.notification_copy_for_event(ev)
    assert copy is not None
    assert iso not in _combined(copy)
    formatted = menubar.format_local_reset(iso)
    assert formatted is not None
    assert formatted in copy.body
    assert "openswap --add-account" not in copy.body


def test_config_warning_notification_is_glanceable():
    ev = ConfigWarningEvent(message="autoswitch.model: Fabel matches no account")
    copy = menubar.notification_copy_for_event(ev)
    assert copy is not None
    assert copy.title == "Settings need a look"
    assert "Fabel" in copy.body
    assert "Account-N (" not in _combined(copy)
    assert "openswap --add-account" not in copy.body


def test_poll_no_switch_sleep_do_not_notify():
    assert menubar.notification_copy_for_event(
        PollEvent(active=None, headroom={}, threshold=90.0)
    ) is None
    assert menubar.notification_copy_for_event(NoSwitchEvent(reason="cooldown")) is None
    assert menubar.notification_copy_for_event(
        SleepEvent(seconds=60, until="2026-09-05T12:00:00Z")
    ) is None


def test_engine_start_failure_notification_names_the_event():
    copy = menubar.notification_copy_for_engine_start_failure("lock timeout")
    assert "Auto-switch" in copy.title
    assert "lock timeout" in copy.body
    assert "Account-" not in _combined(copy)


def test_kickoff_notification_names_accounts_not_slots():
    copy = menubar.notification_copy_for_kickoff(
        [("personal", True, ""), ("adsonline", False, "auth failed")]
    )
    assert copy is not None
    text = _combined(copy)
    assert "personal" in text
    assert "adsonline" in text
    assert "Account-" not in text
    assert "openswap --add-account" not in text


def test_kickoff_notification_drops_noisy_stdin_preamble():
    copy = menubar.notification_copy_for_kickoff(
        [
            (
                "personal",
                False,
                "Reading additional input from stdin...\n"
                "Not inside a trusted directory and --skip-git-repo-check was not specified.",
            )
        ]
    )
    assert copy is not None
    assert copy.title == "Couldn't start personal's 5-hour window"
    assert "stdin" not in copy.body
    assert "trusted directory" in copy.body


# --- signed-out repair (extra) ------------------------------------------------

def test_panel_accounts_relogin_keeps_windows_and_uses_extra_copy():
    snap = {
        "accounts": [
            (
                1,
                "a@x.com",
                False,
                menubar.SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED],
                _USAGE,
                "personal",
                "",
                False,
                None,
            ),
        ]
    }
    cards = menubar.panel_accounts(snap)
    assert cards[0]["needs_relogin"] is True
    assert cards[0]["note"] == menubar.RELOGIN_CARD_NOTE
    assert "openswap" not in cards[0]["note"]
    assert [w["label"] for w in cards[0]["windows"]] == ["5h", "7d"]
    assert cards[0]["windows"][0]["pct"] == 42.0
    assert cards[0]["fetched_at"] is None


def test_panel_accounts_signed_out_does_not_roll_or_tick_last_good():
    usage = {
        "five_hour": {"pct": 42.0, "resets_at": _iso(2 * 3600)},
        "seven_day": {"pct": 95.0, "resets_at": _iso(-86400)},
    }
    fetched = _NOW - 3 * 86400
    snap = {
        "accounts": [
            (
                1,
                "a@x.com",
                False,
                menubar.SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED],
                usage,
                "personal",
                "",
                False,
                fetched,
            ),
        ]
    }
    cards = menubar.panel_accounts(snap, now=_NOW)
    assert cards[0]["fetched_at"] == fetched
    seven = next(w for w in cards[0]["windows"] if w["label"] == "7d")
    five = next(w for w in cards[0]["windows"] if w["label"] == "5h")
    assert seven["pct"] == 95.0
    assert five["pct"] == 42.0
    assert seven["countdown"] is None
    assert seven["resets_at_ts"] is None
    assert five["countdown"] is None
    assert five["resets_at_ts"] is None


def test_panel_accounts_foreign_note_is_not_relogin():
    snap = {
        "accounts": [
            (
                2,
                "a@x.com",
                True,
                menubar.SENTINEL_NOTES[USAGE_FOREIGN_CREDENTIAL],
                _USAGE,
                "adsonline",
                "Ads Online",
                False,
                None,
            ),
        ]
    }
    cards = menubar.panel_accounts(snap)
    assert cards[0]["needs_relogin"] is False
    assert cards[0]["note"] == menubar.SENTINEL_NOTES[USAGE_FOREIGN_CREDENTIAL]


def test_plan_relogin_click_captures_only_on_email_and_org_match():
    slot = ("a@x.com", "org-personal")
    matched = menubar.plan_relogin_click(
        live=slot,
        slot=slot,
        slot_name="personal",
        live_name="personal",
    )
    assert matched is not None
    assert matched.kind == "capture"

    wrong_org = menubar.plan_relogin_click(
        live=("a@x.com", "org-ads"),
        slot=slot,
        slot_name="personal",
        live_name="adsonline",
    )
    assert wrong_org is not None
    assert wrong_org.kind == "confirm_open_login"
    assert wrong_org.kind != "capture"
    title = menubar.relogin_wrong_account_title(wrong_org)
    assert title == "Sign in as personal?"
    msg = menubar.relogin_wrong_account_message(wrong_org)
    assert msg == (
        "Claude Code is using adsonline right now. "
        "Your saved adsonline account is not removed. "
        "Continue to sign in as personal?"
    )

    signed_out = menubar.plan_relogin_click(
        live=None,
        slot=slot,
        slot_name="personal",
        live_name=None,
    )
    assert signed_out is not None
    assert signed_out.kind == "open_login"

    assert menubar.plan_relogin_click(
        live=slot, slot=None, slot_name="personal", live_name="personal"
    ) is None


def test_matching_relogin_slot_requires_org_uuid():
    live = ("a@x.com", "org-ads")
    identities = {
        "1": ("a@x.com", "org-personal"),
        "2": ("a@x.com", "org-ads"),
    }
    assert menubar.matching_relogin_slot(live, identities, {"1"}) is None
    assert menubar.matching_relogin_slot(live, identities, {"2"}) == "2"
    assert menubar.matching_relogin_slot(None, identities, {"2"}) is None
    assert menubar.matching_relogin_slot(live, identities, set()) is None


def test_newly_relogin_slots_only_the_new_ones():
    assert menubar.newly_relogin_slots({"1"}, {"1", "2"}) == {"2"}
    assert menubar.newly_relogin_slots(set(), {"1"}) == {"1"}
    assert menubar.newly_relogin_slots({"1"}, {"1"}) == set()
    assert menubar.newly_relogin_slots({"1"}, set()) == set()


def test_relogin_slot_nums_from_snapshot_display():
    snap = {
        "accounts": [
            (1, "a@x.com", False, menubar.SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED], None, "personal", "", False, None),
            (2, "a@x.com", True, _USAGE, _USAGE, "adsonline", "Ads Online", False, None),
        ]
    }
    assert menubar.relogin_slot_nums(snap) == {"1"}


def test_slot_identity_from_sequence_uses_org_uuid():
    seq = {
        "accounts": {
            "1": {"email": "a@x.com", "organizationUuid": "org-personal"},
            "2": {"email": "a@x.com", "organizationUuid": "org-ads"},
        }
    }
    assert menubar.slot_identity_from_sequence(seq, "1") == ("a@x.com", "org-personal")
    assert menubar.slot_identity_from_sequence(seq, 2) == ("a@x.com", "org-ads")
    assert menubar.slot_identity_from_sequence(seq, "9") is None
    assert menubar.slot_identity_from_sequence(None, "1") is None


def test_notification_copy_for_relogin_has_no_cli():
    copy = menubar.notification_copy_for_relogin("personal")
    assert copy.title == "personal signed out"
    assert "openswap" not in copy.body.lower()
    assert "click" in copy.body.lower()
    captured = menubar.notification_copy_for_relogin_captured("personal")
    assert "personal" in captured.title
    assert "openswap" not in captured.body.lower()


def test_build_login_command_text_quotes_email():
    text = menubar.build_login_command_text(
        "/opt/homebrew/bin/claude", "a@x.com"
    )
    assert text.startswith("#!/bin/bash\n")
    assert "tell application" not in text
    assert "auth login" in text
    assert "--claudeai" in text
    assert "a@x.com" in text


def test_launch_claude_login_opens_command_file(tmp_path):
    calls = []
    dest = tmp_path / "login.command"

    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    menubar.launch_claude_login(
        "a@x.com",
        which=lambda name: "/opt/homebrew/bin/claude" if name == "claude" else None,
        run=run,
        command_path=dest,
    )
    assert dest.is_file()
    if sys.platform != "win32":
        assert dest.stat().st_mode & 0o111
    body = dest.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/bash\n")
    assert "a@x.com" in body
    assert calls[0][0] == "open"
    assert calls[0][1] == str(dest)


def test_launch_claude_login_missing_claude():
    with pytest.raises(ClaudeSwitchError, match="claude"):
        menubar.launch_claude_login(
            "a@x.com",
            which=lambda _name: None,
            run=lambda *_a, **_k: SimpleNamespace(returncode=0),
        )


def test_display_helpers_import_without_rumps():
    """The split helper module must stay import-safe when rumps is absent."""
    import openswap.menubar_display as display

    assert display.format_title
    assert display.MenuBarSettings


def test_run_without_rumps_raises_clean_error(monkeypatch):
    """A missing menubar extra surfaces as ClaudeSwitchError, not a traceback.

    The module is import-safe without rumps, so the CLI's ImportError guard
    around ``from openswap.menubar import run`` can never fire — the import
    failure happens inside ``run()``. Blocking the import (a ``None`` entry in
    ``sys.modules`` makes ``import rumps`` raise) checks that ``run()`` turns
    it into the error type the CLI renders with the install hint.
    """
    monkeypatch.setitem(sys.modules, "rumps", None)
    with pytest.raises(ClaudeSwitchError, match=r"uv tool install --force --editable") as exc:
        menubar.run(switcher=None)
    assert "pip install" not in str(exc.value)
    assert "rumps" in str(exc.value)


from openswap.models import AccountSnapshot, AccountsSnapshot
from openswap.usage_store import UsageEntry


def test_adapt_snapshot_appends_codex_rows_namespaced():
    claude = AccountsSnapshot(active_number="2", taken_at=0.0, accounts=(
        AccountSnapshot("1", "a@x.com", "", "", False, "oauth", True, UsageEntry()),
        AccountSnapshot("2", "b@x.com", "Ads Online", "org-b", True, "oauth", True, UsageEntry()),))
    codex = AccountsSnapshot(active_number="1", taken_at=0.0, accounts=(
        AccountSnapshot("1", "c@x.com", "plus", "acc-c", True, "oauth", True, UsageEntry(), provider="codex"),))
    out = menubar._adapt_snapshot(claude, codex)
    assert [row[0] for row in out["accounts"]] == ["1", "2", "codex:1"]
    assert out["accounts"][2][6] == "plus"                 # org_name slot carries the plan
    assert out["kinds"]["codex:1"] == "oauth"
    assert out["identities"]["codex:1"] == ("c@x.com", "acc-c")
    assert out["codex_active_num"] == "1"
    assert out["active_num"] == "2" and out["active_email"] == "b@x.com"   # Claude only


def test_adapt_snapshot_without_codex_is_unchanged():
    claude = AccountsSnapshot(active_number="1", taken_at=0.0, accounts=(
        AccountSnapshot("1", "a@x.com", "", "", True, "oauth", True, UsageEntry()),))
    out = menubar._adapt_snapshot(claude)
    assert [row[0] for row in out["accounts"]] == ["1"]
    assert out["codex_active_num"] is None


def test_panel_accounts_prefixes_codex_title_and_sets_provider():
    snap = {"accounts": [
        (1, "a@x.com", True, _USAGE, _USAGE, "", "", False, None),
        ("codex:1", "c@x.com", True, _USAGE, _USAGE, "", "plus", False, None)]}
    cards = menubar.panel_accounts(snap, now=_NOW)
    assert cards[0]["provider"] == "claude"
    assert cards[1]["provider"] == "codex" and cards[1]["title"] == "Codex · plus"
    assert cards[1]["num"] == "codex:1"


def test_provider_cards_filters_and_transforms_shared_codex_without_mutating():
    cards = [
        {"provider": "claude", "title": "personal", "num": "1"},
        {"provider": "codex", "title": "Codex · plus", "num": "codex:1"},
    ]
    original = [dict(card) for card in cards]
    assert [card["num"] for card in menubar.provider_cards(cards, "claude")] == ["1"]
    chatgpt = menubar.provider_cards(cards, "chatgpt")
    assert chatgpt == [{"provider": "chatgpt", "title": "plus", "num": "codex:1"}]
    assert cards == original
    assert menubar.provider_cards(cards, "other") == []


@pytest.mark.parametrize("marker", ["kinds", "display"])
def test_panel_accounts_marks_codex_api_key_cli_only_from_either_marker(marker):
    row = ("codex:1", "api@x.com", True, _USAGE, _USAGE, "", "plus", False, None)
    snapshot = {"accounts": [row]}
    if marker == "kinds":
        snapshot["kinds"] = {"codex:1": "api_key"}
    else:
        snapshot["accounts"] = [(*row[:3], menubar.USAGE_API_KEY, *row[4:])]
    raw_cards = menubar.panel_accounts(snapshot)
    assert not raw_cards[0]["disabled"]  # Widget/CLI semantics are unchanged.
    card = menubar.provider_cards(raw_cards, "chatgpt")[0]
    assert card["api_key"] is True
    assert card["disabled"] is True
    assert "CLI-only" in card["note"]


def test_panel_accounts_keeps_normal_disabled_row_distinct_from_api_key():
    row = ("codex:1", "a@x.com", False, _USAGE, _USAGE, "", "plus", True, None)
    card = menubar.panel_accounts({"accounts": [row]})[0]
    assert card["disabled"] is True
    assert card["api_key"] is False
    assert "CLI-only" not in (card["note"] or "")


def test_codex_live_slot_changed():
    snap = {"codex_active_num": "1"}
    assert menubar.codex_live_slot_changed(snap, "2") is True
    assert menubar.codex_live_slot_changed(snap, "1") is False
    assert menubar.codex_live_slot_changed(snap, None) is True
    assert menubar.codex_live_slot_changed({"codex_active_num": None}, None) is False


def test_codex_restart_hint():
    assert menubar.codex_restart_hint() == "Restart Codex to apply."


def test_format_codex_running_line_empty_is_none():
    assert menubar.format_codex_running_line([]) is None
    assert menubar.format_codex_running_line(None) is None


def test_format_codex_running_line_one_without_cwd():
    proc = SimpleNamespace(pid=424242, cwd="", kind="tui")
    line = menubar.format_codex_running_line([proc])
    assert line == "Codex is running."
    assert "424242" not in line
    assert "pid" not in line.lower()


def test_format_codex_running_line_one_with_cwd():
    proc = SimpleNamespace(pid=424242, cwd="/Users/x/proj", kind="tui")
    line = menubar.format_codex_running_line([proc])
    assert line == "Codex is running in x/proj."
    assert "424242" not in line
    assert "pid" not in line.lower()


def test_format_codex_running_line_multiple_sessions():
    procs = [
        SimpleNamespace(pid=11111, cwd="/a/one", kind="tui"),
        SimpleNamespace(pid=22222, cwd="/b/two", kind="tui"),
    ]
    line = menubar.format_codex_running_line(procs)
    assert line == "Codex is running (2 sessions)."
    assert "11111" not in line
    assert "22222" not in line


def test_switch_codex_restart_hint():
    assert menubar.switch_codex_restart_hint(True) == "Restart Codex to apply."
    assert menubar.switch_codex_restart_hint(False) == ""
    assert menubar.codex_restart_hint() == menubar.switch_codex_restart_hint(True)


def test_manual_switch_omits_restart_codex_when_not_running():
    off = menubar.notification_copy_for_manual_switch(
        "work", running=False, provider="codex"
    )
    assert "Restart Codex" not in off.body
    assert "Claude Code" not in off.body
    on = menubar.notification_copy_for_manual_switch(
        "work", running=True, provider="codex"
    )
    assert on.body == "Restart Codex to apply."
    assert "Claude Code" not in on.body
    default = menubar.notification_copy_for_manual_switch("work", provider="codex")
    assert "Restart Codex to apply." in default.body


def test_hold_cache_ignores_codex_events():
    held = NoSwitchEvent(reason="cooldown")
    codex_sw = SwitchEvent(
        trigger="proactive", from_ref=None, to_ref=None, provider="codex"
    )
    ev, slot, tick = menubar.hold_cache_after_event(held, "1", "1", codex_sw)
    assert ev is held and slot == "1" and tick == "1"


def test_codex_switch_event_toast_says_restart_codex():
    ev = SwitchEvent(
        trigger="proactive",
        from_ref={"number": 1, "email": "a@x.com"},
        to_ref={"number": 2, "email": "b@x.com"},
        provider="codex",
    )
    copy = menubar.notification_copy_for_event(ev, running=True)
    assert copy is not None
    assert copy.title == "Switched to b"
    assert "Restart Codex to apply." in copy.body
    assert "Claude Code" not in copy.body


def test_codex_switch_event_respects_running():
    ev = SwitchEvent(
        trigger="proactive",
        from_ref={"number": 1, "email": "a@x.com"},
        to_ref={"number": 2, "email": "b@x.com"},
        provider="codex",
    )
    off = menubar.notification_copy_for_event(ev, running=False)
    assert off is not None
    assert "Restart Codex" not in off.body
    assert "Claude Code" not in off.body
    on = menubar.notification_copy_for_event(ev, running=True)
    assert "Restart Codex to apply." in on.body
    assert "Claude Code" not in on.body
    default = menubar.notification_copy_for_event(ev)
    assert "Restart Codex to apply." in default.body
    assert "Claude Code" not in default.body


def test_codex_quarantine_toast_says_sign_in_with_codex():
    ev = QuarantineEvent(
        number="1",
        email="a@x.com",
        reason="invalid_grant",
        provider="codex",
    )
    copy = menubar.notification_copy_for_event(ev)
    assert copy is not None
    assert (
        "Sign in with this account in Codex, then click it in the extra."
        in copy.body
    )
    assert "Claude Code" not in copy.body


def test_codex_switch_toast_does_not_use_claude_slot_alias():
    ev = SwitchEvent(
        trigger="proactive",
        from_ref={"number": 1, "email": "codex-a@x.com"},
        to_ref={"number": 2, "email": "codex-b@x.com"},
        provider="codex",
    )
    aliases = {
        "1": "work",
        "2": "personal",
        "codex:1": "codex-one",
        "codex:2": "codex-two",
        "work@x.com": "work",
        "codex-a@x.com": "codex-one",
        "codex-b@x.com": "codex-two",
    }
    copy = menubar.notification_copy_for_event(ev, aliases, running=True)
    assert copy is not None
    assert copy.title == "Switched to codex-two"
    assert "Was codex-one." in copy.body
    assert "work" not in copy.title
    assert "personal" not in copy.title
    assert "work" not in copy.body


def test_codex_switch_toast_falls_back_to_email_not_claude_alias():
    ev = SwitchEvent(
        trigger="proactive",
        from_ref=None,
        to_ref={"number": 1, "email": "codex@x.com"},
        provider="codex",
    )
    copy = menubar.notification_copy_for_event(ev, aliases={"1": "work"})
    assert copy is not None
    assert copy.title == "Switched to codex"
    assert "work" not in copy.title


def test_add_codex_login_starts_codex_autoswitch_if_needed():
    import inspect
    src = inspect.getsource(menubar.run)
    assert "def _ensure_codex_engine" in src
    start = src.index("def on_add_codex_login")
    end = src.index("def on_add_token")
    assert "_ensure_codex_engine" in src[start:end]


def test_codex_enable_starts_codex_autoswitch_if_needed():
    import inspect
    src = inspect.getsource(menubar.run)
    start = src.index("def _make_toggle_disabled")
    end = src.index("def on_add_login")
    assert "_ensure_codex_engine" in src[start:end]


def test_codex_snapshot_retries_codex_autoswitch_start():
    import inspect
    src = inspect.getsource(menubar.run)
    start = src.index("def _worker")
    end = src.index("def _log_usage")
    assert "_ensure_codex_engine" in src[start:end]


def test_worker_scans_codex_running_and_keeps_hint_on_error():
    import inspect
    src = inspect.getsource(menubar.run)
    body = src[src.index("def _worker") : src.index("def _log_usage")]
    assert "get_running_codex_instances" in body
    assert 'snap["codex_running"] = True' in body
    assert "codex_running_line" in body
    assert "format_codex_running_line" in body


def test_notify_switched_gates_codex_restart_on_running():
    import inspect
    src = inspect.getsource(menubar.run)
    body = src[src.index("def _notify_switched") : src.index("def _switch_from_widget")]
    assert "codex_running" in body
    assert "provider=\"codex\"" in body or "provider=provider" in body


def test_drain_engine_events_uses_codex_running():
    import inspect
    src = inspect.getsource(menubar.run)
    body = src[
        src.index("def _drain_engine_events") : src.index("def _threshold")
    ]
    assert "codex_running" in body


def test_panel_draws_codex_running_line():
    text = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    assert "codex_running_line" in text
    assert "RUNNING_LINE_H" in text


# --- confirm before switching -----------------------------------------------


def test_settings_page_has_confirm_switch_toggle_defaulting_on():
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(), strategy="best", threshold=90
    )
    row = next(r for r in rows if r["id"] == "confirm_switch")
    assert row["kind"] == "toggle" and row["value"] is True
    ids = [r["id"] for r in rows]
    assert ids.index("confirm_switch") < ids.index("refresh_interval")


def test_confirm_switch_setting_round_trips_and_defaults_on(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    assert menubar.MenuBarSettings.load(path).confirm_switch is True
    menubar.MenuBarSettings(confirm_switch=False).save(path)
    assert menubar.MenuBarSettings.load(path).confirm_switch is False


def test_account_click_confirms_before_switching_for_both_providers():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    click = text[text.index("def _on_account_click") : text.index("def _repair_relogin")]
    codex_branch = click[: click.index("_slot_needs_relogin")]
    claude_branch = click[click.index("_slot_needs_relogin") :]
    assert "_confirm_switch(" in codex_branch
    assert "_confirm_switch(" in claude_branch
    # The re-login repair path has its own dialogs; it must not be gated twice.
    assert claude_branch.index("_repair_relogin(") < claude_branch.index("_confirm_switch(")


# --- store index watch -------------------------------------------------------


def test_sync_tick_refreshes_when_a_store_index_changes():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    sync = text[text.index("def on_sync_tick") : text.index("def _consume_widget_command")]
    assert "_detect_store_change" in sync
    detect = text[text.index("def _detect_store_change") : text.index("def _detect_active_change")]
    assert "store_roster_changed" in detect and "refresh_async" in detect


# --- rename from the extra ---------------------------------------------------


@pytest.mark.parametrize(
    "text, current, expected",
    [
        ("work", None, ("set", "work")),
        ("  work ", None, ("set", "work")),
        ("work", "home", ("set", "work")),
        ("home", "home", ("noop", None)),
        ("", "home", ("clear", None)),
        ("   ", "home", ("clear", None)),
        ("", None, ("noop", None)),
    ],
)
def test_alias_edit_decides_set_clear_or_noop(text, current, expected):
    assert menubar.alias_edit(text, current) == expected


def test_overflow_menu_has_rename_beside_remove_and_disable():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    rebuild = text[text.index("def rebuild_menu") : text.index("def _add_menu")]
    assert "_rename_menu(rumps)" in rebuild
    rename = text[text.index("def _rename_menu") : text.index("def _remove_menu")]
    assert "Rename account" in rename
    make = text[text.index("def _make_rename") : text.index("def _make_remove")]
    assert "alias_edit(" in make and "set_alias(" in make and "unset_alias(" in make




def test_switch_confirm_copy_names_both_ends_and_the_app():
    title, message = menubar.switch_confirm_copy("work", live_name="personal")
    assert title == "Switch to work?"
    assert message == "Claude Code is signed in as personal. Switch it to work?"
    _, codex = menubar.switch_confirm_copy("work", live_name="personal", app="Codex CLI")
    assert codex.startswith("Codex CLI is signed in as personal.")


def test_switch_confirm_copy_without_a_live_account():
    title, message = menubar.switch_confirm_copy("work", live_name=None)
    assert title == "Switch to work?"
    assert message == "Sign Claude Code in as work?"


@pytest.mark.parametrize(
    "enabled, is_active, expected",
    [(True, False, True), (True, True, False), (False, False, False), (False, True, False)],
)
def test_should_confirm_switch_only_for_a_real_switch_with_the_setting_on(enabled, is_active, expected):
    assert menubar.should_confirm_switch(enabled, is_active=is_active) is expected


def test_confirm_switch_names_the_provider_and_reads_the_live_slot():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    body = text[text.index("def _confirm_switch") : text.index("def _live_is_active")]
    # Both branches name the live login from a live read, never the snapshot;
    # a managed Codex slot goes through the roster so its alias is used.
    assert "self.codex.current_account_number()" in body and "Codex CLI" in body
    assert "self.codex.live_identity()" in body
    assert "_name_for_identity(self.switcher.live_identity())" in body
    assert "snapshot" not in body
    # The gate must not trust the cached snapshot: a stale one could call a
    # card active and skip the dialog on a real switch.
    assert "_is_active_row" not in body
    live = text[text.index("def _live_is_active") : text.index("def _repair_relogin")]
    assert "current_account_number()" in live


@pytest.mark.parametrize(
    "strategy, expected",
    [
        (None, "Rotate to the next account?"),
        ("best", "Switch to the account with the most headroom?"),
        ("next-available", "Switch to the next available account?"),
    ],
)
def test_strategy_confirm_copy_names_the_action(strategy, expected):
    title, message = menubar.strategy_confirm_copy(strategy, live_name="work")
    assert title == expected
    assert message == "Claude Code is signed in as work."
    assert menubar.strategy_confirm_copy(strategy, live_name=None)[1] == ""


def test_rotate_and_best_confirm_too():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    body = text[text.index("def _switch(self, strategy)") : text.index("def _make_rename")]
    assert "_confirm_strategy_switch(strategy)" in body


def test_every_settings_row_is_dispatched():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    body = text[text.index("def _on_setting") : text.index("def _popup_overflow")]
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(auto_switch_enabled=True, kickoff_enabled=True),
        strategy="best",
        threshold=90,
    )
    for row in rows:
        if row["kind"] == "group":
            continue
        assert f'"{row["id"]}"' in body, row["id"]


# --- store roster watch ------------------------------------------------------


def _write_index(path: Path, *, alias: str | None = None, stamp: str = "t1", active=1):
    accounts = {"1": {"email": "a@x.com"}, "2": {"email": "b@x.com"}}
    if alias:
        accounts["2"]["alias"] = alias
    path.write_text(json.dumps({
        "activeAccountNumber": active, "lastUpdated": stamp,
        "sequence": [1, 2], "accounts": accounts,
    }))
    os.utime(path, None)


def test_store_roster_changed_primes_silently_then_reports_roster_edits(tmp_path: Path):
    index = tmp_path / "sequence.json"
    _write_index(index)
    seen: dict = {}
    assert menubar.store_roster_changed([index], seen) is False
    assert menubar.store_roster_changed([index], seen) is False
    _write_index(index, alias="work", stamp="t2")
    os.utime(index, (5, 5))
    assert menubar.store_roster_changed([index], seen) is True
    assert menubar.store_roster_changed([index], seen) is False


def test_store_roster_changed_ignores_switch_and_timestamp_only_rewrites(tmp_path: Path):
    # A switch rewrites activeAccountNumber and lastUpdated; the active-slot
    # watcher already covers that, and the app refreshed itself for it.
    index = tmp_path / "sequence.json"
    _write_index(index, active=1, stamp="t1")
    seen: dict = {}
    menubar.store_roster_changed([index], seen)
    _write_index(index, active=2, stamp="t2")
    os.utime(index, (5, 5))
    assert menubar.store_roster_changed([index], seen) is False


def test_store_roster_changed_sees_a_same_size_atomic_rewrite_within_the_same_timestamp(tmp_path: Path):
    # Coarse-timestamp filesystems can give two writes one mtime, and a
    # rename such as work -> home keeps the size; the writers replace the
    # file atomically, so the inode still moves.
    index = tmp_path / "sequence.json"
    _write_index(index, alias="work")
    seen: dict = {}
    menubar.store_roster_changed([index], seen)
    stamp = index.stat().st_mtime
    fresh = tmp_path / "sequence.json.tmp"
    _write_index(fresh, alias="home")
    os.replace(fresh, index)
    os.utime(index, (stamp, stamp))
    assert menubar.store_roster_changed([index], seen) is True


def test_store_roster_changed_treats_appearing_vanishing_and_corrupt_as_changes(tmp_path: Path):
    index = tmp_path / "sequence.json"
    seen: dict = {}
    assert menubar.store_roster_changed([index], seen) is False
    _write_index(index)
    assert menubar.store_roster_changed([index], seen) is True
    index.write_text("{not json")
    os.utime(index, (5, 5))
    assert menubar.store_roster_changed([index], seen) is True
    index.unlink()
    assert menubar.store_roster_changed([index], seen) is True
    assert menubar.store_roster_changed([index], seen) is False


# --- card row suffix ---------------------------------------------------------


@pytest.mark.parametrize(
    "win, stale, expected",
    [
        ({"countdown": "4d 17h", "maxed": True, "ahead": False}, False, "4d 17h"),
        ({"countdown": "", "maxed": True, "ahead": False}, False, "max"),
        ({"countdown": "", "maxed": False, "ahead": True}, False, "ahead"),
        ({"countdown": "2h", "maxed": False, "ahead": True}, False, "2h"),
        ({"countdown": "", "maxed": True, "ahead": True}, True, ""),
        ({"countdown": "2h", "maxed": True, "ahead": True}, True, "2h"),
        ({}, False, ""),
    ],
)
def test_window_suffix_prefers_the_reset_countdown(win, stale, expected):
    assert menubar.window_suffix(win, stale=stale) == expected


def test_card_rows_use_window_suffix():
    panel_path = Path(menubar.__file__).with_name("menubar_panel.py")
    assert "window_suffix(win, stale=stale)" in panel_path.read_text(encoding="utf-8")


def test_every_dialog_goes_through_the_above_popover_helper():
    # The popover floats above a modal alert, so a dialog opened while it is
    # shown lands underneath it. _dialog lowers the popover for the dialog's
    # lifetime instead of closing it, so Cancel leaves the user in place.
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    helper = text[text.index("def _dialog") : text.index("def _alert")]
    assert "popover_window()" in helper and "NSNormalWindowLevel" in helper
    assert "finally:" in helper and "setLevel_(level)" in helper
    assert "activateIgnoringOtherApps_" in helper
    assert ".close()" not in helper
    rest = text[: text.index("def _dialog")] + text[text.index("def _show_error") :]
    assert "rumps.alert(" not in rest
    assert "rumps.Window(" not in rest
    assert "activateIgnoringOtherApps_" not in rest
