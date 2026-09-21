"""Deprecated shim. Prefer :class:`openswap.engine.Engine`.

Kept for one release so existing tests and internal callers that import
``ClaudeAccountSwitcher`` keep working. This module does not construct a
separate implementation — ``ClaudeAccountSwitcher`` *is* ``Engine``.
"""

import os
import shutil

from openswap.engine.notes import FileLock, macos_keychain, _FETCH_STAGGER_S
from openswap.engine import (
    Engine as ClaudeAccountSwitcher,
    ERROR_NOTES,
    KEYRING_SERVICE,
    SENTINEL_NOTES,
    SETUP_TOKEN_SCOPES,
    last_seen_note,
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    SECURITY_SERVICE,
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_LIVE_CREDENTIAL_MISSING,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
    _format_usage_lines,
)

# Names tests historically patched on this module. FileLock / macos_keychain /
# _FETCH_STAGGER_S are rebound here so ``patch("openswap.switcher.X")`` still
# has an attribute to replace; FileLock patches must also hit the mixin
# modules (see tests.conftest.patch_engine_filelock). os/shutil are the
# stdlib modules, so patching ``switcher.shutil.copy`` still reaches salvage.
_ = (os, shutil, FileLock, macos_keychain, _FETCH_STAGGER_S)

__all__ = [
    "ClaudeAccountSwitcher",
    "FileLock",
    "macos_keychain",
    "_FETCH_STAGGER_S",
    "os",
    "shutil",
    "ERROR_NOTES",
    "KEYRING_SERVICE",
    "SENTINEL_NOTES",
    "SETUP_TOKEN_SCOPES",
    "last_seen_note",
    "CLAUDE_CODE_KEYCHAIN_SERVICE",
    "SECURITY_SERVICE",
    "USAGE_API_KEY",
    "USAGE_FOREIGN_CREDENTIAL",
    "USAGE_KEYCHAIN_UNAVAILABLE",
    "USAGE_LIVE_CREDENTIAL_MISSING",
    "USAGE_NO_CREDENTIALS",
    "USAGE_RELOGIN_REQUIRED",
    "USAGE_TOKEN_EXPIRED",
    "_format_usage_lines",
]
