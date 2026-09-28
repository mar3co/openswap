"""Subprocess acceptance test for durable local worker control and recovery.

The child injects an inert adapter through the internal ``run_worker`` seam;
it never launches Codex or reads provider credentials.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

import pytest

from openswap.settings import update_worker_settings
from openswap.worker.client import WorkerClient
from openswap.worker.ipc import IpcError, socket_path
from openswap.worker.journal import LocalJobStore
from openswap.worker.models import JobState


_CHILD = r'''
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

from openswap.worker.models import (
    InterruptResult, JobSubmission, ProviderAvailability, ProviderRun,
)
from openswap.worker.runtime import WorkerRuntime, run_worker
from openswap.worker.leases import stable_account_identity

root = Path(os.environ["WORKER_TEST_ROOT"])
marker = Path(os.environ["WORKER_TEST_MARKER"])

def record(name, value):
    path = marker / name
    path.write_text(value, encoding="utf-8")

class InertAdapter:
    def probe(self):
        return ProviderAvailability(True, None, "fake")

    def start(self, job, workspace, *, worker_epoch):
        starts = marker / "starts.jsonl"
        with starts.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"job_id": job.job_id}) + "\n")
            stream.flush()
        return ProviderRun(os.getpid(), "fake-session", worker_epoch, job.generation)

    def events(self, run, *, after_cursor):
        record("events_waiting", str(after_cursor))
        # Keep the runtime inside the provider event call until the parent
        # terminates this process. This makes stop acknowledgement observable
        # before the runtime can react to it.
        while True:
            time.sleep(0.02)

    def interrupt(self, run):
        record("interrupt_called", "yes")
        return InterruptResult(True, False, "execution_uncertain")

def factory(backup_root):
    runtime = WorkerRuntime(
        backup_root,
        adapter=InertAdapter(),
        account_identity=stable_account_identity("codex", "synthetic-worker-test"),
    )
    submission = JobSubmission(
        idempotency_key="process-acceptance-job",
        provider="codex",
        task="Inert subprocess acceptance fixture",
        capability_profile="research",
        workspace_id="research",
        expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        runtime_limit_s=300,
    )
    first = runtime.submit(submission)
    duplicate = runtime.submit(submission)
    if first.job_id != duplicate.job_id:
        raise RuntimeError("idempotent submission did not return the existing job")
    record("job", first.job_id)
    record("duplicate_job", duplicate.job_id)
    return runtime

raise SystemExit(run_worker(root, runtime_factory=factory))
'''


def _wait_for(predicate, *, timeout: float = 8.0, description: str):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (IpcError, OSError, RuntimeError, ValueError) as exc:
            last_error = exc
        time.sleep(0.025)
    raise AssertionError(f"timed out waiting for {description}; last error={last_error!r}")


def _start_worker(root: Path, marker: Path) -> subprocess.Popen:
    env = {
        "PATH": os.defpath,
        "HOME": str(root / "test-home"),
        "LANG": "C",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "WORKER_TEST_ROOT": str(root),
        "WORKER_TEST_MARKER": str(marker),
    }
    Path(env["HOME"]).mkdir(mode=0o700, parents=True, exist_ok=True)
    log = (marker / "worker.log").open("ab")
    try:
        return subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
    finally:
        log.close()


def _terminate_owned(process: subprocess.Popen, *, hard: bool = False) -> None:
    if process.poll() is not None:
        return
    if hard:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    else:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=3)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="the local worker control protocol uses Unix-domain sockets",
)
def test_worker_process_persists_stop_and_quarantines_ambiguous_restart(tmp_path):
    marker = tmp_path / "markers"
    marker.mkdir(mode=0o700)
    update_worker_settings(tmp_path, enabled=True)
    first = _start_worker(tmp_path, marker)
    contender = None
    second = None
    try:
        try:
            job_id = _wait_for(
                lambda: (marker / "job").read_text(encoding="utf-8"),
                description="first worker job admission",
            )
        except AssertionError as exc:
            log = (marker / "worker.log").read_text(encoding="utf-8", errors="replace")
            raise AssertionError(f"{exc}; child exit={first.poll()}, log={log!r}") from None
        duplicate_id = (marker / "duplicate_job").read_text(encoding="utf-8")
        assert duplicate_id == job_id
        _wait_for(lambda: (marker / "starts.jsonl").exists(), description="fake adapter start")
        _wait_for(lambda: (marker / "events_waiting").exists(), description="fake adapter event wait")

        endpoint = socket_path(tmp_path)
        first_client = WorkerClient(endpoint)
        _wait_for(
            lambda: first_client.status().get("active_job", {}).get("state") == "running",
            description="running job status",
        )
        contender = _start_worker(tmp_path, marker)
        try:
            contender_exit = contender.wait(timeout=4)
        except subprocess.TimeoutExpired:
            _terminate_owned(contender, hard=True)
            raise AssertionError("concurrent worker did not refuse the occupied instance lock") from None
        assert contender_exit != 0
        still_running = first_client.status()
        assert still_running["active_job"]["job_id"] == job_id
        assert still_running["active_job"]["state"] == "running"
        assert len((marker / "starts.jsonl").read_text(encoding="utf-8").splitlines()) == 1

        # A newly constructed client observes the same durable worker-owned row.
        recreated_client = WorkerClient(endpoint)
        status = recreated_client.status()
        assert status["active_job"]["job_id"] == job_id
        assert len((marker / "starts.jsonl").read_text(encoding="utf-8").splitlines()) == 1

        ack = recreated_client.stop(job_id)
        assert ack == {
            "accepted": True,
            "job_id": job_id,
            "diagnostic_code": "stop_requested",
        }
        after_ack = _wait_for(
            lambda: (
                value if (value := recreated_client.status()).get("active_job", {}).get("state")
                == "cancel_requested" else None
            ),
            description="cancel request remains nonterminal after acknowledgement",
        )
        assert after_ack["active_job"]["state"] == "cancel_requested"
        assert after_ack["lease_quarantined"] is True

        # Simulate abrupt worker death while execution is still ambiguous.
        _terminate_owned(first, hard=True)
        assert LocalJobStore(tmp_path).get(job_id).state == JobState.CANCEL_REQUESTED

        second = _start_worker(tmp_path, marker)
        second_client = WorkerClient(endpoint)
        recovered = _wait_for(
            lambda: (
                value if (value := second_client.status()).get("process_state") == "running"
                else None
            ),
            description="replacement worker IPC readiness",
        )
        assert recovered["active_job"] is None
        assert recovered["lease_quarantined"] is True
        assert LocalJobStore(tmp_path).get(job_id).state == JobState.INTERRUPTED
        assert (marker / "starts.jsonl").read_text(encoding="utf-8").splitlines() == [
            json.dumps({"job_id": job_id})
        ]
        # The same idempotency key resolves to the interrupted record; recovery
        # does not put it back into the queue or launch it again.
        assert (marker / "duplicate_job").read_text(encoding="utf-8") == job_id
        assert not (marker / "interrupt_called").exists()
    finally:
        if contender is not None and contender.poll() is None:
            _terminate_owned(contender, hard=True)
        if second is not None:
            _terminate_owned(second)
        if first.poll() is None:
            _terminate_owned(first, hard=True)
