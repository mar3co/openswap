"""Shared constants and display helpers for the OpenSwap account engine."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import threading
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openswap import macos_keychain

from openswap.exceptions import (
    AccountNotFoundError,
    ConfigError,
    CredentialReadError,
    LockError,
    NotLoggedInError,
    SessionError,
    SwitchError,
    ValidationError,
)
from openswap import oauth, pace
from openswap.claude_locks import claude_config_lock, claude_credentials_lock
from openswap.json_output import (
    SCHEMA_VERSION,
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_LIVE_CREDENTIAL_MISSING,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
    account_ref,
    account_row,
    last_good_usage_fields,
    usage_fields,
    usage_freshness_fields,
)
from openswap.credentials import (  # noqa: F401  (constants re-exported for migrations/tests)
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    LEGACY_BACKUP_SECURITY_SERVICE,
    SECURITY_SERVICE,
    ActiveCredentials,
    CredentialStore,
    looks_like_api_key,
    merge_shared_credential_fields,
    shared_credential_fields,
)
from openswap.fsutil import read_text_with_retry
from openswap.locking import FileLock
from openswap.logging_config import setup_logging
from openswap.models import (
    AccountSnapshot,
    AccountsSnapshot,
    Platform,
    SwitchTransaction,
    get_timestamp,
    normalize_alias,
)
from openswap.printer import (
    abbreviate_path,
    accent,
    bold_accent,
    bolded,
    dimmed,
    entrypoint_label,
    error,
    format_age,
    ide_short_name,
    muted,
    warning,
)
from openswap.paths import (
    get_backup_root,
    get_credentials_path,
    get_default_claude_config_home,
    get_global_config_path,
    get_legacy_backup_root,
    migrate_legacy_backup_dir,
)
from openswap.process_detection import get_running_instances
from openswap import poll_policy
from openswap.settings import load_settings, parse_model_names, settings_path
from openswap.usage_store import (
    FetchRecord,
    UsageEntry,
    UsageStore,
    with_sentinel,
)

# Service name under which the legacy ``keyring`` backend stored per-account
# backup credentials on macOS (kept for the one-time keyring → security migration
# and for the Windows Credential Manager migration).
KEYRING_SERVICE = "claude-code"

# SECURITY_SERVICE and CLAUDE_CODE_KEYCHAIN_SERVICE now live in credentials.py
# (storage concerns); re-exported above for migrations.py and the test suite.

# Setup-tokens are inference-only server-side; wider scopes trigger 403s
# on profile endpoints. Matches Claude Code's CLAUDE_CODE_OAUTH_TOKEN path.
SETUP_TOKEN_SCOPES = ("user:inference",)

# Delay between successive usage-request launches in one collect pass, so N
# accounts never burst the shared usage endpoint from one IP in the same
# instant (request hygiene; see issue #85).
_FETCH_STAGGER_S = 0.25

# Show a "· Xm ago" age note on displayed usage older than this. Inside the
# serve TTL the data is current by design (that is the polling cadence), so
# an age note there would be permanent noise.
_USAGE_AGE_NOTE_S = poll_policy.SERVE_TTL_S


def _pace_marker(window: dict, fetched_at: float | None) -> str:
    """"  (ahead of pace)" when a weekly window is meaningfully ahead of pace, else ""."""
    result = pace.compute_pace(window, fetched_at=fetched_at)
    return "  (ahead of pace)" if result and result.ahead else ""


def _format_usage_lines(usage: dict, fetched_at: float | None = None) -> list[str]:
    # Collect (label, body) rows first, then pad every label to the widest one so
    # per-model names (e.g. "Fable") don't shift the columns of the other lines.
    rows: list[tuple[str, str]] = []
    spend = usage.get("spend")
    if spend:
        used = spend["used"]
        limit = spend["limit"]
        pct = spend["pct"]
        cell = oauth.fresh_reset_strings(spend)
        if cell:
            rows.append(("$$", f"{pct:>3.0f}%   resets {cell[1]:<12}  ${used:,.2f} / ${limit:,.2f}"))
        else:
            rows.append(("$$", f"{pct:>3.0f}%   ${used:,.2f} / ${limit:,.2f}"))
    for label, w in (("5h", usage.get("five_hour")), ("7d", usage.get("seven_day"))):
        if w:
            # Pace only applies to the weekly (7d) window, never 5h (issue #125).
            marker = _pace_marker(w, fetched_at) if label == "7d" else ""
            cell = oauth.fresh_reset_strings(w)
            if cell:
                countdown, clock = cell
                rows.append((label, f"{w['pct']:>3.0f}%   resets {clock:<12}  in {countdown}{marker}"))
            else:
                rows.append((label, f"{w['pct']:>3.0f}%{marker}"))
    for w in usage.get("scoped") or []:
        # Per-model weekly limits (e.g. Fable). Flag ones at/over the limit so a
        # maxed model — the usual reason to switch — stands out.
        marker = "  (!)" if w["pct"] >= 100 else _pace_marker(w, fetched_at)
        cell = oauth.fresh_reset_strings(w)
        if cell:
            countdown, clock = cell
            rows.append((w["name"], f"{w['pct']:>3.0f}%   resets {clock:<12}  in {countdown}{marker}"))
        else:
            rows.append((w["name"], f"{w['pct']:>3.0f}%{marker}"))
    width = max((len(label) for label, _ in rows), default=0) + 1  # label + ':'
    return [f"{label + ':':<{width}} {body}" for label, body in rows]


# Stash reasons that mean the slot was NOT freshened, so `error is None`
# would lie to the caller. The other two are excluded deliberately: a REMOVED
# slot has nothing left to activate, and a CAS CONFLICT left the slot holding
# a racing writer's newer valid lineage — freshened, which is the opposite of
# what this demotion denies. An UNREADABLE store is neither: the CAS could not
# be evaluated at all, so the slot may still hold the spent generation.
_DEMOTING_STASH_REASONS = (
    "consume-gate-persist-failed",
    "consume-gate-persist-lock-failed",
    "consume-gate-unpersisted",
    "consume-gate-store-unreadable",
)

# Friendly text for error KINDS that deserve an explanation beyond their
# identifier (rendered in the "usage unavailable (…)" detail line).
ERROR_NOTES = {
    "store-unmirrored": (
        "CLAUDE_SECURESTORAGE_CONFIG_DIR set — unset it or run from a "
        "normal shell"
    ),
    "invalid_client": (
        "openswap's OAuth client was rejected — systemic, not this account"
    ),
    "consume-busy": (
        "another openswap surface holds the slot — retries next pass"
    ),
    "stash-unreadable": (
        "this slot's stashed successor is unreadable — unlock the keychain "
        "or fix the file, then retry; `openswap unclaimed` inspects it"
    ),
}

# Human notes for sentinel usage states (fallback: the raw sentinel string).
# Public: the extra and ``openswap list`` render the same wording so both
# surfaces describe a state identically (e.g. owned-and-expired means Claude
# Code will refresh, not that the user must re-login).
SENTINEL_NOTES = {
    USAGE_TOKEN_EXPIRED: "token expired — refresh deferred this pass; retries automatically",
    USAGE_FOREIGN_CREDENTIAL: "live credential belongs to another account — a switch repairs it",
    USAGE_API_KEY: "API key (no quota)",
    USAGE_KEYCHAIN_UNAVAILABLE: "keychain unavailable — locked or in use; try again",
    USAGE_RELOGIN_REQUIRED: "re-login needed — refresh token dead; log in with Claude Code, then run: openswap add",
    USAGE_LIVE_CREDENTIAL_MISSING: "live login missing — saved credential is available to restore",
    USAGE_NO_CREDENTIALS: "no credentials — sign in with Claude Code",
}


def last_seen_note(entry: UsageEntry) -> str | None:
    """"last seen 53% used · 12m ago" from an entry's last-good measurement.

    Public: the extra renders the same note under sentinel states (see
    ``SENTINEL_NOTES``), so both surfaces stay word-for-word identical.
    """
    if entry.last_good is None or entry.fetched_at is None:
        return None
    headroom = oauth.account_headroom(entry.last_good)
    if headroom is None:
        return None
    return (
        f"last seen {100 - headroom:.0f}% used · "
        f"{format_age(int(entry.fetched_at * 1000))}"
    )


def _usage_entry_lines(entry: UsageEntry) -> list[str]:
    """Styled usage lines (sans indent) for one account's entry.

    Sentinel states render their note first, with a supplementary "last seen"
    line when an older measurement exists. Measurements render as usual, age-
    annotated once older than ``_USAGE_AGE_NOTE_S`` (stale-served); an account
    with no measurement at all shows "usage unavailable" plus the last fetch
    error, so a failing endpoint is visible instead of a silent blank.
    """
    if entry.sentinel is not None:
        out = [dimmed(SENTINEL_NOTES.get(entry.sentinel, entry.sentinel))]
        last_seen = last_seen_note(entry)
        if last_seen is not None and entry.sentinel != USAGE_API_KEY:
            out.append(f"{dimmed('└')} {muted(last_seen)}")
        return out
    if entry.last_good is not None:
        lines = _format_usage_lines(entry.last_good, entry.fetched_at)
        if (
            lines
            and entry.age_s is not None
            and entry.age_s > _USAGE_AGE_NOTE_S
            and entry.fetched_at is not None
        ):
            lines[-1] += f" · {format_age(int(entry.fetched_at * 1000))}"
        return [
            f"{dimmed('└' if j == len(lines) - 1 else '├')} {muted(line)}"
            for j, line in enumerate(lines)
        ]
    detail = "usage unavailable"
    if entry.last_error:
        detail += f" ({ERROR_NOTES.get(entry.last_error, entry.last_error)})"
    return [dimmed(detail)]


def _label_token_status(source: str, credentials: str) -> str | None:
    """Return ``oauth.build_token_status`` relabelled by credential source."""
    status = oauth.build_token_status(credentials)
    if status is None:
        return None
    prefix = "oauth: "
    if status.startswith(prefix):
        return f"{source}: {status.removeprefix(prefix)}"
    return f"{source}: {status}"


def _same_directory(left: Path, right: Path) -> bool:
    """Whether two paths name the same directory, symlinks and ``..`` included.

    Resolved rather than compared as strings: a ``$HOME`` reached through a
    symlink spells the same directory two ways, and the caller is deciding
    which profile a path belongs to — not deriving a keychain service name,
    where claude hashes the raw value and resolving would be wrong.
    """
    try:
        return left.resolve() == right.resolve()
    except OSError:  # unreadable mount / permission — compare as written
        return left == right


class UnreadCredentials:
    """Idle backup not loaded this snapshot pass.

    Truthy (unlike ``""``), so ``if not creds`` does not treat it as empty.
    Distinct from a missing backup: the Ads Online "no credentials" trap was
    classifying unread idle slots as empty.
    """

    def __repr__(self) -> str:
        return "UNREAD_CREDENTIALS"


UNREAD_CREDENTIALS = UnreadCredentials()


def _sweep_legacy_keyring(usernames: list[str], removed_items: list[str]) -> None:
    """Best-effort purge of legacy ``KEYRING_SERVICE`` entries via ``keyring``.

    Used only during ``purge()`` to mop up entries a never-completed
    keyring → file/security migration left behind. Never raises: keyring being
    unavailable or an entry being absent just means nothing to clean up.
    """
    try:
        import keyring  # noqa: PLC0415 - legacy cleanup only

        for username in usernames:
            try:
                keyring.delete_password(KEYRING_SERVICE, username)
                removed_items.append(f"Legacy keyring credential: {username}")
            except Exception:
                pass  # Doesn't exist / other error — ignore
    except Exception:
        pass  # keyring unavailable — nothing to clean up






__all__ = [
    'annotations',
    'dataclasses',
    'json',
    'logging',
    'os',
    're',
    'shutil',
    'threading',
    'sys',
    'time',
    'ThreadPoolExecutor',
    'Path',
    'macos_keychain',
    'AccountNotFoundError',
    'ConfigError',
    'NotLoggedInError',
    'CredentialReadError',
    'LockError',
    'SessionError',
    'SwitchError',
    'ValidationError',
    'oauth',
    'pace',
    'claude_config_lock',
    'claude_credentials_lock',
    'SCHEMA_VERSION',
    'USAGE_API_KEY',
    'USAGE_FOREIGN_CREDENTIAL',
    'USAGE_KEYCHAIN_UNAVAILABLE',
    'USAGE_LIVE_CREDENTIAL_MISSING',
    'USAGE_NO_CREDENTIALS',
    'USAGE_RELOGIN_REQUIRED',
    'USAGE_TOKEN_EXPIRED',
    'account_ref',
    'account_row',
    'last_good_usage_fields',
    'usage_fields',
    'usage_freshness_fields',
    'CLAUDE_CODE_KEYCHAIN_SERVICE',
    'LEGACY_BACKUP_SECURITY_SERVICE',
    'SECURITY_SERVICE',
    'ActiveCredentials',
    'CredentialStore',
    'looks_like_api_key',
    'merge_shared_credential_fields',
    'shared_credential_fields',
    'read_text_with_retry',
    'FileLock',
    'setup_logging',
    'AccountSnapshot',
    'AccountsSnapshot',
    'Platform',
    'SwitchTransaction',
    'get_timestamp',
    'normalize_alias',
    'abbreviate_path',
    'accent',
    'bold_accent',
    'bolded',
    'dimmed',
    'entrypoint_label',
    'error',
    'format_age',
    'ide_short_name',
    'muted',
    'warning',
    'get_backup_root',
    'get_credentials_path',
    'get_default_claude_config_home',
    'get_global_config_path',
    'get_legacy_backup_root',
    'migrate_legacy_backup_dir',
    'get_running_instances',
    'poll_policy',
    'load_settings',
    'parse_model_names',
    'settings_path',
    'FetchRecord',
    'UsageEntry',
    'UsageStore',
    'with_sentinel',
    'KEYRING_SERVICE',
    'SETUP_TOKEN_SCOPES',
    '_FETCH_STAGGER_S',
    '_USAGE_AGE_NOTE_S',
    '_pace_marker',
    '_format_usage_lines',
    '_DEMOTING_STASH_REASONS',
    'ERROR_NOTES',
    'SENTINEL_NOTES',
    'last_seen_note',
    '_usage_entry_lines',
    '_label_token_status',
    '_same_directory',
    '_sweep_legacy_keyring',
    'UnreadCredentials',
    'UNREAD_CREDENTIALS',
]
