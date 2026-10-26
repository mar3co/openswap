"""Shared macOS Keychain access.

A small wrapper around the system ``security`` tool for storing generic
passwords, used instead of the third-party ``keyring`` library. Two reasons:

- The macOS hot path no longer needs the ``keyring`` dependency.
- Keychain items are created and read by the same stable ``security`` binary, so
  reads stay silent across upgrades. ``keyring`` (and any in-process
  Security.framework call) anchors the item's access to the *Python interpreter*,
  which ``uv tool upgrade`` rebuilds — at which point macOS can show the "wants to
  use your keychain" prompt. ``security`` never changes, so creator == reader and
  there is no prompt.

The read/write/delete shapes mirror Claude Code's own implementation
(``utils/secureStorage/macOsKeychainStorage.ts``):

- ``set_password`` hex-encodes the value (``-X``) and pipes the command through
  ``security -i`` (stdin) so the secret never appears in process argv (a
  process-monitor / CrowdStrike concern). It falls back to argv only when the
  command would overflow ``security -i``'s 4096-byte stdin line buffer, which
  would otherwise truncate mid-argument and silently corrupt the entry.
- ``get_password`` uses ``find-generic-password ... -w`` and treats exit code 44
  as "not found" (returns ``None``); any *other* non-zero exit raises so callers
  can tell a genuine miss apart from a locked/denied/unavailable Keychain.

Caveat: values must be printable text. ``find-generic-password -w`` prints the
stored data raw only when it is printable; data with non-printable bytes comes
back *hex-encoded*, so a write/read round-trip would not be identity. Fine for
this codebase (credentials are ASCII JSON), but don't reuse this wrapper for
arbitrary binary data. Claude Code's ``-w`` reads share the same constraint.

This module is import-safe on every platform (platform access is lazy);
its functions are only meaningful on macOS.

``get_password(..., native=True)`` reads another app's item through
Security.framework so macOS identifies the running OpenSwap app in permission
dialogs. The default CLI path stays stable for OpenSwap/Claude Code storage.
Source installs identify their Python interpreter; the packaged app identifies
OpenSwap. Neither path changes an existing item's access rules.
"""

from __future__ import annotations

import os
import subprocess

# ``security -i`` reads stdin with a 4096-byte fgets() buffer (BUFSIZ on darwin).
# A command line longer than this is truncated mid-argument: it fails to write
# while leaving any previous entry intact (Claude Code #30337). 64 bytes of
# headroom guards against line-terminator accounting differences.
SECURITY_STDIN_LINE_LIMIT = 4096 - 64

_NOT_FOUND_RC = 44  # errSecItemNotFound surfaced by find/delete-generic-password

# Bound every ``security`` spawn so a wedged Keychain (a locked login keychain
# prompting for an unlock that never comes on a headless/SSH host) can't hang the
# CLI. 5s, deliberately short: a credential op that has to fall back to the file
# may be followed by a best-effort cleanup spawn, so the per-op budget doubles in
# the worst case. A healthy Keychain answers in well under 100ms.
_TIMEOUT = 5.0

# Pin the absolute path to Apple's system binary rather than resolving via PATH:
# this is a credential tool, so an attacker-controlled ``security`` earlier on
# PATH must not be able to intercept secrets. ``/usr/bin/security`` is present on
# every macOS.
_SECURITY = "/usr/bin/security"


class KeychainError(Exception):
    """A ``security`` invocation failed for a reason other than "not found"."""


# The exceptions a Keychain operation may raise that callers should treat as
# "Keychain unusable" (→ fall back to file storage) rather than a programming
# bug: a wrapper failure (KeychainError, incl. a converted timeout), a raw
# subprocess timeout, or a missing ``security`` binary (OSError). Catching this
# tuple — never bare ``Exception`` — keeps a real bug loud instead of silently
# routing to the file backend mid-invocation.
KEYCHAIN_ERRORS = (KeychainError, subprocess.TimeoutExpired, OSError)


def keychain_account_name() -> str:
    """Account name for the active-credential Keychain item, mirroring Claude
    Code's ``getUsername()`` (``utils/secureStorage/macOsKeychainHelpers.ts``).

    ``$USER`` first, then the OS username, then a stable final fallback. Matching
    this exactly matters on headless/launchd/cron hosts where ``$USER`` is unset:
    a divergent default (e.g. ``"user"``) would key a *different* Keychain item
    than Claude Code, so the two could not see each other's active credential.
    """
    user = os.environ.get("USER")
    if user:
        return user
    try:
        import pwd  # POSIX-only; the account-name call sites are macOS-only

        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return "claude-code-user"


def _quote(value: str) -> str:
    """Quote a value for a ``security -i`` stdin command line.

    ``security -i`` re-parses each line shell-style, so wrap the value in double
    quotes and backslash-escape any embedded ``"``/``\\`` (e.g. the active-
    credential service name contains a space).
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def get_password(service: str, account: str, *, native: bool = False) -> str | None:
    """Return the stored password, or ``None`` if no such item exists (rc 44).

    Raises :class:`KeychainError` on any other non-zero exit (locked / denied /
    unavailable) or a timeout, so a genuine miss is not confused with a transient
    failure.
    """
    if native:
        return _native_get_password(service, account)
    try:
        result = subprocess.run(
            [_SECURITY, "find-generic-password", "-a", account, "-w", "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security find-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode == 0:
        # `-w` prints the value followed by one newline; strip exactly that so
        # values with meaningful leading/trailing whitespace survive intact.
        return result.stdout.removesuffix("\n")
    if result.returncode == _NOT_FOUND_RC:
        return None
    raise KeychainError(
        f"security find-generic-password failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )


def _native_get_password(service: str, account: str) -> str | None:
    """Read one generic password in this process; never spawn a credential tool.

    Keep imports lazy for CLI-only/non-macOS installs. CoreFoundation owns all
    query/result objects; release them even on denial or an unexpected result.
    No writes, access-list edits, credential copying or authentication fallback.
    """
    import ctypes as c
    import sys

    if sys.platform != "darwin":
        raise KeychainError("Native Keychain access is available on macOS.")
    owned = []
    cf = None
    try:
        cf, security, constants = _native_api()
        def string(value):
            ref = cf.CFStringCreateWithCString(None, value.encode("utf-8"), 0x08000100)
            if not ref:
                raise KeychainError("Could not prepare Keychain access.")
            owned.append(ref)
            return ref

        pairs = [(constants["kSecClass"], constants["kSecClassGenericPassword"]),
                 (constants["kSecAttrService"], string(service)),
                 (constants["kSecAttrAccount"], string(account)),
                 (constants["kSecMatchLimit"], constants["kSecMatchLimitOne"]),
                 (constants["kSecReturnData"], constants["kCFBooleanTrue"])]
        query = cf.CFDictionaryCreate(None, (c.c_void_p * len(pairs))(*(k for k, _ in pairs)),
                                      (c.c_void_p * len(pairs))(*(v for _, v in pairs)), len(pairs),
                                      constants["kCFTypeDictionaryKeyCallBacks"],
                                      constants["kCFTypeDictionaryValueCallBacks"])
        if not query:
            raise KeychainError("Could not prepare Keychain access.")
        owned.append(query)
        result = c.c_void_p()
        status = security.SecItemCopyMatching(query, c.byref(result))
        if result.value:
            owned.append(result.value)
        if status == -25300:  # errSecItemNotFound
            return None
        if status != 0:
            raise KeychainError("Keychain access was canceled, denied or unavailable.")
        if not result.value or cf.CFGetTypeID(result) != cf.CFDataGetTypeID():
            raise KeychainError("Keychain returned an unsupported value.")
        size = cf.CFDataGetLength(result)
        if not 0 < size <= 4096:
            raise KeychainError("Keychain returned an unsupported value.")
        pointer = cf.CFDataGetBytePtr(result)
        if not pointer:
            raise KeychainError("Keychain returned an unsupported value.")
        return c.string_at(pointer, size).decode("utf-8")
    except (OSError, AttributeError, ValueError):
        raise KeychainError("Could not read the saved Keychain item.") from None
    finally:
        if cf is not None:
            for ref in reversed(owned):
                cf.CFRelease(ref)


def _native_api():
    """Bind the system frameworks using pointer-sized types on both Mac CPUs."""
    import ctypes as c

    cf = c.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    security = c.CDLL("/System/Library/Frameworks/Security.framework/Security")
    signatures = {
        "CFStringCreateWithCString": ([c.c_void_p, c.c_char_p, c.c_uint32], c.c_void_p),
        "CFDictionaryCreate": ([c.c_void_p, c.POINTER(c.c_void_p), c.POINTER(c.c_void_p), c.c_long, c.c_void_p, c.c_void_p], c.c_void_p),
        "CFGetTypeID": ([c.c_void_p], c.c_ulong),
        "CFDataGetTypeID": ([], c.c_ulong),
        "CFDataGetLength": ([c.c_void_p], c.c_long),
        "CFDataGetBytePtr": ([c.c_void_p], c.c_void_p),
        "CFRelease": ([c.c_void_p], None),
    }
    for name, (args, result) in signatures.items():
        function = getattr(cf, name)
        function.argtypes, function.restype = args, result
    security.SecItemCopyMatching.argtypes = [c.c_void_p, c.POINTER(c.c_void_p)]
    security.SecItemCopyMatching.restype = c.c_int32
    constants = {name: c.c_void_p.in_dll(security, name).value for name in (
        "kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount", "kSecMatchLimit",
        "kSecMatchLimitOne", "kSecReturnData")}
    constants["kCFBooleanTrue"] = c.c_void_p.in_dll(cf, "kCFBooleanTrue").value
    for name in ("kCFTypeDictionaryKeyCallBacks", "kCFTypeDictionaryValueCallBacks"):
        constants[name] = c.addressof(c.c_byte.in_dll(cf, name))
    return cf, security, constants


def item_exists(service: str, account: str) -> bool:
    """Whether a generic-password item exists, without touching its secret.

    Attribute-only lookup (no ``-w``): nothing is decrypted, so this can never
    trigger a Keychain prompt, even for items owned by another app. Returns
    ``True`` only on rc 0; "not found" (rc 44), error exits, a timeout, and a
    missing binary all return ``False``. Deliberately **non-raising**: callers use
    it for cleanup verification, not access decisions, so it must never feed the
    capability cache (a timeout here means "couldn't tell", not "Keychain works").
    """
    try:
        result = subprocess.run(
            [_SECURITY, "find-generic-password", "-a", account, "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def set_password(service: str, account: str, password: str) -> None:
    """Create or update a generic-password item (``-U``).

    Prefers ``security -i`` stdin so the secret stays out of argv; falls back to
    argv only for payloads that would overflow the stdin line buffer. Raises
    :class:`KeychainError` on a non-zero exit or a timeout.
    """
    hex_value = password.encode("utf-8").hex()
    # `-X` passes the value as hex, avoiding any escaping issues for the secret.
    command = (
        f"add-generic-password -U -a {_quote(account)} -s {_quote(service)} "
        f"-X {hex_value}\n"
    )
    try:
        if len(command.encode("utf-8")) <= SECURITY_STDIN_LINE_LIMIT:
            result = subprocess.run(
                [_SECURITY, "-i"],
                input=command,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
        else:
            # Overflows the stdin line buffer; fall back to argv. Hex in argv is
            # recoverable by a determined observer but defeats naive plaintext-grep
            # rules, and the alternative — silent corruption — is strictly worse.
            result = subprocess.run(
                [
                    _SECURITY, "add-generic-password", "-U",
                    "-a", account, "-s", service, "-X", hex_value,
                ],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security add-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode != 0:
        raise KeychainError(
            f"security add-generic-password failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )


def delete_password(service: str, account: str) -> None:
    """Delete a generic-password item. rc 44 (already absent) counts as success.

    Raises :class:`KeychainError` on any other non-zero exit or a timeout.
    """
    try:
        result = subprocess.run(
            [_SECURITY, "delete-generic-password", "-a", account, "-s", service],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise KeychainError(
            f"security delete-generic-password timed out after {_TIMEOUT}s"
        ) from e
    if result.returncode in (0, _NOT_FOUND_RC):
        return
    raise KeychainError(
        f"security delete-generic-password failed (rc={result.returncode}): "
        f"{result.stderr.strip()}"
    )


# More items than any one service plausibly holds; stops a Keychain that keeps
# reporting success without deleting from looping forever.
_DELETE_ALL_LIMIT = 1000


def delete_all(service: str) -> int:
    """Delete every generic-password item of ``service``, whatever its account.

    ``delete-generic-password`` without ``-a`` deletes the first match, so this
    repeats it until "not found" (rc 44). Returns how many were deleted.
    Raises :class:`KeychainError` on any other exit, a timeout, or the limit.
    """
    for deleted in range(_DELETE_ALL_LIMIT):
        try:
            result = subprocess.run(
                [_SECURITY, "delete-generic-password", "-s", service],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            raise KeychainError(
                f"security delete-generic-password timed out after {_TIMEOUT}s"
            ) from e
        if result.returncode == _NOT_FOUND_RC:
            return deleted
        if result.returncode != 0:
            raise KeychainError(
                f"security delete-generic-password failed (rc={result.returncode}): "
                f"{result.stderr.strip()}"
            )
    raise KeychainError(f"more than {_DELETE_ALL_LIMIT} Keychain items for {service}")
