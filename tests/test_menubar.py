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
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar
from openswap import menubar_display
from openswap.exceptions import ConfigError
from openswap.worker.leases import LeaseConflictError
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
from openswap.settings import WorkerSettings, update_worker_settings
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
    assert s.chatgpt_switching_enabled is False
    assert s.kickoff_enabled is False


def test_settings_round_trip(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    original = menubar.MenuBarSettings(desktop_features_enabled=True,
        show_account_name=False,
        title_pct="5h",
        refresh_interval=300,
        auto_switch_enabled=True,
        chatgpt_auto_enabled=True,
        chatgpt_switching_enabled=True,
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
    settings = menubar.MenuBarSettings(menu_bar_provider=provider, title_scoped=True,
                                      desktop_features_enabled=True, chatgpt_switching_enabled=True)
    rows = menubar.settings_page_rows(
        settings, strategy="best", threshold=90, section="general"
    )
    by_id = {row["id"]: row for row in rows}
    picker = by_id["menu_bar_provider"]
    assert picker["label"] == "Show"
    assert picker["kind"] == "popup"
    assert picker["value"] == provider
    assert picker["options"] == [
        ("claude", "Claude Code"), ("chatgpt", "ChatGPT"),
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
        ("desktop", "Desktop"),
        ("remote", "Remote"),
    )


def test_settings_page_sections_separate_display_from_provider_automation():
    settings = menubar.MenuBarSettings(desktop_features_enabled=True,
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
    remote = menubar.settings_page_rows(
        settings,
        strategy="best",
        threshold=90,
        has_codex=True,
        worker_paired=True,
        section=menubar.SETTINGS_SECTION_REMOTE,
    )
    desktop = menubar.settings_page_rows(
        settings, strategy="best", threshold=90, has_codex=True,
        section=menubar.SETTINGS_SECTION_DESKTOP, desktop_app="chatgpt",
    )
    desktop_by_id = {row["id"]: row for row in desktop}

    general_ids = {row["id"] for row in general}
    automation_by_id = {row["id"]: row for row in automation}
    remote_ids = {row["id"] for row in remote}
    assert "show_account_name" in general_ids
    assert "auto_switch_enabled" not in general_ids
    assert not any(row_id.startswith("remote_tasks") for row_id in general_ids)
    assert "group_remote_tasks" not in general_ids
    assert remote_ids == {
        "group_remote_tasks", "remote_tasks_setup", "remote_tasks_enabled",
        "remote_tasks_paired_off", "remote_tasks_account", "remote_tasks_web_choice",
        "remote_tasks_status", "remote_tasks_paused", "remote_tasks_stop",
    }
    assert automation_by_id["auto_switch_enabled"]["label"] == (
        "Auto-switch Claude Code accounts"
    )
    assert "chatgpt_switching_enabled" not in automation_by_id
    assert desktop_by_id["chatgpt_switching_enabled"]["label"] == (
        "Enable ChatGPT switching"
    )
    assert desktop_by_id["chatgpt_auto_enabled"]["label"] == (
        "Suggest ChatGPT account switches"
    )
    assert desktop_by_id["chatgpt_auto_enabled"]["disabled"] is True
    ids = [row["id"] for row in desktop]
    assert ids.index("chatgpt_switching_enabled") < ids.index("chatgpt_auto_enabled")
    assert automation_by_id["codex_enabled"]["label"] == (
        "Auto-switch Codex CLI accounts"
    )
    assert automation_by_id["group_policy"]["label"] == "Shared rotation policy"
    assert all(row["section"] == menubar.SETTINGS_SECTION_GENERAL for row in general)
    assert all(
        row["section"] == menubar.SETTINGS_SECTION_AUTOMATION
        for row in automation
    )
    assert all(row["section"] == menubar.SETTINGS_SECTION_REMOTE for row in remote)


def test_remote_tasks_settings_are_opt_in_redacted_and_stop_targets_job_id():
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(),
        strategy="best",
        threshold=90,
        worker_enabled=True,
        worker_paused=True,
        worker_status={
            "process_state": "running",
            "provider": {"available": False, "diagnostic_code": "live_adapter_disabled"},
            "active_job": {
                "job_id": "job-local-7",
                "state": "cancel_requested",
                "task": "sensitive task text",
            },
            "queue_depth": 1,
        },
        section=menubar.SETTINGS_SECTION_REMOTE,
    )
    by_id = {row["id"]: row for row in rows}

    assert by_id["remote_tasks_enabled"]["value"] is True
    assert by_id["remote_tasks_paused"]["value"] is True
    assert by_id["remote_tasks_stop"]["value"] == "job-local-7"
    assert by_id["remote_tasks_stop"]["disabled"] is False
    status = by_id["remote_tasks_status"]["value"]
    assert "provider live_adapter_disabled" in status
    assert "cancel_requested" in status
    assert "sensitive task text" not in status

    off_rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(),
        strategy="best",
        threshold=90,
        section=menubar.SETTINGS_SECTION_REMOTE,
    )
    off = {row["id"]: row for row in off_rows}
    assert off["remote_tasks_enabled"]["value"] is False
    assert off["remote_tasks_paused"]["disabled"] is True
    assert off["remote_tasks_stop"]["disabled"] is True

    available_rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(),
        strategy="best",
        threshold=90,
        worker_enabled=True,
        worker_status={
            "process_state": "running",
            "provider": {"available": True, "diagnostic_code": None},
            "active_job": {"job_id": "job-local-8", "state": "succeeded"},
        },
        section=menubar.SETTINGS_SECTION_REMOTE,
    )
    available = {row["id"]: row for row in available_rows}
    assert "provider available" in available["remote_tasks_status"]["value"]
    assert "job succeeded" in available["remote_tasks_status"]["value"]


def test_worker_status_poll_refreshes_policy_and_discards_stale_policy(
    tmp_path: Path, monkeypatch
):
    from tests.menubar_harness import extract_class

    update_worker_settings(tmp_path, enabled=True, paused=True)
    monkeypatch.setattr(
        "openswap.worker.cli.read_status",
        lambda _root: {
            "process_state": "running",
            "enabled": True,
            "paused": True,
            "active_job": None,
            "queue_depth": 0,
        },
    )
    app_type = extract_class(
        menubar.__file__,
        "MenuBarApp",
        {"_worker_status_worker", "_with_account_picker", "_drain_worker_result"},
        {},
    )
    app = app_type()
    app.switcher = SimpleNamespace(backup_dir=tmp_path)
    app._worker_generation = 4
    app._worker_result_lock = threading.Lock()
    app._worker_status_inflight = True
    app._worker_operation = None
    app._worker_policy = WorkerSettings()
    app._worker_status_cache = {"process_state": "unavailable"}
    app._worker_result = None
    app._panel = None

    # The status read observes policy changed by a separate CLI process and
    # applies snapshot and policy together when the UI drains that generation.
    app._worker_status_worker(4)
    app._drain_worker_result()
    assert app._worker_policy.enabled is True
    assert app._worker_policy.paused is True
    assert app._worker_status_cache["process_state"] == "running"

    # A result from an older status generation cannot overwrite a newer policy.
    latest_policy = WorkerSettings(enabled=False, paused=False)
    app._worker_policy = latest_policy
    app._worker_generation = 5
    app._worker_status_inflight = True
    app._worker_result = (
        4,
        {"process_state": "stale"},
        WorkerSettings(enabled=True, paused=True),
        None,
    )
    app._drain_worker_result()
    assert app._worker_policy is latest_policy
    assert app._worker_status_cache["process_state"] == "running"


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


def test_chatgpt_capability_freshness_and_activation_helpers():
    from openswap.codex.desktop_app import DesktopCapability, capability_is_fresh

    now = 20.0
    checking = DesktopCapability(state="checking", reason="checking")
    missing = DesktopCapability(state="missing", reason="app_missing")
    stopped = DesktopCapability(state="stopped", reason="stopped", observed_at=16.0)
    expired = DesktopCapability(state="running", reason="running", observed_at=14.9)
    assert capability_is_fresh(checking, now=now) is False
    assert capability_is_fresh(missing, now=now) is True
    assert capability_is_fresh(stopped, now=now) is True
    assert capability_is_fresh(expired, now=now) is False
    settings_off = menubar.MenuBarSettings()
    settings_on = menubar.MenuBarSettings(desktop_features_enabled=True, chatgpt_switching_enabled=True)
    assert menubar.chatgpt_manual_activation_allowed(settings_off, stopped, now=now) is False
    assert menubar.chatgpt_manual_activation_allowed(settings_on, expired, now=now) is False
    assert menubar.chatgpt_manual_activation_allowed(settings_on, stopped, now=now) is True
    cards = [{"num": "codex:1", "disabled": False, "title": "Work"}]
    parent_off = menubar.apply_chatgpt_activation(cards, settings_off, stopped, now=now)
    assert parent_off[0].get("activation_disabled") is not True
    assert "activation_disabled" not in cards[0]
    blocked = menubar.apply_chatgpt_activation(cards, settings_on, missing, now=now)
    assert blocked[0]["activation_disabled"] is True
    assert blocked[0]["disabled"] is False
    allowed = menubar.apply_chatgpt_activation(cards, settings_on, stopped, now=now)
    assert allowed[0].get("activation_disabled") is not True
    title, _body = menubar.desktop_switch_confirm_copy("Work", process_state="stopped")
    assert title == "Switch and open ChatGPT?"
    assert menubar.desktop_switch_confirm_ok("stopped") == "Switch and open ChatGPT"
    title, _body = menubar.desktop_switch_confirm_copy("Work", process_state="running")
    assert title == "Restart ChatGPT?"
    assert menubar.desktop_switch_confirm_ok("running") == "Restart ChatGPT"
    copy = menubar.desktop_capability_status_copy(missing)
    assert "ChatGPT" in copy
    assert "/" not in copy
    incompatible = DesktopCapability(state="invalid", reason="incompatible_backend")
    assert menubar.desktop_capability_status_copy(incompatible) == (
        "This ChatGPT build isn’t compatible with switching."
    )
    legacy = DesktopCapability(state="invalid", reason="legacy_chat_app")
    assert menubar.desktop_capability_status_copy(legacy) == (
        "This is the older ChatGPT app. Update ChatGPT to switch accounts."
    )
    no_cli = DesktopCapability(state="invalid", reason="helper_missing")
    assert menubar.desktop_capability_status_copy(no_cli) == (
        "This ChatGPT build is missing its Codex tools."
    )
    retryable = DesktopCapability(state="invalid", reason="probe_failed", observed_at=now)
    assert menubar.desktop_capability_status_copy(retryable) == (
        "ChatGPT compatibility couldn’t be checked."
    )


def test_chatgpt_desktop_config_check_reuses_transaction_policy(tmp_path, monkeypatch):
    from openswap.codex import desktop

    seen = []
    monkeypatch.setattr(desktop, "_config_check", lambda home: seen.append(home))
    assert menubar.chatgpt_desktop_config_check(tmp_path) is True
    assert seen == [tmp_path]

    def reject(_home):
        raise ClaudeSwitchError("secret policy detail")

    monkeypatch.setattr(desktop, "_config_check", reject)
    assert menubar.chatgpt_desktop_config_check(tmp_path) is False


def test_chatgpt_auto_is_persisted_independently_and_exposes_policy():
    settings = menubar.MenuBarSettings(desktop_features_enabled=True,
        auto_switch_enabled=False,
        chatgpt_switching_enabled=True,
        chatgpt_auto_enabled=True,
    )
    rows = menubar.settings_page_rows(
        settings, strategy="best", threshold=90, has_codex=True,
        codex_enabled=False,
    )
    ids = [row["id"] for row in rows]
    assert settings.auto_switch_enabled is False
    assert settings.chatgpt_auto_enabled is True
    assert settings.chatgpt_switching_enabled is True
    by_id = {row["id"]: row for row in rows}
    assert by_id["chatgpt_auto_enabled"]["disabled"] is False
    assert "threshold" in ids
    assert "strategy" in ids
    assert "codex_enabled" not in ids


def test_chatgpt_switching_parent_defaults_off_and_disables_suggestions_toggle():
    settings = menubar.MenuBarSettings(desktop_features_enabled=True)
    rows = menubar.settings_page_rows(
        settings, strategy="best", threshold=90, has_codex=True,
    )
    ids = [row["id"] for row in rows]
    by_id = {row["id"]: row for row in rows}
    assert settings.chatgpt_switching_enabled is False
    assert "chatgpt_switching_enabled" in ids
    assert ids.index("chatgpt_switching_enabled") < ids.index("chatgpt_auto_enabled")
    assert by_id["chatgpt_switching_enabled"]["value"] is False
    assert by_id["chatgpt_auto_enabled"]["disabled"] is True
    assert by_id["chatgpt_auto_enabled"]["value"] is False


def test_legacy_chatgpt_auto_file_migrates_parent_switching_on(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(json.dumps({"chatgpt_auto_enabled": True}), encoding="utf-8")
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded.chatgpt_auto_enabled is True
    assert loaded.chatgpt_switching_enabled is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "chatgpt_switching_enabled" not in raw
    loaded.save(path)
    assert json.loads(path.read_text(encoding="utf-8"))["chatgpt_switching_enabled"] is True


def test_present_chatgpt_switching_false_wins_over_legacy_auto(tmp_path: Path):
    path = tmp_path / "menubar_settings.json"
    path.write_text(
        json.dumps({
            "chatgpt_auto_enabled": True,
            "chatgpt_switching_enabled": False,
        }),
        encoding="utf-8",
    )
    loaded = menubar.MenuBarSettings.load(path)
    assert loaded.chatgpt_auto_enabled is True
    assert loaded.chatgpt_switching_enabled is False


def test_parent_off_does_not_block_codex_rotation_on_stored_child():
    settings = menubar.MenuBarSettings(desktop_features_enabled=True,
        auto_switch_enabled=True,
        chatgpt_switching_enabled=False,
        chatgpt_auto_enabled=True,
    )
    assert menubar.chatgpt_suggestions_effective(settings) is False
    rows = menubar.settings_page_rows(
        settings, strategy="best", threshold=90, has_codex=True, codex_enabled=False,
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id["chatgpt_auto_enabled"]["disabled"] is True
    assert by_id["codex_enabled"]["disabled"] is False
    on = menubar.MenuBarSettings(desktop_features_enabled=True,
        auto_switch_enabled=True,
        chatgpt_switching_enabled=True,
        chatgpt_auto_enabled=True,
    )
    assert menubar.chatgpt_suggestions_effective(on) is True
    on_rows = menubar.settings_page_rows(
        on, strategy="best", threshold=90, has_codex=True, codex_enabled=False,
    )
    assert {row["id"]: row for row in on_rows}["codex_enabled"]["disabled"] is True


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
    assert "chatgpt_suggestions_effective" in body
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


def test_panel_accounts_titles_linked_key_as_api_credit():
    snap = {
        "accounts": [
            (1, "a@x.com", True, _USAGE, _USAGE, "", "", False, None),
            (2, "api-key-2@token.local", False, "api key", None, "", "", False, None),
            (3, "api-key-3@token.local", False, "api key", None, "", "", False, None),
        ],
        "kinds": {"1": "oauth", "2": "api_key", "3": "api_key"},
        "credit_for": {"2": "1"},
    }
    cards = menubar.panel_accounts(snap)
    assert cards[1]["title"] == "API credit"
    assert cards[1]["subtitle"] == "for a@x.com"
    assert cards[1]["credit_for"] == "1"
    assert cards[1]["api_key"] is True
    assert cards[2]["title"] == "personal"
    assert cards[2]["subtitle"] == "api-key-3@token.local"
    assert cards[2]["credit_for"] == ""


def test_panel_accounts_linked_key_alias_wins_as_title():
    snap = {
        "accounts": [
            (1, "a@x.com", True, _USAGE, _USAGE, "", "", False, None),
            (2, "api-key-2@token.local", False, "api key", None, "team credit", "", False, None),
        ],
        "kinds": {"1": "oauth", "2": "api_key"},
        "credit_for": {"2": "1"},
    }
    cards = menubar.panel_accounts(snap)
    assert cards[1]["title"] == "team credit"
    assert cards[1]["subtitle"] == "for a@x.com"


def test_panel_accounts_linked_key_without_parent_row_keeps_own_subtitle():
    snap = {
        "accounts": [
            (2, "api-key-2@token.local", False, "api key", None, "", "", False, None),
        ],
        "kinds": {"2": "api_key"},
        "credit_for": {"2": "9"},
    }
    cards = menubar.panel_accounts(snap)
    assert cards[0]["title"] == "API credit"
    assert cards[0]["subtitle"] == "api-key-2@token.local"


_TOKEN_ROWS = [
    (1, "a@x.com", True, _USAGE, _USAGE, "work", "", False, None),
    (2, "b@x.com", False, _USAGE, _USAGE, "", "Ads Online", False, None),
    (3, "api-key-3@token.local", False, "api key", None, "", "", False, None),
    ("codex:1", "c@x.com", False, _USAGE, _USAGE, "", "", False, None),
]
_TOKEN_KINDS = {"1": "oauth", "2": "oauth", "3": "api_key", "codex:1": "oauth"}


def test_token_entry_state_empty():
    state = menubar.token_entry_state("", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] is None
    assert state["line"] == ""
    assert state["show_parent"] is False
    assert state["can_add"] is False


def test_token_entry_state_api_key_offers_subscription_parents_only():
    state = menubar.token_entry_state("sk-ant-api03-abc", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] == "api_key"
    assert state["line"] == "API key"
    assert state["show_parent"] is True
    assert state["can_add"] is True
    assert state["choices"] == [
        ("", "None (standalone)"),
        ("1", "work · a@x.com"),
        ("2", "Ads Online · b@x.com"),
    ]


def test_token_entry_state_setup_token_hides_parent():
    state = menubar.token_entry_state("sk-ant-oat01-abc", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] == "setup_token"
    assert state["line"] == "Setup token"
    assert state["show_parent"] is False
    assert state["can_add"] is True


def test_token_entry_state_rejects_garbage():
    state = menubar.token_entry_state("hunter2", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] is None
    assert state["line"] == "Not a Claude token"
    assert state["can_add"] is False


def test_token_entry_state_warns_included_credit_does_not_cover_claude_code():
    key = menubar.token_entry_state("sk-ant-api03-abc", _TOKEN_ROWS, _TOKEN_KINDS)
    assert "doesn't cover" in key["credit_hint"]
    assert "Max and Team" in key["credit_hint"]
    assert "spend limit" in key["credit_hint"]
    for text in ("", "sk-ant-oat01-abc", "hunter2"):
        assert menubar.token_entry_state(text, _TOKEN_ROWS, _TOKEN_KINDS)["credit_hint"] == ""


def test_token_entry_state_strips_whitespace():
    state = menubar.token_entry_state("  sk-ant-api03-abc\n", _TOKEN_ROWS, _TOKEN_KINDS)
    assert state["kind"] == "api_key"
    assert state["can_add"] is True


def test_parent_link_choice_maps_the_popup_item_to_a_link():
    from openswap.menubar_display import STALE_PARENT, parent_link_choice
    identities = {"1": ("a@example.com", "org-a")}
    standalone_value = menubar.token_entry_state("sk-ant-api03-abc", _TOKEN_ROWS, _TOKEN_KINDS)["choices"][0][0]
    standalone_item = (standalone_value, *(identities.get(standalone_value) or ("", "")))
    assert parent_link_choice(None, identities) is None
    assert parent_link_choice(standalone_item, identities) is None
    assert parent_link_choice(("1", "a@example.com", "org-a"), identities) == (
        "a@example.com", "org-a"
    )
    assert parent_link_choice(("1", "b@example.com", "org-a"), identities) is STALE_PARENT
    assert parent_link_choice(("1", "a@example.com", "org-a"), {}) is STALE_PARENT


def test_sanitize_token_error_strips_secrets_and_urls():
    assert menubar.sanitize_token_error(
        "Bad key sk-ant-api03-abcDEF_123 see https://x.test/a?b=c"
    ) == "Bad key see"
    assert menubar.sanitize_token_error("x" * 300).endswith("…")
    assert len(menubar.sanitize_token_error("x" * 300)) == 181


def test_add_token_returns_none_on_success_and_copy_on_failure():
    from openswap.exceptions import ValidationError

    app, logger, _logged = _token_app()
    calls = []
    refreshed = []
    app.switcher = SimpleNamespace(
        add_account_from_token=lambda **kw: calls.append(kw), _logger=logger,
    )
    app.refresh_async = lambda: refreshed.append(True)
    assert app._add_token("sk-ant-api03-abc", "", ("a@x.com", "org-1")) is None
    assert calls == [{
        "token": "sk-ant-api03-abc", "email": None, "slot": None,
        "credit_for": ("a@x.com", "org-1"),
    }]
    assert refreshed == [True]

    def boom(**kw):
        raise ValidationError("rejected sk-ant-api03-abc for https://x.test")

    app.switcher = SimpleNamespace(add_account_from_token=boom, _logger=logger)
    assert app._add_token("sk-ant-api03-abc", "me", None) == "rejected for"
    assert refreshed == [True]

    def boom_secret_only(**kw):
        raise ValidationError("sk-ant-api03-abc")

    app.switcher = SimpleNamespace(add_account_from_token=boom_secret_only, _logger=logger)
    assert app._add_token("sk-ant-api03-abc", "", None) == "Could not add this token."
    assert refreshed == [True]


def _token_app():
    from tests.menubar_harness import extract_class

    scope = {
        "ClaudeSwitchError": menubar.ClaudeSwitchError,
        "sanitize_token_error": menubar.sanitize_token_error,
    }
    app = extract_class(menubar.__file__, "MenuBarApp", {"_add_token"}, scope)()
    logged = []
    logger = SimpleNamespace(
        info=lambda *a, **k: logged.append(("info", a, k)),
        error=lambda *a, **k: logged.append(("error", a, k)),
        warning=lambda *a, **k: logged.append(("warning", a, k)),
    )
    return app, logger, logged


def test_add_token_reports_disk_errors_and_logs_them():
    app, logger, logged = _token_app()

    def boom(**kw):
        raise PermissionError("denied /Users/me/.claude.json")

    app.switcher = SimpleNamespace(add_account_from_token=boom, _logger=logger)
    app.refresh_async = lambda: None
    assert app._add_token("sk-ant-api03-abc", "", None) == (
        "Could not save this token (disk or permission error). See openswap.log."
    )
    assert logged == [("error", ("add-from-token failed",), {"exc_info": True})]


def test_add_token_logs_engine_rejection_without_the_token():
    from openswap.exceptions import ValidationError

    app, logger, logged = _token_app()

    def boom(**kw):
        raise ValidationError("rejected sk-ant-api03-abc")

    app.switcher = SimpleNamespace(add_account_from_token=boom, _logger=logger)
    app.refresh_async = lambda: None
    assert app._add_token("sk-ant-api03-abc", "", None) == "rejected"
    assert len(logged) == 1 and logged[0][0] == "info"
    assert "sk-ant" not in repr(logged)


def test_add_token_succeeds_when_refresh_fails():
    app, logger, logged = _token_app()
    app.switcher = SimpleNamespace(add_account_from_token=lambda **kw: None, _logger=logger)

    def bad_refresh():
        raise RuntimeError("refresh broke")

    app.refresh_async = bad_refresh
    assert app._add_token("sk-ant-api03-abc", "", None) is None
    assert [entry[0] for entry in logged] == ["warning"]


def test_adapt_snapshot_carries_credit_for():
    from types import SimpleNamespace
    from openswap.usage_store import UsageEntry

    def acc(number, email, kind, credit_for=""):
        return SimpleNamespace(
            number=number, email=email, is_active=False, usage=UsageEntry(),
            alias="", org_name="", org_uuid="", disabled=False, kind=kind,
            credit_for=credit_for,
        )

    snap = SimpleNamespace(accounts=[acc("1", "a@x.com", "oauth"), acc("2", "k@x", "api_key", "1")])
    adapted = menubar._adapt_snapshot(snap)
    assert adapted["credit_for"] == {"1": "", "2": "1"}


def test_hold_line_on_api_credit():
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="on-api-credit", detail="no subscription account below 90%"),
        active_title="API credit", strategy="best",
    ) == "Holding on API credit: no subscription account below 90%."
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="on-api-credit"), active_title="API credit", strategy="best",
    ) == "Holding on API credit."


def test_hold_line_names_a_dead_api_key():
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="api-key-dead", detail="API key is revoked and no account has room"),
        active_title="API credit", strategy="best",
    ) == "Holding: API key is revoked and no account has room."
    assert menubar.hold_line_from_event(
        NoSwitchEvent(reason="api-key-dead"), active_title="API credit", strategy="best",
    ) == "Holding: this API key no longer works."


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
    assert cards[0]["note"] == menubar.RECONCILE_CARD_NOTE
    assert cards[0]["action_required"] is True
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
    # Taller than the title row, yet still centered on it.
    row_mid = 12.0 + menubar.HEADER_TITLE_H / 2.0
    assert control[1] + control[3] / 2.0 == row_mid
    assert label[1] + label[3] / 2.0 == row_mid


def test_trailing_header_frames_outset_reaches_into_the_margin():
    _, control = menubar.trailing_header_frames(
        390.0, 12.0, (28.0, 28.0), (28.0, 28.0), gap=0.0, outset=3.0
    )
    assert control[0] + control[2] == 390.0 - 12.0 + 3.0


def test_settings_header_frames_share_the_main_header_center_line():
    back, title = menubar.settings_header_frames(12.0, (64.0, 22.0), (54.0, 16.0))
    row_mid = 12.0 + menubar.HEADER_TITLE_H / 2.0
    assert back == (12.0, row_mid - 11.0, 64.0, 22.0)
    assert title[0] == 12.0 + 64.0 + menubar.SETTINGS_HEADER_GAP
    # The title's capitals, not its frame, sit on the button's center.
    assert title[1] + menubar.HEADER_LABEL_CAP_MID == back[1] + back[3] / 2.0


def test_card_list_shows_every_card_when_the_panel_fits():
    assert menubar.card_list_height(600.0, 200.0, 877.0) == (600.0, False)
    assert menubar.card_list_height(677.0, 200.0, 877.0) == (677.0, False)


def test_card_list_scrolls_when_the_panel_would_leave_the_screen():
    # Six accounts with three usage rows each on a 877pt-tall visible frame.
    assert menubar.card_list_height(796.0, 142.0, 877.0) == (735.0, True)


def test_card_list_keeps_a_usable_height_on_a_short_screen():
    floor = menubar.MIN_CARD_LIST_H
    assert menubar.card_list_height(796.0, 142.0, 200.0) == (floor, True)
    assert menubar.card_list_height(floor, 142.0, 200.0) == (floor, False)


def test_card_list_is_uncapped_without_a_screen():
    assert menubar.card_list_height(796.0, 142.0, None) == (796.0, False)


def test_rebuilding_the_open_card_list_keeps_its_scroll_position():
    assert menubar.kept_card_list_offset(240.0, same_provider=True, shown=True) == 240.0


def test_card_list_starts_at_the_top_when_opened_or_on_another_tab():
    assert menubar.kept_card_list_offset(240.0, same_provider=True, shown=False) == 0.0
    assert menubar.kept_card_list_offset(240.0, same_provider=False, shown=True) == 0.0
    assert menubar.kept_card_list_offset(None, same_provider=True, shown=True) == 0.0


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
    rebuild = text[text.index("def rebuild_menu") : text.index("def _claude_desktop_changed")]
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
        text.index("def _reload_main_panel_if_shown") : text.index("def _worker_view_active")
    ]
    assert '== MAIN_PAGE' in reload_fn
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
        [
            menubar.KickoffResult("claude", "1", "personal", True),
            menubar.KickoffResult(
                "codex", "codex:2", "adsonline", False, "auth failed"
            ),
        ]
    )
    assert copy is not None
    text = _combined(copy)
    assert "personal" in text
    assert "adsonline" in text
    assert "Claude" in text
    assert "ChatGPT" in text
    assert "Account-" not in text
    assert "openswap --add-account" not in text


def test_kickoff_notification_drops_noisy_stdin_preamble():
    copy = menubar.notification_copy_for_kickoff(
        [
            menubar.KickoffResult(
                "codex",
                "codex:1",
                "personal",
                False,
                "Reading additional input from stdin...\n"
                "Not inside a trusted directory and --skip-git-repo-check was not specified.",
            )
        ]
    )
    assert copy is not None
    assert copy.title == "Couldn't start ChatGPT 5-hour window"
    assert "stdin" not in copy.body
    assert "trusted directory" in copy.body


def test_kickoff_expired_oauth_notification_is_actionable():
    copy = menubar.notification_copy_for_kickoff(
        [
            menubar.KickoffResult(
                "codex",
                "codex:1",
                "personal",
                False,
                "Failed to authenticate: OAuth session expired and could not be refreshed",
            )
        ]
    )
    assert copy is not None
    assert copy.title == "Couldn't start ChatGPT 5-hour window"
    assert copy.body.startswith("personal:")
    assert "Sign in again" in copy.body
    assert "Codex" in copy.body
    assert "could not be refreshed" not in copy.body


def test_kickoff_action_warning_persists_until_newer_healthy_usage():
    failure = menubar.KickoffResult(
        "codex",
        "codex:1",
        "personal",
        False,
        "OAuth session expired and could not be refreshed",
    )
    warnings = menubar.kickoff_action_required_after_results(
        {}, [failure], now=100.0
    )
    assert warnings == {"codex:1": 100.0}

    cached = (
        "codex:1", "a@x.com", True, _USAGE, _USAGE, "personal", "", False, 99.0
    )
    assert menubar.reconcile_kickoff_action_required(warnings, [cached]) == warnings

    healthy = (*cached[:8], 101.0)
    assert menubar.reconcile_kickoff_action_required(warnings, [healthy]) == {}


def test_successful_kickoff_clears_action_warning_for_only_that_account():
    current = {"1": 90.0, "codex:1": 91.0}
    success = menubar.KickoffResult("codex", "codex:1", "personal", True)
    assert menubar.kickoff_action_required_after_results(
        current, [success], now=100.0
    ) == {"1": 90.0}


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
    assert cards[0]["action_required"] is True
    assert cards[0]["note"] == menubar.RELOGIN_CARD_NOTE
    assert "Action required" in cards[0]["note"]
    assert "OAuth expired" in cards[0]["note"]
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
    assert cards[0]["needs_reconcile"] is True
    assert cards[0]["action_required"] is True
    assert cards[0]["note"] == menubar.RECONCILE_CARD_NOTE


def test_panel_accounts_marks_old_fallback_stale_with_age():
    fetched = _NOW - 3600
    snap = {
        "accounts": [(
            2, "a@x.com", True,
            menubar.SENTINEL_NOTES[USAGE_FOREIGN_CREDENTIAL],
            _USAGE, "adsonline", "Ads Online", False, fetched,
        )]
    }

    card = menubar.panel_accounts(snap, now=_NOW)[0]

    assert card["stale"] is True
    assert card["action_required"] is True
    assert card["note"] == menubar.RECONCILE_CARD_NOTE
    assert all(window["countdown"] is None for window in card["windows"])


def test_panel_accounts_saved_login_is_current_and_actionable():
    from openswap.json_output import USAGE_LIVE_CREDENTIAL_MISSING

    fetched = _NOW - 30
    snap = {
        "accounts": [(
            2, "a@x.com", True,
            menubar.SENTINEL_NOTES[USAGE_LIVE_CREDENTIAL_MISSING],
            _USAGE, "adsonline", "Ads Online", False, fetched,
        )]
    }

    card = menubar.panel_accounts(snap, now=_NOW)[0]

    assert card["needs_restore"] is True
    assert card["action_required"] is True
    assert card["stale"] is False
    assert card["note"] == (
        "Usage verified just now · Click to restore the saved Claude login."
    )
    assert menubar.provider_tab_title([card], "claude") == "Claude Code · Fix login"


def test_store_only_missing_live_login_marks_cached_usage_stale_and_restorable():
    from openswap.json_output import USAGE_NO_CREDENTIALS

    fetched = _NOW - 3600
    snap = {
        "accounts": [(
            2, "a@x.com", True,
            menubar.SENTINEL_NOTES[USAGE_NO_CREDENTIALS],
            _USAGE, "adsonline", "Ads Online", False, fetched,
        )]
    }

    card = menubar.panel_accounts(snap, now=_NOW)[0]

    assert card["needs_restore"] is True
    assert card["action_required"] is True
    assert card["stale"] is True
    assert card["note"] == (
        "Stale · last updated 1h ago · Click to restore the saved Claude login."
    )


def test_missing_saved_login_is_an_actionable_sign_in_state():
    from openswap.json_output import USAGE_NO_CREDENTIALS

    snap = {"accounts": [(
        2, "a@x.com", False,
        menubar.SENTINEL_NOTES[USAGE_NO_CREDENTIALS],
        None, "adsonline", "Ads Online", False, None,
    )]}

    card = menubar.panel_accounts(snap, now=_NOW)[0]

    assert card["action_required"] is True
    assert card["needs_restore"] is False
    assert card["note"] == menubar.MISSING_LOGIN_CARD_NOTE
    assert menubar.provider_tab_title([card], "claude") == "Claude Code · Sign in"


def test_saved_login_usage_becomes_visibly_stale_after_serve_ttl():
    from openswap.json_output import USAGE_LIVE_CREDENTIAL_MISSING

    fetched = _NOW - menubar.SERVE_TTL_S - 1
    snap = {
        "accounts": [(
            2, "a@x.com", True,
            menubar.SENTINEL_NOTES[USAGE_LIVE_CREDENTIAL_MISSING],
            _USAGE, "adsonline", "Ads Online", False, fetched,
        )]
    }

    card = menubar.panel_accounts(snap, now=_NOW)[0]

    assert card["stale"] is True
    assert card["note"].startswith("Stale · last updated 3m ago ·")


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
    assert title == "personal needs a new sign-in"
    msg = menubar.relogin_wrong_account_message(wrong_org)
    assert msg == (
        "personal's saved login has expired, so OpenSwap can't switch to it. "
        "Sign in as personal in the browser window that opens.\n\n"
        "Claude Code is on adsonline right now and will sign out of it. "
        "adsonline stays saved in OpenSwap, so you can switch back later."
    )

    assert wrong_org.reason == "expired"

    no_saved_login = menubar.plan_relogin_click(
        live=("a@x.com", "org-ads"),
        slot=slot,
        slot_name="personal",
        live_name="adsonline",
        reason="missing",
    )
    assert no_saved_login is not None
    assert no_saved_login.kind == "confirm_open_login"
    assert (
        menubar.relogin_wrong_account_title(no_saved_login)
        == "personal needs a sign-in"
    )
    assert menubar.relogin_wrong_account_message(no_saved_login).startswith(
        "OpenSwap has no saved login for personal, so it can't switch to it. "
        "Sign in as personal in the browser window that opens."
    )

    unverified = menubar.plan_relogin_click(
        live=("a@x.com", "org-ads"),
        slot=slot,
        slot_name="personal",
        live_name="adsonline",
        reason="unverified",
    )
    assert unverified is not None
    assert menubar.relogin_wrong_account_message(unverified).startswith(
        "OpenSwap couldn't confirm personal's saved login, so it can't "
        "switch to it."
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


def test_relogin_slot_filter_keeps_provider_credentials_separate():
    relogin = menubar.SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED]
    snapshot = {
        "accounts": [
            ("1", "claude@x.com", False, relogin, None, "", "", False, None),
            (
                "codex:1",
                "codex@x.com",
                False,
                relogin,
                None,
                "",
                "",
                False,
                None,
            ),
        ]
    }
    assert menubar.relogin_slot_nums(snapshot) == {"1", "codex:1"}
    assert menubar.relogin_slot_nums(snapshot, provider="claude") == {"1"}
    assert menubar.relogin_slot_nums(snapshot, provider="codex") == {"codex:1"}


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
    assert copy.title == "Claude · personal signed out"
    assert "openswap" not in copy.body.lower()
    assert "click" in copy.body.lower()
    codex = menubar.notification_copy_for_relogin("work", provider="codex")
    assert codex.title == "ChatGPT · work signed out"
    assert "Codex" in codex.body
    assert "Claude Code" not in codex.body
    captured = menubar.notification_copy_for_relogin_captured("personal")
    assert "personal" in captured.title
    assert "openswap" not in captured.body.lower()


def test_claude_login_copy_names_the_browser_and_the_retry():
    opened = menubar.relogin_login_opened_message("personal")
    assert "browser" in opened
    assert "Terminal" not in opened
    mismatch = menubar.claude_login_mismatch_message("personal", "work")
    assert "signed in as work, not personal" in mismatch
    assert "different account" in menubar.claude_login_mismatch_message("personal", None)
    failed = menubar.claude_login_failed_message(
        "personal", "Opening browser\nError: network down"
    )
    assert failed.endswith("Error: network down")
    assert "could not sign in as personal" in failed
    assert "\n" not in menubar.claude_login_failed_message("personal", "")
    assert "personal" in menubar.claude_login_timed_out_message("personal")
    assert "no account signed in" in menubar.claude_login_no_login_message("personal")


def test_claude_login_result_is_drained_before_auto_capture_and_canceled_on_quit():
    text = Path(menubar.__file__).read_text()
    tick = text[text.index("def on_sync_tick") : text.index("def _consume_widget_command")]
    assert tick.index("_drain_claude_login_result()") < tick.index("_maybe_auto_capture_relogin()")
    quit_ = text[text.index("def on_quit") : text.index("def on_toggle_name")]
    assert "_claude_login.cancel()" in quit_
    assert "launch_claude_login" not in text


def test_resolve_claude_bin_returns_none_when_path_and_extra_dirs_miss(tmp_path):
    assert (
        menubar_display.resolve_claude_bin(
            which=lambda _name: None, extra_dirs=(str(tmp_path),)
        )
        is None
    )


def test_resolve_claude_bin_falls_back_to_extra_dirs(tmp_path):
    candidate = tmp_path / "claude"
    candidate.write_text("#!/bin/sh\n")
    resolved = menubar_display.resolve_claude_bin(
        which=lambda _name: None, extra_dirs=(str(tmp_path),)
    )
    assert resolved == str(candidate)


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


def test_codex_expired_oauth_uses_provider_specific_action_copy():
    snapshot = {
        "accounts": [
            (
                "codex:1",
                "c@x.com",
                True,
                menubar.SENTINEL_NOTES[USAGE_RELOGIN_REQUIRED],
                _USAGE,
                "work",
                "plus",
                False,
                None,
            )
        ]
    }
    card = menubar.provider_cards(menubar.panel_accounts(snapshot), "chatgpt")[0]
    assert card["action_required"] is True
    assert card["note"] == menubar.CODEX_RELOGIN_CARD_NOTE
    assert "Action required" in card["note"]
    assert "Codex" in card["note"]
    assert "Claude Code" not in card["note"]


def test_kickoff_oauth_failure_marks_card_and_provider_tab_for_action():
    snapshot = {
        "accounts": [
            (
                "codex:1",
                "c@x.com",
                True,
                _USAGE,
                _USAGE,
                "work",
                "plus",
                False,
                _NOW - 60,
            )
        ],
        "kickoff_action_required": ["codex:1"],
    }
    cards = menubar.panel_accounts(snapshot, now=_NOW)
    assert cards[0]["needs_relogin"] is True
    assert cards[0]["action_required"] is True
    assert "OAuth expired" in cards[0]["note"]
    assert menubar.provider_tab_title(cards, "chatgpt") == "ChatGPT · Sign in"
    assert menubar.provider_tab_title(cards, "claude") == "Claude Code"


def test_provider_cards_filters_and_transforms_shared_codex_without_mutating():
    cards = [
        {"provider": "claude", "title": "personal", "num": "1"},
        {"provider": "codex", "title": "Codex · plus", "num": "codex:1"},
    ]
    original = [dict(card) for card in cards]
    assert [card["num"] for card in menubar.provider_cards(cards, "claude")] == ["1"]
    chatgpt = menubar.provider_cards(cards, "chatgpt")
    assert chatgpt == [
        {"provider": "chatgpt", "title": "plus", "num": "codex:1", "account_disabled": False}
    ]
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


def test_account_click_routes_login_mismatch_to_explained_repair():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    click = text[text.index("def _on_account_click") : text.index("def _repair_relogin")]
    claude_branch = click[click.index("_slot_needs_relogin") :]
    # A mismatch on the active card must not fall into the silent self-switch.
    assert claude_branch.index("_repair_reconcile(") < claude_branch.index("_confirm_switch(")
    repair = text[text.index("def _repair_reconcile") : text.index("def _capture_relogin")]
    # Look up whose login is live and explain it before touching anything.
    # The engine decides ownership (uuid-first), never display names.
    assert "live_credential_owner(num)" in repair
    # Roster/config errors surface as an alert, like a failed switch.
    assert "self.switcher.live_credential_owner(num)" in repair
    assert repair.index("_run_switch(") < repair.index("live_credential_owner(num)")
    assert "except OSError" in repair
    assert "_name_for_identity" not in repair
    # Already fixed since the card was drawn: refresh, don't prompt.
    assert repair.index('"matches"') < repair.index("self._alert(")
    # Every restore goes through the dialog; no unconfirmed switch path.
    assert '!= "own"' not in repair
    assert repair.index("self._alert(") < repair.index("switch_to(")
    assert "reconcile_dialog_copy(" in repair
    assert repair.index("self._alert(") < repair.index("switch_to(")
    # Unverifiable owner: offer a fresh sign-in instead of a blind restore.
    assert repair.index("force_login=True") < repair.index("switch_to(")
    assert '"repaired"' in repair and "refresh_async()" in repair


def test_reconcile_dialog_names_the_real_owner_and_both_choices():
    title, body, ok = menubar.reconcile_dialog_copy(
        "adsonline", "gomryo@gmail.com",
        owner_email="yohan@virtualshield.com", owner_name="virtualshield",
    )
    assert ok == "Restore adsonline"
    assert "adsonline (gomryo@gmail.com)" in body
    assert "belongs to yohan@virtualshield.com" in body
    assert "click virtualshield's card" in body
    # Switching uses the saved login, which may need a fresh sign-in.
    assert "sign in again" in body
    assert "Claude app" in body


def test_reconcile_dialog_for_unsaved_owner_says_to_add_it():
    _t, body, _ok = menubar.reconcile_dialog_copy(
        "adsonline", "gomryo@gmail.com",
        owner_email="someone@example.com", owner_name=None,
    )
    assert "add it as an account" in body


def test_reconcile_dialog_when_owner_unknown_changes_nothing():
    title, body, ok = menubar.reconcile_dialog_copy(
        "adsonline", "gomryo@gmail.com", owner_email=None, owner_name=None,
    )
    assert title == "Couldn't check Claude Code's login"
    assert "Nothing has been changed" in body
    assert ok == "Sign in as adsonline"


def test_account_click_restores_saved_live_login_before_normal_confirmation():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    click = text[text.index("def _on_account_click") : text.index("def _repair_relogin")]
    start = click.index("_slot_needs_restore")
    restore = click[start:]

    assert "has_live_credentials()" in restore
    assert restore.index("has_live_credentials()") < restore.index("force=True")
    assert "switch_to(" in restore
    assert "force=True" in restore
    assert "force_if_live_missing=True" in restore
    assert "_confirm_switch(" in restore


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
        if row["kind"] in {"group", "status"}:
            continue
        if row["id"] == "desktop_app":
            continue  # local settings navigation, covered by panel tests
        if row["id"] == "claude_desktop_profiles":
            # Native menus need the clicked button as their popover anchor.
            panel = Path(menubar.__file__).with_name("menubar_panel.py").read_text()
            assert 'self._claude_desktop_profiles if row["id"] == "claude_desktop_profiles"' in panel
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


def _claude_login_app():
    import logging
    from tests.menubar_harness import extract_class

    app_type = extract_class(
        menubar.__file__, "MenuBarApp",
        {
            "_on_claude_login_done", "_drain_claude_login_result", "_finish_claude_add",
            "_keep_outgoing_claude_login", "on_add_claude_login", "_on_empty_action",
        },
        dict(vars(menubar)),
    )
    app = app_type()
    app._event_lock = threading.Lock()
    app._claude_login_results = []
    app._auto_captured_nums = set()
    app._claude_login = object()
    app._claude_login_num = "2"
    app.live = None
    app._name_for_num = lambda num: {"2": "personal"}[str(num)]
    app._name_for_identity = lambda ident: "work" if ident == ("w@x.com", "org-w") else None
    app._slot_identity = lambda num: ("a@x.com", "org-1")
    app.switcher = SimpleNamespace(
        live_identity=lambda: app.live, _logger=logging.getLogger("test")
    )
    app.errors = []
    app._show_error = app.errors.append
    app.captured = []
    app.capture_ok = True

    def capture(num, close_panel):
        app.captured.append((num, close_panel))
        return app.capture_ok

    app._capture_relogin = capture
    return app


def test_claude_login_drain_ignores_a_superseded_run_and_captures_the_current_one():
    from openswap.claude_login import LoginResult

    app = _claude_login_app()
    app.live = ("a@x.com", "org-1")
    app._on_claude_login_done(object(), LoginResult(None, False, True, ""))
    app._on_claude_login_done(
        app._claude_login, LoginResult(0, False, False, "Login successful")
    )
    app._drain_claude_login_result()
    assert app.captured == [("2", False)]
    assert "2" in app._auto_captured_nums
    assert app._claude_login is None and app._claude_login_num is None
    assert app.errors == []


def test_claude_login_drain_leaves_auto_capture_free_to_retry_a_failed_capture():
    from openswap.claude_login import LoginResult

    app = _claude_login_app()
    app.live = ("a@x.com", "org-1")
    app.capture_ok = False
    app._on_claude_login_done(app._claude_login, LoginResult(0, False, False, ""))
    app._drain_claude_login_result()
    assert app.captured == [("2", False)]
    assert "2" not in app._auto_captured_nums


def test_claude_login_drain_keeps_waiting_when_only_a_superseded_run_reported():
    from openswap.claude_login import LoginResult

    app = _claude_login_app()
    current = app._claude_login
    app._on_claude_login_done(object(), LoginResult(None, False, True, ""))
    app._drain_claude_login_result()
    assert app._claude_login is current
    assert app.captured == [] and app.errors == []


def test_claude_login_drain_alerts_on_wrong_account_and_failure_without_capturing():
    from openswap.claude_login import LoginResult

    app = _claude_login_app()
    app.live = ("w@x.com", "org-w")
    app._on_claude_login_done(app._claude_login, LoginResult(0, False, False, ""))
    app._drain_claude_login_result()
    assert app.captured == []
    assert "signed in as work, not personal" in app.errors[-1]

    app._claude_login = object()
    app._claude_login_num = "2"
    app.live = ("a@x.com", "org-1")  # the dead token is still the live login
    app._on_claude_login_done(
        app._claude_login, LoginResult(1, False, False, "Error: boom")
    )
    app._drain_claude_login_result()
    assert app.captured == []
    assert app.errors[-1].endswith("Error: boom")
    assert "2" not in app._auto_captured_nums


def test_refresh_requested_while_in_flight_runs_on_the_next_tick():
    from tests.menubar_harness import extract_class

    app_type = extract_class(
        menubar.__file__, "MenuBarApp", {"refresh_async"}, dict(vars(menubar))
    )
    app = app_type()
    app._refreshing = True
    app._desktop_switching = False
    app._refresh_pending = False
    app.refresh_async()
    assert app._refresh_pending is True
    text = Path(menubar.__file__).read_text()
    tick = text[text.index("def on_sync_tick") : text.index("def _consume_widget_command")]
    assert tick.index("_refresh_pending") < tick.index("_poll_login()")


def _claude_add_app():
    add_app = _claude_login_app()
    add_app._claude_login_num = None
    add_app.switcher.current_account_number = lambda: (
        "2" if add_app.live == ("a@x.com", "org-1") else None
    )
    add_app.added = []
    add_app.add_error = None

    def add_account(slot):
        if add_app.add_error is not None:
            raise add_app.add_error
        add_app.added.append(slot)

    add_app.switcher.add_account = add_account
    add_app.notes = []
    add_app._notify = add_app.notes.append
    add_app.refreshes = 0

    def refresh_async():
        add_app.refreshes += 1

    add_app.refresh_async = refresh_async
    add_app.alerts = []
    add_app._alert_credential_read_error = lambda e: add_app.alerts.append(str(e))
    return add_app


def test_claude_add_drain_saves_a_new_account_and_says_who():
    from openswap.claude_login import LoginResult

    app = _claude_add_app()
    app.live = ("w@x.com", "org-w")
    app._on_claude_login_done(app._claude_login, LoginResult(0, False, False, ""))
    app._drain_claude_login_result()
    assert app.added == [None]
    assert app.notes[-1].title == "w@x.com added"
    assert app.refreshes == 1
    assert app.captured == [] and app.errors == []
    assert app._claude_login is None


def test_claude_add_drain_says_when_the_account_was_already_added():
    from openswap.claude_login import LoginResult

    app = _claude_add_app()
    app.live = ("a@x.com", "org-1")
    app._on_claude_login_done(app._claude_login, LoginResult(0, False, False, ""))
    app._drain_claude_login_result()
    assert app.added == [None]
    assert app.notes[-1].title == "personal is already added"
    assert "2" in app._auto_captured_nums


@pytest.mark.parametrize(
    "result, live, expected",
    [
        ((None, True, False, ""), ("a@x.com", "org-1"), "not finished in time"),
        ((1, False, False, "Error: boom"), ("a@x.com", "org-1"), "Error: boom"),
        ((0, False, False, ""), None, "no account signed in"),
    ],
)
def test_claude_add_drain_alerts_without_saving(result, live, expected):
    from openswap.claude_login import LoginResult

    app = _claude_add_app()
    app.live = live
    app._on_claude_login_done(app._claude_login, LoginResult(*result))
    app._drain_claude_login_result()
    assert app.added == [] and app.notes == [] and app.refreshes == 0
    assert expected in app.errors[-1]
    assert "Add account" in app.errors[-1]


def test_claude_add_drain_is_silent_when_canceled():
    from openswap.claude_login import LoginResult

    app = _claude_add_app()
    app._on_claude_login_done(app._claude_login, LoginResult(None, False, True, ""))
    app._drain_claude_login_result()
    assert app.added == [] and app.errors == [] and app.notes == []


def test_claude_add_drain_surfaces_save_errors():
    from openswap.claude_login import LoginResult

    app = _claude_add_app()
    app.live = ("w@x.com", "org-w")
    app.add_error = menubar.ClaudeSwitchError("disk full")
    app._on_claude_login_done(app._claude_login, LoginResult(0, False, False, ""))
    app._drain_claude_login_result()
    assert app.errors == ["disk full"] and app.notes == [] and app.refreshes == 0

    app._claude_login = object()
    app.add_error = menubar.CredentialReadError("Failed to read")
    app._on_claude_login_done(app._claude_login, LoginResult(0, False, False, ""))
    app._drain_claude_login_result()
    assert app.alerts == ["Failed to read"] and app.notes == []

    app._claude_login = object()
    app.add_error = menubar.CredentialMissingError("No credentials found")
    app._on_claude_login_done(app._claude_login, LoginResult(0, False, False, ""))
    app._drain_claude_login_result()
    assert app.errors[-1] == "No credentials found" and app.alerts == ["Failed to read"]


@pytest.mark.parametrize(
    "live, backup_error, confirm, backed_up, started",
    [
        (None, None, None, False, True),
        (("a@x.com", "org-1"), None, None, True, True),
        (("a@x.com", "org-1"), menubar.ClaudeSwitchError("locked"), 1, False, True),
        (("a@x.com", "org-1"), menubar.ClaudeSwitchError("locked"), 0, False, False),
        (("a@x.com", "org-1"), menubar.CredentialReadError("keychain"), None, False, False),
        (("a@x.com", "org-1"), menubar.CredentialMissingError("No credentials"), 1, False, True),
        (("a@x.com", "org-1"), LeaseConflictError("worker lease"), None, False, False),
        (("u@x.com", "org-u"), None, 1, False, True),
        (("u@x.com", "org-u"), None, 0, False, False),
        ("torn", None, 1, False, True),
        ("torn", None, 0, False, False),
    ],
)
def test_claude_add_saves_or_confirms_the_login_it_signs_out(
    live, backup_error, confirm, backed_up, started
):
    app = _claude_add_app()
    app.live = live
    app.add_error = backup_error

    def strict_read(*, strict):
        assert strict
        if live == "torn":
            raise ConfigError("could not be parsed")
        return None if live is None else (*live, "uuid")

    app.switcher._get_current_identity_triple = strict_read
    app._claude_bin = lambda: "/bin/claude"
    asked = []

    def alert(**kw):
        asked.append(kw)
        return confirm

    app._alert = alert
    starts = []
    app._start_claude_login = lambda claude_bin, email, num: starts.append((email, num))
    app.on_add_claude_login(None)
    assert bool(asked) == (confirm is not None)
    if live == "torn":
        assert "could not be parsed" in asked[0]["message"]
    elif asked:
        assert str(backup_error or live[0]) in asked[0]["message"]
    if isinstance(backup_error, LeaseConflictError):
        assert app.errors == ["worker lease"]
    assert app.alerts == (["keychain"] if str(backup_error) == "keychain" else [])
    assert app.added == ([None] if backed_up else [])
    assert starts == ([(None, None)] if started else [])
    assert len(app.notes) == int(started)


def test_claude_empty_state_signs_in_and_saves_through_separate_actions():
    app = _claude_add_app()
    app._desktop_switching = app._refreshing = False
    calls = []
    app.on_add_claude_login = lambda _s: calls.append("sign_in")
    app.on_add_login = lambda _s: calls.append("save")
    app._on_empty_action("claude", "start")
    app._on_empty_action("claude", "add")
    assert calls == ["sign_in", "save"]


def test_claude_add_is_offered_on_the_claude_page_and_in_the_add_menu():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    add_menu = text[text.index("def _add_menu") : text.index("def _rename_menu")]
    assert add_menu.index('"Sign in to Claude Code…"') < add_menu.index('"Save current Claude Code login"')
    assert "on_add_claude=" in text
    panel = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    footer = panel[panel.index('if provider == "claude" and cards:') :]
    footer = footer[: footer.index("else:")]
    assert '"Add account"' in footer and "self._on_add_claude" in footer


# -- Popover chrome: header icons, per-card menu, trimmed More ----------------


def test_trailing_row_frames_pack_right_to_left_in_order():
    frames = menubar.trailing_row_frames(
        390.0, 12.0, [(67.0, 14.0), (26.0, 16.0), (90.0, 22.0)],
        row_y=100.0, row_h=38.0, gap=8.0,
    )
    label, switch, add = frames
    assert add[0] + add[2] == 378.0  # last control hugs the right pad
    assert switch[0] + switch[2] + 8.0 == add[0]
    assert label[0] + label[2] + 8.0 == switch[0]
    # Each control is centered on the row.
    assert add[1] == 100.0 + (38.0 - 22.0) / 2.0
    assert label[1] == 100.0 + (38.0 - 14.0) / 2.0
    assert menubar.trailing_row_frames(390.0, 12.0, [], row_y=0.0, row_h=38.0) == []


def test_card_menu_items_say_what_the_disable_flag_really_does():
    assert menubar.card_menu_items({"disabled": False}, "claude") == [
        ("rename", "Rename…"), ("toggle_disabled", "Disable"), ("remove", "Remove…"),
    ]
    assert menubar.card_menu_items({"disabled": True}, "claude")[1] == ("toggle_disabled", "Enable")
    # Desktop-app tab: the flag only holds the slot out of Codex rotation.
    assert menubar.card_menu_items({"disabled": False}, "chatgpt")[1] == (
        "toggle_disabled", "Hold out of rotation"
    )
    assert menubar.card_menu_items({"disabled": True}, "chatgpt")[1] == (
        "toggle_disabled", "Back in rotation"
    )
    # Codex tab: plain Disable / Enable, like Claude.
    assert menubar.card_menu_items({"disabled": False}, "chatgpt", desktop=False)[1] == (
        "toggle_disabled", "Disable"
    )
    assert menubar.card_menu_items({"disabled": True}, "chatgpt", desktop=False)[1] == (
        "toggle_disabled", "Enable"
    )


def test_panel_accounts_carry_email_and_alias_for_the_card_menu():
    snap = {"accounts": [
        (2, "a@x.com", True, _USAGE, _USAGE, "work", "Acme", False, _NOW - 5),
        (3, "b@x.com", False, _USAGE, _USAGE, None, "Acme", False, _NOW - 5),
    ]}
    cards = menubar.panel_accounts(snap, now=_NOW)
    assert (cards[0]["email"], cards[0]["alias"]) == ("a@x.com", "work")
    assert (cards[1]["email"], cards[1]["alias"]) == ("b@x.com", "")


def test_panel_header_holds_settings_and_more_icons_and_footer_holds_autoswitch():
    text = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    build = text[text.index("    def _build(self):") : text.index("    def _build_settings(self):")]
    header = build[: build.index("inner_w = PANEL_WIDTH - PAD * 2")]
    assert '"gearshape"' in header and "self._show_settings" in header
    assert '"ellipsis.circle"' in header and "self._more" in header
    assert "trailing_header_frames" in header
    # The Auto-switch switch is built in the header block but only placed in
    # the footer row, next to Rotate / Best.
    assert "root.addSubview_(auto)" not in header
    footer = build[build.index("# Footer") :]
    assert "trailing_row_frames" in footer
    assert "[auto_label, auto] if show_auto" in footer
    assert '"Settings"' not in footer and '"More"' not in footer
    icon = text[text.index("def _add_icon_button") : text.index("def _alloc_switch")]
    assert "imageWithSystemSymbolName_accessibilityDescription_" in icon
    assert "setBordered_(bordered)" in icon


def test_card_menu_button_opens_the_account_menu_instead_of_switching():
    text = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    hit = text[text.index("    def hitTest_(self, point):") : text.index("    def mouseUp_(self, event):")]
    assert "menu_button" in hit and "isDescendantOf_" in hit
    build = text[text.index("    def _build(self):") : text.index("    def _build_settings(self):")]
    card = build[build.index("for card in cards:") :]
    assert '"ellipsis"' in card and "card_view.menu_button = menu_btn" in card
    assert "self._card_menu(c, _s)" in card
    menu = text[text.index("def _card_menu") : text.index("def _select_provider")]
    assert "_hold_overflow(True)" in menu and "self._on_card_menu(card, sender)" in menu
    assert "self.close()" not in menu


def test_popup_card_menu_wires_rename_disable_remove_for_that_card():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    assert "on_card_menu=self._popup_card_menu" in text
    body = text[text.index("def _popup_card_menu") : text.index("def _fit_status_item")]
    assert "card_menu_items(" in body and "desktop=chatgpt_tab_desktop(self.settings)" in body
    assert "_make_rename(num" in body and "_make_toggle_disabled(num" in body
    assert "_make_remove(num)" in body
    assert "popUpMenuPositioningItem_atLocation_inView_" in body
    assert "_ns_to_py_and_callback" in body  # same rumps leak guard as rebuild_menu


def test_more_menu_is_management_only_once_the_popover_is_attached():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    rebuild = text[text.index("def rebuild_menu") : text.index("def _add_menu")]
    assert "fallback = self._panel is None" in rebuild
    # Rotate / Best and the per-account submenus stay only for the no-popover
    # fallback; next-available, Add, creds, history, refresh, quit remain.
    gated = rebuild[rebuild.index("self.menu = [") :]
    for name in ("rotate to next", "switch to best", "_rename_menu(rumps)",
                 "_disable_menu(rumps)", "_remove_menu(rumps)", "_desktop_menu(rumps)"):
        assert name in gated
    always = gated.replace("if fallback else []", "")
    assert '"Claude Code: next available"' in always
    assert gated.count("if fallback else []") == 2


# -- Second tab: Codex CLI or the ChatGPT desktop app, by one setting ---------


def test_chatgpt_tab_desktop_follows_the_switching_setting():
    assert menubar.chatgpt_tab_desktop(menubar.MenuBarSettings()) is False
    assert menubar.chatgpt_tab_desktop(
        menubar.MenuBarSettings(desktop_features_enabled=True, chatgpt_switching_enabled=True)
    ) is True
    assert menubar.chatgpt_tab_desktop(None) is False


def test_second_tab_copy_is_codex_when_desktop_switching_is_off():
    assert menubar.provider_tab_title([], "chatgpt", desktop=False) == "Codex"
    assert menubar.provider_tab_title([], "chatgpt") == "ChatGPT"
    assert menubar.provider_tab_title([], "claude", desktop=False) == "Claude Code"
    assert menubar.provider_shared_copy("chatgpt", desktop=False) == ""
    assert menubar.provider_shared_copy("chatgpt") == "Shared with Codex · Codex usage"
    empty = menubar.provider_empty_state("chatgpt", desktop=False)
    assert empty["title"] == "Add a Codex account"
    assert empty["button"] == "Sign in with ChatGPT"
    assert menubar.provider_empty_state("chatgpt")["title"] == "Add a ChatGPT account"
    assert menubar.provider_empty_state("chatgpt", "loading", desktop=False)["title"] == (
        "Loading Codex accounts…"
    )


def test_api_key_rows_stay_switchable_on_the_codex_tab():
    cards = [{"provider": "codex", "title": "Codex · key", "num": "codex:3", "api_key": True}]
    desktop = menubar.provider_cards(cards, "chatgpt")[0]
    assert desktop["disabled"] is True and "CLI-only" in desktop["note"]
    # The desktop-only mark never masquerades as the stored rotation flag.
    assert desktop["account_disabled"] is False
    cli = menubar.provider_cards(cards, "chatgpt", desktop=False)[0]
    assert "disabled" not in cli and "note" not in cli
    assert cli["account_disabled"] is False


def test_card_menu_toggles_the_stored_flag_not_the_tab_mark():
    # A Codex API key on the desktop-app tab is marked disabled for the tab
    # only; its menu must still offer to hold it out of rotation.
    card = {"provider": "chatgpt", "api_key": True, "disabled": True, "account_disabled": False}
    assert menubar.account_disabled(card) is False
    assert menubar.card_menu_items(card, "chatgpt")[1] == ("toggle_disabled", "Hold out of rotation")
    assert menubar.card_menu_items({**card, "account_disabled": True}, "chatgpt")[1] == (
        "toggle_disabled", "Back in rotation"
    )
    # Claude cards carry no tab mark, so the plain flag is the stored one.
    assert menubar.account_disabled({"disabled": True}) is True
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    body = text[text.index("def _popup_card_menu") : text.index("def _fit_status_item")]
    assert "_make_toggle_disabled(num, account_disabled(card))" in body
    # provider_cards relabels Codex rows "chatgpt" before the panel sees them.
    assert 'card.get("provider") in ("codex", "chatgpt")' in body
    assert '_reg.pop(getattr(menu, "_menuitem", None), None)' in body


def test_card_badge_and_suffix_follow_the_tab_mode():
    active = {"active": True}
    assert menubar.card_badge_text(active, "claude") == "active"
    assert menubar.card_badge_text(active, "chatgpt") == "shared"
    assert menubar.card_badge_text(active, "chatgpt", desktop=False) == "active"
    assert menubar.card_badge_text({"active": False}, "chatgpt") == ""
    assert menubar.card_badge_text({"action_required": True, "needs_restore": True}, "claude") == "repair login"
    assert menubar.card_badge_text({"action_required": True}, "chatgpt") == "sign in needed"
    disabled = {"disabled": True}
    assert menubar.card_title_suffix(disabled, "claude") == "disabled"
    assert menubar.card_title_suffix(disabled, "chatgpt") == "out of rotation"
    assert menubar.card_title_suffix(disabled, "chatgpt", desktop=False) == "disabled"
    assert menubar.card_title_suffix({"disabled": True, "api_key": True}, "chatgpt") == "CLI only"
    assert menubar.card_title_suffix({"disabled": True, "api_key": True}, "claude") == "CLI only"
    # The Codex tab is the CLI: a disabled key there is simply disabled.
    assert menubar.card_title_suffix({"disabled": True, "api_key": True}, "chatgpt", desktop=False) == "disabled"
    assert menubar.card_title_suffix({"disabled": False}, "chatgpt") == ""


def test_switching_setting_explains_what_the_second_tab_does():
    rows = menubar.settings_page_rows(
        menubar.MenuBarSettings(desktop_features_enabled=True), strategy="best", threshold=90.0, has_codex=True,
    )
    ids = [row["id"] for row in rows]
    assert ids.index("chatgpt_switching_enabled") < ids.index("chatgpt_switching_hint") < ids.index("chatgpt_auto_enabled")
    hint = next(row for row in rows if row["id"] == "chatgpt_switching_hint")
    assert hint["style"] == "hint"
    assert hint["label"] == menubar.CHATGPT_SWITCHING_HINT
    assert "Codex CLI" in hint["label"] and "ChatGPT app" in hint["label"]


def test_panel_second_tab_toggle_and_copy_follow_the_mode():
    text = (Path(menubar.__file__).resolve().parent / "menubar_panel.py").read_text(
        encoding="utf-8"
    )
    build = text[text.index("    def _build(self):") : text.index("    def _build_settings(self):")]
    assert "desktop_tab = chatgpt_tab_desktop(settings)" in build
    for call in (
        "provider_cards(all_cards, provider, desktop=desktop_tab)",
        "provider_tab_title(all_cards, tab_provider, desktop=desktop_tab)",
        "provider_shared_copy(provider, desktop=desktop_tab)",
        "card_title_suffix(card, provider, desktop=desktop_tab)",
        "card_badge_text(card, provider, desktop=desktop_tab)",
    ):
        assert call in build, call
    toggle = build[build.index('if provider == "chatgpt" and desktop_tab:') : build.index("auto_label = _label(")]
    assert '"Suggest switches"' in toggle and "self._on_toggle_chatgpt_auto" in toggle
    assert "self._codex_enabled()" in toggle and "self._on_toggle_codex_auto" in toggle
    # Codex rotation rides on the Claude auto-switch engine, so the Codex tab's
    # toggle is hidden (no callback) and reads off until that master is on,
    # exactly like the Settings row (settings_page_rows gates on it too).
    assert "auto_cb = self._on_toggle_codex_auto if master else None" in toggle
    assert "auto_on = master and" in toggle and "master = bool(self._auto_enabled())" in toggle
    # The desktop status line only belongs to the desktop-app tab.
    assert 'if (provider == "chatgpt" and desktop_tab) else ""' in build
    assert "'paused'" not in build and '"selected"' not in build


def test_codex_tab_click_switches_codex_cli_instead_of_asking_to_enable():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    click = text[text.index("def _on_panel_account_click") : text.index("def _slot_needs_relogin")]
    # Expired logins are refused in both modes, before any switch path.
    assert click.index("if self._slot_needs_relogin(num):") < click.index("if not desktop_tab:")
    off = click[click.index("if not desktop_tab:") : click.index("if not self._chatgpt_switching_persisted():")]
    assert "self._on_account_click(num, close_panel=True)" in off
    # The tab's mode comes from the in-memory setting the panel also reads; a
    # persisted mismatch falls back to the Settings offer, never a CLI switch.
    assert "desktop_tab = chatgpt_tab_desktop(self.settings)" in click
    assert "_offer_enable_chatgpt_switching" in click
    toggle = text[text.index("def _toggle_codex_auto") : text.index("def _popup_overflow")]
    assert "autoswitch.codexEnabled" in toggle and "_reload_main_panel_if_shown" in toggle
    assert "on_toggle_codex_auto=lambda *_a: self._toggle_codex_auto()" in text
    setting = text[text.index("def _on_setting") : text.index("def _toggle_codex_auto")]
    assert "self._toggle_codex_auto()" in setting


def test_card_menu_edits_reload_the_open_popover_once_the_refresh_lands():
    text = Path(menubar.__file__).read_text(encoding="utf-8")
    init = text[text.index("self._hold_reload_pending = False") :][:400]
    assert "self._reload_after_refresh = False" in init
    helper = text[text.index("def _refresh_and_reload_cards") : text.index("def on_add_login")]
    assert "self._reload_after_refresh = True" in helper and "self.refresh_async()" in helper
    rename = text[text.index("def _make_rename") : text.index("def _make_remove")]
    assert "self._refresh_and_reload_cards()" in rename and "self.refresh_async()" not in rename
    toggle = text[text.index("def _make_toggle_disabled") : text.index("def _refresh_and_reload_cards")]
    assert "self._refresh_and_reload_cards()" in toggle and "self.refresh_async()" not in toggle
    # The worker turns that request into the deferred main-page reload that
    # the 1s tick honors (hold_panel_reload_plan), and clears it.
    worker = text[text.index("def _worker(self, full):") : text.index("def _config_mtime_or_none")]
    tail = worker[worker.index("finally:") :]
    # An edit queued behind an in-flight pass keeps its request for the
    # queued pass; the running worker must not spend it on the old snapshot.
    assert "edit_landed = self._reload_after_refresh and not self._refresh_pending" in tail
    assert "or edit_landed" in tail
    assert "self._reload_after_refresh = False" in tail and "self._hold_reload_pending = True" in tail
