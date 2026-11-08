"""Webhook delivery for reset events: signed POSTs on a background thread.

Each destination's signing secret lives in the Keychain, keyed by its id.
A delivery makes three attempts, then gives up and reports the last error.
"""

from __future__ import annotations

import http.client
import secrets
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

from openswap import macos_keychain
from openswap.reset_events import Destination, signature_header

KEYCHAIN_SERVICE = "OpenSwap reset events"
RETRY_DELAYS_S = (30, 60)
TIMEOUT_S = 10
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class DeliveryError(Exception):
    pass


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _open_without_redirects(request: urllib.request.Request, timeout: float):
    # Built per call so the HTTPS context picks up the truststore injection
    # even when this module is imported before it.
    return urllib.request.build_opener(_RefuseRedirect).open(request, timeout=timeout)


def validate_webhook_url(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "Webhook URL is not valid."
    if parts.scheme not in ("http", "https"):
        return "Webhook URL must start with https://."
    if not host:
        return "Webhook URL needs a host."
    if parts.scheme == "http" and host not in _LOCAL_HOSTS:
        return "Webhook URL must use https:// unless the host is localhost."
    return None


def new_secret(dest_id: str) -> str:
    secret = secrets.token_urlsafe(32)
    macos_keychain.set_password(KEYCHAIN_SERVICE, dest_id, secret)
    return secret


def get_secret(dest_id: str) -> str | None:
    return macos_keychain.get_password(KEYCHAIN_SERVICE, dest_id)


def delete_secret(dest_id: str) -> None:
    macos_keychain.delete_password(KEYCHAIN_SERVICE, dest_id)


def post_once(
    url: str, body: bytes, secret: str, *, now: float, opener=_open_without_redirects
) -> None:
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "OpenSwap-Signature": signature_header(secret, body, int(now)),
        },
    )
    try:
        with opener(request, timeout=TIMEOUT_S):
            pass
    except urllib.error.HTTPError as e:
        raise DeliveryError(f"HTTP {e.code}") from None
    except TimeoutError:
        raise DeliveryError(f"timed out after {TIMEOUT_S} s") from None
    except urllib.error.URLError as e:
        raise DeliveryError(str(e.reason) or "connection failed") from None
    except (OSError, http.client.HTTPException) as e:
        raise DeliveryError(str(e) or type(e).__name__) from None


def deliver_async(
    dest: Destination,
    body: bytes,
    *,
    on_result: Callable[[str, str], None],
    sleep=time.sleep,
    opener=_open_without_redirects,
) -> threading.Thread:
    thread = threading.Thread(
        target=_deliver,
        args=(dest, body, on_result, sleep, opener),
        name=f"reset-webhook-{dest.id}",
        daemon=True,
    )
    thread.start()
    return thread


def _deliver(dest, body, on_result, sleep, opener) -> None:
    try:
        secret = get_secret(dest.id)
    except macos_keychain.KEYCHAIN_ERRORS as e:
        on_result(dest.id, f"Keychain unavailable: {e}")
        return
    if not secret:
        on_result(dest.id, "secret missing; re-add this webhook")
        return
    error = ""
    for delay in (0, *RETRY_DELAYS_S):
        if delay:
            sleep(delay)
        try:
            post_once(dest.url, body, secret, now=time.time(), opener=opener)
        except DeliveryError as e:
            error = str(e)
        else:
            on_result(dest.id, "")
            return
    on_result(dest.id, error)
