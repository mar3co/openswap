"""Device enrollment stored only in the user's macOS login Keychain."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unicodedata

from openswap import macos_keychain
from openswap.exceptions import LockError
from openswap.locking import FileLock
from openswap.settings import configure_worker_service, load_worker_settings
from openswap.worker.protocol import ProtocolError, fields, text, timestamp, validate_url
from openswap.worker.remote import Transport

SERVICE = "openswap"
# Connect extension: labels a `pair` response carries only when the request
# sent a `code_verifier`. A pasted code keeps the exact closed shape.
CONNECT_LABELS = frozenset({"workspace_label", "owner_label"})
MAX_PAIR_LABEL = 200


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


def _pairing_lock(root: Path) -> FileLock:
    """Serializes whole pair/unpair transactions across processes, so a key and the
    worker ID recorded in settings always come from the same enrollment."""
    return FileLock(Path(root) / ".worker-pairing.lock", timeout=15.0)


def _locked(root: Path, operation, *args, **kwargs):
    try:
        with _pairing_lock(root):
            return operation(root, *args, **kwargs)
    except LockError:
        raise ProtocolError("pairing_in_progress") from None


def _drop_status(root: Path):
    """Called once the configured enrollment has changed: a new enrollment (including
    a renewal that keeps the URL and worker ID) starts without the old one's saved
    status. A refused or failed command, or one that only removed an orphan, keeps it."""
    try:
        (Path(root) / "worker" / "remote-status.json").unlink(missing_ok=True)
    except OSError:
        pass


class PairedWorker(str):
    """The paired worker ID. A pairing redeemed with a ``code_verifier`` (Connect)
    also carries the service's ``workspace_label`` and ``owner_label``, if sent."""

    workspace_label: str | None
    owner_label: str | None

    def __new__(cls, worker_id: str, workspace_label: str | None = None, owner_label: str | None = None):
        value = super().__new__(cls, worker_id)
        value.workspace_label, value.owner_label = workspace_label, owner_label
        return value


def pair(root: Path, url: str, code: str, *, transport=None, code_verifier: str | None = None,
         cancelled=None) -> PairedWorker:
    """Invoking this local command is the owner's explicit pairing approval.

    ``code_verifier`` is the PKCE verifier of a Connect flow (never logged or
    stored); only then may the response add ``workspace_label`` and ``owner_label``.
    ``cancelled`` (Connect's Cancel) is checked before the code is redeemed and
    again before the key is saved; the save is the point of no return.
    """
    require_macos()
    return _locked(root, _pair, url, code, transport=transport, code_verifier=code_verifier, cancelled=cancelled)


def _check_cancelled(cancelled) -> None:
    if cancelled is not None and cancelled():
        raise ProtocolError("connect_cancelled")


def _redeem(url: str, code: str, transport, code_verifier: str | None) -> tuple[Enrollment, dict]:
    request = {"code": text(code)}
    if code_verifier is not None:
        request["code_verifier"] = text(code_verifier, 128)
    result = (transport or Transport(url)).request("pair", request)
    data = fields(result, {"worker_id", "device_key", "expires_at"},
                  CONNECT_LABELS if code_verifier is not None else frozenset())
    enrollment = Enrollment(text(data["worker_id"]), text(data["device_key"]), timestamp(data["expires_at"]))
    if enrollment.expires_at <= datetime.now(timezone.utc):
        raise ProtocolError("device_expired", 401)
    labels = {name: _display_label(data.get(name)) for name in CONNECT_LABELS}
    return enrollment, {name: label for name, label in labels.items() if label is not None}


def _display_label(value: object) -> str | None:
    """A response label made safe to show, or None.

    Labels are display-only and arrive after the service has consumed the code
    and issued the key, so an odd one (a long workspace name, a control
    character, a non-string) must never fail the pairing: control characters
    are dropped and the rest is cut to ``MAX_PAIR_LABEL``."""
    if not isinstance(value, str):
        return None
    label = "".join(c for c in value if unicodedata.category(c) != "Cc").strip()[:MAX_PAIR_LABEL].strip()
    return label or None


def _store(url: str, enrollment: Enrollment) -> None:
    try:
        macos_keychain.set_password(SERVICE, account_name(url), json.dumps({
            "worker_id": enrollment.worker_id, "device_key": enrollment.device_key,
            "expires_at": enrollment.expires_at.isoformat(),
        }))
    except macos_keychain.KEYCHAIN_ERRORS:
        raise ProtocolError("device_key_unavailable") from None


def _record_key(root: Path, url: str, enrollment: Enrollment):
    """Start the new enrollment's saved status with its key expiry (never the key),
    so status and the menu know when it ends even before the worker first runs,
    or while it stays off. A failed write only loses that hint."""
    from openswap.worker.remote_state import save_status

    try:
        save_status(Path(root), url, "offline", None, enrollment.worker_id,
                    key_expires_at=enrollment.expires_at)
    except (OSError, RuntimeError, ValueError):
        pass


def _pair(root: Path, url: str, code: str, *, transport=None, code_verifier: str | None = None,
          cancelled=None) -> PairedWorker:
    url = validate_url(url)
    # A live enrollment (settings name a URL) must be unpaired first. With no
    # configured URL, any item left at this account is an orphan from a reset
    # or an interrupted pairing, and the new key simply replaces it.
    if load_worker_settings(root).control_service_url is not None:
        raise ProtocolError("unpair_before_pairing")
    _check_cancelled(cancelled)
    enrollment, labels = _redeem(url, code, transport, code_verifier)
    _check_cancelled(cancelled)
    _store(url, enrollment)
    try:
        configure_worker_service(root, url, enrollment.worker_id)
    except (OSError, RuntimeError, ValueError):
        # A key must not become an orphan when URL persistence fails.
        try:
            macos_keychain.delete_password(SERVICE, account_name(url))
        except macos_keychain.KEYCHAIN_ERRORS:
            pass
        raise ProtocolError("pairing_settings_unavailable") from None
    _drop_status(root)
    _record_key(root, url, enrollment)
    return PairedWorker(enrollment.worker_id, labels.get("workspace_label"), labels.get("owner_label"))


def reconnect(root: Path, url: str, code: str, *, code_verifier: str, transport=None, cancelled=None) -> PairedWorker:
    """Replace the configured enrollment's key with one from a Connect renewal code.

    Unlike ``pair`` there is no ``unpair_before_pairing`` refusal: the URL and
    worker ID stay as configured, and only the Keychain item is updated in place
    (``security add-generic-password -U``), under the pairing lock. A response
    for another worker ID, or any failure, leaves the old key untouched; the
    service keeps the new key pending until it is first used, so a Mac whose old
    key is still live keeps working. ``cancelled`` is checked as in ``pair``.
    """
    require_macos()
    return _locked(root, _reconnect, url, code, code_verifier=code_verifier, transport=transport,
                   cancelled=cancelled)


def _reconnect(root: Path, url: str, code: str, *, code_verifier: str, transport=None, cancelled=None) -> PairedWorker:
    url = validate_url(url)
    policy = load_worker_settings(root)
    configured = policy.control_service_url
    if configured is None or account_name(configured) != account_name(url):
        raise ProtocolError("not_paired_with_this_server")
    if policy.control_service_worker_id is None:
        raise ProtocolError("worker_id_unknown")
    _check_cancelled(cancelled)
    enrollment, labels = _redeem(url, code, transport, text(code_verifier, 128))
    if enrollment.worker_id != policy.control_service_worker_id:
        raise ProtocolError("worker_mismatch")
    _check_cancelled(cancelled)
    _store(url, enrollment)
    _drop_status(root)
    _record_key(root, url, enrollment)
    return PairedWorker(enrollment.worker_id, labels.get("workspace_label"), labels.get("owner_label"))


def unpair(root: Path, url: str | None = None) -> bool:
    """Remove the configured enrollment, or the one for an explicit ``url``; see ``_unpair``."""
    require_macos()
    return _locked(root, _unpair, url)


def _unpair(root: Path, url: str | None = None) -> bool:
    """Remove the enrollment for the configured URL, or for an explicit ``url``.

    The explicit form also recovers an orphaned Keychain item when settings no
    longer name a URL. The configured URL is cleared only when it is the one
    being unpaired, and it is cleared *before* the key is deleted: the runtime
    re-reads the URL at every launch's commit point, so once it is gone no
    remote launch that was not already committed can start, however the
    Keychain call goes. A key left behind by a failed delete is an orphan that
    an explicit ``unpair <url>`` removes.

    Returns whether the configured enrollment was the one removed; ``False``
    means only an orphan was cleaned up and any configured service is untouched.
    """
    require_macos()
    configured = load_worker_settings(root).control_service_url
    target = validate_url(url) if url is not None else configured
    if target is None:
        return False
    removed = configured is not None and account_name(configured) == account_name(target)
    if removed:
        configure_worker_service(root, None)
        _drop_status(root)
    try:
        macos_keychain.delete_password(SERVICE, account_name(target))
    except macos_keychain.KEYCHAIN_ERRORS:
        raise ProtocolError("device_key_unavailable") from None
    return removed


def store_rotated_key(root: Path, url: str, enrollment: Enrollment, *, previous_key: str) -> bool:
    """Save a key that ``rotate_key`` returned, in place of ``previous_key``.

    Runs under the pairing lock, and writes only while settings still name
    ``url`` for this worker and the Keychain item still holds ``previous_key``:
    an unpair, a re-pair or a reconnect that landed since the client read its
    key wins, and nothing is written (returns False). The URL, the worker ID
    and the saved status are kept, because the enrollment is the same one with
    a new key. A Keychain failure raises ``device_key_unavailable`` and the
    item keeps the old key, which the service still accepts while the new one
    is only pending.
    """
    require_macos()
    return _locked(root, _store_rotated_key, url, enrollment, previous_key=previous_key)


def _store_rotated_key(root: Path, url: str, enrollment: Enrollment, *, previous_key: str) -> bool:
    url = validate_url(url)
    policy = load_worker_settings(root)
    if policy.control_service_url != url or policy.control_service_worker_id not in {None, enrollment.worker_id}:
        return False
    if enrollment.expires_at <= datetime.now(timezone.utc):
        return False
    try:
        raw = macos_keychain.get_password(SERVICE, account_name(url))
        if raw is None:
            return False
        saved = json.loads(raw)
        if (not isinstance(saved, dict) or saved.get("worker_id") != enrollment.worker_id
                or saved.get("device_key") != previous_key):
            return False
        macos_keychain.set_password(SERVICE, account_name(url), json.dumps({
            "worker_id": enrollment.worker_id, "device_key": enrollment.device_key,
            "expires_at": enrollment.expires_at.isoformat(),
        }))
    except macos_keychain.KEYCHAIN_ERRORS:
        raise ProtocolError("device_key_unavailable") from None
    except (ValueError, TypeError, RecursionError):
        return False
    return True
