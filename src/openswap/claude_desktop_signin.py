"""Deliver a sign-in or connector callback to one verified Claude process.

macOS's shared claude:// handler cannot select a user-data-dir. This recovery
path addresses a running process directly; it never launches another instance,
stores the link, or puts it in subprocess arguments.
Claude remains responsible for validating and consuming the login callback.
"""

from dataclasses import asdict
from pathlib import Path
import re
from urllib.parse import parse_qsl, urlsplit

from openswap.claude_desktop import BUNDLE_ID, ClaudeDesktopApp, ClaudeDesktopError


INVALID_LINK = 'This sign-in link is incomplete or unsupported. Restart sign-in in the intended Claude profile.'
INVALID_CALLBACK = 'This Claude link is incomplete or unsupported. Start again from the intended Claude profile.'


def signin_link(value: str) -> str:
    """Accept only the login routes inspected in Claude 3.6556.2."""
    try:
        if not isinstance(value, str) or len(value) > 16384:
            raise ValueError
        value = value.strip()
        if not value or any(ord(c) <= 32 or ord(c) >= 127 or c == "\\" for c in value):
            raise ValueError
        url = urlsplit(value)
        if url.scheme != "claude" or url.netloc not in ("login", "claude.ai"):
            raise ValueError
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        query = dict(pairs)
        if len(query) != len(pairs):
            raise ValueError
        if (url.netloc, url.path) == ("login", "/google-auth"):
            valid = query.get("code") and query.get("hop_nonce") and not url.fragment
        elif (url.netloc, url.path) == ("claude.ai", "/magic-link"):
            parts = url.fragment.split(":")
            valid = len(parts) == 2 and all(parts)
        elif (url.netloc, url.path) == ("claude.ai", "/sso-callback"):
            valid = query.get("code") and query.get("state") and not url.fragment
        else:
            valid = False
        if not valid:
            raise ValueError
        return value
    except (ValueError, TypeError):
        raise ClaudeDesktopError(INVALID_LINK) from None


def _fourcc(value):
    return int.from_bytes(value.encode("ascii"), "big")


def callback_link(value: str) -> tuple[str, str]:
    """Classify inspected Claude routes without rewriting credential payloads.

    Desktop 3.6556.2 sends Code/Cowork MCP authorization to
    /mcp-auth-callback/sdk and connector returns to /mcp-auth-callback/<id>.
    These are not account sign-ins. Claude owns OAuth state/PKCE validation;
    OpenSwap only delivers the original link to the explicitly chosen process.
    """
    try:
        return signin_link(value), "signin"
    except ClaudeDesktopError:
        pass
    try:
        if not isinstance(value, str) or not 0 < len(value) <= 16384:
            raise ValueError
        value = value.strip()
        if not value or any(ord(c) <= 32 or ord(c) >= 127 or c == "\\" for c in value):
            raise ValueError
        url = urlsplit(value)
        if (url.scheme != "claude" or url.netloc != "claude.ai" or url.fragment
                or not re.fullmatch(r"/mcp-auth-callback/[A-Za-z0-9_-]+", url.path)):
            raise ValueError
        pairs = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        if len(dict(pairs)) != len(pairs):
            raise ValueError
        # Connector returns can carry step=finish rather than code/state, and
        # OAuth denial returns carry error. Do not require a success credential.
        return value, "connector"
    except (ValueError, TypeError):
        raise ClaudeDesktopError(INVALID_CALLBACK) from None


def send_to_process(pid: int, value: str, binary: Path) -> None:
    from AppKit import NSRunningApplication
    from Foundation import NSAppleEventDescriptor

    target = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if (target is None or target.isTerminated() or target.bundleIdentifier() != BUNDLE_ID
            or target.executableURL() is None or target.executableURL().path() != str(binary)):
        raise ClaudeDesktopError("This Claude profile closed or changed. Open it and restart sign-in.")
    # Address by PID, never by bundle identifier (which all profiles share).
    address = NSAppleEventDescriptor.descriptorWithProcessIdentifier_(pid)
    event = NSAppleEventDescriptor.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_(
        _fourcc("GURL"), _fourcc("GURL"), address, -1, 0)
    event.setParamDescriptor_forKeyword_(NSAppleEventDescriptor.descriptorWithString_(value), _fourcc("----"))
    # Wait only for event delivery, not completion of the web sign-in. Never
    # retry automatically: a timeout could still mean Claude received the link.
    reply, error = event.sendEventWithOptions_timeout_error_(0x03 | 0x10, 5.0, None)
    if error is not None or reply is None:
        raise ClaudeDesktopError("macOS could not deliver the sign-in link. Keep this profile open and try again.")
    event_error = reply.paramDescriptorForKeyword_(_fourcc("errn"))
    if event_error is not None and event_error.int32Value() != 0:
        raise ClaudeDesktopError("Claude could not accept the sign-in link. Restart sign-in in this profile.")


def finish_signin(store, identifier, value, *, app=None, sender=None, expected_pid=None):
    value, kind = callback_link(value)
    app = app or ClaudeDesktopApp()
    app.validate()
    with store._lock():
        profile = store.get(identifier)
        desktop, code = store._directories(profile)
        argv = [str(app.binary)] if code is None else [str(app.binary), f"--user-data-dir={desktop}"]
        pid = app._running(argv, desktop)
        if pid is None or (expected_pid is not None and pid != expected_pid):
            raise ClaudeDesktopError("This Claude profile closed or restarted. Open it and restart sign-in.")
        try:
            if app._validation_stamp is None or app._stamp() != app._validation_stamp:
                raise ClaudeDesktopError("Claude updated during sign-in. Open this profile and restart sign-in.")
            (sender or send_to_process)(pid, value, app.binary)
        except ClaudeDesktopError:
            raise
        except Exception:
            # Native errors may contain the callback. Do not surface or log it.
            raise ClaudeDesktopError("Could not send the link to this Claude profile. Restart sign-in and try again.") from None
    return {"state": "connector_sent" if kind == "connector" else "signin_sent", "pid": pid, "profile": asdict(profile)}
