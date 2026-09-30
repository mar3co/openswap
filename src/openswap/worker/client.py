"""Bounded client for the worker-owned local control socket."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from openswap.worker.ipc import (
    IPC_VERSION,
    IO_TIMEOUT_SECONDS,
    MAX_RESPONSE_BYTES,
    IpcError,
    _read_line,
)


class WorkerClient:
    """Issue only status, stop and admission-pause requests to the worker."""

    def __init__(self, socket_path: Path):
        self.socket_path = Path(socket_path)

    def status(self) -> dict:
        result = self._request({"version": IPC_VERSION, "command": "status"})
        if not isinstance(result, dict):
            raise IpcError("invalid_status_response")
        return result

    def stop(self, job_id: str | None = None) -> dict:
        request = {"version": IPC_VERSION, "command": "stop"}
        if job_id is not None:
            request["job_id"] = job_id
        result = self._request(request)
        if not isinstance(result, dict):
            raise IpcError("invalid_control_response")
        return result

    def set_paused(self, paused: bool) -> dict:
        if type(paused) is not bool:
            raise IpcError("invalid_paused_value")
        result = self._request(
            {"version": IPC_VERSION, "command": "pause", "paused": paused}
        )
        if not isinstance(result, dict):
            raise IpcError("invalid_control_response")
        return result

    def _request(self, request: dict) -> dict:
        try:
            encoded = json.dumps(
                request, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii") + b"\n"
        except (TypeError, ValueError):
            raise IpcError("invalid_request") from None
        if len(encoded) > 4096:
            raise IpcError("request_too_large")
        family = getattr(socket, "AF_UNIX", None)
        if family is None:
            # No Unix sockets (Windows): the worker never runs on this host.
            raise IpcError("worker_unavailable")
        connection = socket.socket(family, socket.SOCK_STREAM)
        connection.settimeout(IO_TIMEOUT_SECONDS)
        try:
            connection.connect(str(self.socket_path))
            connection.sendall(encoded)
            raw = _read_line(connection, MAX_RESPONSE_BYTES)
        except IpcError:
            raise
        except (OSError, TimeoutError) as exc:
            raise IpcError("worker_unavailable") from exc
        finally:
            connection.close()
        try:
            response = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError):
            raise IpcError("invalid_response") from None
        if (
            not isinstance(response, dict)
            or response.get("version") != IPC_VERSION
            or type(response.get("ok")) is not bool
        ):
            raise IpcError("invalid_response")
        if response["ok"] is False:
            error = response.get("error")
            if not isinstance(error, str) or len(error) > 80:
                raise IpcError("worker_rejected_request")
            raise IpcError(error)
        if set(response) != {"version", "ok", "result"}:
            raise IpcError("invalid_response")
        result = response["result"]
        if not isinstance(result, dict):
            raise IpcError("invalid_response")
        return result
