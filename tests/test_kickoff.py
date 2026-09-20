"""Tests for the scheduled 5-hour-window kickoff helpers."""

from __future__ import annotations

import inspect
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openswap.exceptions import SessionError
from openswap.kickoff import (
    KICKOFF_PROMPT,
    KICKOFF_RELOGIN_RETRY_BACKOFF_S,
    KICKOFF_RETRY_BACKOFF_S,
    build_kickoff_argv,
    build_kickoff_env,
    format_kickoff_time,
    invoke_codex_kickoff,
    invoke_kickoff,
    kickoff_account_eligible,
    kickoff_backoff_active,
    kickoff_failure_requires_relogin,
    kickoff_failure_signature,
    kickoff_is_due,
    kickoff_pass_complete,
    kickoff_retry_backoff,
    kickoff_time_options,
    kickoff_time_value,
    kickoff_uses_default_login,
    parse_kickoff_time,
)
from openswap.session import AUTH_OVERRIDE_ENV_VARS


# --- due-once-per-local-day ----------------------------------------------------

def test_kickoff_due_at_or_after_scheduled_time_if_not_yet_run_today():
    now = datetime(2026, 9, 5, 7, 0, 0)
    assert kickoff_is_due(True, 7, 0, last_date="", now=now)
    later = datetime(2026, 9, 5, 8, 15, 0)
    assert kickoff_is_due(True, 7, 0, last_date="", now=later)


def test_kickoff_does_not_fire_twice_the_same_local_day():
    now = datetime(2026, 9, 5, 9, 0, 0)
    assert not kickoff_is_due(True, 7, 0, last_date="2026-09-05", now=now)


def test_kickoff_does_not_fire_on_first_enable_before_scheduled_time():
    early = datetime(2026, 9, 5, 6, 59, 0)
    assert not kickoff_is_due(True, 7, 0, last_date="", now=early)


def test_kickoff_fires_next_day_after_a_recorded_run():
    nxt = datetime(2026, 9, 6, 7, 0, 0)
    assert kickoff_is_due(True, 7, 0, last_date="2026-09-05", now=nxt)


def test_kickoff_disabled_never_due():
    now = datetime(2026, 9, 5, 7, 0, 0)
    assert not kickoff_is_due(False, 7, 0, last_date="", now=now)


def test_kickoff_custom_minute_is_respected():
    before = datetime(2026, 9, 5, 7, 29, 0)
    at = datetime(2026, 9, 5, 7, 30, 0)
    assert not kickoff_is_due(True, 7, 30, last_date="", now=before)
    assert kickoff_is_due(True, 7, 30, last_date="", now=at)


def test_kickoff_pass_complete_only_when_nothing_failed():
    """Persist last_date after an empty or all-ok pass, never after a failure."""
    assert kickoff_pass_complete([]) is True
    assert kickoff_pass_complete([("personal", True, "")]) is True
    assert kickoff_pass_complete([("a", True, ""), ("b", True, "")]) is True
    assert kickoff_pass_complete([("personal", False, "auth failed")]) is False
    assert kickoff_pass_complete(
        [("personal", True, ""), ("adsonline", False, "timeout")]
    ) is False


def test_kickoff_backoff_active_until_retry_after():
    assert kickoff_backoff_active(now=100.0, retry_after=150.0) is True
    assert kickoff_backoff_active(now=150.0, retry_after=150.0) is False
    assert kickoff_backoff_active(now=151.0, retry_after=150.0) is False
    assert kickoff_backoff_active(now=100.0, retry_after=None) is False


def test_expired_oauth_uses_slow_retry_and_stable_notification_signature():
    first = [
        (
            "personal",
            False,
            "Failed to authenticate: OAuth session expired and could not be refreshed",
        )
    ]
    repeated = [
        (
            "personal",
            False,
            "FAILED TO AUTHENTICATE: OAuth session expired and could not be refreshed",
        )
    ]
    assert kickoff_failure_requires_relogin(first[0][2]) is True
    assert kickoff_retry_backoff(first) == KICKOFF_RELOGIN_RETRY_BACKOFF_S
    assert kickoff_failure_signature(first) == kickoff_failure_signature(repeated)


def test_transient_kickoff_failure_keeps_short_retry():
    results = [("personal", False, "service temporarily unavailable")]
    assert kickoff_failure_requires_relogin(results[0][2]) is False
    assert kickoff_retry_backoff(results) == KICKOFF_RETRY_BACKOFF_S


def test_kickoff_uses_default_login_only_for_the_active_slot():
    assert kickoff_uses_default_login(is_active=True) is True
    assert kickoff_uses_default_login(is_active=False) is False


# --- eligibility ---------------------------------------------------------------

_ELIG_NOW = 1_000_000.0


def _five_hour(pct: float, delta_s: float | None) -> dict:
    window: dict = {"pct": pct}
    if delta_s is not None:
        window["resets_at"] = datetime.fromtimestamp(
            _ELIG_NOW + delta_s, timezone.utc
        ).isoformat()
    return {"five_hour": window}


def test_eligibility_skips_api_key_and_requires_a_reported_window():
    assert not kickoff_account_eligible(is_api_key=True, usage=None, now=_ELIG_NOW)
    assert not kickoff_account_eligible(
        is_api_key=True, usage=_five_hour(0.0, -3600), now=_ELIG_NOW
    )
    assert not kickoff_account_eligible(is_api_key=False, usage=None, now=_ELIG_NOW)
    assert kickoff_account_eligible(
        is_api_key=False, usage=_five_hour(0.0, None), now=_ELIG_NOW
    )


def test_eligibility_rejects_a_weekly_only_plan():
    weekly_only = {"seven_day": {"pct": 46.0}}
    assert not kickoff_account_eligible(
        is_api_key=False,
        usage=weekly_only,
        now=_ELIG_NOW,
    )


def test_eligibility_expired_last_good_is_idle_even_when_pct_positive():
    usage = _five_hour(87.0, -3600)
    assert kickoff_account_eligible(is_api_key=False, usage=usage, now=_ELIG_NOW)


def test_eligibility_open_five_hour_with_future_reset_is_skipped():
    usage = _five_hour(12.0, 3600)
    assert not kickoff_account_eligible(is_api_key=False, usage=usage, now=_ELIG_NOW)


# --- invoke path ---------------------------------------------------------------

def test_invoke_kickoff_print_argv_session_dir_and_returning_subprocess(tmp_path: Path):
    session_dir = tmp_path / "sessions" / "1-a_x.com"
    session_dir.mkdir(parents=True)
    captured: dict = {}

    def fake_which(name: str):
        return "/opt/fake/claude" if name == "claude" else None

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    result = invoke_kickoff(
        session_dir,
        which=fake_which,
        run=fake_run,
        environ={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-test"},
    )

    argv = captured["argv"]
    assert argv[0] == "/opt/fake/claude"
    assert "-p" in argv or "--print" in argv
    assert KICKOFF_PROMPT in argv
    env = captured["kwargs"]["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(session_dir)
    assert "ANTHROPIC_API_KEY" not in env
    for var in AUTH_OVERRIDE_ENV_VARS:
        assert var not in env
    assert captured["kwargs"].get("check") is False
    assert result.returncode == 0
    assert result.stdout == "ok"


def test_invoke_kickoff_source_uses_subprocess_not_exec():
    src = inspect.getsource(invoke_kickoff)
    assert "os.execvpe(" not in src
    assert "os.execvp(" not in src
    assert "os.exec(" not in src
    assert "run_fn(" in src


def test_invoke_codex_kickoff_exec_argv_and_home(tmp_path):
    captured = {}
    def fake_which(name): return "/opt/fake/codex" if name == "codex" else None
    def fake_run(argv, **kw):
        captured["argv"] = list(argv); captured["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    invoke_codex_kickoff(tmp_path, which=fake_which, run=fake_run, environ={"PATH": "/usr/bin", "OPENAI_API_KEY": "sk"})
    assert captured["argv"] == [
        "/opt/fake/codex", "exec", "--skip-git-repo-check", KICKOFF_PROMPT
    ]
    assert captured["kw"]["env"]["CODEX_HOME"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in captured["kw"]["env"]
    assert captured["kw"]["stdin"] is subprocess.DEVNULL

def test_invoke_codex_kickoff_live_login_has_no_codex_home():
    captured = {}
    def fake_run(argv, **kw):
        captured["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    invoke_codex_kickoff(None, which=lambda n: "/opt/fake/codex", run=fake_run,
                         environ={"PATH": "/usr/bin", "CODEX_HOME": "/elsewhere"})
    assert "CODEX_HOME" not in captured["kw"]["env"]
    assert captured["kw"]["cwd"] is None

def test_invoke_codex_kickoff_missing_binary_raises():
    with pytest.raises(SessionError, match="codex"):
        invoke_codex_kickoff(None, which=lambda n: None, run=lambda *a, **k: None)

def test_invoke_codex_kickoff_source_uses_subprocess_not_exec():
    src = inspect.getsource(invoke_codex_kickoff); assert "os.exec" not in src


def test_invoke_kickoff_missing_claude_raises(tmp_path: Path):
    with pytest.raises(SessionError, match="claude"):
        invoke_kickoff(tmp_path, which=lambda _name: None, run=lambda *_a, **_k: None)


def test_invoke_kickoff_default_login_omits_config_dir():
    """Live default login: no second credential copy, no CLAUDE_CONFIG_DIR."""
    captured: dict = {}

    def fake_which(name: str):
        return "/opt/fake/claude" if name == "claude" else None

    def fake_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    result = invoke_kickoff(
        None,
        which=fake_which,
        run=fake_run,
        environ={
            "PATH": "/usr/bin",
            "CLAUDE_CONFIG_DIR": "/tmp/other-session",
            "ANTHROPIC_API_KEY": "sk-test",
        },
    )

    argv = captured["argv"]
    assert "-p" in argv or "--print" in argv
    assert KICKOFF_PROMPT in argv
    env = captured["kwargs"]["env"]
    assert "CLAUDE_CONFIG_DIR" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert captured["kwargs"].get("cwd") in (None, "")
    assert captured["kwargs"].get("check") is False
    assert result.returncode == 0


def test_build_kickoff_argv_is_print_mode():
    argv = build_kickoff_argv("/usr/bin/claude")
    assert argv[0] == "/usr/bin/claude"
    assert "-p" in argv or "--print" in argv
    assert KICKOFF_PROMPT in argv


def test_build_kickoff_env_sets_config_dir_and_scrubs_auth_overrides():
    env = build_kickoff_env(
        "/tmp/session-1",
        environ={
            "PATH": "/usr/bin",
            "ANTHROPIC_API_KEY": "sk-secret",
            "HOME": "/Users/demo",
        },
    )
    assert env["CLAUDE_CONFIG_DIR"] == "/tmp/session-1"
    assert "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/usr/bin"


def test_parse_and_format_kickoff_time():
    assert parse_kickoff_time("7:00") == (7, 0)
    assert parse_kickoff_time("7:30 AM") == (7, 30)
    assert parse_kickoff_time("7 PM") == (19, 0)
    assert parse_kickoff_time("12:00 AM") == (0, 0)
    assert parse_kickoff_time("12:15 PM") == (12, 15)
    assert parse_kickoff_time("nope") is None
    assert format_kickoff_time(7, 0) == "7:00 AM"
    assert format_kickoff_time(19, 30) == "7:30 PM"


def test_kickoff_time_options_are_hourly_and_keep_off_hour_current():
    hourly = kickoff_time_options(7, 0)
    assert len(hourly) == 24
    assert hourly[0] == ("0:00", "12:00 AM")
    assert hourly[7] == ("7:00", "7:00 AM")
    assert hourly[19] == ("19:00", "7:00 PM")
    assert hourly[-1] == ("23:00", "11:00 PM")
    assert kickoff_time_value(7, 0) == "7:00"

    odd = kickoff_time_options(19, 30)
    assert len(odd) == 25
    values = [value for value, _lab in odd]
    assert "19:00" in values
    assert "19:30" in values
    assert "20:00" in values
    assert values.index("19:00") < values.index("19:30") < values.index("20:00")
    assert ("19:30", "7:30 PM") in odd
