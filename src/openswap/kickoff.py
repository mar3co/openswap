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
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from openswap.codex.auth import CODEX_HOME_ENV
from openswap.exceptions import SessionError
from openswap.session import AUTH_OVERRIDE_ENV_VARS

KICKOFF_PROMPT = "ok"
KICKOFF_TIMEOUT_S = 90.0
KICKOFF_RETRY_BACKOFF_S = 300.0
KICKOFF_RELOGIN_RETRY_BACKOFF_S = 3600.0


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


def kickoff_retry_backoff(results: list[tuple[str, bool, str]]) -> float:
    """Choose a retry delay appropriate for the failure mode.

    A dead OAuth session cannot improve through rapid retries, so check it at
    most hourly. Other failures retain the short retry used for transient
    process and network errors.
    """
    failures = [err for _name, ok, err in results if not ok]
    if failures and all(kickoff_failure_requires_relogin(err) for err in failures):
        return KICKOFF_RELOGIN_RETRY_BACKOFF_S
    return KICKOFF_RETRY_BACKOFF_S


def kickoff_failure_signature(
    results: list[tuple[str, bool, str]],
) -> tuple[tuple[str, str], ...] | None:
    """Stable identity for failed kickoff state, used to dedupe notices."""
    failures: list[tuple[str, str]] = []
    for name, ok, err in results:
        if ok:
            continue
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
        failures.append((str(name), category))
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


def kickoff_pass_complete(results: list[tuple[str, bool, str]]) -> bool:
    """True when this pass should mark the local day done.

    An empty pass (nothing eligible) is complete. Any failed ping keeps the
    day open so a later tick can retry.
    """
    return all(ok for _name, ok, _err in results)


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
    return run_fn(
        argv,
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
    return run_fn(
        argv,
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
