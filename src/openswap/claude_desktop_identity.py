"""Account labels from one registered Claude Desktop cookie store.

No default-profile fallback, Code credentials, writes to Claude's files or token
refresh. Default is discovered only while Desktop features are enabled.
Decrypted cookies and the macOS key exist only in memory; only display labels
and a detection timestamp leave this module. Claude's private bootstrap API and
Chromium v24 cookie format may change: an unknown shape fails without guessing.
"""

from __future__ import annotations

import ctypes
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from openswap.claude_desktop import ClaudeDesktopApp, ClaudeDesktopError, DesktopProfiles, account_label
from openswap import macos_keychain

BOOTSTRAP_URL = "https://claude.ai/api/bootstrap"
COOKIE_NAMES = ("lastActiveOrg", "sessionKey", "sessionKeyV3", "sessionKeyLC", "sessionKeyV3LC")
MAX_RESPONSE = 2_000_000
UNAVAILABLE = "Could not detect this profile's account. Sign in inside its Claude window, then try again or enter the details yourself."
CHANGED = "The account changed during detection. Try again after choosing the account in Claude."


class DetectionError(ClaudeDesktopError):
    """Safe, user-facing errors only; never include cookies or response bodies."""


class NoSavedLogin(DetectionError):
    """No usable sign-in cookies; distinct from an unreadable/unknown format."""


class KeyAccessError(DetectionError):
    """Automatic detection must stop prompting until the user retries."""


def _uuid(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", value)


def _file(path: Path, *, optional=False, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return False
        raise DetectionError(UNAVAILABLE) from None
    if ((not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)
            or info.st_uid != os.getuid() or (not directory and info.st_size > 64_000_000)):
        raise DetectionError("Could not safely read this profile's account details. Enter them yourself.")
    return True


def _read(path, limit):
    # Do not follow a file replaced with a link between inspection and open.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_size > limit:
            raise DetectionError(UNAVAILABLE)
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise DetectionError(UNAVAILABLE)
    return data


def _snapshot(desktop: Path):
    """Read a consistent cookie transaction, including WAL, without decrypting.

    Refuse ambiguous root/Network stores, partitioned cookies, links and unknown
    schemas. Both domain scopes may exist only if each name has one candidate.
    """
    candidates = []
    if _file(desktop / "Cookies", optional=True):
        candidates.append(desktop / "Cookies")
    network = desktop / "Network"
    if _file(network, optional=True, directory=True) and _file(network / "Cookies", optional=True):
        candidates.append(network / "Cookies")
    if not candidates:
        raise NoSavedLogin("Waiting for sign-in in Claude.")
    if len(candidates) != 1:
        raise DetectionError(UNAVAILABLE)
    database = candidates[0]
    for suffix in ("-wal", "-shm", "-journal"):
        _file(Path(str(database) + suffix), optional=True)
    config = desktop / "config.json"
    account = None
    if _file(config, optional=True):
        account = json.loads(_read(config, MAX_RESPONSE)).get("lastKnownAccountUuid")
        if account is not None and not _uuid(account):
            raise DetectionError(UNAVAILABLE)
    # SQLite can create or update a WAL shared-memory file even with mode=ro.
    # Read a stable encrypted DB/WAL pair into a private, temporary directory;
    # SQLite's locks and sidecars then never touch Claude's profile. No decrypted
    # cookie is written to disk. Refuse a checkpoint/write during the copy.
    paths = (database, Path(str(database) + "-wal"))
    def stamps():
        result = []
        for path in paths:
            if _file(path, optional=True):
                info = path.stat()
                result.append((info.st_ino, info.st_size, info.st_mtime_ns))
            else:
                result.append(None)
        return result
    before = stamps()
    with tempfile.TemporaryDirectory(prefix="openswap-identity-") as temporary:
        copy = Path(temporary) / "Cookies"
        for path, stamp in zip(paths, before):
            if stamp is not None:
                data = _read(path, 64_000_000)
                target = Path(temporary) / ("Cookies" if path == database else "Cookies-wal")
                target.touch(mode=0o600)
                target.write_bytes(data)
        if stamps() != before:
            raise DetectionError(CHANGED)
        rows = _cookie_rows(copy)
    return account, rows


def _cookie_rows(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.5)) as db:
        db.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + 1
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.execute("BEGIN")
        version = db.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        if version != ("24",):
            raise DetectionError("This Claude version's sign-in format isn't supported. Enter the details yourself.")
        now = int((time.time() + 11644473600) * 1_000_000)
        rows = db.execute(
            "SELECT name, host_key, value, encrypted_value FROM cookies "
            "WHERE host_key IN ('claude.ai', '.claude.ai') AND path='/' "
            "AND top_frame_site_key='' AND is_secure=1 "
            "AND (expires_utc=0 OR expires_utc>?) "
            "AND name IN (?, ?, ?, ?, ?) ORDER BY name, host_key LIMIT 11",
            (now, *COOKIE_NAMES),
        ).fetchall()
    names = [row[0] for row in rows]
    if not {"sessionKey", "sessionKeyV3"}.intersection(names):
        raise NoSavedLogin("Waiting for sign-in in Claude.")
    if len(names) != len(set(names)) or "lastActiveOrg" not in names:
        raise DetectionError(UNAVAILABLE)
    for _, _, plain, encrypted in rows:
        if not isinstance(plain, str) or not isinstance(encrypted, bytes) or len(plain) + len(encrypted) > 16384:
            raise DetectionError(UNAVAILABLE)
        if (not plain and not encrypted) or (plain and encrypted):
            raise DetectionError(UNAVAILABLE)
    return rows


def _key():
    # Use the shared helper's app-identified read for this Claude-owned item.
    # The ordinary Code credential path keeps its existing stable CLI reader.
    try:
        password = macos_keychain.get_password("Claude Safe Storage", "Claude Key", native=True)
        if not password or len(password.encode("utf-8")) > 4096:
            raise macos_keychain.KeychainError("Unavailable")
        return hashlib.pbkdf2_hmac("sha1", password.encode("utf-8"), b"saltysalt", 1003, 16)
    except macos_keychain.KEYCHAIN_ERRORS:
        raise KeyAccessError("Account detection is paused. Choose Detect in Account details to retry Keychain access, or enter the details yourself.") from None


def _decrypt(encrypted: bytes, host: str, key: bytes):
    if not encrypted.startswith(b"v10") or len(encrypted) < 19 or (len(encrypted) - 3) % 16:
        raise DetectionError(UNAVAILABLE)
    # CommonCrypto avoids a new crypto dependency; AES-CBC/PKCS7 is Chromium's
    # macOS v10 format, not an encryption format designed by OpenSwap.
    library = ctypes.CDLL("/usr/lib/system/libcommonCrypto.dylib")
    crypt = library.CCCrypt
    crypt.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                      ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                      ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    crypt.restype = ctypes.c_int32
    data = encrypted[3:]
    output = ctypes.create_string_buffer(len(data) + 16)
    length = ctypes.c_size_t()
    status = crypt(1, 0, 1, key, len(key), b" " * 16, data, len(data), output, len(output), ctypes.byref(length))
    plain = output.raw[:length.value]
    if status or len(plain) < 32 or not hmac.compare_digest(plain[:32], hashlib.sha256(host.encode()).digest()):
        raise DetectionError(UNAVAILABLE)
    return plain[32:].decode("utf-8")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _bootstrap(cookies):
    request = urllib.request.Request(BOOTSTRAP_URL, headers={
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        "Accept": "application/json", "User-Agent": "OpenSwap/account-details",
    })
    # No redirects or credential-bearing URLs. A challenge/login page is a
    # detection failure; never automate around it or scrape conversations.
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=5) as response:
        if response.status != 200 or response.geturl() != BOOTSTRAP_URL:
            raise DetectionError(UNAVAILABLE)
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise DetectionError(UNAVAILABLE)
        return json.loads(data)


def _labels(data, org_id, expected_account):
    account = data.get("account") if isinstance(data, dict) else None
    if not isinstance(account, dict) or not _uuid(account.get("uuid")):
        raise DetectionError(UNAVAILABLE)
    if expected_account and account["uuid"].lower() != expected_account.lower():
        raise DetectionError(CHANGED)
    memberships = account.get("memberships")
    if not isinstance(memberships, list) or len(memberships) > 1000:
        raise DetectionError(UNAVAILABLE)
    orgs = [m["organization"] for m in memberships if isinstance(m, dict)
            and isinstance(m.get("organization"), dict)
            and isinstance(m["organization"].get("uuid"), str)
            and m["organization"]["uuid"].lower() == org_id.lower()]
    if len(orgs) != 1:
        raise DetectionError(UNAVAILABLE)
    email = account_label(account.get("email_address"), email=True)
    organization = account_label(orgs[0].get("name"))
    if not email or not organization:
        raise DetectionError(UNAVAILABLE)
    return {"email": email, "organization": organization,
            "detected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def login_fingerprint(snapshot):
    account, rows = snapshot
    payload = [account, [[name, host, plain, encrypted.hex()] for name, host, plain, encrypted in rows]]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def observe_login(store, profile):
    """Inspect only this profile's cookie state; no Keychain or network access."""
    desktop, _ = store._directories(profile)
    try:
        return login_fingerprint(_snapshot(desktop))
    except NoSavedLogin:
        return ""


def disk_stamp(store, profile):
    """Cheap hint for scheduling reads. Never used as authenticated identity."""
    desktop, _ = store._directories(profile)
    result = []
    for relative in ("config.json", "Cookies", "Cookies-wal", "Network", "Network/Cookies", "Network/Cookies-wal"):
        path = desktop / relative
        if not _file(path, optional=True, directory=relative == "Network"):
            result.append(None)
        else:
            info = path.stat()
            result.append((info.st_ino, info.st_size, info.st_mtime_ns))
    return tuple(result)


class AccountDetector:
    """Keep the decryption key in memory and avoid repeated permission prompts."""

    def __init__(self, store=None):
        self.store = store
        self._cached_key = None
        try:
            self.denied = bool(store and store.keychain_paused())
        except (ClaudeDesktopError, OSError):
            self.denied = True  # unreadable permission state must not prompt
        self._lock = threading.Lock()
        self._generation = 0

    def retry(self):
        if self.store:
            self.store.pause_keychain(False)
        self._generation += 1
        self.denied = False

    def clear(self):
        # Called from the UI thread: never wait for a macOS permission dialog.
        self._generation += 1
        self._cached_key = None
        # Turning the feature off/on must preserve a previous denial.
        if self.store:
            try:
                self.denied |= self.store.keychain_paused()
            except (ClaudeDesktopError, OSError):
                self.denied = True

    def _get_key(self):
        with self._lock:
            if self.denied:
                raise KeyAccessError("Choose Detect in Account details to allow Keychain access.")
            if self._cached_key is None:
                generation = self._generation
                try:
                    if self.store:
                        self.store.pause_keychain(True)
                    key = _key()
                    if generation != self._generation:
                        raise DetectionError("Account detection stopped.")
                    if self.store:
                        self.store.pause_keychain(False)
                    self._cached_key = key
                except (KeyAccessError, ClaudeDesktopError, OSError):
                    if generation == self._generation:
                        self.denied = True
                    raise KeyAccessError("Account detection is paused. Choose Detect in Account details to retry Keychain access.") from None
            return self._cached_key

    def __call__(self, store, identifier, *, expected_fingerprint=None):
        return detect_account(store, identifier, key_provider=self._get_key, expected_fingerprint=expected_fingerprint)


def detect_account(store: DesktopProfiles, identifier: str, *, app=None, key_provider=None, expected_fingerprint=None):
    """Return a last-detected label pair, never a verified live session state."""
    try:
        if sys.platform != "darwin":
            raise DetectionError("Account detection is available on macOS. You can enter the details yourself.")
        profile = store.get(identifier)  # archived profiles are not inspected
        desktop, _ = store._directories(profile)
        before = _snapshot(desktop)
        if expected_fingerprint is not None and login_fingerprint(before) != expected_fingerprint:
            raise DetectionError(CHANGED)
        (app or ClaudeDesktopApp()).validate()
        key = (key_provider or _key)() if any(row[3] for row in before[1]) else None
        cookies = {}
        for name, host, plain, encrypted in before[1]:
            value = _decrypt(encrypted, host, key) if encrypted else plain
            if not value or not re.fullmatch(r"[\x21-\x7e]+", value) or any(c in value for c in ';"\\,'):
                raise DetectionError(UNAVAILABLE)
            cookies[name] = value
        org_id = cookies["lastActiveOrg"]
        if not _uuid(org_id):
            raise DetectionError(UNAVAILABLE)
        current = store.get(identifier)
        if expected_fingerprint is not None and not current.auto_detect:
            raise DetectionError("Automatic account detection was turned off.")
        result = _labels(_bootstrap(cookies), org_id, before[0])
        store.get(identifier)
        store._directories(profile)
        if _snapshot(desktop) != before:
            raise DetectionError(CHANGED)
        return result
    except DetectionError:
        raise
    except Exception:
        # Network, JSON, SQLite, crypto and file failures never expose secrets.
        raise DetectionError(UNAVAILABLE) from None
