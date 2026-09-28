"""Scheduled headless ping that starts an idle 5-hour usage window.

The menu bar (or any other host) decides *when* to fire; this module is the
pure due/eligibility policy plus a returning ``claude -p`` invoke. It never
replaces the current process (no POSIX ``exec``) and never writes the default
``~/.claude`` login. Inactive accounts ping under a session profile via
``CLAUDE_CONFIG_DIR``; the live default login is pinged in place so a second
credential copy cannot rotate its refresh token.
"""

from __future__ import annotations

import os
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openswap.codex.auth import CODEX_HOME_ENV
from openswap.exceptions import SessionError
from openswap.session import AUTH_OVERRIDE_ENV_VARS
from openswap import paths
from openswap.settings import load_worker_settings
from openswap.worker.leases import AccountLeaseStore, ReleaseEvidence, stable_account_identity

KICKOFF_PROMPT = "ok"
KICKOFF_TIMEOUT_S = 90.0
KICKOFF_RETRY_BACKOFF_S = 300.0
KICKOFF_RELOGIN_RETRY_BACKOFF_S = 3600.0


@dataclass(frozen=True)
class KickoffResult:
    """One provider/account outcome from a scheduled kickoff pass."""

    provider: str
    account_num: str
    name: str
    success: bool
    error: str = ""


def kickoff_failure_requires_relogin(message: str) -> bool:
    """True when another unattended retry cannot repair the login.

    Keep this deliberately narrow. Generic authentication failures can be
    caused by a temporary service/network problem, while these messages mean
    the locally stored OAuth grant itself needs user action.
    """
    text = " ".join(str(message or "").casefold().split())
    return any(
        marker in text
        for marker in (
            "oauth session expired and could not be refreshed",
            "refresh token dead",
            "invalid_grant",
            "run codex login",
            "please login to codex",
            "please log in to codex",
        )
    )


def kickoff_retry_backoff(results: list[KickoffResult]) -> float:
    """Choose a retry delay appropriate for the failure mode.

    A dead OAuth session cannot improve through rapid retries, so check it at
    most hourly. Other failures retain the short retry used for transient
    process and network errors.
    """
    failures = [result.error for result in results if not result.success]
    if failures and all(kickoff_failure_requires_relogin(err) for err in failures):
        return KICKOFF_RELOGIN_RETRY_BACKOFF_S
    return KICKOFF_RETRY_BACKOFF_S


def kickoff_failure_signature(
    results: list[KickoffResult],
) -> tuple[tuple[str, str, str], ...] | None:
    """Stable identity for failed kickoff state, used to dedupe notices."""
    failures: list[tuple[str, str, str]] = []
    for result in results:
        if result.success:
            continue
        err = result.error
        if kickoff_failure_requires_relogin(err):
            category = "relogin"
        else:
            text = " ".join(str(err or "").casefold().split())
            if "timed out" in text or "timeout" in text:
                category = "timeout"
            elif any(
                marker in text
                for marker in (
                    "network",
                    "connection",
                    "temporarily unavailable",
                    "name resolution",
                    "dns",
                )
            ):
                category = "network"
            else:
                category = text[:120] or "unknown"
        failures.append((result.provider, result.account_num, category))
    return tuple(failures) or None


def kickoff_is_due(
    enabled: bool,
    hour: int,
    minute: int,
    last_date: str,
    now: datetime,
) -> bool:
    """True when a local-time kickoff should run (at most once per local day).

    Does not fire before the scheduled time on a given day, even on first
    enable. Fires at or after that time if today has not been recorded yet.
    """
    if not enabled:
        return False
    try:
        hour_i = int(hour)
        minute_i = int(minute)
    except (TypeError, ValueError):
        return False
    if not (0 <= hour_i <= 23 and 0 <= minute_i <= 59):
        return False
    today = now.date().isoformat()
    if last_date == today:
        return False
    scheduled = now.replace(hour=hour_i, minute=minute_i, second=0, microsecond=0)
    return now >= scheduled


def kickoff_pass_complete(results: list[KickoffResult]) -> bool:
    """True when this pass should mark the local day done.

    An empty pass (nothing eligible) is complete. Any failed ping keeps the
    day open so a later tick can retry.
    """
    return all(result.success for result in results)


def kickoff_backoff_active(*, now: float, retry_after: float | None) -> bool:
    """True while a failed pass is cooling down (avoids a 1s retry/notify loop)."""
    return retry_after is not None and now < retry_after


def kickoff_uses_default_login(*, is_active: bool) -> bool:
    """The live default login must not get a second credential copy."""
    return bool(is_active)


def _as_posix(now: datetime | float | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _five_hour_resets_at_ts(usage: dict | str | None) -> float | None:
    """POSIX timestamp of the 5h ``resets_at``, or None if missing/unparseable."""
    if not isinstance(usage, dict):
        return None
    window = usage.get("five_hour")
    if not isinstance(window, dict):
        return None
    raw = window.get("resets_at")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _kickoff_account_identity(provider: str, selected_home: Path | str | None) -> str:
    """Resolve a kickoff's stable identity from the local OpenSwap roster.

    Slot paths are used only to locate a roster row; the lease identity is
    derived from provider account metadata, never the mutable slot number.
    """
    backup_root = paths.get_backup_root()
    state_file = backup_root / ("codex/sequence.json" if provider == "codex" else "sequence.json")
    try:
        roster = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise SessionError("Cannot verify the account for scheduled kickoff.") from None
    if not isinstance(roster, dict):
        raise SessionError("Cannot verify the account for scheduled kickoff.")
    accounts = roster.get("accounts")
    if not isinstance(accounts, dict):
        raise SessionError("Cannot verify the account for scheduled kickoff.")

    if provider == "claude" and selected_home is None:
        # The no-override kickoff runs against Claude's default profile, whose
        # identity can change outside OpenSwap without updating activeAccountNumber.
        # Read only the public account metadata from the default global config;
        # never inspect credential contents to choose a lease identity.
        try:
            default_config = json.loads(
                paths.get_default_global_config_path().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise SessionError("Cannot verify the account for scheduled kickoff.") from None
        oauth_account = (
            default_config.get("oauthAccount")
            if isinstance(default_config, dict)
            else None
        )
        if not isinstance(oauth_account, dict):
            raise SessionError("Cannot verify the account for scheduled kickoff.")
        email = oauth_account.get("emailAddress")
        organization = oauth_account.get("organizationUuid", "")
        if organization is None:
            organization = ""
        if (
            not isinstance(email, str)
            or not email
            or not isinstance(organization, str)
        ):
            raise SessionError("Cannot verify the account for scheduled kickoff.")

        # Use the engine's canonical (email, organizationUuid) slot semantics,
        # but reject duplicates instead of choosing the first matching row.
        matching_slots = [
            number
            for number, record in accounts.items()
            if isinstance(record, dict)
            and record.get("email") == email
            and record.get("organizationUuid", "") == organization
        ]
        if len(matching_slots) != 1:
            raise SessionError("Cannot verify the account for scheduled kickoff.")
        return stable_account_identity(provider, email, organization)

    if provider == "codex" and selected_home is None:
        # Same reasoning as the Claude branch above, for the default Codex
        # login: resolve identity the way the engine does (CodexEngine
        # current_account_number / _live_slot, via the live auth.json's own
        # OAuth claims), never the roster's possibly-stale activeAccountNumber.
        from openswap.codex.auth import auth_path, codex_home, parse_auth

        try:
            live_text = auth_path(codex_home()).read_text(encoding="utf-8")
        except OSError:
            raise SessionError("Cannot verify the account for scheduled kickoff.") from None
        identity = parse_auth(live_text)
        if identity is None or not (identity.email or identity.account_id):
            raise SessionError("Cannot verify the account for scheduled kickoff.")
        matching_slots = [
            number
            for number, record in accounts.items()
            if isinstance(record, dict)
            and record.get("email") == identity.email
            and record.get("accountId") == identity.account_id
        ]
        if len(matching_slots) != 1 or not identity.account_id:
            raise SessionError("Cannot verify the account for scheduled kickoff.")
        return stable_account_identity(provider, identity.account_id)

    selected_num = str(roster.get("activeAccountNumber") or "")
    if selected_home is not None:
        home = Path(selected_home)
        if provider == "codex":
            expected_parent = (backup_root / "codex" / "slots").resolve()
            try:
                if home.resolve().parent == expected_parent:
                    selected_num = home.name
            except OSError:
                pass
        else:
            from openswap.session import slugify_email

            for number, record in accounts.items():
                if not isinstance(record, dict):
                    continue
                email = str(record.get("email") or "")
                if email and home.name == f"{number}-{slugify_email(email)}":
                    selected_num = str(number)
                    break

    record = accounts.get(selected_num)
    if not isinstance(record, dict):
        raise SessionError("Cannot verify the account for scheduled kickoff.")
    if provider == "codex":
        account_id = record.get("accountId")
        if not isinstance(account_id, str) or not account_id:
            raise SessionError("Cannot verify the account for scheduled kickoff.")
        return stable_account_identity(provider, account_id)
    email = record.get("email")
    organization = record.get("organizationUuid") or ""
    if not isinstance(email, str) or not email:
        raise SessionError("Cannot verify the account for scheduled kickoff.")
    return stable_account_identity(provider, email, str(organization))


def _run_kickoff_with_lease(provider: str, selected_home, run_fn, argv, **kwargs):
    backup_root = paths.get_backup_root()
    if not load_worker_settings(backup_root).enabled:
        # Remote tasks were never opted into, so the worker lease this
        # coordinates with cannot be held by anything else either. Taking one
        # here anyway would let a bare kickoff timeout quarantine an account
        # for a feature nobody turned on, with no worker CLI around to clear
        # it. Match pre-worker behaviour: just run the ping.
        return run_fn(argv, **kwargs)
    store = AccountLeaseStore(backup_root, provider)
    # Identity is resolved only while holding the same provider lock used for
    # lease acquisition and account mutations, closing the snapshot/acquire race.
    with store.mutation_guard() as guard:
        guard.assert_available()
        account_identity = _kickoff_account_identity(provider, selected_home)
        token = guard.acquire(
            job_id=f"kickoff-{uuid.uuid4().hex}",
            account_identity=account_identity,
            worker_pid=os.getpid(),
            worker_epoch=time.time_ns(),
            ttl_s=float(kwargs["timeout"]) + 30.0,
        )
    try:
        result = run_fn(argv, **kwargs)
    except subprocess.TimeoutExpired:
        # subprocess.run() kills and reaps only the direct child; a helper it
        # spawned may still be using the profile, so stopping is not proven.
        # The owner can clear this with `openswap worker lease release`.
        store.mark_uncertain(token, "kickoff_timeout")
        raise
    except OSError:
        store.release(token, ReleaseEvidence.UNLAUNCHED)
        raise
    except BaseException:
        store.mark_uncertain(token, "kickoff_outcome_unknown")
        raise
    # Owner decision (plan 017): for a scheduled kickoff ping, the direct
    # child's normal exit is the stop evidence. A helper it detached could
    # outlive it; that is the same best-effort process-tree limit recorded in
    # the phase-one cancellation-boundary research, accepted here so a routine
    # ping does not leave the account quarantined.
    store.release(token, ReleaseEvidence.CONFIRMED_STOPPED)
    return result


def kickoff_account_eligible(
    *,
    is_api_key: bool,
    usage: dict | str | None = None,
    now: datetime | float | None = None,
) -> bool:
    """Idle OAuth accounts only: skip API keys and currently-open 5h windows.

    A 5h window is open only while its ``resets_at`` is still in the future.
    Stale last-good rows from yesterday (pct > 0, reset already passed) are
    idle again and must be pinged. The account must report a numeric
    ``five_hour`` window first: missing usage is unsupported/unknown, never
    permission to spend quota on a probe.
    """
    if is_api_key:
        return False
    window = usage.get("five_hour") if isinstance(usage, dict) else None
    if not (
        isinstance(window, dict)
        and isinstance(window.get("pct"), (int, float))
    ):
        return False
    resets_at = _five_hour_resets_at_ts(usage)
    if resets_at is not None and resets_at > _as_posix(now):
        return False
    return True


def format_kickoff_time(hour: int, minute: int = 0) -> str:
    """Local-clock label like ``7:00 AM``."""
    h24 = int(hour) % 24
    m = max(0, min(int(minute), 59))
    suffix = "AM" if h24 < 12 else "PM"
    h12 = h24 % 12 or 12
    return f"{h12}:{m:02d} {suffix}"


def kickoff_time_value(hour: int, minute: int = 0) -> str:
    """Stable popup value, ``H:MM`` in 24-hour local time."""
    h24 = int(hour) % 24
    m = max(0, min(int(minute), 59))
    return f"{h24}:{m:02d}"


def kickoff_time_options(hour: int = 7, minute: int = 0) -> list[tuple[str, str]]:
    """Hourly choices for the extra's time popup.

    A saved time that is not on the hour stays in the list so enabling the
    popup does not silently change it. New picks are on the hour.
    """
    items = [
        (kickoff_time_value(h, 0), format_kickoff_time(h, 0)) for h in range(24)
    ]
    current = kickoff_time_value(hour, minute)
    if current not in {value for value, _lab in items}:
        items.append((current, format_kickoff_time(hour, minute)))
        items.sort(key=lambda item: parse_kickoff_time(item[0]) or (99, 0))
    return items


def parse_kickoff_time(text: str) -> tuple[int, int] | None:
    """Parse ``7:30``, ``07:30``, ``7:30 AM``, or ``7 AM`` into ``(hour, minute)``."""
    raw = (text or "").strip().lower()
    if not raw:
        return None
    ampm = None
    if raw.endswith("am") or raw.endswith("pm"):
        ampm = raw[-2:]
        raw = raw[:-2].strip()
    if not raw:
        return None
    if ":" in raw:
        left, _, right = raw.partition(":")
        if not left.isdigit() or not right.isdigit():
            return None
        hour = int(left)
        minute = int(right)
    elif raw.isdigit():
        hour = int(raw)
        minute = 0
    else:
        return None
    if minute > 59:
        return None
    if ampm is not None:
        if hour < 1 or hour > 12:
            return None
        if ampm == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = hour if hour == 12 else hour + 12
    elif hour > 23:
        return None
    return hour, minute


def build_kickoff_argv(claude_bin: str) -> list[str]:
    """``claude -p`` plus the trivial prompt that opens a 5h window."""
    return [claude_bin, "-p", KICKOFF_PROMPT]


def build_kickoff_env(
    session_dir: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Env for a ping: no auth overrides; session dir only for inactive slots."""
    src = os.environ if environ is None else environ
    env = {k: v for k, v in src.items() if k not in AUTH_OVERRIDE_ENV_VARS}
    if session_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(session_dir)
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def invoke_kickoff(
    session_dir: Path | str | None = None,
    *,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
    timeout: float = KICKOFF_TIMEOUT_S,
    environ: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Headless print-mode ping against one account.

    ``session_dir`` is the isolated profile for an inactive slot. ``None``
    pings the live default login (no ``CLAUDE_CONFIG_DIR``) so the backup
    refresh token is not spent a second time.

    Uses a returning ``subprocess.run`` (or the injected ``run``). Never calls
    ``os.execvpe`` / ``os.execvp`` — the menu-bar process must keep running.
    """
    which_fn = shutil.which if which is None else which
    run_fn = subprocess.run if run is None else run
    claude_bin = which_fn("claude")
    if not claude_bin:
        raise SessionError(
            "'claude' was not found on PATH. Install Claude Code first."
        )
    argv = build_kickoff_argv(claude_bin)
    env = build_kickoff_env(session_dir, environ)
    cwd = str(session_dir) if session_dir is not None else None
    return _run_kickoff_with_lease(
        "claude", session_dir, run_fn, argv,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def build_codex_kickoff_argv(codex_bin: str) -> list[str]:
    # A kickoff is intentionally projectless: it sends a trivial prompt only
    # to open the account's reported 5h window. CODEX_HOME is not a Git repo,
    # so make that explicit instead of letting the CLI fail its repo guard.
    return [codex_bin, "exec", "--skip-git-repo-check", KICKOFF_PROMPT]


def build_codex_kickoff_env(home, environ=None) -> dict[str, str]:
    """``CODEX_HOME`` pinned to the slot home; ``None`` pings the live login."""
    src = os.environ if environ is None else environ
    env = {k: v for k, v in src.items() if k != "OPENAI_API_KEY"}
    if home is not None:
        env[CODEX_HOME_ENV] = str(home)
    else:
        env.pop(CODEX_HOME_ENV, None)
    return env


def invoke_codex_kickoff(
    home=None,
    *,
    which=None,
    run=None,
    timeout=KICKOFF_TIMEOUT_S,
    environ=None,
):
    """Headless ``codex exec`` ping against one Codex login.

    ``home`` is the slot ``CODEX_HOME`` for an idle slot. ``None`` pings the
    live login (no ``CODEX_HOME``). Uses a returning ``subprocess.run``.
    """
    which_fn = shutil.which if which is None else which
    run_fn = subprocess.run if run is None else run
    codex_bin = which_fn("codex")
    if not codex_bin:
        raise SessionError(
            "'codex' was not found on PATH. Install Codex CLI first."
        )
    argv = build_codex_kickoff_argv(codex_bin)
    env = build_codex_kickoff_env(home, environ)
    cwd = str(home) if home is not None else None
    return _run_kickoff_with_lease(
        "codex", home, run_fn, argv,
        env=env,
        cwd=cwd,
        # A GUI app can inherit a pipe on stdin. Close it so Codex does not
        # append unrelated input or emit "Reading additional input from stdin".
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
