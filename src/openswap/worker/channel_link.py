"""The worker's long-lived socket connections for replies (plan 025 S2).

Two kinds of local connection receive something back from the worker, so each
is served only after the peer's place in the process tree is checked:

- ``channel``: OpenSwap's channel, a stdio MCP server Claude Code started. It
  must be a direct child of a Claude Code process started with the channel
  named and running from a managed config home; the first such channel of that
  process's life holds the registration. It relays permission prompts in and
  takes verdicts and messages out, one JSON object per line.
- ``question``: the synchronous question hook of a session with a registered
  channel. It gets one answer (option positions), or none, and ends.
"""

from __future__ import annotations

import json
import select
import socket
import threading

from openswap.worker import peers

LINE_LIMIT = 65536
POLL_SECONDS = 0.5


class Link:
    """One connected channel; ``send`` is safe from any thread."""

    def __init__(self, connection: socket.socket):
        self.connection = connection
        self._send_lock = threading.Lock()
        self.closed = threading.Event()

    def send(self, message: dict) -> bool:
        data = json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        with self._send_lock:
            if self.closed.is_set():
                return False
            try:
                self.connection.sendall(data)
                return True
            except OSError:
                self.close()
                return False

    def close(self) -> None:
        self.closed.set()
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _reply(connection: socket.socket, payload: dict) -> bool:
    try:
        connection.sendall(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode() + b"\n")
        return True
    except OSError:
        return False


def serve(store, connection: socket.socket, request: dict, stop_event: threading.Event, release) -> None:
    """Serve one ``channel`` or ``question`` connection until it ends. Never raises.
    ``release()`` frees the caller's short-request slot once the peer is accepted."""
    try:
        if request.get("command") == "channel" and set(request) == {"version", "command"}:
            _serve_channel(store, connection, stop_event, release)
        elif request.get("command") == "question" and {"version", "command", "session_id", "tool_key"} <= set(
                request) <= {"version", "command", "session_id", "tool_key", "agent_id"}:
            _serve_question(store, connection, request, stop_event, release)
        else:
            _reply(connection, {"version": 1, "ok": False, "error": "unsupported_request"})
    except Exception:  # noqa: BLE001 - a local peer must never take the worker down
        pass


def _serve_channel(store, connection, stop_event, release) -> None:
    channel = peers.process(peers.peer_pid(connection))
    claude = peers.process(channel.ppid) if channel is not None else None
    link = Link(connection)
    # Held until the handshake is out: a verdict sent meanwhile would come first.
    with link._send_lock:
        accepted = (claude is not None and peers.names_channel(claude.args) and peers.is_channel(channel.args)
                    and store.register_channel(channel, claude, link))
        answered = _reply(connection, {"version": 1, "ok": True, "result": {}} if accepted
                          else {"version": 1, "ok": False, "error": "channel_refused"})
    if not accepted:
        return
    release()
    try:
        if not answered:
            return
        buffer = b""
        connection.settimeout(POLL_SECONDS)
        while not stop_event.is_set() and not link.closed.is_set():
            try:
                chunk = connection.recv(65536)
            except (socket.timeout, TimeoutError):
                continue
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(message, dict) and set(message) == {"permission_request"}:
                    store.channel_request(claude.pid, link, message["permission_request"])
            if len(buffer) > LINE_LIMIT:
                return
    except OSError:
        return
    finally:
        link.close()
        store.unregister_channel(claude.pid, link)


def _serve_question(store, connection, request, stop_event, release) -> None:
    pid = peers.peer_pid(connection)
    hook = peers.process(pid)
    # The session's own synchronous hook: OpenSwap's question command, under Claude Code.
    claude = peers.hook_claude(pid) if hook is not None and hook.args[-2:] == ("attention", "question") else None
    waiter = store.question_waiter(claude, request["session_id"], request["tool_key"],
                                   request.get("agent_id", "")) if claude else None
    if waiter is None:
        _reply(connection, {"answers": None})
        return
    release()
    handed = False
    try:
        while not stop_event.is_set() and not store.waiter_over(waiter):
            if waiter.done.wait(POLL_SECONDS):
                break
            readable, _, _ = select.select([connection], [], [], 0)
            if readable and not connection.recv(1, socket.MSG_PEEK):
                return  # the hook is gone (Claude Code moved on)
        answers = waiter.answers if waiter.done.is_set() else None
        if _reply(connection, {"answers": answers}) and answers is not None:
            handed = True
            store.handed_over(waiter.reply_id)
    except OSError:
        return
    finally:
        store.end_waiter(waiter, handed=handed)
