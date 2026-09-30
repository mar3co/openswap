"""Device enrollment stored only in the user's macOS login Keychain."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from openswap import macos_keychain
from openswap.settings import configure_worker_service, load_worker_settings
from openswap.worker.protocol import ProtocolError, fields, text, timestamp, validate_url
from openswap.worker.remote import Transport

SERVICE = "openswap"


@dataclass(frozen=True)
class Enrollment:
    worker_id: str
    device_key: str = field(repr=False)  # never in logs, tracebacks or status output
    expires_at: datetime


def require_macos():
    if sys.platform != "darwin":
        raise ProtocolError("pairing_unsupported_on_this_platform")


def account_name(url):
    return "worker-device:" + hashlib.sha256(validate_url(url).encode()).hexdigest()


def load_enrollment(url: str) -> Enrollment | None:
    require_macos()
    try:
        raw = macos_keychain.get_password(SERVICE, account_name(url))
        if raw is None:
            return None
        data = fields(json.loads(raw), {"worker_id", "device_key", "expires_at"})
        enrollment = Enrollment(text(data["worker_id"]), text(data["device_key"]), timestamp(data["expires_at"]))
        if enrollment.expires_at <= datetime.now(timezone.utc):
            raise ProtocolError("device_expired", 401)
        return enrollment
    except macos_keychain.KEYCHAIN_ERRORS:
        raise ProtocolError("device_key_unavailable") from None
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError("device_key_unavailable") from None


def pair(root: Path, url: str, code: str, *, transport=None) -> str:
    """Invoking this local command is the owner's explicit pairing approval."""
    require_macos()
    url = validate_url(url)
    # A live enrollment (settings name a URL) must be unpaired first. With no
    # configured URL, any item left at this account is an orphan from a reset
    # or an interrupted pairing, and the new key simply replaces it.
    if load_worker_settings(root).control_service_url is not None:
        raise ProtocolError("unpair_before_pairing")
    result = (transport or Transport(url)).request("pair", {"code": text(code)})
    data = fields(result, {"worker_id", "device_key", "expires_at"})
    enrollment = Enrollment(text(data["worker_id"]), text(data["device_key"]), timestamp(data["expires_at"]))
    if enrollment.expires_at <= datetime.now(timezone.utc):
        raise ProtocolError("device_expired", 401)
    try:
        macos_keychain.set_password(SERVICE, account_name(url), json.dumps({
            "worker_id": enrollment.worker_id, "device_key": enrollment.device_key,
            "expires_at": enrollment.expires_at.isoformat(),
        }))
    except macos_keychain.KEYCHAIN_ERRORS:
        raise ProtocolError("device_key_unavailable") from None
    try:
        configure_worker_service(root, url)
    except (OSError, RuntimeError, ValueError):
        # A key must not become an orphan when URL persistence fails.
        try:
            macos_keychain.delete_password(SERVICE, account_name(url))
        except macos_keychain.KEYCHAIN_ERRORS:
            pass
        raise ProtocolError("pairing_settings_unavailable") from None
    return enrollment.worker_id


def unpair(root: Path, url: str | None = None) -> None:
    """Remove the enrollment for the configured URL, or for an explicit ``url``.

    The explicit form also recovers an orphaned Keychain item when settings no
    longer name a URL. The configured URL is cleared only when it is the one
    being unpaired.
    """
    require_macos()
    configured = load_worker_settings(root).control_service_url
    target = validate_url(url) if url is not None else configured
    if target is None:
        return
    try:
        macos_keychain.delete_password(SERVICE, account_name(target))
    except macos_keychain.KEYCHAIN_ERRORS:
        raise ProtocolError("device_key_unavailable") from None
    if configured is not None and account_name(configured) == account_name(target):
        configure_worker_service(root, None)
