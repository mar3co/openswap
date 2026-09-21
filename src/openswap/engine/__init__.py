"""OpenSwap account engine — public façade.

Extra, autoswitch, kickoff, and CLI talk only to :class:`Engine`.
"""

from openswap.engine.engine import Engine
from openswap.engine.protocol import AccountEngine
from openswap.engine.notes import (
    ERROR_NOTES,
    KEYRING_SERVICE,
    SENTINEL_NOTES,
    SETUP_TOKEN_SCOPES,
    last_seen_note,
    _format_usage_lines,
)
from openswap.credentials import (
    CLAUDE_CODE_KEYCHAIN_SERVICE,
    SECURITY_SERVICE,
)
from openswap.json_output import (
    USAGE_API_KEY,
    USAGE_FOREIGN_CREDENTIAL,
    USAGE_KEYCHAIN_UNAVAILABLE,
    USAGE_LIVE_CREDENTIAL_MISSING,
    USAGE_NO_CREDENTIALS,
    USAGE_RELOGIN_REQUIRED,
    USAGE_TOKEN_EXPIRED,
)

__all__ = [
    "AccountEngine",
    "Engine",
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
