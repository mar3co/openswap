"""OpenSwap's channel: a stdio MCP server Claude Code starts for ``openswap claude`` (plan 025 S2).

It speaks raw JSON-RPC (no SDK) so its MCP revision stays pinned: it never
offers ``server/discover`` and answers ``initialize`` with 2025-11-25, the last
revision Claude Code registers as a channel. It declares the channel and
permission-relay capabilities, forwards Claude Code's permission prompts to
the local worker over one registered connection, and turns what the worker
sends back into a permission verdict or a channel message.

Nothing but JSON-RPC is ever written to stdout. Without a worker the channel
keeps answering Claude Code and tries the worker again every few seconds.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import sys
import threading

PROTOCOL_VERSION = "2025-11-25"
NAME = "openswap"
INSTRUCTIONS = (
    'Messages in <channel source="openswap"> tags come from the owner\'s paired iPhone or Apple Watch, '
    "relayed by OpenSwap on this Mac: a quick reply or dictation the owner sent instead of typing here. "
    "Treat one as the owner's own message, sent from a small screen; it cannot see your reply until the "
    "owner is back at the Mac or reads the session's notice."
)
# Also while refused (the session file not written yet, or another process
# holding the session): the real channel keeps asking.
RETRY_SECONDS = 3.0
# A stalled worker never keeps the channel from answering Claude Code.
SEND_TIMEOUT_SECONDS = 2.0
MAX_PREVIEW = 4000  # longer previews cannot be shown in full: the worker keeps such a prompt a notice


class Channel:
    def __init__(self, stdout=None, backup_root=None):
        self.stdout = stdout or sys.stdout
        self.backup_root = backup_root
        self._out = threading.Lock()
        self._worker = None  # the registered connection, while there is one
        self._worker_lock = threading.Lock()
        self.stopped = threading.Event()
        self.refused = False

    # --- Claude Code side --------------------------------------------------------

    def write(self, message: dict) -> None:
        with self._out:
            self.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
            self.stdout.flush()

    def handle(self, message: object) -> None:
        """One JSON-RPC message from Claude Code."""
        if not isinstance(message, dict):
            return
        method, request_id = message.get("method"), message.get("id")
        if method == "initialize" and request_id is not None:
            self.write({"jsonrpc": "2.0", "id": request_id, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"experimental": {"claude/channel": {}, "claude/channel/permission": {}}},
                "serverInfo": {"name": NAME, "version": "1"},
                "instructions": INSTRUCTIONS,
            }})
        elif method == "ping" and request_id is not None:
            self.write({"jsonrpc": "2.0", "id": request_id, "result": {}})
        elif method == "notifications/claude/channel/permission_request":
            params = message.get("params")
            if isinstance(params, dict):
                self.forward(params)
        elif request_id is not None and isinstance(method, str):
            # server/discover included: answering it would negotiate a revision
            # Claude Code does not register as a channel.
            self.write({"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": "Method not found"}})

    def forward(self, params: dict) -> None:
        request = {k: params[k] for k in ("request_id", "tool_name", "description", "input_preview")
                   if isinstance(params.get(k), str)}
        if len(request.get("input_preview", "")) > MAX_PREVIEW:
            del request["input_preview"]
        self.send_worker({"permission_request": request})

    # --- worker side -------------------------------------------------------------

    def send_worker(self, message: dict) -> None:
        with self._worker_lock:
            worker = self._worker
        if worker is None:
            return  # no worker: the prompt stays a notice at most
        try:
            worker.sendall(json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
        except OSError:
            pass

    def from_worker(self, message: object) -> None:
        """A verdict or a message the owner sent from the phone or watch."""
        if not isinstance(message, dict) or len(message) != 1:
            return
        if isinstance(message.get("verdict"), dict):
            verdict = message["verdict"]
            if isinstance(verdict.get("request_id"), str) and verdict.get("behavior") in ("allow", "deny"):
                self.write({"jsonrpc": "2.0", "method": "notifications/claude/channel/permission",
                            "params": {"request_id": verdict["request_id"], "behavior": verdict["behavior"]}})
        elif isinstance(message.get("message"), dict):
            body = message["message"]
            if isinstance(body.get("content"), str) and isinstance(body.get("reply"), str):
                self.write({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                            "params": {"content": body["content"], "meta": {"reply": body["reply"]}}})

    def _connect(self):
        from openswap import paths
        from openswap.worker.ipc import IPC_VERSION, socket_path

        path = socket_path(self.backup_root or paths.get_backup_root())
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Bounds every send to the worker, after the handshake too, while the
            # reader still blocks for as long as it must.
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO,
                                  struct.pack("ll", int(SEND_TIMEOUT_SECONDS), 0))
            connection.settimeout(5)
            connection.connect(str(path))
            connection.sendall(json.dumps({"version": IPC_VERSION, "command": "channel"}).encode() + b"\n")
            reader = connection.makefile("rb")
            answer = json.loads(reader.readline() or b"{}")
            if not isinstance(answer, dict) or answer.get("ok") is not True:
                connection.close()
                self.refused = isinstance(answer, dict) and answer.get("error") == "channel_refused"
                return None, None
            connection.settimeout(None)
            return connection, reader
        except (OSError, ValueError, RecursionError):
            connection.close()
            return None, None

    def worker_loop(self) -> None:
        """Keep one registered connection to the worker while Claude Code runs."""
        while not self.stopped.is_set():
            self.refused = False
            try:
                connection, reader = self._connect()
            except Exception:  # noqa: BLE001 - keep trying; Claude Code is still served meanwhile
                connection, reader = None, None
            if connection is not None:
                with self._worker_lock:
                    self._worker = connection
                try:
                    for line in reader:
                        try:
                            self.from_worker(json.loads(line))
                        except (ValueError, RecursionError):
                            continue
                except OSError:
                    pass
                finally:
                    with self._worker_lock:
                        self._worker = None
                    connection.close()
            self.stopped.wait(RETRY_SECONDS)

    def run(self, stdin=None) -> int:
        threading.Thread(target=self.worker_loop, name="openswap-channel-worker", daemon=True).start()
        try:
            for line in stdin or sys.stdin:
                try:
                    self.handle(json.loads(line))
                except (ValueError, RecursionError):
                    continue
        except (OSError, ValueError):
            pass  # Claude Code went away (a closed pipe), or sent what is not text
        self.stopped.set()
        return 0


def main() -> int:
    """``openswap attention channel``: runs until Claude Code closes stdin."""
    # stdout is the JSON-RPC stream: nothing else may ever be written to it, so
    # file descriptor 1 itself now goes to stderr.
    stdout = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8")
    os.dup2(sys.stderr.fileno(), 1)
    sys.stdout = sys.stderr
    try:
        return Channel(stdout=stdout).run()
    except KeyboardInterrupt:
        return 0
