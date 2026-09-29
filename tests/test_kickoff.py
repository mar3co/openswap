"""Tests for the scheduled 5-hour-window kickoff helpers."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openswap.exceptions import SessionError
from openswap.kickoff import (
    KICKOFF_PROMPT,
    KICKOFF_RELOGIN_RETRY_BACKOFF_S,
    KICKOFF_RETRY_BACKOFF_S,
    KickoffResult,
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


def _fake_lease_roster(monkeypatch, tmp_path: Path, provider: str) -> Path:
    root = tmp_path / "openswap-data"
    root.mkdir()
    state_dir = root / "codex" if provider == "codex" else root
    state_dir.mkdir(exist_ok=True)
    row = (
        {"accountId": "acct-fake", "email": "fake@example.test"}
        if provider == "codex"
        else {"email": "fake@example.test", "organizationUuid": "org-fake"}
    )
    (state_dir / "sequence.json").write_text(
        json.dumps({"activeAccountNumber": "1", "accounts": {"1": row}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("openswap.kickoff.paths.get_backup_root", lambda: root)
    return root


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
    assert kickoff_pass_complete([KickoffResult("claude", "1", "personal", True)]) is True
    assert kickoff_pass_complete(
        [
            KickoffResult("claude", "1", "personal", True),
            KickoffResult("codex", "codex:1", "work", True),
        ]
    ) is True
    assert kickoff_pass_complete(
        [KickoffResult("claude", "1", "personal", False, "auth failed")]
    ) is False
    assert kickoff_pass_complete(
        [
            KickoffResult("claude", "1", "personal", True),
            KickoffResult("codex", "codex:1", "adsonline", False, "timeout"),
        ]
    ) is False


def test_kickoff_backoff_active_until_retry_after():
    assert kickoff_backoff_active(now=100.0, retry_after=150.0) is True
    assert kickoff_backoff_active(now=150.0, retry_after=150.0) is False
    assert kickoff_backoff_active(now=151.0, retry_after=150.0) is False
    assert kickoff_backoff_active(now=100.0, retry_after=None) is False


def test_expired_oauth_uses_slow_retry_and_stable_notification_signature():
    first = [
        KickoffResult(
            "codex",
            "codex:1",
            "personal",
            False,
            "Failed to authenticate: OAuth session expired and could not be refreshed",
        )
    ]
    repeated = [
        KickoffResult(
            "codex",
            "codex:1",
            "personal",
            False,
            "FAILED TO AUTHENTICATE: OAuth session expired and could not be refreshed",
        )
    ]
    assert kickoff_failure_requires_relogin(first[0].error) is True
    assert kickoff_retry_backoff(first) == KICKOFF_RELOGIN_RETRY_BACKOFF_S
    assert kickoff_failure_signature(first) == kickoff_failure_signature(repeated)


def test_transient_kickoff_failure_keeps_short_retry():
    results = [
        KickoffResult(
            "claude", "1", "personal", False, "service temporarily unavailable"
        )
    ]
    assert kickoff_failure_requires_relogin(results[0].error) is False
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

def test_invoke_kickoff_print_argv_session_dir_and_returning_subprocess(tmp_path: Path, monkeypatch):
    _fake_lease_roster(monkeypatch, tmp_path, "claude")
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
    assert "_run_kickoff_with_lease(" in src


def test_invoke_codex_kickoff_exec_argv_and_home(tmp_path, monkeypatch):
    _fake_lease_roster(monkeypatch, tmp_path, "codex")
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

def test_invoke_codex_kickoff_live_login_has_no_codex_home(tmp_path, monkeypatch):
    _fake_lease_roster(monkeypatch, tmp_path, "codex")
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


def test_invoke_kickoff_default_login_omits_config_dir(tmp_path: Path, monkeypatch):
    """Live default login: no second credential copy, no CLAUDE_CONFIG_DIR."""
    root = _fake_lease_roster(monkeypatch, tmp_path, "claude")
    default_config = tmp_path / "default-claude.json"
    default_config.write_text(
        json.dumps({"oauthAccount": {
            "emailAddress": "fake@example.test",
            "organizationUuid": "org-fake",
        }}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openswap.kickoff.paths.get_default_global_config_path",
        lambda: default_config,
    )
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


# A personal account's roster row may store a null organization.
@pytest.mark.parametrize("organization", ["org-b", None])
def test_live_default_kickoff_leases_detected_identity_not_stale_active_slot(
    tmp_path: Path, monkeypatch, organization
):
    from openswap.settings import update_worker_settings
    from openswap.worker.leases import AccountLeaseStore, stable_account_identity

    root = _fake_lease_roster(monkeypatch, tmp_path, "claude")
    update_worker_settings(root, enabled=True)
    roster_path = root / "sequence.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    roster["accounts"]["2"] = {
        "email": "live-b@example.test",
        "organizationUuid": organization,
    }
    # External Claude login changed to account B, but OpenSwap's remembered
    # active slot remains A. The lease must follow the profile Claude will use.
    roster_path.write_text(json.dumps(roster), encoding="utf-8")
    default_config = tmp_path / "default-claude.json"
    default_config.write_text(
        json.dumps({"oauthAccount": {
            "emailAddress": "live-b@example.test",
            "organizationUuid": organization,
        }}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openswap.kickoff.paths.get_default_global_config_path",
        lambda: default_config,
    )
    captured = {}

    def fake_run(argv, **kwargs):
        captured["lease"] = AccountLeaseStore(root, "claude").current()
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    invoke_kickoff(
        None,
        which=lambda _name: "/opt/fake/claude",
        run=fake_run,
        environ={"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/tmp/other"},
    )

    lease = captured["lease"]
    assert lease is not None
    assert lease.account_identity == stable_account_identity(
        "claude", "live-b@example.test", organization or ""
    )
    assert "CLAUDE_CONFIG_DIR" not in captured["env"]


@pytest.mark.parametrize("explicit_home", [False, True])
def test_live_default_codex_kickoff_leases_detected_identity_not_stale_active_slot(
    tmp_path: Path, monkeypatch, explicit_home: bool
):
    """The default Codex kickoff must resolve identity the way the engine
    does (auth.json's own OAuth claims), not the roster's possibly-stale
    ``activeAccountNumber`` (openswap.codex.engine.CodexEngine._live_slot),
    whether it gets ``None`` or the live home the menu bar passes."""
    from openswap.settings import update_worker_settings
    from openswap.worker.leases import AccountLeaseStore, stable_account_identity
    from tests.test_codex_auth import _auth

    root = _fake_lease_roster(monkeypatch, tmp_path, "codex")
    update_worker_settings(root, enabled=True)
    roster_path = root / "codex" / "sequence.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    roster["accounts"]["2"] = {"email": "live-b@example.test", "accountId": "acc-b"}
    # External Codex login changed to account B, but OpenSwap's remembered
    # active slot remains A. The lease must follow the profile Codex will use.
    roster_path.write_text(json.dumps(roster), encoding="utf-8")
    codex_home_dir = tmp_path / "codex-home"
    codex_home_dir.mkdir()
    (codex_home_dir / "auth.json").write_text(
        _auth(email="live-b@example.test", account_id="acc-b"), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home_dir))
    captured = {}

    def fake_run(argv, **kwargs):
        captured["lease"] = AccountLeaseStore(root, "codex").current()
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    invoke_codex_kickoff(
        codex_home_dir if explicit_home else None,
        which=lambda _name: "/opt/fake/codex", run=fake_run,
        environ={"PATH": "/usr/bin"},
    )

    lease = captured["lease"]
    assert lease is not None
    assert lease.account_identity == stable_account_identity("codex", "acc-b")


@pytest.mark.parametrize(
    "live_identity,duplicate",
    [
        (("unmanaged@example.test", "org-unknown"), False),
        (("fake@example.test", "org-fake"), True),
    ],
)
def test_live_default_kickoff_refuses_unmatched_or_ambiguous_identity(
    tmp_path: Path, monkeypatch, live_identity, duplicate: bool
):
    from openswap.settings import update_worker_settings
    from openswap.worker.leases import AccountLeaseStore

    root = _fake_lease_roster(monkeypatch, tmp_path, "claude")
    update_worker_settings(root, enabled=True)
    roster_path = root / "sequence.json"
    roster = json.loads(roster_path.read_text(encoding="utf-8"))
    if duplicate:
        roster["accounts"]["2"] = dict(roster["accounts"]["1"])
        roster_path.write_text(json.dumps(roster), encoding="utf-8")
    default_config = tmp_path / "default-claude.json"
    default_config.write_text(
        json.dumps({"oauthAccount": {
            "emailAddress": live_identity[0],
            "organizationUuid": live_identity[1],
        }}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openswap.kickoff.paths.get_default_global_config_path",
        lambda: default_config,
    )
    run_called = False

    def fake_run(*_args, **_kwargs):
        nonlocal run_called
        run_called = True

    with pytest.raises(SessionError, match="Cannot verify the account"):
        invoke_kickoff(
            None,
            which=lambda _name: "/opt/fake/claude",
            run=fake_run,
            environ={"PATH": "/usr/bin"},
        )

    assert not run_called
    assert AccountLeaseStore(root, "claude").current() is None


@pytest.mark.parametrize(
    "failure,expected_state",
    [("popen", "released"), ("communicate", "uncertain"), ("injected", "uncertain")],
)
def test_kickoff_oserror_releases_the_lease_only_when_launch_is_disproved(
    tmp_path: Path, monkeypatch, failure: str, expected_state: str
):
    """Only a Popen failure proves the ping never launched; an OSError after
    launch (or from an injected runner that cannot say) stays uncertain."""
    from openswap.settings import update_worker_settings
    from openswap.worker.leases import AccountLeaseStore
    from tests.test_codex_auth import _auth

    root = _fake_lease_roster(monkeypatch, tmp_path, "codex")
    update_worker_settings(root, enabled=True)
    codex_home_dir = tmp_path / "codex-home"
    codex_home_dir.mkdir()
    (codex_home_dir / "auth.json").write_text(
        _auth(email="fake@example.test", account_id="acct-fake"), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home_dir))

    class FakePopen:
        def __init__(self, argv, **kwargs):
            if failure == "popen":
                raise FileNotFoundError(argv[0])

        def communicate(self, timeout=None):
            raise OSError("pipe failed after launch")

        def kill(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def injected(argv, **kwargs):
        raise OSError("unknown launch outcome")

    monkeypatch.setattr("openswap.kickoff.subprocess.Popen", FakePopen)
    with pytest.raises(OSError):
        invoke_codex_kickoff(
            None,
            which=lambda _name: "/opt/fake/codex",
            run=injected if failure == "injected" else None,
            environ={"PATH": "/usr/bin"},
        )

    assert AccountLeaseStore(root, "codex").read_current().state == expected_state


@pytest.mark.skipif(os.name != "posix", reason="fake executable uses a POSIX shebang")
def test_default_kickoff_runner_captures_output_and_confirms_stop(tmp_path: Path, monkeypatch):
    from openswap.settings import update_worker_settings
    from openswap.worker.leases import AccountLeaseStore
    from tests.test_codex_auth import _auth

    root = _fake_lease_roster(monkeypatch, tmp_path, "codex")
    update_worker_settings(root, enabled=True)
    codex_home_dir = tmp_path / "codex-home"
    codex_home_dir.mkdir()
    (codex_home_dir / "auth.json").write_text(
        _auth(email="fake@example.test", account_id="acct-fake"), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home_dir))
    fake = tmp_path / "fake-codex"
    fake.write_text("#!/bin/sh\necho pong\n", encoding="utf-8")
    fake.chmod(0o700)

    result = invoke_codex_kickoff(
        None, which=lambda _name: str(fake), environ={"PATH": "/usr/bin:/bin"}
    )

    assert (result.returncode, result.stdout) == (0, "pong\n")
    lease = AccountLeaseStore(root, "codex").read_current()
    assert lease.state == "released"


def test_kickoff_timeout_quarantines_account_until_owner_confirms_release(
    tmp_path: Path, monkeypatch
):
    """subprocess.run() kills only the direct child on timeout; a helper it
    spawned may still use the profile, so the lease stays uncertain and the
    kickoff is not replayed until the owner confirms and releases it."""
    from openswap.kickoff import paths
    from openswap.settings import update_worker_settings
    from openswap.worker import cli as worker_cli
    from openswap.worker.leases import AccountLeaseStore, LeaseConflictError
    from tests.test_codex_auth import _auth

    root = _fake_lease_roster(monkeypatch, tmp_path, "codex")
    update_worker_settings(root, enabled=True)
    codex_home_dir = tmp_path / "codex-home"
    codex_home_dir.mkdir()
    (codex_home_dir / "auth.json").write_text(
        _auth(email="fake@example.test", account_id="acct-fake"), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home_dir))
    started = []

    def timed_out(argv, **kwargs):
        started.append(list(argv))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    invoke = lambda run: invoke_codex_kickoff(
        None,
        which=lambda _name: "/opt/fake/codex",
        run=run,
        timeout=0.01,
        environ={"PATH": "/usr/bin"},
    )
    with pytest.raises(subprocess.TimeoutExpired):
        invoke(timed_out)
    lease = AccountLeaseStore(root, "codex").current()
    assert lease.state == "uncertain"
    assert lease.reason == "kickoff_timeout"

    def must_not_relaunch(*_args, **_kwargs):
        pytest.fail("kickoff was replayed after an uncertain timeout")

    with pytest.raises(LeaseConflictError):
        invoke(must_not_relaunch)

    # The menu process that holds it is still alive; once the lease has
    # expired, the owner's explicit confirmation releases it.
    assert worker_cli.release_lease(root, "codex")[2] == "lease_not_expired"
    with monkeypatch.context() as expired:
        expired.setattr(AccountLeaseStore, "_now", lambda self: lease.expires_at + 1)
        assert worker_cli.release_lease(root, "codex")[2] == "stop_unproven_confirm_required"
        assert worker_cli.release_lease(root, "codex", confirm_stopped=True)[0] is True

    def relaunch(argv, **kwargs):
        started.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    invoke(relaunch)
    assert len(started) == 2


def test_kickoff_with_worker_disabled_still_refuses_a_leftover_unresolved_lease(
    tmp_path: Path, monkeypatch
):
    from openswap.worker.leases import AccountLeaseStore, LeaseConflictError, stable_account_identity

    root = _fake_lease_roster(monkeypatch, tmp_path, "codex")
    store = AccountLeaseStore(root, "codex")
    token = store.acquire(
        job_id="kickoff-" + "f" * 32,
        account_identity=stable_account_identity("codex", "acct-fake"),
        worker_pid=os.getpid(), worker_epoch=1, ttl_s=60,
    )
    store.mark_uncertain(token, "kickoff_timeout")

    def must_not_run(*_args, **_kwargs):
        pytest.fail("kickoff ran against an unresolved lease")

    with pytest.raises(LeaseConflictError):
        invoke_codex_kickoff(
            None, which=lambda _name: "/opt/fake/codex", run=must_not_run,
            timeout=0.01, environ={"PATH": "/usr/bin"},
        )


def test_kickoff_with_worker_disabled_takes_no_lease_and_timeout_leaves_switching_open(
    tmp_path: Path, monkeypatch
):
    """Remote tasks were never enabled, so a kickoff must behave exactly as
    it did before the worker existed: no lease taken, and a bare timeout
    cannot block switching afterward."""
    from openswap.worker.leases import AccountLeaseStore

    root = _fake_lease_roster(monkeypatch, tmp_path, "codex")
    # Worker settings default to disabled; this test relies on that default.

    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    with pytest.raises(subprocess.TimeoutExpired):
        invoke_codex_kickoff(
            None, which=lambda _name: "/opt/fake/codex", run=timed_out,
            timeout=0.01, environ={"PATH": "/usr/bin"},
        )

    assert AccountLeaseStore(root, "codex").current() is None
    with AccountLeaseStore(root, "codex").mutation_guard() as guard:
        guard.assert_available()  # switching is not blocked


def test_enabling_remote_tasks_is_refused_during_an_unleased_kickoff(
    tmp_path: Path, monkeypatch
):
    """A kickoff with Remote tasks off runs without a lease; enabling the
    worker mid-run would let it lease the same account, so it is refused."""
    from openswap.exceptions import ClaudeSwitchError
    from openswap.settings import load_worker_settings
    from openswap.worker import cli as worker_cli

    root = _fake_lease_roster(monkeypatch, tmp_path, "codex")
    monkeypatch.setattr(
        worker_cli, "install", lambda: pytest.fail("installed during an unleased kickoff")
    )
    refused = []

    def run_and_try_enabling(argv, **kwargs):
        with pytest.raises(ClaudeSwitchError, match="kickoff_in_progress"):
            worker_cli.enable_worker(root)
        refused.append(True)
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    invoke_codex_kickoff(
        None, which=lambda _name: "/opt/fake/codex", run=run_and_try_enabling,
        environ={"PATH": "/usr/bin"},
    )

    assert refused == [True]
    assert load_worker_settings(root).enabled is False


def test_kickoff_waiting_on_an_enable_rereads_policy_and_takes_a_lease(
    tmp_path: Path, monkeypatch
):
    """If enabling wins the unleased-run lock first, the kickoff waits, then
    sees Remote tasks on and takes the leased path."""
    import threading

    from openswap.settings import update_worker_settings
    from openswap.worker.leases import AccountLeaseStore
    from tests.test_codex_auth import _auth

    root = _fake_lease_roster(monkeypatch, tmp_path, "codex")
    codex_home_dir = tmp_path / "codex-home"
    codex_home_dir.mkdir()
    (codex_home_dir / "auth.json").write_text(
        _auth(email="fake@example.test", account_id="acct-fake"), encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home_dir))
    store = AccountLeaseStore(root, "codex")
    holding = threading.Event()
    release = threading.Event()

    def enabling():
        with store.unleased_run(timeout=0) as held:
            assert held
            holding.set()
            time.sleep(0.2)  # the kickoff reads "disabled" and waits on the lock
            update_worker_settings(root, enabled=True)
            release.wait(3)

    enabler = threading.Thread(target=enabling)
    enabler.start()
    assert holding.wait(3)
    captured = {}

    def fake_run(argv, **kwargs):
        captured["lease"] = AccountLeaseStore(root, "codex").current()
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    threading.Timer(0.4, release.set).start()
    invoke_codex_kickoff(
        None, which=lambda _name: "/opt/fake/codex", run=fake_run,
        environ={"PATH": "/usr/bin"},
    )
    enabler.join(3)

    assert captured["lease"] is not None and captured["lease"].state == "active"


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
