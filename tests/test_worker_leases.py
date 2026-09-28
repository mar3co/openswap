"""Fail-closed tests for provider-scoped worker leases."""

from __future__ import annotations

import json
import math

import pytest

from openswap.worker.leases import (
    AccountLeaseStore,
    LeaseConflictError,
    LeaseStateError,
    ReleaseEvidence,
    stable_account_identity,
)


def _store(tmp_path, *, clock=lambda: 100.0):
    root = tmp_path / "backup"
    root.mkdir()
    return AccountLeaseStore(root, "codex", clock=clock)


def test_lease_blocks_mutation_even_after_expiry_until_explicit_release(tmp_path):
    now = [100.0]
    store = _store(tmp_path, clock=lambda: now[0])
    token = store.acquire(
        job_id="job-1",
        account_identity=stable_account_identity("codex", "acct-1"),
        worker_pid=123,
        worker_epoch=7,
        ttl_s=5,
    )
    now[0] = 106.0
    with store.mutation_guard() as guard, pytest.raises(LeaseConflictError, match="expired"):
        guard.assert_unleased()
    store.release(token, ReleaseEvidence.CONFIRMED_STOPPED)
    with store.mutation_guard() as guard:
        guard.assert_unleased()


def test_lease_generation_is_distinct_and_increments_after_release(tmp_path):
    store = _store(tmp_path)
    identity = stable_account_identity("codex", "acct-1")
    first = store.acquire(
        job_id="job-1", account_identity=identity, worker_pid=123, worker_epoch=9, ttl_s=30
    )
    store.release(first, ReleaseEvidence.UNLAUNCHED)
    second = store.acquire(
        job_id="job-2", account_identity=identity, worker_pid=123, worker_epoch=9, ttl_s=30
    )
    assert second.worker_epoch == first.worker_epoch
    assert second.lease_generation == first.lease_generation + 1


@pytest.mark.parametrize(
    "case",
    [
        lambda document: document.pop("lease"),
        lambda document: document.update(lease=None),
        lambda document: document.update(lease=[]),
        lambda document: document.update(lease="bad"),
        lambda document: document.update(schema_version=True),
        lambda document: document["lease"].update(job_id=7),
        lambda document: document["lease"].update(account_identity="claude:" + "a" * 64),
        lambda document: document["lease"].update(worker_pid=True),
        lambda document: document["lease"].update(worker_epoch=True),
        lambda document: document["lease"].update(expires_at=float("nan")),
        lambda document: document["lease"].update(expires_at=float("inf")),
        lambda document: document["lease"].update(lease_generation=2),
        lambda document: document.update(lease_generation=math.inf),
        b"\xff\xfe",
        b"[" * 1500 + b"]" * 1500,
    ],
)
def test_malformed_lease_metadata_fails_closed(tmp_path, case):
    store = _store(tmp_path)
    token = store.acquire(
        job_id="job-1",
        account_identity=stable_account_identity("codex", "acct-1"),
        worker_pid=123,
        worker_epoch=7,
        ttl_s=30,
    )
    if isinstance(case, bytes):
        store.lease_file.write_bytes(case)
    else:
        document = json.loads(store.lease_file.read_text(encoding="utf-8"))
        case(document)
        store.lease_file.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(LeaseStateError):
        with store.mutation_guard() as guard:
            guard.assert_unleased()


def test_expired_clock_or_duration_values_refuse_acquisition(tmp_path):
    store = _store(tmp_path, clock=lambda: float("nan"))
    with pytest.raises(LeaseStateError):
        store.acquire(
            job_id="job-1",
            account_identity=stable_account_identity("codex", "acct-1"),
            worker_pid=123,
            worker_epoch=7,
            ttl_s=30,
        )
