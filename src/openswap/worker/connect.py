"""Connect to OpenTag: pair (or reconnect) this Mac by approving it in the browser.

The flow follows the shape of RFC 8252 (an authorization code with PKCE,
returned to a loopback redirect) without being an OAuth client:

1. Take ``<backup root>/.worker-connect.lock`` without waiting; a second Connect
   is refused with ``connect_in_progress`` before it opens a listener or a page.
2. Read ``<origin>/.well-known/openswap-worker``: a closed ``{"protocol": "v1",
   "connect_endpoint": …}`` on the same origin. A 404 or a malformed document
   means the server has no Connect page: pair with the pasted command instead.
3. Listen once on ``127.0.0.1`` (port 0), open the browser on the connect page
   with ``redirect_uri``, ``state``, the S256 ``code_challenge`` and the Mac's
   label (plus ``worker_id`` for a reconnect).
4. The first ``GET /openswap/callback`` with the right ``state`` closes the
   listener. Its pair code is redeemed through ``/v1/pair`` with the
   ``code_verifier`` (``pairing.pair`` or ``pairing.reconnect``), and only then
   does the browser tab get its result page.

The verifier and the code live only in local variables of one flow: they are
never logged, printed or written anywhere.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import html
import http.client
import json
from pathlib import Path
import secrets
import select
import socket
import ssl
import subprocess
import threading
import time
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from openswap.exceptions import LockError
from openswap.locking import FileLock
from openswap.settings import load_worker_settings
from openswap.worker import pairing
from openswap.worker.protocol import ProtocolError, fields, text, validate_url

# The OpenTag portal: what Connect pairs with unless the owner names another server.
DEFAULT_SERVER = "https://opentag.me"
WELL_KNOWN_PATH = "/.well-known/openswap-worker"
CALLBACK_PATH = "/openswap/callback"
LOCK_NAME = ".worker-connect.lock"
# A pair code lives 10 minutes; the listener waits no longer.
WAIT_SECONDS = 600.0
# The worker transport's bound on one request.
REQUEST_TIMEOUT = 5.0
MAX_DOCUMENT = 16_384
MAX_LABEL = 64
# The request line must fit in this; the rest of the head (a browser sends every
# cookie it holds for 127.0.0.1, whatever the port) is read up to MAX_HEAD_DRAIN
# so closing the connection does not reset it, then ignored.
MAX_REQUEST_HEAD = 8192
MAX_HEAD_DRAIN = 262_144
# A paired Mac whose key ends within this window reconnects instead of
# refusing as already paired (the same 7 days key rotation starts at).
RECONNECT_WINDOW = timedelta(days=7)
_POLL_SECONDS = 0.2


# --- PKCE and state ----------------------------------------------------------------------------


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def code_challenge(verifier: str) -> str:
    """RFC 7636 S256: ``BASE64URL(SHA256(ASCII(code_verifier)))`` without padding."""
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def new_verifier(raw: bytes | None = None) -> str:
    """A PKCE verifier from 32 random bytes: 43 base64url characters."""
    return _b64url(secrets.token_bytes(32) if raw is None else raw)


def new_state() -> str:
    """16 random bytes, base64url: 22 characters."""
    return _b64url(secrets.token_bytes(16))


# --- the Mac's label ---------------------------------------------------------------------------


def clean_label(value: str) -> str:
    """At most 64 printable characters: control characters dropped, whitespace collapsed."""
    spaced = "".join(" " if c.isspace() else c for c in value)
    printable = "".join(c for c in spaced if not unicodedata.category(c).startswith("C"))
    return " ".join(printable.split())[:MAX_LABEL].strip()


def computer_name(run=subprocess.run) -> str:
    """The Mac's Computer Name (``scutil --get ComputerName``), cut to 64 characters."""
    try:
        result = run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5)
        name = clean_label(result.stdout or "") if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        name = ""
    if not name:
        try:
            name = clean_label(socket.gethostname().split(".")[0])
        except OSError:
            name = ""
    return name or "Mac"


# --- discovery ---------------------------------------------------------------------------------


def _opener(url: str):
    """The worker transport's rules: no proxy, no redirects, the system trust store."""
    from openswap.worker.remote import NoRedirect

    handlers = [ProxyHandler({}), NoRedirect()]
    if url.startswith("https:"):
        import truststore
        handlers.append(HTTPSHandler(context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)))
    return build_opener(*handlers)


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _nonfinite(_):
    raise ValueError("non-finite number")


def connect_endpoint(url: str, value: object) -> str:
    """Validate a discovery document: closed, ``v1``, an endpoint on ``url``'s own origin."""
    try:
        data = fields(value, {"protocol", "connect_endpoint"})
    except ProtocolError:
        raise ProtocolError("connect_unsupported") from None
    endpoint = data["connect_endpoint"]
    if data["protocol"] != "v1" or not isinstance(endpoint, str):
        raise ProtocolError("connect_unsupported")
    try:
        endpoint = text(endpoint, 2048)
        parts = urlsplit(endpoint)
        if (any(c.isspace() for c in endpoint) or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or "?" in endpoint or "#" in endpoint
                or not parts.path.startswith("/")):
            raise ProtocolError("connect_unsupported")
        origin = validate_url(f"{parts.scheme}://{parts.netloc}")
    except (ProtocolError, ValueError):
        raise ProtocolError("connect_unsupported") from None
    if origin != validate_url(url):
        raise ProtocolError("connect_endpoint_refused")
    return f"{origin}{parts.path}"


def discover(url: str, *, opener=None) -> str:
    """The connect page ``url`` names in its well-known document.

    ``connect_unsupported`` (a 404, a redirect or a malformed document) means
    the server has no Connect page; ``connect_endpoint_refused`` that it named a
    page on another origin.
    """
    url = validate_url(url)
    request = Request(url + WELL_KNOWN_PATH, headers={"Accept": "application/json"}, method="GET")
    try:
        with (opener or _opener(url)).open(request, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read(MAX_DOCUMENT + 1)
    except HTTPError as exc:
        with exc:
            pass
        if exc.code in (404, 410):
            raise ProtocolError("connect_unsupported", 404) from None
        raise ProtocolError("service_unavailable", exc.code) from None
    except ProtocolError:
        # A redirect (NoRedirect) is not a discovery document.
        raise ProtocolError("connect_unsupported") from None
    except (URLError, TimeoutError, OSError, http.client.HTTPException):
        # HTTPException: something that is not an HTTP server answered.
        raise ProtocolError("service_unavailable", 503) from None
    if len(raw) > MAX_DOCUMENT:
        raise ProtocolError("connect_unsupported")
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("connect_unsupported") from None
    return connect_endpoint(url, value)


# --- the one-shot loopback listener ------------------------------------------------------------


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>body{{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1.5rem;
color:#1d1d1f;background:#fff}}h1{{font-size:1.4rem}}p{{color:#555}}
@media (prefers-color-scheme:dark){{body{{color:#f5f5f7;background:#1d1d1f}}p{{color:#aaa}}}}</style>
</head><body><h1>{title}</h1><p>{body}</p></body></html>
"""


def page(title: str, body: str) -> bytes:
    return _PAGE.format(title=html.escape(title), body=html.escape(body)).encode("utf-8")


_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed"}


def _send(conn: socket.socket, status: int, title: str, body: str, extra: str = "") -> None:
    content = page(title, body)
    head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(content)}\r\n"
            "Cache-Control: no-store\r\n"
            "Referrer-Policy: no-referrer\r\n"
            "Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'\r\n"
            "X-Content-Type-Options: nosniff\r\n"
            f"{extra}Connection: close\r\n\r\n")
    try:
        conn.sendall(head.encode("ascii") + content)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


class CallbackListener:
    """``GET /openswap/callback`` on ``127.0.0.1``, answered once.

    Requests for another path or method, or with the wrong ``state``, get an
    error page and the listener keeps waiting. The first request with the
    right ``state`` closes the listening socket; its connection is held so the
    result page can be sent after redemption (:meth:`respond`).
    """

    host = "127.0.0.1"

    def __init__(self, state: str, *, timeout: float = WAIT_SECONDS):
        self._state = state.encode("ascii")
        self._timeout = timeout
        self._cancelled = threading.Event()
        self._held: socket.socket | None = None
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._server.bind((self.host, 0))
            self._server.listen(8)
        except OSError:
            self._server.close()
            raise
        self.port = self._server.getsockname()[1]

    @property
    def redirect_uri(self) -> str:
        return f"http://{self.host}:{self.port}{CALLBACK_PATH}"

    @property
    def closed(self) -> bool:
        return self._server.fileno() == -1

    def cancel(self) -> None:
        """From any thread: :meth:`wait` stops within a fraction of a second."""
        self._cancelled.set()

    def wait(self) -> dict[str, list[str]]:
        """The callback's query once a request carries the right ``state``.

        Raises ``connect_cancelled`` or ``connect_timed_out``. The listening
        socket is closed whichever way this returns.
        """
        deadline = time.monotonic() + self._timeout
        try:
            while True:
                if self._cancelled.is_set():
                    raise ProtocolError("connect_cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProtocolError("connect_timed_out")
                ready, _, _ = select.select([self._server], [], [], min(_POLL_SECONDS, remaining))
                if not ready:
                    continue
                try:
                    conn, _address = self._server.accept()
                except OSError:
                    continue
                query = self._handle(conn)
                if query is not None:
                    return query
        finally:
            self._close_server()

    def _read_head(self, conn: socket.socket) -> bytes | None:
        """The request head, read within ``REQUEST_TIMEOUT`` overall (a slow client
        cannot hold the one waiting thread), or None on cancel or a socket error.
        Cancel is honoured between reads."""
        until = time.monotonic() + REQUEST_TIMEOUT
        raw = b""
        try:
            while b"\r\n\r\n" not in raw and len(raw) <= MAX_HEAD_DRAIN:
                if b"\r\n" not in raw and len(raw) > MAX_REQUEST_HEAD:
                    break  # no request line within its bound: answered as a bad request
                if self._cancelled.is_set():
                    return None
                remaining = until - time.monotonic()
                if remaining <= 0:
                    break
                conn.settimeout(min(_POLL_SECONDS, remaining))
                try:
                    chunk = conn.recv(16_384)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                raw += chunk
        except OSError:
            return None
        return raw

    def _handle(self, conn: socket.socket) -> dict[str, list[str]] | None:
        raw = self._read_head(conn)
        if raw is None:
            conn.close()
            return None
        line, complete, _rest = raw.partition(b"\r\n")
        parts = line.decode("latin-1").split(" ")
        if not complete or len(line) > MAX_REQUEST_HEAD or len(parts) != 3 or not parts[2].startswith("HTTP/"):
            _send(conn, 400, "Bad request", "OpenSwap did not understand this request.")
            return None
        method, target = parts[0], parts[1]
        address = urlsplit(target)
        if address.path != CALLBACK_PATH:
            _send(conn, 404, "Not found", "OpenSwap is waiting for OpenTag to send you back here.")
            return None
        if method != "GET":
            _send(conn, 405, "Not allowed", "OpenSwap accepts only the link OpenTag sends you back with.",
                  "Allow: GET\r\n")
            return None
        try:
            query = parse_qs(address.query, keep_blank_values=True, max_num_fields=16)
        except ValueError:
            query = {}
        state = query.get("state", [])
        if len(state) != 1 or not hmac.compare_digest(state[0].encode("utf-8"), self._state):
            # Another page cannot hand this Mac a code from its own account.
            _send(conn, 400, "This link is not from your Connect",
                  "OpenSwap did not start this request. Go back to the OpenSwap window you started "
                  "Connect from; it is still waiting.")
            return None
        self._held = conn
        return query

    def respond(self, ok: bool, title: str, body: str) -> None:
        """The result page for the held callback request; then its connection closes."""
        conn, self._held = self._held, None
        if conn is not None:
            _send(conn, 200 if ok else 400, title, body)

    def _close_server(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass

    def close(self) -> None:
        self._close_server()
        if self._held is not None:
            self.respond(False, "OpenSwap stopped", "Nothing was paired. Start Connect again in OpenSwap.")


# --- the flow ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """Where Connect pairs: a new pairing (``worker_id`` None) or a reconnect."""
    url: str
    worker_id: str | None = None

    @property
    def reconnect(self) -> bool:
        return self.worker_id is not None


class AlreadyPaired(ProtocolError):
    def __init__(self, url: str, expires_at: datetime):
        super().__init__("already_paired")
        self.url, self.expires_at = url, expires_at


def target(root: Path, url: str | None = None, *, now: datetime | None = None) -> Target:
    """A new pairing on an unpaired Mac; a reconnect when the configured key has
    expired, is missing or ends within 7 days; otherwise :class:`AlreadyPaired`."""
    policy = load_worker_settings(root)
    configured = policy.control_service_url
    if configured is None:
        return Target(validate_url(url if url is not None else DEFAULT_SERVER))
    if url is not None and validate_url(url) != validate_url(configured):
        raise ProtocolError("unpair_before_pairing")
    try:
        enrollment = pairing.load_enrollment(configured)
    except ProtocolError as exc:
        if exc.code != "device_expired":
            raise
        enrollment = None
    now = now or datetime.now(timezone.utc)
    if enrollment is not None and enrollment.expires_at - now > RECONNECT_WINDOW:
        raise AlreadyPaired(configured, enrollment.expires_at)
    if policy.control_service_worker_id is None:
        raise ProtocolError("worker_id_unknown")
    return Target(validate_url(configured), policy.control_service_worker_id)


def open_in_browser(url: str) -> bool:
    """``open <url>``: the owner's default browser on this Mac."""
    try:
        return subprocess.run(["open", url], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass(frozen=True)
class ConnectResult:
    worker_id: str
    url: str
    reconnected: bool
    workspace_label: str | None = None
    owner_label: str | None = None


def connect_lock(root: Path) -> FileLock:
    return FileLock(Path(root) / LOCK_NAME, timeout=0)


class ConnectFlow:
    """One Connect attempt. :meth:`start` then :meth:`finish` (or :meth:`run`);
    :meth:`cancel` from any thread stops the wait."""

    def __init__(self, root: Path, url: str | None = None, *, open_browser=open_in_browser,
                 label: str | None = None, timeout: float | None = None, transport=None, opener=None):
        self.root = Path(root)
        self._url = url
        self._open_browser = open_browser
        self._label = label
        self._timeout = timeout
        self._transport = transport
        self._opener = opener
        self._lock: FileLock | None = None
        self._listener: CallbackListener | None = None
        self._cancelled = threading.Event()
        self._verifier: str | None = None
        self.target: Target | None = None
        self.connect_url: str | None = None

    def __repr__(self) -> str:  # never the verifier
        return f"ConnectFlow(target={self.target!r})"

    def cancel(self) -> None:
        self._cancelled.set()
        listener = self._listener
        if listener is not None:
            listener.cancel()

    def start(self) -> str:
        """Lock, discover, listen and open the browser; returns the connect page URL."""
        pairing.require_macos()
        try:
            self._acquire()
            self.target = target(self.root, self._url)
            endpoint = discover(self.target.url, opener=self._opener)
            if self._cancelled.is_set():
                raise ProtocolError("connect_cancelled")
            state = new_state()
            self._verifier = new_verifier()
            self._listener = CallbackListener(state, timeout=WAIT_SECONDS if self._timeout is None else self._timeout)
            if self._cancelled.is_set():
                self._listener.cancel()
            params = {
                "redirect_uri": self._listener.redirect_uri,
                "state": state,
                "code_challenge": code_challenge(self._verifier),
                "code_challenge_method": "S256",
                "label": clean_label(self._label) if self._label is not None else computer_name(),
            }
            if self.target.reconnect:
                params["worker_id"] = self.target.worker_id
            self.connect_url = f"{endpoint}?{urlencode(params)}"
            if not self._open_browser(self.connect_url):
                raise ProtocolError("browser_unavailable")
            return self.connect_url
        except BaseException:
            self.close()
            raise

    def finish(self) -> ConnectResult:
        """Wait for the browser, redeem the code, then answer the tab."""
        listener = self._listener
        if listener is None or self.target is None:
            raise ProtocolError("connect_not_started")
        try:
            query = listener.wait()
            code, error = query.get("code", []), query.get("error", [])
            if error:
                denied = error == ["access_denied"]
                listener.respond(False, "Not paired", "You cancelled in OpenTag. Nothing was paired."
                                 if denied else "OpenTag did not pair this Mac. Start Connect again in OpenSwap.")
                raise ProtocolError("access_denied" if denied else "connect_refused")
            if len(code) != 1 or not code[0] or len(code[0]) > 200:
                listener.respond(False, "Not paired", "OpenTag sent no usable code. Start Connect again in OpenSwap.")
                raise ProtocolError("invalid_callback")
            verifier, self._verifier = self._verifier, None
            # Cancel still counts until the key is saved (``cancelled``).
            try:
                if self.target.reconnect:
                    worker = pairing.reconnect(self.root, self.target.url, code[0], code_verifier=verifier,
                                               transport=self._transport, cancelled=self._cancelled.is_set)
                else:
                    worker = pairing.pair(self.root, self.target.url, code[0], code_verifier=verifier,
                                          transport=self._transport, cancelled=self._cancelled.is_set)
            except ProtocolError as exc:
                if exc.code == "connect_cancelled":
                    listener.respond(False, "Not paired", "You cancelled in OpenSwap. Nothing was saved on this Mac.")
                else:
                    listener.respond(False, "Not paired", f"OpenSwap could not finish ({exc.code}). "
                                     + next_step(exc.code, self.target.url))
                raise
            except Exception:
                listener.respond(False, "Not paired", "OpenSwap could not save the pairing. Start Connect again.")
                raise
            listener.respond(True, "Reconnected" if self.target.reconnect else "Paired",
                             "You can close this tab and go back to OpenSwap.")
            return ConnectResult(str(worker), self.target.url, self.target.reconnect,
                                 getattr(worker, "workspace_label", None), getattr(worker, "owner_label", None))
        finally:
            self.close()

    def run(self) -> ConnectResult:
        self.start()
        return self.finish()

    def _acquire(self) -> None:
        lock = connect_lock(self.root)
        try:
            acquired = lock.acquire(timeout=0)
        except (LockError, OSError):
            acquired = False
        if not acquired:
            raise ProtocolError("connect_in_progress")
        self._lock = lock

    def close(self) -> None:
        """Close the listener and release the connect lock (idempotent)."""
        self._verifier = None
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()


# --- owner-facing copy (CLI and menu bar) ------------------------------------------------------


def pair_command(url: str) -> str:
    return f"openswap worker pair {url} <code>"


def next_step(code: str, url: str | None = None) -> str:
    """What to do after a failed Connect, in one sentence."""
    server = url or DEFAULT_SERVER
    return {
        "connect_in_progress": "Another Connect is already waiting for the browser. Finish or cancel it first.",
        "connect_unsupported": (f"This server has no Connect page. Pair with the command from its Workers "
                                f"page instead: `{pair_command(server)}`."),
        "connect_endpoint_refused": (f"The server named a Connect page on another site, so OpenSwap stopped. "
                                     f"Pair with the command from its Workers page: `{pair_command(server)}`."),
        "access_denied": "You cancelled in the browser. Nothing was paired.",
        "connect_refused": "The server did not pair this Mac. Start Connect again.",
        "connect_cancelled": "Cancelled. Nothing was paired.",
        "connect_timed_out": "Nothing came back from the browser in 10 minutes. Start Connect again.",
        "browser_unavailable": "OpenSwap could not open your browser. Run `openswap worker connect --no-browser`.",
        "unpair_before_pairing": "This Mac is paired with another server. Run `openswap worker unpair` first.",
        "worker_mismatch": "The server returned a different Mac. Nothing changed; this Mac keeps its old key.",
        "worker_id_unknown": "This Mac's worker ID is unknown. Run `openswap worker unpair`, then Connect again.",
        "invalid_code": "The code was refused or has expired. Start Connect again.",
        "https_required": "Use the server's https:// address.",
        "pairing_in_progress": "Another pairing is in progress. Try again in a moment.",
        "device_key_unavailable": "The login Keychain is locked or unavailable. Unlock it and try again.",
        "pairing_unsupported_on_this_platform": "Pairing works only on macOS.",
        "revoked": "This Mac was removed in OpenTag. Run `openswap worker unpair`, then Connect as a new Mac.",
    }.get(code, "Start Connect again, or pair with the command from the Workers page.")
