"""Focused tests for the worker's bounded local control protocol."""

from __future__ import annotations

import json
import os
import stat
import socket
import threading
import time
from pathlib import Path

import pytest

from openswap.worker import ipc

# The control socket is a private AF_UNIX socket checked by POSIX owner and
# mode; the worker only runs on POSIX hosts.
pytestmark = pytest.mark.skipif(os.name != "posix", reason="worker IPC is POSIX-only")
from openswap.worker.client import WorkerClient
from openswap.worker.models import (
    ControlResult,
    ProviderAvailability,
    RemoteConnectivity,
    WorkerProcessState,
    WorkerSnapshot,
)


class FakeControl:
    def __init__(self):
        self.stop_ids: list[str | None] = []
        self.pause_values: list[bool] = []
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return WorkerSnapshot(
            enabled=True,
            paused=False,
            process_state=WorkerProcessState.RUNNING,
            remote_connectivity=RemoteConnectivity.DISABLED,
            provider=ProviderAvailability(False, "live_adapter_disabled"),
            active_job=None,
            queue_depth=0,
        )

    def stop(self, job_id):
        self.stop_ids.append(job_id)
        return ControlResult(True, job_id=job_id)

    def set_paused(self, paused):
        self.pause_values.append(paused)
        return ControlResult(True)


@pytest.fixture
def running_server(tmp_path):
    path = ipc.socket_path(tmp_path)
    control = FakeControl()
    stop_event = threading.Event()
    errors = []
    ready = threading.Event()

    def run():
        try:
            ipc.serve(path, control, stop_event, ready_event=ready)
        except Exception as exc:  # propagate thread failures to the test
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    # The socket file exists from bind(); only after listen() is a connect
    # guaranteed to be accepted, so wait for serve() to say so.
    assert ready.wait(2), f"worker IPC socket did not become ready: {errors!r}"
    yield path, control, stop_event, thread, errors
    stop_event.set()
    # Wake an accept() already in progress. The server polls at a short timeout.
    thread.join(timeout=2)
    if thread.is_alive():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as wake:
            wake.connect(str(path))
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert errors == []
    try:
        parent = path.parent.lstat()
        if (
            stat.S_ISDIR(parent.st_mode)
            and not stat.S_ISLNK(parent.st_mode)
            and parent.st_uid == os.getuid()
            and path.parent.parent == Path("/tmp")
            and path.parent.name.startswith(f"openswap-worker-{os.getuid()}-")
        ):
            path.parent.rmdir()
    except OSError:
        pass


def _exchange(path: Path, data: bytes, *, timeout: float = 2) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(str(path))
        connection.sendall(data)
        return connection.recv(ipc.MAX_RESPONSE_BYTES)


def test_client_exposes_only_status_stop_and_pause(running_server):
    path, control, *_ = running_server
    client = WorkerClient(path)

    snapshot = client.status()
    stopped = client.stop("job-7")
    paused = client.set_paused(True)

    assert snapshot["remote_connectivity"] == "disabled"
    assert snapshot["provider"]["diagnostic_code"] == "live_adapter_disabled"
    assert stopped == {"accepted": True, "job_id": "job-7", "diagnostic_code": None}
    assert paused["accepted"] is True
    assert control.status_calls == 1
    assert control.stop_ids == ["job-7"]
    assert control.pause_values == [True]


@pytest.mark.parametrize(
    ("raw_request", "error"),
    [
        (b'{"version":1,"command":"submit"}\n', "unsupported_request"),
        (b'{"version":1,"command":"status","path":"/tmp"}\n', "unsupported_request"),
        (b'{"version":1,"command":"pause","paused":1}\n', "invalid_paused_value"),
            (b'{"version":1,"command":"stop","job_id":"bad\\u0000id"}\n', "invalid_job_id"),
        (b"{not-json}\n", "invalid_json"),
    ],
)
def test_protocol_rejects_unlisted_or_malformed_requests(running_server, raw_request, error):
    path, control, *_ = running_server
    response = json.loads(_exchange(path, raw_request).splitlines()[0])

    assert response == {"version": 1, "ok": False, "error": error}
    assert control.status_calls == 0
    assert control.stop_ids == []
    assert control.pause_values == []


def test_protocol_rejects_oversized_request_without_dispatch(running_server):
    path, control, *_ = running_server
    request = b"{" + (b" " * (ipc.MAX_REQUEST_BYTES + 1)) + b"}\n"
    response = json.loads(_exchange(path, request).splitlines()[0])

    assert response["error"] == "request_too_large"
    assert control.status_calls == 0


def test_parser_recursion_error_is_rejected_without_killing_server(running_server, monkeypatch):
    path, control, *_ = running_server
    real_loads = json.loads
    injected = False

    def recurse_once(payload):
        nonlocal injected
        if not injected:
            injected = True
            raise RecursionError("nested JSON")
        return real_loads(payload)

    monkeypatch.setattr(ipc.json, "loads", recurse_once)
    response = real_loads(
        _exchange(path, b'{"version":1,"command":"status"}\n').splitlines()[0]
    )

    assert response["error"] == "invalid_json"
    next_response = real_loads(
        _exchange(path, b'{"version":1,"command":"status"}\n').splitlines()[0]
    )
    assert next_response["result"]["enabled"] is True
    assert control.status_calls == 1


def test_slow_drip_request_has_absolute_deadline_and_server_remains_usable(
    running_server, monkeypatch
):
    path, control, *_ = running_server
    monkeypatch.setattr(ipc, "IO_TIMEOUT_SECONDS", 0.15)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
        slow.settimeout(1)
        slow.connect(str(path))
        slow.sendall(b'{"version":1,')
        time.sleep(0.2)
        response = json.loads(slow.recv(ipc.MAX_RESPONSE_BYTES).splitlines()[0])

    assert response["error"] == "request_timeout"
    assert control.status_calls == 0
    assert WorkerClient(path).status()["enabled"] is True


def test_socket_directory_and_socket_are_private(running_server):
    path, *_ = running_server
    parent = path.parent.lstat()
    endpoint = path.lstat()

    assert stat.S_IMODE(parent.st_mode) & 0o077 == 0
    assert stat.S_IMODE(endpoint.st_mode) & 0o077 == 0
    assert parent.st_uid == os.getuid()
    assert endpoint.st_uid == os.getuid()


def test_long_state_roots_use_deterministic_short_private_socket_path(tmp_path):
    root = tmp_path / ("a" * 120)
    path = ipc.socket_path(root)

    assert path == ipc.socket_path(root)
    assert len(os.fsencode(path)) <= ipc.MAX_SOCKET_PATH_BYTES
    assert path.parent.name.startswith(f"openswap-worker-{os.getuid()}-")


def test_existing_active_socket_is_not_removed_or_replaced(running_server):
    path, *_ = running_server
    inode = path.lstat().st_ino
    stop_event = threading.Event()

    with pytest.raises(ipc.IpcError, match="worker_already_running"):
        ipc.serve(path, FakeControl(), stop_event)

    assert path.lstat().st_ino == inode
    assert WorkerClient(path).status()["enabled"] is True
