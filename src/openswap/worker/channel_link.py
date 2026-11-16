"""The worker's long-lived socket connections for replies (plan 025 S2).

Two kinds of local connection receive something back from the worker, so each
is served only after the peer's place in the process tree is checked:

- ``channel``: OpenSwap's channel, a stdio MCP server Claude Code started. It
  must be a direct child of a Claude Code process started with the channel
  named and running from a managed config home; the first such channel of that
  process's life holds the registration. It relays permission prompts in and
  takes verdicts and messages out, one JSON object per line.
- ``question`` and ``permission``: a session's synchronous hook waiting for
  the watch's reply to its own prompt (plan 028: in a session without a
  channel, a permission's verdict too). It gets one reply (a question's option
  positions, or ``allow`` or ``deny``), or none, and ends.
- ``wake``: a session's wake hook, waiting since its turn's ``Stop`` for a
  typed message to that session (plan 028, in a session without a channel).
  It gets the message's text, or none, and ends.
"""

from __future__ import annotations

import json
import select
import socket
import threading

from openswap.worker import peers

LINE_LIMIT = 65536
POLL_SECONDS = 0.5
# Each waiting hook's request fields (required, optional), the field its reply
# goes in, and the fields carrying its own prompt as shown and its word that
# this is the prompt exactly or whole.
WAITERS = {
    "question": ({"version", "command", "session_id", "tool_key"}, {"agent_id", "questions", "whole"},
                 "answers", "questions", "whole"),
    "permission": ({"version", "command", "session_id", "tool_key", "tool_name"}, {"agent_id", "preview", "exact"},
                   "behavior", "preview", "exact"),
    "wake": ({"version", "command", "session_id"}, set(), "message", None, None),
}


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
    """Serve one ``channel``, ``question``, ``permission`` or ``wake`` connection
    until it ends. Never raises. ``release()`` frees the caller's short-request slot once
    the peer is accepted."""
    try:
        command = request.get("command")
        if command == "channel" and set(request) == {"version", "command"}:
            _serve_channel(store, connection, stop_event, release)
        elif command in WAITERS and WAITERS[command][0] <= set(request) <= WAITERS[command][0] | WAITERS[command][1]:
            _serve_waiter(store, connection, request, command, stop_event, release)
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


def _serve_waiter(store, connection, request, kind, stop_event, release) -> None:
    pid = peers.peer_pid(connection)
    hook = peers.process(pid)
    _, _, field, shown, whole = WAITERS[kind]
    # The session's own reply hook (question, permission or wake): OpenSwap's command for ``kind``,
    # under Claude Code.
    ours = hook is not None and hook.args[-2:] == ("attention", kind)
    waiter = None
    if ours and kind == "wake":
        # Taken only when its launch is known: it takes a message only once paired with the turn's
        # own Stop (``_paired``).
        launch = peers.hook_launch(pid)
        if launch is not None:
            waiter = store.wake_waiter(launch[0], request["session_id"], peers.started_at(launch[1]))
    elif ours:
        claude = peers.hook_claude(pid)
        extra = {"tool_name": request["tool_name"]} if kind == "permission" else {}
        if claude is not None:
            waiter = store.waiter(kind, claude, request["session_id"], request["tool_key"],
                                  request.get("agent_id", ""), shown=request.get(shown),
                                  whole=request.get(whole) is True, **extra)
    if waiter is None:
        _reply(connection, {field: None})
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
        result = waiter.result if waiter.done.is_set() else None
        if _reply(connection, {field: result}) and result is not None:
            handed = True
            store.handed_over(waiter.reply_id)
    except OSError:
        return
    finally:
        store.end_waiter(waiter, handed=handed)
