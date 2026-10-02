"""Bounded JSON HTTP listener; TLS belongs to the owner's reverse proxy."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import socket
import sqlite3

from openswap.worker.protocol import MAX_BODY, ProtocolError


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("invalid_request")
        value[key] = item
    return value


def _nonfinite(_):
    raise ProtocolError("invalid_request")


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenSwapReference/1"
    sys_version = ""

    def version_string(self):
        return self.server_version  # never advertise the Python version

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_):
        pass  # Codes, keys, bodies and even unknown paths are never logged.

    def do_POST(self):
        try:
            if not self.path.startswith("/v1/") or self.path.count("/") != 2:
                raise ProtocolError("unsupported_version", 404)
            key = None
            if self.path != "/v1/pair":
                # Refuse an absent or malformed credential before reading a single body byte.
                auth = self.headers.get_all("Authorization", [])
                if len(auth) != 1 or not auth[0].startswith("Bearer ") or not 7 < len(auth[0]) <= 207:
                    raise ProtocolError("unauthorized", 401)
                key = auth[0][7:]
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                raise ProtocolError("invalid_request")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ProtocolError("invalid_request")
            length = int(lengths[0])
            if length > MAX_BODY:
                raise ProtocolError("body_too_large", 413)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ProtocolError("invalid_request")
            data = json.loads(body, object_pairs_hook=_unique, parse_constant=_nonfinite)
            result = self.server.store.request(self.path[4:], data, key)
            self._reply(200, result)
        except ProtocolError as exc:
            self._reply(exc.status, {"error": exc.code})
        except (UnicodeError, ValueError, RecursionError):
            self._reply(400, {"error": "invalid_request"})
        except (OSError, sqlite3.Error):
            self._reply(503, {"error": "service_unavailable"})
        except Exception:  # never let a bug surface a traceback or an empty reply
            self._reply(500, {"error": "service_unavailable"})

    def _method_not_allowed(self):
        self._reply(405, {"error": "invalid_request"}, allow="POST")

    do_GET = do_HEAD = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _method_not_allowed

    def _reply(self, status, value, *, allow=None):
        encoded = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        self.wfile.write(encoded)


def make_server(store, host="127.0.0.1", port=8765, *, behind_owner_controlled_tls=False):
    """Binding happens before return, so callers can use a readiness event."""
    try:
        address = ipaddress.ip_address("127.0.0.1" if host == "localhost" else host)
    except ValueError:
        raise ValueError("bind host must be a literal IP or localhost") from None
    if not address.is_loopback and not behind_owner_controlled_tls:
        raise ValueError("non-loopback HTTP must sit behind owner-controlled TLS termination")
    class Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        daemon_threads = True
        # The default backlog of 5 is too small: Windows refuses connections past the
        # backlog instead of queueing them, and one worker's heartbeat, renewal and
        # synchronization threads plus a CLI can exceed it. Each refusal reads as an
        # outage, so a launch fence that lands on one fails the job.
        request_queue_size = 64
    server = Server((str(address), port), Handler)
    server.store = store
    return server
