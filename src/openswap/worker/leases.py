"""Fail-closed account leases shared by the worker and account mutations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

from openswap.exceptions import ClaudeSwitchError
from openswap.locking import FileLock


LEASE_SCHEMA_VERSION = 1
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_-]*:[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AccountLeaseError(ClaudeSwitchError):
    """Lease state is unavailable, invalid, or blocks this operation."""


class LeaseConflictError(AccountLeaseError):
    """An active or unresolved lease owns the affected provider account."""


class LeaseStateError(AccountLeaseError):
    """Lease state could not be read or is not safe to interpret."""


class ReleaseEvidence(str, Enum):
    UNLAUNCHED = "unlaunched"
    CONFIRMED_STOPPED = "confirmed_stopped"
    OWNER_RELEASED = "owner_released"


@dataclass(frozen=True)
class LeaseToken:
    job_id: str
    provider: str
    account_identity: str
    worker_pid: int
    worker_epoch: int
    lease_generation: int


@dataclass(frozen=True)
class AccountLease:
    job_id: str
    provider: str
    account_identity: str
    worker_pid: int
    worker_epoch: int
    lease_generation: int
    expires_at: float
    state: str
    reason: str | None = None

    def token(self) -> LeaseToken:
        return LeaseToken(
            job_id=self.job_id,
            provider=self.provider,
            account_identity=self.account_identity,
            worker_pid=self.worker_pid,
            worker_epoch=self.worker_epoch,
            lease_generation=self.lease_generation,
        )


def stable_account_identity(provider: str, *parts: str) -> str:
    """Return a provider-scoped opaque key from stable identity fields only.

    Callers must pass account IDs or provider identity metadata, never tokens,
    auth JSON, slot numbers, or display aliases.
    """
    if (
        not _PROVIDER_RE.fullmatch(provider)
        or not parts
        or not isinstance(parts[0], str)
        or not parts[0]
        or any(not isinstance(part, str) for part in parts)
    ):
        raise LeaseStateError("A stable provider account identity is required.")
    encoded = json.dumps([provider, *parts], separators=(",", ":"), ensure_ascii=False)
    return f"{provider}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


class LeaseMutationGuard:
    """Lease checks to use while the provider mutation lock is held."""

    def __init__(self, store: AccountLeaseStore):
        self._store = store

    def assert_unleased(self, account_identities: Iterable[str] | None = None) -> None:
        """Reject matching leases; ``None`` conservatively checks all accounts."""
        lease = self._store._read_lease()
        if lease is None or lease.state == "released":
            return
        identities = None if account_identities is None else set(account_identities)
        if identities is not None and lease.account_identity not in identities:
            return
        if lease.state not in {"active", "uncertain"}:
            raise LeaseStateError("Account lease state is invalid; refusing mutation.")
        qualifier = "expired and unresolved" if lease.expires_at <= self._store._now() else "active"
        raise LeaseConflictError(
            f"Cannot modify {self._store.provider} account while its worker lease is {qualifier}."
        )

    def assert_available(self) -> None:
        """Enforce one active or unresolved lease per provider host."""
        self.assert_unleased(None)

    def acquire(
        self,
        *,
        job_id: str,
        account_identity: str,
        worker_pid: int,
        worker_epoch: int,
        ttl_s: float,
    ) -> LeaseToken:
        """Acquire while this guard already owns the provider lock."""
        return self._store._acquire_locked(
            job_id=job_id,
            account_identity=account_identity,
            worker_pid=worker_pid,
            worker_epoch=worker_epoch,
            ttl_s=ttl_s,
            guard=self,
        )

    def current(self) -> AccountLease | None:
        """Read the lease while this guard already owns the provider lock."""
        return self._store._read_lease()

    def release(self, token: LeaseToken, evidence: ReleaseEvidence) -> None:
        """Release while this guard already owns the provider lock."""
        self._store._release_locked(token, evidence)


class AccountLeaseStore:
    """One durable lease record per provider, serialized by its engine lock.

    Lock order is intentionally just the existing provider lock. Mutations and
    lease state changes must enter :meth:`mutation_guard`; the worker does not
    hold this lock while running a provider process.
    """

    def __init__(
        self,
        backup_root: Path,
        provider: str = "codex",
        *,
        clock=time.time,
    ):
        if not _PROVIDER_RE.fullmatch(provider):
            raise ValueError("invalid provider name")
        self.backup_root = Path(backup_root)
        self.provider = provider
        self.clock = clock
        self.lease_dir = self.backup_root / "worker" / "leases"
        self.lease_file = self.lease_dir / f"{provider}.json"
        self.provider_lock = (
            self.backup_root / ".lock"
            if provider == "claude"
            else self.backup_root / provider / ".lock"
        )

    @contextmanager
    def mutation_guard(self, *, timeout: float | None = None) -> Iterator[LeaseMutationGuard]:
        """Hold the existing provider lock across lease check and mutation."""
        lock = (
            FileLock(self.provider_lock)
            if timeout is None
            else FileLock(self.provider_lock, timeout=timeout)
        )
        with lock:
            yield LeaseMutationGuard(self)

    def acquire(
        self,
        *,
        job_id: str,
        account_identity: str,
        worker_pid: int,
        worker_epoch: int,
        ttl_s: float,
    ) -> LeaseToken:
        with self.mutation_guard() as guard:
            guard.assert_available()
            return guard.acquire(
                job_id=job_id,
                account_identity=account_identity,
                worker_pid=worker_pid,
                worker_epoch=worker_epoch,
                ttl_s=ttl_s,
            )

    def _acquire_locked(
        self,
        *,
        job_id: str,
        account_identity: str,
        worker_pid: int,
        worker_epoch: int,
        ttl_s: float,
        guard: LeaseMutationGuard,
    ) -> LeaseToken:
        if (
            not job_id
            or not _IDENTITY_RE.fullmatch(account_identity)
            or not account_identity.startswith(f"{self.provider}:")
        ):
            raise LeaseStateError("Lease job and stable account identity are required.")
        if (
            type(worker_pid) is not int or worker_pid <= 0
            or type(worker_epoch) is not int or worker_epoch < 0
            or type(ttl_s) not in (int, float) or not math.isfinite(ttl_s) or ttl_s <= 0
        ):
            raise LeaseStateError("Lease owner and expiry values are invalid.")
        guard.assert_available()
        previous = self._read_document()
        generation = previous["lease_generation"] + 1 if previous else 1
        expires_at = self._now() + ttl_s
        if not math.isfinite(expires_at):
            raise LeaseStateError("Lease expiry value is invalid.")
        lease = AccountLease(
            job_id=job_id,
            provider=self.provider,
            account_identity=account_identity,
            worker_pid=worker_pid,
            worker_epoch=worker_epoch,
            lease_generation=generation,
            expires_at=expires_at,
            state="active",
        )
        self._write_document(lease)
        return lease.token()

    def renew(self, token: LeaseToken, ttl_s: float) -> LeaseToken:
        if type(ttl_s) not in (int, float) or not math.isfinite(ttl_s) or ttl_s <= 0:
            raise LeaseStateError("Lease renewal duration must be positive.")
        with self.mutation_guard():
            lease = self._require_token(token)
            now = self._now()
            if lease.state != "active" or lease.expires_at <= now:
                if lease.state == "active":
                    self._write_document(
                        AccountLease(**{**lease.__dict__, "state": "uncertain", "reason": "lease_expired"})
                    )
                raise LeaseConflictError("Expired or unresolved account lease cannot be renewed.")
            expires_at = now + ttl_s
            if not math.isfinite(expires_at):
                raise LeaseStateError("Lease expiry value is invalid.")
            renewed = AccountLease(**{**lease.__dict__, "expires_at": expires_at})
            self._write_document(renewed)
            return renewed.token()

    def mark_uncertain(self, token: LeaseToken, reason: str) -> None:
        if not _REASON_RE.fullmatch(reason):
            raise LeaseStateError("Lease uncertainty reason must be a safe reason code.")
        with self.mutation_guard():
            lease = self._require_token(token)
            if lease.state == "released":
                raise LeaseStateError("A released lease cannot be marked uncertain.")
            self._write_document(AccountLease(**{**lease.__dict__, "state": "uncertain", "reason": reason}))

    def release(self, token: LeaseToken, evidence: ReleaseEvidence) -> None:
        if not isinstance(evidence, ReleaseEvidence):
            raise LeaseStateError("Explicit lease release evidence is required.")
        with self.mutation_guard() as guard:
            guard.release(token, evidence)

    def _release_locked(self, token: LeaseToken, evidence: ReleaseEvidence) -> None:
        lease = self._require_token(token)
        if lease.state == "released":
            return
        released = AccountLease(**{**lease.__dict__, "state": "released", "reason": evidence.value})
        self._write_document(released)

    def current(self) -> AccountLease | None:
        with self.mutation_guard():
            return self._read_lease()

    def read_current(self) -> AccountLease | None:
        """Read and validate the atomic lease document without creating locks.

        This is for read-only status snapshots. Lease files are replaced as a
        complete document, so readers may observe either generation but never
        need to mutate or create the provider lock path.
        """
        return self._read_lease()

    def _require_token(self, token: LeaseToken) -> AccountLease:
        lease = self._read_lease()
        if lease is None or lease.token() != token:
            raise LeaseStateError("Lease ownership token is stale or does not match.")
        return lease

    def _read_document(self) -> dict | None:
        try:
            raw = self.lease_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError:
            raise LeaseStateError("Account lease state is invalid; refusing operation.") from None
        except OSError:
            raise LeaseStateError("Account lease state could not be read.") from None
        try:
            document = json.loads(raw)
            if (
                not isinstance(document, dict)
                or type(document.get("schema_version")) is not int
                or document.get("schema_version") != LEASE_SCHEMA_VERSION
                or type(document.get("lease_generation")) is not int
                or document["lease_generation"] < 0
            ):
                raise ValueError
            return document
        except (json.JSONDecodeError, RecursionError, TypeError, ValueError, KeyError):
            raise LeaseStateError("Account lease state is invalid; refusing operation.") from None

    def _read_lease(self) -> AccountLease | None:
        document = self._read_document()
        if document is None:
            return None
        if "lease" not in document or document.get("lease") is None:
            raise LeaseStateError("Account lease state is invalid; refusing operation.")
        value = document.get("lease")
        if not isinstance(value, dict):
            raise LeaseStateError("Account lease state is invalid; refusing operation.")
        try:
            lease = AccountLease(
                job_id=value["job_id"],
                provider=value["provider"],
                account_identity=value["account_identity"],
                worker_pid=value["worker_pid"],
                worker_epoch=value["worker_epoch"],
                lease_generation=value["lease_generation"],
                expires_at=value["expires_at"],
                state=value["state"],
                reason=value.get("reason"),
            )
            if (
                not isinstance(lease.job_id, str)
                or not lease.job_id
                or lease.provider != self.provider
                or not _IDENTITY_RE.fullmatch(lease.account_identity)
                or not lease.account_identity.startswith(f"{self.provider}:")
                or type(lease.worker_pid) is not int or lease.worker_pid <= 0
                or type(lease.worker_epoch) is not int or lease.worker_epoch < 0
                or type(lease.lease_generation) is not int or lease.lease_generation <= 0
                or type(document.get("lease_generation")) is not int
                or lease.lease_generation != document["lease_generation"]
                or type(lease.expires_at) not in (int, float)
                or not math.isfinite(lease.expires_at)
                or lease.state not in {"active", "uncertain", "released"}
            ):
                raise ValueError
            if lease.reason is not None and (not isinstance(lease.reason, str) or not _REASON_RE.fullmatch(lease.reason)):
                raise ValueError
            if lease.state == "released" and lease.reason not in {
                ReleaseEvidence.UNLAUNCHED.value,
                ReleaseEvidence.CONFIRMED_STOPPED.value,
                ReleaseEvidence.OWNER_RELEASED.value,
            }:
                raise ValueError
            return lease
        except (KeyError, TypeError, ValueError):
            raise LeaseStateError("Account lease state is invalid; refusing operation.") from None

    def _write_document(self, lease: AccountLease) -> None:
        if not self.backup_root.is_dir():
            raise LeaseStateError("Account backup directory is unavailable.")
        self._ensure_private_dir(self.lease_dir.parent, create_parents=False)
        self._ensure_private_dir(self.lease_dir)
        document = {
            "schema_version": LEASE_SCHEMA_VERSION,
            "lease_generation": lease.lease_generation,
            "lease": {
                "job_id": lease.job_id,
                "provider": lease.provider,
                "account_identity": lease.account_identity,
                "worker_pid": lease.worker_pid,
                "worker_epoch": lease.worker_epoch,
                "lease_generation": lease.lease_generation,
                "expires_at": lease.expires_at,
                "state": lease.state,
                "reason": lease.reason,
            },
        }
        fd, temp_name = tempfile.mkstemp(prefix=".lease-", suffix=".tmp", dir=self.lease_dir)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.lease_file)
            if os.name != "nt":
                dir_fd = os.open(self.lease_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _now(self) -> float:
        value = self.clock()
        if type(value) not in (int, float) or not math.isfinite(value):
            raise LeaseStateError("Lease clock value is invalid.")
        return float(value)

    @staticmethod
    def _ensure_private_dir(path: Path, *, create_parents: bool = True) -> None:
        try:
            path.mkdir(mode=0o700, parents=create_parents, exist_ok=True)
            info = path.lstat()
        except OSError:
            raise LeaseStateError("Private account lease directory is unavailable.") from None
        if not path.is_dir() or path.is_symlink():
            raise LeaseStateError("Private account lease directory is unsafe.")
        if os.name != "nt" and (
            info.st_uid != os.getuid() or info.st_mode & 0o077
        ):
            raise LeaseStateError("Private account lease directory permissions are unsafe.")
