"""An abandoned launch (stop or deadline while the remote guard blocks) never starts the provider."""
from __future__ import annotations

import threading
import time

import pytest

from tests.test_worker_remote import remote_setup, submit  # noqa: F401

from openswap.worker import runtime as runtime_mod
from openswap.worker.models import JobState


def _blocking_guard(runtime, on_enter=None):
    guard_entered, release_guard = threading.Event(), threading.Event()

    def guard(_):
        guard_entered.set()
        if on_enter is not None:
            on_enter()
        assert release_guard.wait(5)
        return True  # the service still authorizes the launch

    runtime.remote_launch_guard = guard
    return guard_entered, release_guard


def _assert_unlaunched(runtime, adapter, local, release_guard, start_thread):
    release_guard.set()
    start_thread.join(3)
    assert not start_thread.is_alive()
    assert adapter.starts == 0 and not adapter.entered.is_set()
    assert runtime.store.get(local.job_id).state == JobState.INTERRUPTED
    lease = runtime.leases.read_current()
    assert lease.job_id == local.job_id and (lease.state, lease.reason) == ("released", "unlaunched")
    assert runtime.reconcile_once() is None  # nothing pending; admission is open again


def test_stop_during_slow_guard_never_launches(remote_setup, monkeypatch):
    remote, runtime, adapter, _, _, _, _ = remote_setup
    monkeypatch.setattr(runtime_mod, "START_CANCEL_GRACE_SECONDS", 0.2)
    submit(remote_setup)
    remote.tick()
    local = runtime.store.queue()[0]
    guard_entered, release_guard = _blocking_guard(runtime)
    results = []
    runner = threading.Thread(target=lambda: results.append(runtime.reconcile_once()))
    runner.start()
    assert guard_entered.wait(15)
    start_thread = runtime._event_reader
    assert runtime.stop(local.job_id).accepted
    runner.join(3)
    assert not runner.is_alive(), "the monitor must abandon the launch after the grace period"
    assert results[0].state == JobState.INTERRUPTED and adapter.starts == 0
    _assert_unlaunched(runtime, adapter, local, release_guard, start_thread)


def test_deadline_during_slow_guard_never_launches(remote_setup):
    remote, runtime, adapter, _, _, _, _ = remote_setup
    submit(remote_setup, seconds=100)
    remote.tick()
    local = runtime.store.queue()[0]
    base, offset = time.monotonic(), [0.0]
    runtime.monotonic = lambda: base + offset[0]
    # The 60 s runtime limit elapses (deterministically) while the guard blocks.
    guard_entered, release_guard = _blocking_guard(runtime, on_enter=lambda: offset.__setitem__(0, 10_000.0))
    results = []
    runner = threading.Thread(target=lambda: results.append(runtime.reconcile_once()))
    runner.start()
    assert guard_entered.wait(15)
    start_thread = runtime._event_reader
    runner.join(3)
    assert not runner.is_alive()
    assert (results[0].state, results[0].diagnostic_code) == (JobState.INTERRUPTED, "execution_uncertain")
    _assert_unlaunched(runtime, adapter, local, release_guard, start_thread)


def test_committed_launch_is_still_interrupted_not_replayed(remote_setup, monkeypatch):
    """The fix must not regress the other side: a stop after commit interrupts the started run."""
    remote, runtime, adapter, _, _, _, _ = remote_setup
    submit(remote_setup)
    remote.tick()
    local = runtime.store.queue()[0]
    adapter.finish.clear()
    runner = threading.Thread(target=runtime.reconcile_once)
    runner.start()
    assert adapter.entered.wait(15)
    assert runtime.stop(local.job_id).accepted
    runner.join(3)
    assert not runner.is_alive() and adapter.starts == 1
    assert runtime.store.get(local.job_id).state in {JobState.CANCELLED, JobState.SUCCEEDED}
