import hmac
import http.client
import http.server
import threading
import urllib.error

import pytest

from openswap import macos_keychain
from openswap import reset_events_delivery as delivery
from openswap.reset_events import Destination, signature_header

BODY = b'{"id":"evt_1"}'
URL = "https://hooks.example.com/reset"


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Response()


def _http_error(code):
    return urllib.error.HTTPError(URL, code, "error", {}, None)


def _dest():
    return Destination(id="wh_1", kind="webhook", enabled=True, url=URL)


def _deliver(opener, *, secret="s3cret"):
    if secret is not None:
        macos_keychain.set_password(delivery.KEYCHAIN_SERVICE, "wh_1", secret)
    results, sleeps = [], []
    thread = delivery.deliver_async(
        _dest(), BODY,
        on_result=lambda dest_id, error: results.append((dest_id, error)),
        sleep=sleeps.append, opener=opener,
    )
    thread.join(timeout=5)
    assert not thread.is_alive()
    return results, sleeps


def test_validate_rejects_http_non_localhost():
    assert delivery.validate_webhook_url("http://hooks.example.com/x")
    assert delivery.validate_webhook_url("http://192.168.1.5/x")


def test_validate_accepts_http_localhost_and_https():
    for url in (
        "http://localhost:8080/hook",
        "http://127.0.0.1/hook",
        "http://[::1]:9000/hook",
        "http://LOCALHOST/hook",
        "https://hooks.example.com/reset",
    ):
        assert delivery.validate_webhook_url(url) is None, url


def test_validate_rejects_ftp_and_hostless():
    for url in ("ftp://hooks.example.com/x", "https:///path", "hooks.example.com", "", "http://[::1/x"):
        assert delivery.validate_webhook_url(url), url


def test_post_once_sends_signature_and_json_headers():
    opener = FakeOpener(None)
    delivery.post_once(URL, BODY, "s3cret", now=1_700_000_000.7, opener=opener)
    (request, timeout), = opener.requests
    assert request.get_method() == "POST"
    assert request.full_url == URL
    assert request.data == BODY
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 10
    header = request.get_header("Openswap-signature")
    t = int(dict(part.split("=", 1) for part in header.split(","))["t"])
    assert t == 1_700_000_000
    assert hmac.compare_digest(header, signature_header("s3cret", BODY, t))


def test_post_once_raises_on_500():
    with pytest.raises(delivery.DeliveryError, match="HTTP 500"):
        delivery.post_once(URL, BODY, "s", now=0, opener=FakeOpener(_http_error(500)))


def test_post_once_does_not_follow_redirect():
    paths = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            paths.append(self.path)
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(302)
            self.send_header("Location", "/moved")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = do_POST

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(delivery.DeliveryError, match="HTTP 302"):
            delivery.post_once(f"http://127.0.0.1:{server.server_port}/hook", BODY, "s", now=0)
    finally:
        server.shutdown()
        server.server_close()
    assert paths == ["/hook"]


@pytest.mark.parametrize("error, message", [
    (urllib.error.URLError(ConnectionRefusedError("refused")), "refused"),
    (TimeoutError(), "timed out"),
    (http.client.BadStatusLine("garbage"), "garbage"),
])
def test_post_once_wraps_network_failures(error, message):
    with pytest.raises(delivery.DeliveryError, match=message):
        delivery.post_once(URL, BODY, "s", now=0, opener=FakeOpener(error))


def test_deliver_retries_then_reports_last_error():
    opener = FakeOpener(_http_error(502), _http_error(503), _http_error(500))
    results, sleeps = _deliver(opener)
    assert sleeps == [30, 60]
    assert len(opener.requests) == 3
    assert results == [("wh_1", "HTTP 500")]


def test_deliver_success_after_one_failure_reports_empty():
    opener = FakeOpener(_http_error(500), None)
    results, sleeps = _deliver(opener)
    assert sleeps == [30]
    assert results == [("wh_1", "")]


def test_missing_secret_reports_without_network():
    opener = FakeOpener()
    results, sleeps = _deliver(opener, secret=None)
    assert results == [("wh_1", "secret missing; re-add this webhook")]
    assert opener.requests == [] and sleeps == []


def test_keychain_error_reports_without_network(monkeypatch):
    def locked(*_a, **_kw):
        raise macos_keychain.KeychainError("locked")

    monkeypatch.setattr(macos_keychain, "get_password", locked)
    opener = FakeOpener()
    results, _ = _deliver(opener, secret=None)
    (dest_id, error), = results
    assert dest_id == "wh_1" and "Keychain" in error and "locked" in error
    assert opener.requests == []


def test_secret_lifecycle_uses_reset_events_service(block_real_keychain):
    secret = delivery.new_secret("wh_1")
    assert block_real_keychain.data[("OpenSwap reset events", "wh_1")] == secret
    assert delivery.get_secret("wh_1") == secret
    delivery.delete_secret("wh_1")
    assert delivery.get_secret("wh_1") is None
