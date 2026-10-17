"""Connect to OpenTag (plan 022, phase S2): pair or reconnect this Mac in the browser.

A stub server plays OpenTag: the well-known document, a connect page that
redirects back to the loopback listener, and ``/v1/pair`` checking the PKCE
verifier. A fake browser follows that redirect. Keychain access is mocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from openswap import macos_keychain, menubar
from openswap.settings import configure_worker_service, load_worker_settings
from openswap.worker import cli, connect, guided_setup, pairing
from openswap.worker.protocol import ProtocolError
from tests.test_worker_pairing_status import keychain  # noqa: F401 (fixture)

# RFC 7636 appendix B.
RFC_OCTETS = bytes([116, 24, 223, 180, 151, 153, 224, 37, 79, 250, 96, 125, 216, 173, 187, 186, 22, 212,
                    37, 77, 105, 214, 191, 240, 91, 88, 5, 88, 83, 132, 141, 121])
RFC_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
RFC_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
_COMPUTER_NAME = connect.computer_name  # before the autouse fixture replaces it


def _later(days=30):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


@pytest.fixture(autouse=True)
def _no_ssh_and_fixed_name(monkeypatch):
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(connect, "computer_name", lambda *_a, **_k: "Test Mac")
    monkeypatch.setattr(connect, "WAIT_SECONDS", 20.0)


# --- the stub OpenTag and the fake browser ------------------------------------------------------


class StubOpenTag:
    """Well-known document, connect page and ``/v1/pair`` on loopback."""

    def __init__(self, *, worker_id="worker-1", deny=False, pair_error=None, document=None):
        self.worker_id, self.deny, self.pair_error, self.document = worker_id, deny, pair_error, document
        self.connect_params: list[dict] = []
        self.pair_requests: list[dict] = []
        self.codes: dict[str, str] = {}  # pair code -> challenge
        self.tab_answered_at_pair: list[bool] = []
        self.browser = None
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _json(self, status, value):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                address = urlsplit(self.path)
                if address.path == "/.well-known/openswap-worker":
                    document = stub.document if stub.document is not None else {
                        "protocol": "v1", "connect_endpoint": stub.url + "/workers/connect"}
                    if document == 404:
                        return self._json(404, {"error": "not_found"})
                    return self._json(200, document)
                if address.path == "/workers/connect":
                    params = {k: v[0] for k, v in parse_qs(address.query).items()}
                    stub.connect_params.append(params)
                    if stub.deny:
                        query = {"error": "access_denied", "state": params["state"]}
                    else:
                        code = f"pair_{len(stub.codes) + 1}"
                        stub.codes[code] = params["code_challenge"]
                        query = {"code": code, "state": params["state"]}
                    self.send_response(302)
                    self.send_header("Location", f"{params['redirect_uri']}?{urlencode(query)}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                return self._json(404, {"error": "not_found"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.pair_requests.append(body)
                stub.tab_answered_at_pair.append(bool(stub.browser and stub.browser.answered.is_set()))
                if stub.pair_error:
                    return self._json(400, {"error": stub.pair_error})
                challenge = stub.codes.pop(body.get("code"), None)
                verifier = body.get("code_verifier", "")
                expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
                if challenge is None or expected.decode() != challenge:
                    return self._json(400, {"error": "invalid_code"})
                return self._json(200, {"worker_id": stub.worker_id, "device_key": "connect-device-key",
                                        "expires_at": _later(), "workspace_label": "Acme",
                                        "owner_label": "Owner"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeBrowser:
    """Opens the connect page, follows its redirect to the loopback listener and keeps the tab's page."""

    def __init__(self, stub=None, *, gate: threading.Event | None = None):
        self.gate = gate  # follow the page only once this is set (the owner reads the dialog first)
        self.opened: list[str] = []
        self.page = None
        self.status = None
        self.answered = threading.Event()
        if stub is not None:
            stub.browser = self

    def __call__(self, url):
        self.opened.append(url)
        threading.Thread(target=self._follow, args=(url,), daemon=True).start()
        return True

    @staticmethod
    def _get(url):
        parts = urlsplit(url)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=30)
        conn.request("GET", parts.path + ("?" + parts.query if parts.query else ""))
        response = conn.getresponse()
        return response.status, response.getheader("Location"), response.read().decode()

    def _follow(self, url):
        if self.gate is not None:
            assert self.gate.wait(20)
        status, location, _ = self._get(url)
        assert status == 302
        self.status, _, self.page = self._get(location)
        self.answered.set()


@pytest.fixture
def stub():
    server = StubOpenTag()
    yield server
    server.close()


# --- PKCE and state ----------------------------------------------------------------------------


def test_pkce_matches_rfc_7636_appendix_b():
    assert connect.new_verifier(RFC_OCTETS) == RFC_VERIFIER
    assert connect.code_challenge(RFC_VERIFIER) == RFC_CHALLENGE


def test_fresh_verifier_and_state_have_the_documented_sizes():
    verifier, state = connect.new_verifier(), connect.new_state()
    assert len(verifier) == 43 and len(state) == 22 and verifier != connect.new_verifier()
    assert set(verifier + state) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


def test_label_is_cut_to_64_printable_characters():
    assert connect.clean_label("Yohan’s\tMacBook\x00 Pro\n") == "Yohan’s MacBook Pro"
    assert len(connect.clean_label("M" * 100)) == 64


def test_computer_name_reads_scutil():
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="Studio " + "x" * 80 + "\n")

    name = _COMPUTER_NAME(run)
    assert calls == [["scutil", "--get", "ComputerName"]] and name.startswith("Studio ") and len(name) == 64
    assert _COMPUTER_NAME(lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="")) != ""


# --- the listener ------------------------------------------------------------------------------


def _request(port, target, method="GET"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(method, target)
    response = conn.getresponse()
    return response.status, response.read().decode()


def _waiting(listener):
    box = {}

    def run():
        try:
            box["query"] = listener.wait()
        except ProtocolError as exc:
            box["error"] = exc.code

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def test_listener_binds_only_to_loopback_ipv4():
    listener = connect.CallbackListener("s" * 22, timeout=5)
    try:
        assert listener._server.getsockname()[0] == "127.0.0.1"
        assert listener.redirect_uri == f"http://127.0.0.1:{listener.port}/openswap/callback"
    finally:
        listener.close()
    assert listener.closed


def test_listener_refuses_wrong_state_paths_and_methods_then_accepts_one_request():
    state = connect.new_state()
    listener = connect.CallbackListener(state, timeout=20)
    thread, box = _waiting(listener)
    port = listener.port
    status, body = _request(port, f"/openswap/callback?code=evil&state={'x' * 22}")
    assert status == 400 and "not from your Connect" in body
    assert _request(port, f"/openswap/callback?code=c&state={state}&state={state}")[0] == 400
    assert _request(port, "/openswap/callback")[0] == 400
    assert _request(port, f"/other?code=c&state={state}")[0] == 404
    assert _request(port, "/favicon.ico")[0] == 404
    assert _request(port, f"/openswap/callback?code=c&state={state}", method="POST")[0] == 405
    assert thread.is_alive()  # still waiting after every refusal

    answer = {}
    client = threading.Thread(target=lambda: answer.update(
        zip(("status", "body"), _request(port, f"/openswap/callback?code=pair_1&state={state}"))))
    client.start()
    thread.join(10)
    assert box["query"]["code"] == ["pair_1"] and listener.closed
    # The page waits for the result (sent after redemption).
    assert client.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    listener.respond(True, "Paired", "You can close this tab.")
    client.join(10)
    assert answer["status"] == 200 and "Paired" in answer["body"]


def test_listener_times_out_and_closes():
    listener = connect.CallbackListener(connect.new_state(), timeout=0.3)
    started = time.monotonic()
    with pytest.raises(ProtocolError, match="connect_timed_out"):
        listener.wait()
    assert time.monotonic() - started < 5 and listener.closed


def test_listener_cancel_closes_it():
    listener = connect.CallbackListener(connect.new_state(), timeout=20)
    thread, box = _waiting(listener)
    listener.cancel()
    thread.join(5)
    assert box == {"error": "connect_cancelled"} and listener.closed



def test_listener_accepts_a_callback_carrying_large_cookies():
    # Browsers send every cookie they hold for 127.0.0.1, whatever the port.
    state = connect.new_state()
    listener = connect.CallbackListener(state, timeout=20)
    thread, box = _waiting(listener)
    with socket.create_connection(("127.0.0.1", listener.port), timeout=10) as client:
        client.sendall(f"GET /openswap/callback?code=pair_1&state={state} HTTP/1.1\r\n"
                       f"Host: 127.0.0.1\r\nCookie: big={'x' * 20_000}\r\n\r\n".encode())
        thread.join(10)
        assert box["query"]["code"] == ["pair_1"]
        listener.respond(True, "Paired", "You can close this tab.")
        assert client.recv(64).startswith(b"HTTP/1.1 200")


def test_a_slow_local_client_cannot_hold_cancel_or_the_deadline():
    listener = connect.CallbackListener(connect.new_state(), timeout=20)
    thread, box = _waiting(listener)
    with socket.create_connection(("127.0.0.1", listener.port), timeout=10) as client:
        client.sendall(b"G")  # and nothing more for now
        time.sleep(0.3)
        started = time.monotonic()
        listener.cancel()
        thread.join(5)
        assert box == {"error": "connect_cancelled"} and time.monotonic() - started < 2
    listener = connect.CallbackListener(connect.new_state(), timeout=1)
    started = time.monotonic()
    with socket.create_connection(("127.0.0.1", listener.port), timeout=10) as client:
        dripping = threading.Event()

        def drip():
            while not dripping.is_set():
                try:
                    client.sendall(b"G")
                except OSError:
                    return
                dripping.wait(0.1)
        dripper = threading.Thread(target=drip, daemon=True)
        dripper.start()
        try:
            with pytest.raises(ProtocolError, match="connect_timed_out"):
                listener.wait()
        finally:
            dripping.set()
    assert time.monotonic() - started < connect.REQUEST_TIMEOUT + 3  # one connection's bound, not forever


def test_discovery_against_something_that_is_not_http_is_unavailable():
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def answer():
        conn, _ = server.accept()
        with conn:
            conn.recv(4096)
            conn.sendall(b"SSH-2.0-OpenSSH_9.0\r\n")
    threading.Thread(target=answer, daemon=True).start()
    try:
        with pytest.raises(ProtocolError, match="service_unavailable"):
            connect.discover(f"http://127.0.0.1:{server.getsockname()[1]}")
    finally:
        server.close()


# --- discovery ---------------------------------------------------------------------------------


def test_discovery_returns_the_same_origin_endpoint(stub):
    assert connect.discover(stub.url) == stub.url + "/workers/connect"


@pytest.mark.parametrize("document", [
    404,
    {"protocol": "v1"},
    {"protocol": "v2", "connect_endpoint": "/workers/connect"},
    {"protocol": "v1", "connect_endpoint": "{url}/workers/connect", "extra": True},
    {"protocol": "v1", "connect_endpoint": "{url}/workers/connect?x=1"},
    {"protocol": "v1", "connect_endpoint": "{url}"},
    {"protocol": "v1", "connect_endpoint": 7},
    ["not", "an", "object"],
])
def test_a_missing_or_malformed_document_falls_back_to_the_pasted_command(document):
    server = StubOpenTag()
    try:
        if isinstance(document, dict) and isinstance(document.get("connect_endpoint"), str):
            document = {**document, "connect_endpoint": document["connect_endpoint"].format(url=server.url)}
        server.document = document
        with pytest.raises(ProtocolError) as refused:
            connect.discover(server.url)
        assert refused.value.code == "connect_unsupported"
        assert "openswap worker pair" in connect.next_step("connect_unsupported", server.url)
    finally:
        server.close()


@pytest.mark.parametrize("endpoint", [
    "https://evil.example/workers/connect",
    "http://127.0.0.1:1/workers/connect",
    "http://localhost:{port}/workers/connect",
])
def test_a_connect_endpoint_on_another_origin_is_refused(endpoint):
    server = StubOpenTag()
    try:
        server.document = {"protocol": "v1",
                           "connect_endpoint": endpoint.format(port=server.server.server_port)}
        with pytest.raises(ProtocolError, match="connect_endpoint_refused"):
            connect.discover(server.url)
    finally:
        server.close()


def test_discovery_refuses_redirects_and_non_json():
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):
            if self.path.startswith("/.well-known"):
                self.send_response(302)
                self.send_header("Location", "https://evil.example/.well-known/openswap-worker")
                self.send_header("Content-Length", "0")
                self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ProtocolError, match="connect_unsupported"):
            connect.discover(f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(ProtocolError, match="connect_unsupported"):
        connect.connect_endpoint("https://opentag.me", {"protocol": "v1", "connect_endpoint": None})
    with pytest.raises(ProtocolError, match="https_required"):
        connect.discover("http://opentag.me")


def test_discovery_of_an_unreachable_server_is_unavailable():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ProtocolError, match="service_unavailable"):
        connect.discover(f"http://127.0.0.1:{port}")


# --- pair with and without a verifier ----------------------------------------------------------


class LabelTransport:
    def __init__(self, response=None, error=None):
        self.requests = []
        self.response, self.error = response, error

    def request(self, operation, data):
        self.requests.append((operation, data))
        if self.error:
            raise ProtocolError(self.error)
        return dict(self.response)


def _pair_response(worker_id="worker-1", **extra):
    return {"worker_id": worker_id, "device_key": "new-device-key", "expires_at": _later(), **extra}


def test_pair_with_a_verifier_accepts_the_labels(tmp_path, keychain):
    transport = LabelTransport(_pair_response(workspace_label="Acme", owner_label="Owner"))
    worker = pairing.pair(tmp_path, "http://localhost", "pair_1", transport=transport, code_verifier=RFC_VERIFIER)
    assert worker == "worker-1" and (worker.workspace_label, worker.owner_label) == ("Acme", "Owner")
    assert transport.requests == [("pair", {"code": "pair_1", "code_verifier": RFC_VERIFIER})]
    assert load_worker_settings(tmp_path).control_service_worker_id == "worker-1"
    assert RFC_VERIFIER not in (tmp_path / "settings.json").read_text()


def test_pair_with_a_verifier_accepts_a_response_without_labels(tmp_path, keychain):
    worker = pairing.pair(tmp_path, "http://localhost", "pair_1", transport=LabelTransport(_pair_response()),
                          code_verifier=RFC_VERIFIER)
    assert worker == "worker-1" and worker.workspace_label is None


def test_pair_without_a_verifier_keeps_the_closed_shape(tmp_path, keychain):
    transport = LabelTransport(_pair_response(workspace_label="Acme", owner_label="Owner"))
    with pytest.raises(ProtocolError, match="invalid_request"):
        pairing.pair(tmp_path, "http://localhost", "one-use", transport=transport)
    assert transport.requests == [("pair", {"code": "one-use"})]
    assert not keychain and load_worker_settings(tmp_path).control_service_url is None


def test_pair_with_a_verifier_refuses_unknown_fields(tmp_path, keychain):
    with pytest.raises(ProtocolError, match="invalid_request"):
        pairing.pair(tmp_path, "http://localhost", "pair_1", transport=LabelTransport(_pair_response(surprise=1)),
                     code_verifier=RFC_VERIFIER)
    assert not keychain


@pytest.mark.parametrize("extra, shown", [
    ({"workspace_label": "Bad\x07name"}, "Badname"), ({"workspace_label": ""}, None),
    ({"workspace_label": 3}, None), ({"workspace_label": "W" * 500}, "W" * 200),
    ({"workspace_label": "Acme\u202egnp.exe"}, "Acmegnp.exe"),  # no bidi override reorders it
    ({"workspace_label": "Ac\u200bme\ufeff"}, "Acme"), ({"workspace_label": " Two\n\tlines "}, "Two lines"),
    ({"workspace_label": "\u2066\u2069"}, None),
])
def test_odd_labels_never_fail_a_pairing(tmp_path, keychain, extra, shown):
    # The code is already consumed and the key issued: a label is only for display.
    worker = pairing.pair(tmp_path, "http://localhost", "pair_1", transport=LabelTransport(_pair_response(**extra)),
                          code_verifier=RFC_VERIFIER)
    assert worker.workspace_label == shown and keychain


# --- reconnect ---------------------------------------------------------------------------------


URL = "http://localhost"


def _paired(root, keychain, *, days=-1, worker_id="worker-1"):
    configure_worker_service(root, URL, worker_id)
    old = json.dumps({"worker_id": worker_id, "device_key": "old-device-key", "expires_at": _later(days)})
    keychain[("openswap", pairing.account_name(URL))] = old
    return old


def test_reconnect_replaces_the_key_in_place(tmp_path, keychain):
    _paired(tmp_path, keychain)
    (tmp_path / "worker").mkdir()
    (tmp_path / "worker" / "remote-status.json").write_text("{}")
    worker = pairing.reconnect(tmp_path, URL, "pair_1", code_verifier=RFC_VERIFIER,
                               transport=LabelTransport(_pair_response(workspace_label="Acme")))
    assert worker == "worker-1" and worker.workspace_label == "Acme"
    saved = json.loads(keychain[("openswap", pairing.account_name(URL))])
    assert saved["device_key"] == "new-device-key" and len(keychain) == 1
    assert pairing.load_enrollment(URL).device_key == "new-device-key"
    policy = load_worker_settings(tmp_path)
    assert (policy.control_service_url, policy.control_service_worker_id) == (URL, "worker-1")
    # The old status is gone; the new one holds only the new key's expiry.
    status = json.loads((tmp_path / "worker" / "remote-status.json").read_text())
    assert status["state"] == "offline" and status["last_seen_at"] is None
    assert status["key_expires_at"] == pairing.load_enrollment(URL).expires_at.isoformat()
    assert "new-device-key" not in json.dumps(status)


@pytest.mark.parametrize("transport, code", [
    (LabelTransport(_pair_response(worker_id="someone-else")), "worker_mismatch"),
    (LabelTransport(error="invalid_code"), "invalid_code"),
    (LabelTransport(error="service_unavailable"), "service_unavailable"),
    (LabelTransport({"worker_id": "worker-1"}), "invalid_request"),
    (LabelTransport(_pair_response(expires_at="2000-01-01T00:00:00Z")), "device_expired"),
])
def test_reconnect_keeps_the_old_key_on_any_failure(tmp_path, keychain, transport, code):
    old = _paired(tmp_path, keychain)
    with pytest.raises(ProtocolError) as refused:
        pairing.reconnect(tmp_path, URL, "pair_1", code_verifier=RFC_VERIFIER, transport=transport)
    assert refused.value.code == code
    assert keychain == {("openswap", pairing.account_name(URL)): old}
    assert load_worker_settings(tmp_path).control_service_worker_id == "worker-1"


def test_reconnect_keeps_the_old_key_when_the_keychain_write_fails(tmp_path, keychain, monkeypatch):
    old = _paired(tmp_path, keychain)

    def locked(*_args):
        raise macos_keychain.KeychainError("locked")

    monkeypatch.setattr(macos_keychain, "set_password", locked)
    with pytest.raises(ProtocolError, match="device_key_unavailable"):
        pairing.reconnect(tmp_path, URL, "pair_1", code_verifier=RFC_VERIFIER,
                          transport=LabelTransport(_pair_response()))
    assert keychain == {("openswap", pairing.account_name(URL)): old}


def test_reconnect_needs_the_configured_server(tmp_path, keychain):
    with pytest.raises(ProtocolError, match="not_paired_with_this_server"):
        pairing.reconnect(tmp_path, URL, "pair_1", code_verifier=RFC_VERIFIER,
                          transport=LabelTransport(_pair_response()))
    _paired(tmp_path, keychain)
    with pytest.raises(ProtocolError, match="not_paired_with_this_server"):
        pairing.reconnect(tmp_path, "https://other.example", "pair_1", code_verifier=RFC_VERIFIER,
                          transport=LabelTransport(_pair_response()))


# --- which Connect a Mac needs -----------------------------------------------------------------


def test_target_is_a_new_pairing_on_an_unpaired_mac(tmp_path, keychain):
    assert connect.target(tmp_path) == connect.Target("https://opentag.me")
    assert connect.target(tmp_path, "http://127.0.0.1:9/") == connect.Target("http://127.0.0.1:9")


@pytest.mark.parametrize("days, reconnect", [(-1, True), (3, True), (6.9, True), (8, False), (29, False)])
def test_target_reconnects_an_expired_or_expiring_key(tmp_path, keychain, days, reconnect):
    _paired(tmp_path, keychain, days=days)
    if reconnect:
        assert connect.target(tmp_path) == connect.Target(URL, "worker-1")
    else:
        with pytest.raises(connect.AlreadyPaired) as paired:
            connect.target(tmp_path)
        assert paired.value.url == URL


def test_target_reconnects_a_missing_key_and_refuses_another_server(tmp_path, keychain):
    configure_worker_service(tmp_path, URL, "worker-1")
    assert connect.target(tmp_path) == connect.Target(URL, "worker-1")
    with pytest.raises(ProtocolError, match="unpair_before_pairing"):
        connect.target(tmp_path, "https://other.example")


# --- the connect lock --------------------------------------------------------------------------


def test_a_second_connect_is_refused_before_any_listener_or_page(tmp_path, keychain, monkeypatch):
    holder = connect.connect_lock(tmp_path)
    assert holder.acquire(timeout=0)
    try:
        monkeypatch.setattr(connect, "CallbackListener", lambda *_a, **_k: pytest.fail("listener opened"))
        monkeypatch.setattr(connect, "discover", lambda *_a, **_k: pytest.fail("discovery ran"))
        browser = FakeBrowser()
        with pytest.raises(ProtocolError, match="connect_in_progress"):
            connect.ConnectFlow(tmp_path, URL, open_browser=browser).start()
        assert browser.opened == []
    finally:
        holder.release()
    assert (tmp_path / ".worker-connect.lock").exists()
    assert not (tmp_path / ".worker-pairing.lock").exists()


def test_the_connect_lock_is_held_while_waiting_and_freed_after(tmp_path, keychain, stub):
    first = connect.ConnectFlow(tmp_path, stub.url, open_browser=lambda _url: True)
    first.start()
    try:
        with pytest.raises(ProtocolError, match="connect_in_progress"):
            connect.ConnectFlow(tmp_path, stub.url, open_browser=lambda _url: True).start()
        # Unpair and pairing are not blocked by a waiting Connect.
        with pairing._pairing_lock(tmp_path):
            pass
    finally:
        first.cancel()
        with pytest.raises(ProtocolError, match="connect_cancelled"):
            first.finish()
    probe = connect.connect_lock(tmp_path)
    assert probe.acquire(timeout=0)
    probe.release()


def test_a_failed_start_frees_the_lock(tmp_path, keychain):
    server = StubOpenTag(document=404)
    try:
        with pytest.raises(ProtocolError, match="connect_unsupported"):
            connect.ConnectFlow(tmp_path, server.url, open_browser=FakeBrowser()).start()
    finally:
        server.close()
    probe = connect.connect_lock(tmp_path)
    assert probe.acquire(timeout=0)
    probe.release()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX kill semantics; the lock is per descriptor everywhere")
def test_a_crashed_holder_frees_the_connect_lock(tmp_path):
    script = ("import sys, time\n"
              "from pathlib import Path\n"
              "from openswap.worker.connect import connect_lock\n"
              "lock = connect_lock(Path(sys.argv[1]))\n"
              "assert lock.acquire(timeout=0)\n"
              "print('locked', flush=True)\n"
              "time.sleep(60)\n")
    holder = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        probe = connect.connect_lock(tmp_path)
        assert not probe.acquire(timeout=0)
        holder.kill()
        holder.wait(10)
        assert probe.acquire(timeout=0)
        probe.release()
    finally:
        if holder.poll() is None:
            holder.kill()
        holder.stdout.close()


# --- end to end with the fake browser ----------------------------------------------------------


def _files_text(root: Path) -> str:
    return "".join(path.read_bytes().decode("utf-8", "replace") for path in root.rglob("*") if path.is_file())


def test_connect_pairs_a_new_mac_end_to_end(tmp_path, keychain, stub, caplog):
    browser = FakeBrowser(stub)
    flow = connect.ConnectFlow(tmp_path, stub.url, open_browser=browser)
    result = flow.run()
    assert (result.worker_id, result.workspace_label, result.reconnected) == ("worker-1", "Acme", False)
    (params,) = stub.connect_params
    assert set(params) == {"redirect_uri", "state", "code_challenge", "code_challenge_method", "label"}
    assert params["code_challenge_method"] == "S256" and params["label"] == "Test Mac"
    assert urlsplit(params["redirect_uri"]).hostname == "127.0.0.1"
    assert urlsplit(params["redirect_uri"]).path == "/openswap/callback"
    (request,) = stub.pair_requests
    assert connect.code_challenge(request["code_verifier"]) == params["code_challenge"]
    # The tab got its page only after redemption, and it says paired.
    assert stub.tab_answered_at_pair == [False]
    assert browser.answered.wait(10) and browser.status == 200 and "This Mac is paired" in browser.page
    assert "<dd>Acme</dd>" in browser.page and request["code"] not in browser.page
    # Paired with a server other than OpenTag: the page names that server.
    assert f"{urlsplit(stub.url).netloc} can now send tasks" in browser.page and "OpenTag can" not in browser.page
    assert load_worker_settings(tmp_path).control_service_url == stub.url
    assert pairing.load_enrollment(stub.url).device_key == "connect-device-key"
    # The verifier and the code are never written or logged.
    assert request["code_verifier"] not in _files_text(tmp_path) and request["code"] not in _files_text(tmp_path)
    assert request["code_verifier"] not in caplog.text and request["code_verifier"] not in repr(flow)
    probe = connect.connect_lock(tmp_path)
    assert probe.acquire(timeout=0)
    probe.release()


def test_connect_reconnects_an_expired_mac_keeping_its_worker_id(tmp_path, keychain, stub):
    configure_worker_service(tmp_path, stub.url, "worker-1")
    keychain[("openswap", pairing.account_name(stub.url))] = json.dumps(
        {"worker_id": "worker-1", "device_key": "old-device-key", "expires_at": _later(-1)})
    browser = FakeBrowser(stub)
    result = connect.ConnectFlow(tmp_path, None, open_browser=browser).run()
    assert result.reconnected and result.worker_id == "worker-1"
    assert stub.connect_params[0]["worker_id"] == "worker-1"
    assert pairing.load_enrollment(stub.url).device_key == "connect-device-key"
    assert browser.answered.wait(10) and "This Mac is reconnected" in browser.page


def test_a_reconnect_returning_another_worker_keeps_the_old_key(tmp_path, keychain):
    server = StubOpenTag(worker_id="someone-else")
    try:
        configure_worker_service(tmp_path, server.url, "worker-1")
        old = json.dumps({"worker_id": "worker-1", "device_key": "old-device-key", "expires_at": _later(-1)})
        keychain[("openswap", pairing.account_name(server.url))] = old
        browser = FakeBrowser(server)
        with pytest.raises(ProtocolError, match="worker_mismatch"):
            connect.ConnectFlow(tmp_path, None, open_browser=browser).run()
        assert keychain[("openswap", pairing.account_name(server.url))] == old
        assert browser.answered.wait(10) and browser.status == 400 and "worker_mismatch" in browser.page
    finally:
        server.close()


def test_owner_cancel_in_the_portal_stops_the_mac(tmp_path, keychain):
    server = StubOpenTag(deny=True)
    try:
        browser = FakeBrowser(server)
        with pytest.raises(ProtocolError, match="access_denied"):
            connect.ConnectFlow(tmp_path, server.url, open_browser=browser).run()
        assert server.pair_requests == [] and not keychain
        assert browser.answered.wait(10) and "cancelled" in browser.page
    finally:
        server.close()



@pytest.mark.parametrize("reconnect", [False, True])
def test_cancel_during_redemption_saves_nothing(tmp_path, keychain, stub, reconnect):
    from openswap.worker.remote import Transport

    old = json.dumps({"worker_id": "worker-1", "device_key": "old-device-key", "expires_at": _later(-1)})
    if reconnect:
        configure_worker_service(tmp_path, stub.url, "worker-1")
        keychain[("openswap", pairing.account_name(stub.url))] = old
    flows = []

    class CancelledMidway:
        def request(self, operation, data):
            flows[0].cancel()  # the owner presses Cancel while the code is being redeemed
            return Transport(stub.url).request(operation, data)
    browser = FakeBrowser(stub)
    flows.append(connect.ConnectFlow(tmp_path, None if reconnect else stub.url, open_browser=browser,
                                     transport=CancelledMidway()))
    with pytest.raises(ProtocolError, match="connect_cancelled"):
        flows[0].run()
    assert len(stub.pair_requests) == 1
    if reconnect:
        assert keychain[("openswap", pairing.account_name(stub.url))] == old  # the new key stays unused
        assert load_worker_settings(tmp_path).control_service_worker_id == "worker-1"
    else:
        assert not keychain and load_worker_settings(tmp_path).control_service_url is None
    assert browser.answered.wait(10) and browser.status == 400 and "cancelled in OpenSwap" in browser.page


def test_a_refused_redemption_shows_the_error_in_the_tab(tmp_path, keychain):
    server = StubOpenTag(pair_error="invalid_code")
    try:
        browser = FakeBrowser(server)
        with pytest.raises(ProtocolError, match="invalid_code"):
            connect.ConnectFlow(tmp_path, server.url, open_browser=browser).run()
        assert browser.answered.wait(10) and browser.status == 400 and "invalid_code" in browser.page
        assert server.tab_answered_at_pair == [False]
        assert not keychain and load_worker_settings(tmp_path).control_service_url is None
    finally:
        server.close()


def test_a_healthy_paired_mac_is_already_paired(tmp_path, keychain, stub):
    _paired(tmp_path, keychain, days=20)
    browser = FakeBrowser()
    with pytest.raises(connect.AlreadyPaired):
        connect.ConnectFlow(tmp_path, None, open_browser=browser).start()
    assert browser.opened == []


def test_a_browser_that_cannot_open_frees_everything(tmp_path, keychain, stub):
    with pytest.raises(ProtocolError, match="browser_unavailable"):
        connect.ConnectFlow(tmp_path, stub.url, open_browser=lambda _url: False).start()
    probe = connect.connect_lock(tmp_path)
    assert probe.acquire(timeout=0)
    probe.release()


# --- CLI ---------------------------------------------------------------------------------------


def _run(root, *argv):
    return cli.main(list(argv), backup_root=root)


@pytest.fixture
def guided(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_guided_setup", lambda root, **_k: calls.append(root))
    return calls


def test_cli_connect_opens_the_browser_and_runs_the_setup(tmp_path, keychain, stub, monkeypatch, capsys, guided):
    browser = FakeBrowser(stub)
    monkeypatch.setattr(connect, "open_in_browser", browser)
    assert _run(tmp_path, "connect", stub.url) == 0
    out = capsys.readouterr().out
    assert "Opened OpenTag in your browser." in out and "select Pair this Mac" in out
    assert "✓ Paired with Acme (worker-1)." in out and guided == [tmp_path]
    assert stub.pair_requests[0]["code_verifier"] not in out


def test_cli_connect_no_browser_prints_the_address(tmp_path, keychain, stub, monkeypatch, capsys, guided):
    monkeypatch.setattr(connect, "open_in_browser", lambda _url: pytest.fail("browser opened"))
    browser = FakeBrowser(stub)
    real = connect.ConnectFlow

    class Following(real):
        def __init__(self, *args, open_browser, **kwargs):
            super().__init__(*args, open_browser=lambda url: open_browser(url) and browser(url), **kwargs)

    monkeypatch.setattr(connect, "ConnectFlow", Following)
    assert _run(tmp_path, "connect", stub.url, "--no-browser") == 0
    out = capsys.readouterr().out
    assert "Open this address in a browser on this Mac:" in out and f"  {browser.opened[0]}" in out
    assert "✓ Paired with Acme" in out


def test_cli_connect_over_ssh_points_to_pair(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 5000 10.0.0.3 22")
    for argv in (["connect"], ["connect", "--no-browser"]):
        assert _run(tmp_path, *argv) == 1
        err = capsys.readouterr().err
        assert "SSH" in err and "`openswap worker pair https://opentag.me <code>`" in err
    assert not (tmp_path / ".worker-connect.lock").exists()


def test_cli_connect_on_a_healthy_paired_mac_stops(tmp_path, keychain, monkeypatch, capsys):
    _paired(tmp_path, keychain, days=20)
    monkeypatch.setattr(connect, "open_in_browser", lambda _url: pytest.fail("browser opened"))
    assert _run(tmp_path, "connect") == 0
    assert "already paired with http://localhost" in capsys.readouterr().out


def test_cli_connect_reconnects_an_expired_mac(tmp_path, keychain, stub, monkeypatch, capsys, guided):
    configure_worker_service(tmp_path, stub.url, "worker-1")
    keychain[("openswap", pairing.account_name(stub.url))] = json.dumps(
        {"worker_id": "worker-1", "device_key": "old-device-key", "expires_at": _later(-1)})
    monkeypatch.setattr(connect, "open_in_browser", FakeBrowser(stub))
    assert _run(tmp_path, "connect") == 0
    out = capsys.readouterr().out
    assert "select Reconnect" in out and "✓ Reconnected this Mac (worker-1)" in out and guided == []


def test_cli_connect_falls_back_when_the_server_has_no_connect_page(tmp_path, keychain, capsys):
    server = StubOpenTag(document=404)
    try:
        assert _run(tmp_path, "connect", server.url, "--no-browser") == 1
        err = capsys.readouterr().err
        assert "connect_unsupported" in err and f"`openswap worker pair {server.url} <code>`" in err
    finally:
        server.close()


def test_cli_connect_refuses_a_second_connect(tmp_path, keychain, capsys):
    holder = connect.connect_lock(tmp_path)
    assert holder.acquire(timeout=0)
    try:
        assert _run(tmp_path, "connect", "--no-browser") == 1
        assert "connect_in_progress" in capsys.readouterr().err
    finally:
        holder.release()


@pytest.mark.parametrize("argv", [["pair"], ["pair", "https://opentag.me"]])
def test_cli_pair_without_a_code_suggests_connect(tmp_path, capsys, argv):
    assert _run(tmp_path, *argv) == 2
    err = capsys.readouterr().err
    assert "`openswap worker connect`" in err and "openswap worker pair <url> <code>" in err


# --- menu bar ----------------------------------------------------------------------------------


def _remote_rows(**kwargs):
    return menubar.settings_page_rows(menubar.MenuBarSettings(), strategy="best", threshold=90,
                                      section=menubar.SETTINGS_SECTION_REMOTE, **kwargs)


def test_menu_offers_connect_on_an_unpaired_mac():
    rows = {row["id"]: row for row in _remote_rows()}
    assert rows["remote_tasks_connect"]["label"] == "Connect to OpenTag…"
    assert rows["remote_tasks_connect"]["kind"] == "button" and not rows["remote_tasks_connect"]["disabled"]
    assert rows["remote_tasks_connect_other"]["label"] == "Use another server…"
    assert rows["remote_tasks_setup"]["label"] == "Set up Remote tasks…"  # the pasted command stays
    assert "remote_tasks_reconnect" not in rows
    busy = {row["id"]: row for row in _remote_rows(worker_status={"operation": "worker_account_update"})}
    assert busy["remote_tasks_connect"]["disabled"] and busy["remote_tasks_connect_other"]["disabled"]


def test_menu_offers_reconnect_when_the_key_expired():
    paired = {row["id"] for row in _remote_rows(worker_paired=True, worker_status={"remote_connectivity": "online"})}
    assert not paired & {"remote_tasks_connect", "remote_tasks_connect_other", "remote_tasks_reconnect"}
    rows = {row["id"]: row for row in _remote_rows(worker_paired=True,
                                                    worker_status={"remote_connectivity": "expired"})}
    assert rows["remote_tasks_reconnect"]["title"] == "Reconnect" and "remote_tasks_connect" not in rows
    revoked = {row["id"] for row in _remote_rows(worker_paired=True,
                                                 worker_status={"remote_connectivity": "revoked"})}
    assert "remote_tasks_reconnect" not in revoked


def test_menu_offers_reconnect_from_the_saved_expiry_while_the_worker_is_off():
    from datetime import datetime, timedelta, timezone

    def rows(**status):
        return {row["id"]: row for row in _remote_rows(
            worker_paired=True, worker_status={"remote_connectivity": "disabled", **status})}

    now = datetime.now(timezone.utc)
    expired = rows(remote_key_expires_at=(now - timedelta(days=2)).isoformat())
    assert expired["remote_tasks_reconnect"]["label"] == "Key expired · Reconnect this Mac"
    soon = rows(remote_key_expires_at=(now + timedelta(days=3, hours=1)).isoformat())
    assert soon["remote_tasks_reconnect"]["label"] == "Key expires in 3 days · Reconnect this Mac"
    rotating = rows(remote_key_renews=True, remote_key_expires_at=(now + timedelta(days=3)).isoformat())
    assert "remote_tasks_reconnect" not in rotating  # rotation renews it; no need to reconnect
    plenty = rows(remote_key_expires_at=(now + timedelta(days=20)).isoformat())
    assert "remote_tasks_reconnect" not in plenty
    assert "remote_tasks_reconnect" not in rows()  # nothing saved yet


def test_pairing_saves_the_key_expiry_for_status(tmp_path, monkeypatch):
    from openswap.worker.remote_state import read_key_status

    store = {}
    monkeypatch.setattr(pairing.macos_keychain, "set_password", lambda service, account, value: store.update({account: value}))
    monkeypatch.setattr(pairing.macos_keychain, "get_password", lambda service, account: store.get(account))
    monkeypatch.setattr(pairing, "require_macos", lambda: None)
    expires = datetime.now(timezone.utc) + timedelta(days=30)

    class Pair:
        def request(self, operation, data):
            return {"worker_id": "worker", "device_key": "k", "expires_at": expires.isoformat()}

    pairing.pair(tmp_path, "https://control.example", "one-use", transport=Pair())
    assert read_key_status(tmp_path) == (None, expires)  # off and never run: expiry still known


class _Dialogs:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.shown = []
        self.waits = []
        self.waiting_shown = threading.Event()

    def alert(self, **kwargs):
        self.shown.append(("alert", kwargs))
        return self.answers.pop(0) if self.answers else 0

    def prompt(self, **kwargs):
        self.shown.append(("prompt", kwargs))
        clicked, text = self.answers.pop(0) if self.answers else (0, "")
        return SimpleNamespace(clicked=clicked, text=text)

    def choose_folder(self, **kwargs):
        return None

    def wait_until_done(self, *, title, message, done):
        """A modal waiting dialog the owner leaves open: it ends when the flow does."""
        self.waits.append((title, message, threading.current_thread()))
        self.waiting_shown.set()
        assert done.wait(20)
        return False

    def cancel_at_once(self, *, title, message, done):
        self.waits.append((title, message, threading.current_thread()))
        return True


def _menu(root, dialogs, *, wait):
    from tests.menubar_harness import extract_class

    app_type = extract_class(menubar.__file__, "MenuBarApp",
                             {"_run_connect", "_connect_other_server", "_on_setting", "_drain_guided_setup",
                              "_run_guided_setup"},
                             {"threading": threading})
    app = app_type()
    app.switcher = SimpleNamespace(backup_dir=root)
    app._worker_operation = None
    app._panel = None
    app._alert, app._prompt, app._choose_folder = dialogs.alert, dialogs.prompt, dialogs.choose_folder
    app._wait_dialog = wait
    app.refreshed = 0
    app._worker_view_active = lambda: setattr(app, "refreshed", app.refreshed + 1)
    return app


def _settle(app):
    deadline = time.monotonic() + 30
    while getattr(app, "_guided_setup", None) is not None:
        assert time.monotonic() < deadline, "connect did not finish"
        app._drain_guided_setup()
        time.sleep(0.005)


@pytest.fixture
def default_server(stub, monkeypatch):
    """Connect to OpenTag… pairs with the stub, never the real portal."""
    monkeypatch.setattr(connect, "DEFAULT_SERVER", stub.url)
    return stub


@pytest.fixture
def setup_runs(monkeypatch):
    calls = []
    monkeypatch.setattr(guided_setup, "run", lambda root, ui: calls.append(root) or ui.say("SETUP RAN"))
    return calls


def test_menu_connect_waits_in_one_dialog_then_pairs_and_runs_the_setup(tmp_path, keychain, default_server,
                                                                       monkeypatch, setup_runs):
    stub = default_server
    threads = []
    dialogs = _Dialogs([1])
    browser = FakeBrowser(stub, gate=dialogs.waiting_shown)

    def opening(url):
        threads.append(threading.current_thread())
        return browser(url)

    monkeypatch.setattr(connect, "open_in_browser", opening)
    app = _menu(tmp_path, dialogs, wait=dialogs.wait_until_done)
    app._on_setting("remote_tasks_connect", None)
    assert app._guided_setup is not None and app.refreshed == 0  # the UI thread returned at once
    _settle(app)
    ((title, message, ui_thread),) = dialogs.waits
    assert title == "Finish in your browser…" and "Pair this Mac" in message
    assert ui_thread is threading.current_thread() and threads[0] is not threading.current_thread()
    assert setup_runs == [tmp_path]
    (final,) = [kwargs["message"] for kind, kwargs in dialogs.shown if kind == "alert"]
    assert "✓ Paired with Acme (worker-1)." in final and "SETUP RAN" in final
    assert load_worker_settings(tmp_path).control_service_url == stub.url and app.refreshed == 1


def test_menu_cancel_closes_the_listener_and_pairs_nothing(tmp_path, keychain, default_server, monkeypatch,
                                                          setup_runs):
    stub = default_server
    opened = []
    monkeypatch.setattr(connect, "open_in_browser", lambda url: opened.append(url) or True)
    dialogs = _Dialogs([1])
    app = _menu(tmp_path, dialogs, wait=dialogs.cancel_at_once)
    app._on_setting("remote_tasks_connect", None)
    _settle(app)
    assert len(dialogs.waits) == 1 and setup_runs == []
    (final,) = [kwargs["message"] for kind, kwargs in dialogs.shown if kind == "alert"]
    assert "Cancelled. Nothing was paired." in final
    port = urlsplit(parse_qs(urlsplit(opened[0]).query)["redirect_uri"][0]).port
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    assert stub.pair_requests == [] and load_worker_settings(tmp_path).control_service_url is None
    probe = connect.connect_lock(tmp_path)
    assert probe.acquire(timeout=0)
    probe.release()


def test_menu_uses_another_server_typed_by_the_owner(tmp_path, keychain, stub, monkeypatch, setup_runs):
    monkeypatch.setattr(connect, "open_in_browser", FakeBrowser(stub))
    dialogs = _Dialogs([(1, stub.url), 1])
    app = _menu(tmp_path, dialogs, wait=dialogs.wait_until_done)
    app._on_setting("remote_tasks_connect_other", None)
    _settle(app)
    assert dialogs.shown[0][1]["title"] == "Use another server"
    assert load_worker_settings(tmp_path).control_service_url == stub.url and setup_runs == [tmp_path]


def test_menu_another_server_cancelled_does_nothing(tmp_path, keychain, monkeypatch):
    monkeypatch.setattr(connect, "open_in_browser", lambda _url: pytest.fail("browser opened"))
    dialogs = _Dialogs([(0, "https://x.example")])
    app = _menu(tmp_path, dialogs, wait=dialogs.wait_until_done)
    app._on_setting("remote_tasks_connect_other", None)
    assert getattr(app, "_guided_setup", None) is None and len(dialogs.shown) == 1


def test_menu_reconnect_keeps_the_worker_and_skips_the_setup(tmp_path, keychain, stub, monkeypatch, setup_runs):
    configure_worker_service(tmp_path, stub.url, "worker-1")
    keychain[("openswap", pairing.account_name(stub.url))] = json.dumps(
        {"worker_id": "worker-1", "device_key": "old-device-key", "expires_at": _later(-1)})
    dialogs = _Dialogs([1])
    # The browser follows the page only once the waiting dialog is up, as the owner
    # would; otherwise a fast flow can end before the dialog is ever shown.
    monkeypatch.setattr(connect, "open_in_browser", FakeBrowser(stub, gate=dialogs.waiting_shown))
    app = _menu(tmp_path, dialogs, wait=dialogs.wait_until_done)
    app._on_setting("remote_tasks_reconnect", None)
    _settle(app)
    assert "Reconnect" in dialogs.waits[0][1] and setup_runs == []
    (final,) = [kwargs["message"] for kind, kwargs in dialogs.shown if kind == "alert"]
    assert "✓ Reconnected this Mac (worker-1)." in final
    assert pairing.load_enrollment(stub.url).device_key == "connect-device-key"


def test_menu_connect_without_a_connect_page_points_to_the_pasted_command(tmp_path, keychain, monkeypatch):
    server = StubOpenTag(document=404)
    try:
        monkeypatch.setattr(connect, "open_in_browser", lambda _url: pytest.fail("browser opened"))
        dialogs = _Dialogs([(1, server.url), 1])
        app = _menu(tmp_path, dialogs, wait=dialogs.wait_until_done)
        app._on_setting("remote_tasks_connect_other", None)
        _settle(app)
        assert dialogs.waits == []
        (final,) = [kwargs["message"] for kind, kwargs in dialogs.shown if kind == "alert"]
        assert "connect_unsupported" in final and "openswap worker pair" in final
    finally:
        server.close()


def test_reconnect_survives_an_expiry_seen_while_on_and_then_turning_off(tmp_path):
    from types import SimpleNamespace

    from openswap.settings import update_worker_settings
    from openswap.worker.remote_state import ConfiguredRemote, read_key_status, save_status
    from openswap.worker.runtime import read_worker_snapshot

    url = "https://control.example"
    lapsed = datetime.now(timezone.utc) - timedelta(hours=1)
    configure_worker_service(tmp_path, url, "worker")
    update_worker_settings(tmp_path, enabled=True)
    save_status(tmp_path, url, "online", datetime.now(timezone.utc), "worker", key_renews=False, key_expires_at=lapsed)

    def expired(_url):
        raise ProtocolError("device_expired", 401)

    configured = ConfiguredRemote(SimpleNamespace(backup_root=tmp_path), enrollment_loader=expired)
    assert configured._resolve() is None  # writes "expired" without a client
    assert read_key_status(tmp_path) == (False, lapsed)  # the key fields survive that write
    update_worker_settings(tmp_path, enabled=False)
    snapshot = read_worker_snapshot(tmp_path).to_dict()
    assert menubar.remote_reconnect_label(snapshot) == "Key expired · Reconnect this Mac"


@pytest.mark.parametrize("enabled", [True, False])
def test_a_removed_mac_is_never_offered_reconnect(tmp_path, enabled):
    from openswap.settings import update_worker_settings
    from openswap.worker.remote_state import read_key_status, save_status
    from openswap.worker.runtime import read_worker_snapshot

    url = "https://control.example"
    configure_worker_service(tmp_path, url, "worker")
    update_worker_settings(tmp_path, enabled=enabled)
    for expires in (datetime.now(timezone.utc) - timedelta(hours=1), datetime.now(timezone.utc) + timedelta(days=3)):
        # A revoked status keeps the saved key fields (see _kept_key).
        save_status(tmp_path, url, "revoked", None, "worker", key_renews=False, key_expires_at=expires)
        assert read_key_status(tmp_path) == (None, None)
        snapshot = read_worker_snapshot(tmp_path).to_dict()
        assert menubar.remote_reconnect_label(snapshot) is None
        assert menubar.remote_key_copy(snapshot) == ""
        assert cli._key_status(snapshot) is None
    assert menubar.remote_reconnect_label({"remote_connectivity": "revoked",
                                           "remote_key_expires_at": expires.isoformat()}) is None


def test_a_save_from_before_a_reconnect_never_restores_the_old_expiry(tmp_path):
    from openswap.worker.remote_state import ConfiguredRemote, read_key_status, save_status

    url = "https://control.example"
    configure_worker_service(tmp_path, url, "worker")
    old = datetime.now(timezone.utc) + timedelta(days=3)
    renewed = datetime.now(timezone.utc) + timedelta(days=30)
    # Reconnect saved the new key's expiry (pairing._record_key)...
    save_status(tmp_path, url, "offline", None, "worker", key_expires_at=renewed)
    # ...while a pass of the running worker still held the old enrollment.
    stale = SimpleNamespace(url=url, worker_id="worker", state="online", last_seen_at=None,
                            key_expires_at=old, key_renewal=lambda: False)
    ConfiguredRemote(SimpleNamespace(backup_root=tmp_path, remote_launch_guard=None))._save(stale)
    assert read_key_status(tmp_path) == (False, renewed)


def test_the_result_page_is_escaped_and_clears_the_code_from_the_address():
    import re

    body = connect.page("<b>title</b>", "a & b", kind="ok", details=(("Workspace", "<i>W</i>"), ("Empty", "")),
                        next_step="next <x>", clean_url=True).decode()
    assert "<b>title</b>" not in body and "&lt;b&gt;title&lt;/b&gt;" in body and "a &amp; b" in body
    assert "&lt;i&gt;W&lt;/i&gt;" in body and "Empty" not in body and "next &lt;x&gt;" in body
    (script,) = re.findall(r"<script>(.*?)</script>", body)
    assert script == 'history.replaceState(null,"","/openswap/done")'
    assert base64.b64encode(hashlib.sha256(script.encode()).digest()).decode() == connect._CLEAN_URL_HASH
    assert "<script>" not in connect.page("Not found", "waiting").decode()  # only the result page swaps it


def test_the_result_response_allows_only_that_script():
    state = connect.new_state()
    listener = connect.CallbackListener(state, timeout=20)
    thread, box = _waiting(listener)
    with socket.create_connection(("127.0.0.1", listener.port), timeout=10) as client:
        client.sendall(f"GET /openswap/callback?code=pair_1&state={state} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        thread.join(10)
        listener.respond(True, "This Mac is paired", "Done.", details=(("Workspace", "Acme"),))
        raw = b""
        while chunk := client.recv(65536):
            raw += chunk
    head, _, page_bytes = raw.partition(b"\r\n\r\n")
    csp = [line for line in head.decode().split("\r\n") if line.startswith("Content-Security-Policy:")]
    assert csp == [f"Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; "
                   f"script-src 'sha256-{connect._CLEAN_URL_HASH}'"]
    assert b"<dd>Acme</dd>" in page_bytes and b"You can close this tab." in page_bytes


def _raw_response(port, target):
    with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
        client.sendall(f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        raw = b""
        while chunk := client.recv(65536):
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    return head.decode(), body.decode()


def test_error_pages_carry_no_script_and_a_failed_result_still_clears_the_address():
    state = connect.new_state()
    listener = connect.CallbackListener(state, timeout=20)
    thread, box = _waiting(listener)
    for target in ("/favicon.ico", f"/openswap/callback?code=c&state={'x' * 22}"):
        head, body = _raw_response(listener.port, target)
        assert "script-src" not in head and "<script>" not in body
    held = {}
    client = threading.Thread(target=lambda: held.update(
        zip(("head", "body"), _raw_response(listener.port, f"/openswap/callback?code=c&state={state}"))))
    client.start()
    thread.join(10)
    listener.respond(False, "This Mac wasn't reconnected", "You cancelled in OpenTag. Nothing was changed.")
    client.join(10)
    assert held["head"].startswith("HTTP/1.1 400") and f"script-src 'sha256-{connect._CLEAN_URL_HASH}'" in held["head"]
    assert "This Mac wasn&#x27;t reconnected" in held["body"] and "<script>" in held["body"]


def test_a_hostile_kind_falls_back_to_info():
    body = connect.page("t", "b", kind='"><script>x</script>').decode()
    assert '<body class="info">' in body and "<script>" not in body
