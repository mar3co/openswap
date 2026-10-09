"""Private, bounded, versioned JSON IPC for the per-user worker."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import tempfile
import time
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from openswap.worker.models import ControlResult, WorkerSnapshot

IPC_VERSION = 1
MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 65536
MAX_SOCKET_PATH_BYTES = 103  # Leave room for the terminating NUL in sun_path.
IO_TIMEOUT_SECONDS = 2.0
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


def _dispatch(request: object, control: WorkerControl) -> tuple[object | None, str | None]:
    if not isinstance(request, dict) or request.get("version") != IPC_VERSION:
        return None, "unsupported_request"
    command = request.get("command")
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


def _handle_connection(connection: socket.socket, control: WorkerControl) -> None:
    connection.settimeout(IO_TIMEOUT_SECONDS)
    try:
        raw = _read_line(connection, MAX_REQUEST_BYTES)
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            response = _encode_response(error="invalid_json")
        else:
            try:
                result, error = _dispatch(request, control)
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
) -> None:
    """Serve one bounded request at a time until ``stop_event`` is set."""
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
        server.listen(8)
        if ready_event is not None:
            ready_event.set()
        server.settimeout(0.2)
        try:
            while not stop_event.is_set():
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    _handle_connection(connection, control)
        finally:
            server.close()
    finally:
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
        except FileNotFoundError:
            pass
        except OSError:
            pass
