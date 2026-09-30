"""Typed, privacy-safe models for the local worker boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
from pathlib import Path


class JobState(StrEnum):
    """Canonical plan-017 job states; approval is reserved but unsupported."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    STARTING = "starting"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    EXPIRED = "expired"


class WorkerProcessState(StrEnum):
    """Worker process health, deliberately separate from job state."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


MAX_JOB_RUNTIME_SECONDS = 4 * 60 * 60


class RemoteConnectivity(StrEnum):
    """Service connectivity is separate from provider/job/process state."""

    DISABLED = "disabled"
    ONLINE = "online"
    OFFLINE = "offline"
    REVOKED = "revoked"
    EXPIRED = "expired"  # the enrollment's key reached its 30-day limit; re-pair


@dataclass(frozen=True)
class ProviderAvailability:
    available: bool
    diagnostic_code: str | None
    version: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "diagnostic_code": self.diagnostic_code,
            "version": self.version,
        }


@dataclass(frozen=True)
class JobSubmission:
    """Closed internal request schema; callers cannot supply execution details."""

    idempotency_key: str
    provider: str
    task: str
    capability_profile: str
    workspace_id: str
    expires_at: datetime
    runtime_limit_s: float

    def __post_init__(self) -> None:
        if not self.idempotency_key or len(self.idempotency_key) > 200:
            raise ValueError("idempotency_key must contain 1 to 200 characters")
        if self.provider != "codex":
            raise ValueError("only the fixed Codex provider is defined in Phase 2")
        if not self.task or len(self.task) > 32_000:
            raise ValueError("task must contain 1 to 32,000 characters")
        if not self.capability_profile or len(self.capability_profile) > 80:
            raise ValueError("capability_profile must be a bounded name")
        if self.capability_profile != "research":
            raise ValueError("only the fixed research capability is defined in Phase 2")
        if not self.workspace_id or len(self.workspace_id) > 200:
            raise ValueError("workspace_id must be a bounded opaque identifier")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if not math.isfinite(self.runtime_limit_s) or self.runtime_limit_s <= 0:
            raise ValueError("runtime_limit_s must be finite and positive")
        if self.runtime_limit_s > MAX_JOB_RUNTIME_SECONDS:
            raise ValueError("runtime_limit_s exceeds the local Phase 2 limit")


@dataclass(frozen=True)
class JobRecord:
    """Private durable row; never serialize this object to status or IPC."""

    job_id: str
    idempotency_key: str
    owner_ref: str
    provider: str
    task: str
    capability_profile: str
    workspace_id: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    runtime_limit_s: float
    pinned_account_ref: str | None
    provider_session_id: str | None
    worker_epoch: int
    generation: int
    diagnostic_code: str | None
    event_cursor: int

    def snapshot(self) -> JobSnapshot:
        return JobSnapshot(
            job_id=self.job_id,
            state=self.state,
            created_at=self.created_at,
            updated_at=self.updated_at,
            expires_at=self.expires_at,
            event_cursor=self.event_cursor,
            diagnostic_code=self.diagnostic_code,
            output_ref=f"{self.workspace_id}/{self.job_id}",
        )


@dataclass(frozen=True)
class JobSnapshot:
    """Safe job status: excludes task, paths, account refs and provider session IDs."""

    job_id: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    event_cursor: int
    diagnostic_code: str | None
    output_ref: str

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "state": self.state.value,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "expires_at": _iso(self.expires_at),
            "event_cursor": self.event_cursor,
            "diagnostic_code": self.diagnostic_code,
            "output_ref": self.output_ref,
        }


class SafeEventKind(StrEnum):
    STATE_CHANGED = "state_changed"
    PROVIDER_STARTED = "provider_started"
    PROVIDER_FINISHED = "provider_finished"
    STOP_REQUESTED = "stop_requested"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True)
class SafeEvent:
    """Allowlisted event envelope; free-form provider payloads are not retained."""

    job_id: str
    cursor: int
    timestamp: datetime
    kind: SafeEventKind
    state: JobState | None = None
    diagnostic_code: str | None = None
    execution_stopped: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "cursor": self.cursor,
            "timestamp": _iso(self.timestamp),
            "kind": self.kind.value,
            "state": self.state.value if self.state is not None else None,
            "diagnostic_code": self.diagnostic_code,
            "execution_stopped": self.execution_stopped,
        }


@dataclass(frozen=True)
class EventPage:
    events: tuple[SafeEvent, ...]
    next_cursor: int

    def to_dict(self) -> dict[str, object]:
        return {"events": [event.to_dict() for event in self.events], "next_cursor": self.next_cursor}


@dataclass(frozen=True)
class WorkerSnapshot:
    enabled: bool
    paused: bool
    process_state: WorkerProcessState
    remote_connectivity: RemoteConnectivity
    provider: ProviderAvailability
    active_job: JobSnapshot | None
    queue_depth: int
    last_seen_at: datetime | None = None
    worker_pid: int | None = None
    lease_quarantined: bool = True
    remote_last_seen_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "paused": self.paused,
            "process_state": self.process_state.value,
            "remote_connectivity": self.remote_connectivity.value,
            "provider": self.provider.to_dict(),
            "active_job": self.active_job.to_dict() if self.active_job else None,
            "queue_depth": self.queue_depth,
            "last_seen_at": _iso(self.last_seen_at) if self.last_seen_at else None,
            "worker_pid": self.worker_pid,
            "lease_quarantined": self.lease_quarantined,
            "remote_last_seen_at": _iso(self.remote_last_seen_at) if self.remote_last_seen_at else None,
        }


@dataclass(frozen=True)
class ControlResult:
    accepted: bool
    job_id: str | None = None
    diagnostic_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "job_id": self.job_id,
            "diagnostic_code": self.diagnostic_code,
        }


@dataclass(frozen=True)
class ResolvedWorkspace:
    """Local-only path resolved from a registered opaque workspace ID."""

    workspace_id: str
    output_root: Path
    readonly_sources: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ProviderRun:
    """Private adapter handle; never returned over IPC."""

    process_id: int | None
    session_id: str | None
    worker_epoch: int
    generation: int
    provider_event_cursor: int = 0


@dataclass(frozen=True)
class InterruptResult:
    requested: bool
    execution_stopped: bool
    diagnostic_code: str | None = None


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("worker timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
