"""Private, bounded, versioned JSON IPC for the per-user worker."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Protocol

from openswap.worker.attention import log_limited

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from openswap.worker.attention import AttentionStore
    from openswap.worker.models import ControlResult, WorkerSnapshot

IPC_VERSION = 1
# A session notice carries up to a few thousand characters of detail.
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 65536
MAX_SOCKET_PATH_BYTES = 103  # Leave room for the terminating NUL in sun_path.
IO_TIMEOUT_SECONDS = 2.0
# Connections are served concurrently, so a slow or idle client cannot stall
# status, pause or stop; past this many at once, a new one is closed unread.
MAX_CONNECTIONS = 32
# Of those, kept for status, pause and stop: notices, channels and question
# hooks never take the last of them.
CONTROL_RESERVED = 2
# ``accept()`` errors that mean the listening socket itself is unusable.
PERMANENT_ACCEPT_ERRORS = frozenset({errno.EBADF, errno.EINVAL, errno.ENOTSOCK})
# A listening socket that cannot be opened again is retried at most this far apart.
LISTEN_RETRY_MAX_SECONDS = 5.0
CONTROL_COMMANDS = frozenset({"status", "stop", "pause"})
_JOB_ID_LIMIT = 128


class WorkerControl(Protocol):
    def status(self) -> WorkerSnapshot: ...

    def stop(self, job_id: str | None) -> ControlResult: ...

    def set_paused(self, paused: bool) -> ControlResult: ...


class IpcError(Exception):
    """A safe local IPC protocol or transport error."""


def socket_path(backup_root: Path) -> Path:
    """Return a deterministic AF_UNIX path that fits macOS ``sun_path``."""
    root = Path(backup_root)
    candidate = root / "worker" / "control.sock"
    if len(os.fsencode(candidate)) <= MAX_SOCKET_PATH_BYTES:
        return candidate
    digest = hashlib.sha256(os.fsencode(os.path.abspath(root))).hexdigest()[:16]
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        # No POSIX uid (Windows): the worker never serves there, so this path
        # only has to be deterministic for a client that will find nothing.
        return Path(tempfile.gettempdir()) / f"openswap-worker-{digest}" / "control.sock"
    return Path("/tmp") / f"openswap-worker-{getuid()}-{digest}" / "control.sock"


def _object_dict(value: object) -> dict:
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict):
        raise IpcError("invalid_response")
    result = to_dict()
    if not isinstance(result, dict):
        raise IpcError("invalid_response")
    return result


def _encode_response(result: object | None = None, error: str | None = None) -> bytes:
    envelope: dict[str, object] = {"version": IPC_VERSION, "ok": error is None}
    if error is not None:
        envelope["error"] = error
    elif result is not None:
        envelope["result"] = result if isinstance(result, dict) else _object_dict(result)
    encoded = json.dumps(envelope, separators=(",", ":"), ensure_ascii=True).encode()
    if len(encoded) > MAX_RESPONSE_BYTES:
        return b'{"version":1,"ok":false,"error":"response_too_large"}\n'
    return encoded + b"\n"


def _read_line(connection: socket.socket, limit: int) -> bytes:
    data = bytearray()
    deadline = time.monotonic() + IO_TIMEOUT_SECONDS
    while len(data) <= limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise IpcError("request_timeout")
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(min(1024, limit + 1 - len(data)))
        except (socket.timeout, TimeoutError) as exc:
            raise IpcError("request_timeout") from exc
        if not chunk:
            break
        newline = chunk.find(b"\n")
        if newline >= 0:
            data.extend(chunk[:newline])
            return bytes(data)
        data.extend(chunk)
    if len(data) > limit:
        raise IpcError("request_too_large")
    raise IpcError("incomplete_request")


def _dispatch(
    request: object,
    control: WorkerControl,
    notices: AttentionStore | None = None,
    control_lock: threading.Lock | None = None,
) -> tuple[object | None, str | None]:
    if not isinstance(request, dict) or request.get("version") != IPC_VERSION:
        return None, "unsupported_request"
    command = request.get("command")
    if command == "notice" and set(request) == {"version", "command", "notice"} and notices is not None:
        # A hook's notice. The answer is a bare acknowledgement: a notice
        # connection never receives anything else.
        from openswap.worker.attention import NoticeError, validate_notice

        try:
            notice = validate_notice(request.get("notice"))
        except NoticeError:
            return None, "invalid_notice"
        notices.notice(notice)
        return {}, None
    if command not in ("status", "stop", "pause"):
        return None, "unsupported_request"
    # Control calls stay one at a time, as when the server itself was serial.
    with control_lock or _UNSHARED_LOCK:
        return _dispatch_control(request, command, control)


_UNSHARED_LOCK = threading.Lock()


def _dispatch_control(request: dict, command: object, control: WorkerControl) -> tuple[object | None, str | None]:
    if command == "status" and set(request) == {"version", "command"}:
        return control.status(), None
    if command == "stop" and set(request) <= {"version", "command", "job_id"}:
        job_id = request.get("job_id")
        if job_id is not None and (
            not isinstance(job_id, str)
            or not job_id
            or len(job_id) > _JOB_ID_LIMIT
            or any(ord(char) < 32 for char in job_id)
        ):
            return None, "invalid_job_id"
        return control.stop(job_id), None
    if command == "pause" and set(request) == {"version", "command", "paused"}:
        paused = request.get("paused")
        if type(paused) is not bool:
            return None, "invalid_paused_value"
        return control.set_paused(paused), None
    return None, "unsupported_request"


# Connections that stay open and receive something back (plan 025 S2).
LINKED_COMMANDS = frozenset({"channel", "question"})


def _handle_connection(
    connection: socket.socket,
    control: WorkerControl,
    notices: AttentionStore | None = None,
    control_lock: threading.Lock | None = None,
    stop_event: Event | None = None,
    release=None,
    control_only: bool = False,
) -> None:
    connection.settimeout(IO_TIMEOUT_SECONDS)
    try:
        raw = _read_line(connection, MAX_REQUEST_BYTES)
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            response = _encode_response(error="invalid_json")
        else:
            command = request.get("command") if isinstance(request, dict) else None
            if control_only and not (isinstance(command, str) and command in CONTROL_COMMANDS):
                response = _encode_response(error="busy")
            elif (notices is not None and isinstance(command, str) and command in LINKED_COMMANDS
                    and request.get("version") == IPC_VERSION):
                from openswap.worker import channel_link

                channel_link.serve(notices, connection, request, stop_event or Event(), release or (lambda: None))
                return
            else:
                try:
                    result, error = _dispatch(request, control, notices, control_lock)
                    response = _encode_response(result, error)
                except Exception:
                    # Do not send callback exception strings or runtime details to
                    # an arbitrary local client.
                    response = _encode_response(error="control_failed")
    except IpcError as exc:
        response = _encode_response(error=str(exc))
    except (OSError, TimeoutError):
        return
    try:
        connection.sendall(response)
    except (OSError, TimeoutError):
        return


def _verify_private_parent(path: Path) -> None:
    parent = path.parent
    try:
        info = parent.lstat()
    except FileNotFoundError:
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            pass
        except OSError as exc:
            raise IpcError("socket_directory_unavailable") from exc
        try:
            info = parent.lstat()
        except OSError as exc:
            raise IpcError("socket_directory_unavailable") from exc
    except OSError as exc:
        raise IpcError("socket_directory_unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise IpcError("socket_directory_not_private")


def _remove_stale_socket(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise IpcError("socket_path_unavailable") from exc
    if (
        not stat.S_ISSOCK(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
    ):
        raise IpcError("socket_path_unsafe")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.2)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink()
    except OSError as exc:
        raise IpcError("existing_worker_unavailable") from exc
    else:
        raise IpcError("worker_already_running")
    finally:
        probe.close()


def serve(
    socket_path: Path,
    control: WorkerControl,
    stop_event: Event,
    *,
    ready_event: Event | None = None,
    notices: AttentionStore | None = None,
) -> None:
    """Serve bounded requests, each connection on its own thread, until ``stop_event`` is set.

    ``notices`` receives session notices (``openswap attention``); without it
    a notice is an unsupported request. Returns once every connection thread
    has finished, so no control call runs after it.
    """
    path = Path(socket_path)
    if len(os.fsencode(path)) > MAX_SOCKET_PATH_BYTES:
        raise IpcError("socket_path_too_long")
    _verify_private_parent(path)
    _remove_stale_socket(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound_info = None
    try:
        server.bind(str(path))
        bound_info = path.lstat()
        os.chmod(path, 0o600, follow_symlinks=False)
        server.listen(MAX_CONNECTIONS)
        if ready_event is not None:
            ready_event.set()
        server.settimeout(0.2)
        slots = threading.BoundedSemaphore(MAX_CONNECTIONS - CONTROL_RESERVED)
        reserve = threading.BoundedSemaphore(CONTROL_RESERVED)
        control_lock = threading.Lock()
        handlers: list[threading.Thread] = []

        def handle(connection: socket.socket, pool: threading.BoundedSemaphore) -> None:
            # A channel or a waiting question hook, once accepted, gives its slot
            # back: the attention store bounds how many of those stay open.
            released = threading.Event()

            def release() -> None:
                if not released.is_set():
                    released.set()
                    pool.release()

            try:
                with connection:
                    _handle_connection(connection, control, notices, control_lock, stop_event, release,
                                       control_only=pool is reserve)
            finally:
                release()

        try:
            while not stop_event.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError as exc:
                    if exc.errno in PERMANENT_ACCEPT_ERRORS:
                        # The listening socket itself is broken: a new one at the same
                        # path brings status, pause, stop and notices back, while the
                        # worker, a running job and the connections already open go on.
                        logger.error("Worker socket can no longer accept connections (%s); opening it again", exc)
                        server.close()
                        server, bound_info = _listen_again(path, bound_info, stop_event)
                        if server is None:
                            break
                        continue
                    # Out of descriptors, say: the server keeps serving.
                    log_limited("ipc_accept", "Worker socket could not accept a connection: %s", exc)
                    stop_event.wait(0.05)
                    continue
                pool = slots if slots.acquire(blocking=False) else reserve if reserve.acquire(blocking=False) else None
                if pool is None:
                    log_limited("ipc_full", "Worker socket busy: %d connections open; one was refused",
                                MAX_CONNECTIONS)
                    connection.close()
                    continue
                handlers = [thread for thread in handlers if thread.is_alive()]
                thread = threading.Thread(target=handle, args=(connection, pool),
                                          name="openswap-worker-ipc-connection", daemon=True)
                try:
                    thread.start()
                except RuntimeError:
                    pool.release()
                    connection.close()
                    continue
                handlers.append(thread)
        finally:
            if server is not None:
                server.close()
            for thread in handlers:
                thread.join()
    finally:
        _unlink_ours(path, bound_info)


def _unlink_ours(path: Path, bound_info) -> None:
    """Remove the socket file at ``path`` if it is still the one this server bound."""
    try:
        info = path.lstat()
        if (
            bound_info is not None
            and info.st_dev == bound_info.st_dev
            and info.st_ino == bound_info.st_ino
            and stat.S_ISSOCK(info.st_mode)
            and info.st_uid == os.getuid()
        ):
            path.unlink()
    except OSError:
        pass


def _listen_again(path: Path, bound_info, stop_event: Event):
    """A new listening socket at ``path`` in place of a broken one, tried again
    with a growing wait until it works or the worker stops. Returns (socket,
    the file it bound), or (None, the last file bound) once stopping."""
    wait = 0.1
    while not stop_event.is_set():
        _unlink_ours(path, bound_info)
        server = None
        try:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            _verify_private_parent(path)
            server.bind(str(path))
            bound_info = path.lstat()
            os.chmod(path, 0o600, follow_symlinks=False)
            server.listen(MAX_CONNECTIONS)
            server.settimeout(0.2)
            return server, bound_info
        except (OSError, IpcError) as exc:
            if server is not None:
                server.close()
            log_limited("ipc_listen", "Worker socket could not be opened again (%r); retrying", exc)
            stop_event.wait(wait)
            wait = min(wait * 2, LISTEN_RETRY_MAX_SECONDS)
    return None, bound_info
