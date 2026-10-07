"""Local-only, single-active-job coordinator for the Phase 2 worker."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import os
from pathlib import Path
import shutil
import sqlite3
import signal
import stat
import sys
import tempfile
import threading
import time
from urllib.parse import quote

from openswap.settings import load_worker_settings, update_worker_settings
from openswap.worker.accounts import codex_account_in_roster, codex_accounts
from openswap.worker.adapter import ProviderAdapter, production_adapter
from openswap.locking import FileLock
from openswap.worker.journal import (
    AdmissionError,
    LocalJobStore,
    StaleWriteError,
    validate_event_fields,
)
from openswap.worker.leases import (
    AccountLeaseError,
    AccountLeaseStore,
    LeaseConflictError,
    LeaseStateError,
    ReleaseEvidence,
    START_PENDING_REASON,
)
from openswap.worker.models import (
    ControlResult,
    EventPage,
    InterruptResult,
    JobRecord,
    JobState,
    JobSubmission,
    ProviderAvailability,
    RemoteAuthorization,
    RemoteConnectivity,
    ResolvedWorkspace,
    WorkerProcessState,
    WorkerSnapshot,
    SafeEventKind,
    SafeEvent,
    ProviderRun,
)

HEALTH_STALE_AFTER_SECONDS = 30.0
# After a stop arrives while the provider is still starting, how long start()
# may take to return a handle (so the stop can be enforced with proof) before
# the launch is abandoned as uncertain.
START_CANCEL_GRACE_SECONDS = 2.0
LOCAL_OWNER_REF = "local-user"


def _unavailable_provider() -> ProviderAvailability:
    return ProviderAvailability(False, "live_adapter_disabled", None)


def _lease_is_quarantined(backup_root: Path) -> bool:
    """Whether any provider's lease is unresolved (malformed counts as unresolved).

    Both stores are checked: a Claude kickoff lease must block disable just as
    a Codex worker lease does, or disabling would let kickoff bypass it.
    """
    for provider in ("codex", "claude"):
        try:
            lease = AccountLeaseStore(Path(backup_root), provider).read_current()
        except Exception:
            return True
        if lease is not None and lease.state != "released":
            return True
    return False


def _local_snapshot_from_store(
    backup_root: Path,
    *,
    process_state: WorkerProcessState,
    enabled: bool,
    paused: bool,
    store: LocalJobStore | None = None,
    last_seen_at: datetime | None = None,
    worker_pid: int | None = None,
) -> WorkerSnapshot:
    active_job = None
    queue_depth = 0
    if store is not None:
        try:
            active = store.active()
            active_job = active.snapshot() if active else None
            queue_depth = len(store.queue())
            pid, seen = store.health()
            worker_pid = worker_pid if worker_pid is not None else pid
            last_seen_at = last_seen_at or seen
        except (OSError, sqlite3.Error, RuntimeError, ValueError):
            process_state = WorkerProcessState.UNAVAILABLE
            active_job = None
            queue_depth = 0
            worker_pid = None
            last_seen_at = None
    return WorkerSnapshot(
        enabled=enabled,
        paused=paused,
        process_state=process_state,
        remote_connectivity=RemoteConnectivity.DISABLED,
        provider=_unavailable_provider(),
        active_job=active_job,
        queue_depth=queue_depth,
        last_seen_at=last_seen_at,
        worker_pid=worker_pid,
        lease_quarantined=_lease_is_quarantined(backup_root),
    )


def _with_remote_status(snapshot, backup_root, *, now=None):
    from openswap.worker.remote_state import read_status
    state, seen = read_status(Path(backup_root), now=now)
    # A stopped/stale local worker cannot be online just because it wrote a
    # recent service heartbeat before exit. Revocation remains visible.
    if snapshot.process_state != WorkerProcessState.RUNNING and state == RemoteConnectivity.ONLINE:
        state = RemoteConnectivity.OFFLINE
    return replace(snapshot, remote_connectivity=state, remote_last_seen_at=seen)


def _snapshot_from_store(backup_root, **kwargs):
    return _with_remote_status(_local_snapshot_from_store(backup_root, **kwargs), backup_root)


def read_worker_snapshot(backup_root: Path, *, now: datetime | None = None) -> WorkerSnapshot:
    return _with_remote_status(_read_local_worker_snapshot(backup_root, now=now), backup_root, now=now)


def _read_local_worker_snapshot(backup_root: Path, *, now: datetime | None = None) -> WorkerSnapshot:
    """Pure read-only CLI/UI fallback; never creates state or loads credentials."""
    backup_root = Path(backup_root)
    policy = load_worker_settings(backup_root)
    db_path = backup_root / "worker" / "jobs.sqlite3"
    try:
        db_info = db_path.lstat()
    except FileNotFoundError:
        return _snapshot_from_store(
            backup_root, process_state=WorkerProcessState.STOPPED,
            enabled=policy.enabled, paused=policy.paused,
        )
    except OSError:
        return WorkerSnapshot(
            enabled=policy.enabled, paused=policy.paused,
            process_state=WorkerProcessState.UNAVAILABLE,
            remote_connectivity=RemoteConnectivity.DISABLED,
            provider=_unavailable_provider(), active_job=None, queue_depth=0,
            lease_quarantined=True,
        )
    state_dir = db_path.parent
    try:
        state_info = state_dir.lstat()
    except OSError:
        state_info = None
    if (
        stat.S_ISLNK(db_info.st_mode) or not stat.S_ISREG(db_info.st_mode)
        or state_info is None or stat.S_ISLNK(state_info.st_mode)
        or not stat.S_ISDIR(state_info.st_mode)
        or (os.name != "nt" and (state_info.st_uid != os.getuid() or state_info.st_mode & 0o077))
    ):
        return WorkerSnapshot(
            enabled=policy.enabled, paused=policy.paused,
            process_state=WorkerProcessState.UNAVAILABLE,
            remote_connectivity=RemoteConnectivity.DISABLED,
            provider=_unavailable_provider(), active_job=None, queue_depth=0,
            lease_quarantined=True,
        )
    db: sqlite3.Connection | None = None
    scratch: tempfile.TemporaryDirectory | None = None
    try:
        # mode=ro alone makes SQLite recreate missing WAL sidecars (and fail in
        # a directory it cannot write). With no WAL content the journal is
        # fully checkpointed, so open it immutable: nothing is created and
        # nothing unmerged can be missed. WAL content with its -shm present is
        # read in place with mode=ro. WAL content without -shm means no live
        # writer (a running journal always has one), so read a private copy
        # rather than let SQLite recreate -shm beside the journal.
        wal = db_path.with_name(db_path.name + "-wal")
        shm = db_path.with_name(db_path.name + "-shm")
        try:
            wal_has_content = wal.lstat().st_size > 0
        except FileNotFoundError:
            wal_has_content = False
        open_path, access = db_path, "mode=ro&immutable=1"
        if wal_has_content:
            if os.path.lexists(shm):
                access = "mode=ro"
            else:
                scratch = tempfile.TemporaryDirectory(prefix="openswap-status-")
                open_path = Path(scratch.name) / db_path.name
                shutil.copyfile(db_path, open_path)
                shutil.copyfile(wal, open_path.with_name(open_path.name + "-wal"))
                access = "mode=ro"
        uri = f"file:{quote(str(open_path.resolve()))}?{access}"
        db = sqlite3.connect(uri, uri=True, timeout=0.25)
        db.row_factory = sqlite3.Row
        metadata = {row["key"]: row["value"] for row in db.execute(
            "SELECT key,value FROM metadata WHERE key IN ('worker_pid','last_seen_at')"
        )}
        row = db.execute(
            "SELECT * FROM jobs WHERE state IN ('claimed','starting','running','cancel_requested') "
            "ORDER BY updated_at LIMIT 1"
        ).fetchone()
        queue_depth = db.execute("SELECT COUNT(*) FROM jobs WHERE state='queued'").fetchone()[0]
        active_job = LocalJobStore._record(row).snapshot() if row else None
        raw_pid = metadata.get("worker_pid")
        pid = int(raw_pid) if raw_pid else None
        seen = datetime.fromisoformat(metadata["last_seen_at"]) if metadata.get("last_seen_at") else None
        current = now or datetime.now(timezone.utc)
        state = WorkerProcessState.STOPPED
        if pid and seen:
            age = (current - seen).total_seconds()
            if age < 0 or age > HEALTH_STALE_AFTER_SECONDS:
                state = WorkerProcessState.STALE
            elif _pid_exists(pid):
                state = WorkerProcessState.RUNNING
            else:
                state = WorkerProcessState.STALE
        return WorkerSnapshot(
            enabled=policy.enabled,
            paused=policy.paused,
            process_state=state,
            remote_connectivity=RemoteConnectivity.DISABLED,
            provider=_unavailable_provider(),
            active_job=active_job,
            queue_depth=queue_depth,
            last_seen_at=seen,
            worker_pid=pid,
            lease_quarantined=_lease_is_quarantined(backup_root),
        )
    except (OSError, sqlite3.Error, ValueError, KeyError):
        return WorkerSnapshot(
            enabled=policy.enabled,
            paused=policy.paused,
            process_state=WorkerProcessState.UNAVAILABLE,
            remote_connectivity=RemoteConnectivity.DISABLED,
            provider=_unavailable_provider(),
            active_job=None,
            queue_depth=0,
            lease_quarantined=True,
        )
    finally:
        if db is not None:
            db.close()
        if scratch is not None:
            scratch.cleanup()


def _pid_exists(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) is not a probe on Windows: signal 0 is CTRL_C_EVENT,
        # which interrupts every process on the console.
        return _pid_exists_windows(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _pid_exists_windows(pid: int) -> bool:
    """Liveness without signalling; anything unclear counts as alive."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            error = kernel32.GetLastError()
            if error == error_invalid_parameter:
                return False  # no process has this pid
            return True  # access denied, or unknown: fail closed
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return True


class WorkerRuntime:
    """In-process submission/control API; no submit RPC or CLI is provided."""

    def __init__(
        self,
        backup_root: Path,
        *,
        adapter: ProviderAdapter | None = None,
        owner_ref: str = LOCAL_OWNER_REF,
        max_pending: int = 20,
        account_identity: str | None = None,
        clock=time.time,
        monotonic=time.monotonic,
        sleeper=time.sleep,
    ):
        self.backup_root = Path(backup_root)
        self.store = LocalJobStore(self.backup_root, max_pending=max_pending)
        self.adapter = adapter if adapter is not None else production_adapter()
        self.owner_ref = owner_ref
        # A fixed identity is a test seam: it bypasses the owner's local pin
        # and its roster check. Production reads the pin for every launch.
        self._fixed_account_identity = account_identity
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.leases = AccountLeaseStore(self.backup_root, "codex")
        self.worker_pid = os.getpid()
        self.worker_epoch, self.recovered_job_ids = self.store.start_epoch(self.worker_pid)
        self._quarantine_lease_for_recovered_jobs()
        self._admission_lock = threading.RLock()
        self._launch_lock = threading.RLock()
        # Set under _launch_lock when the final fence passes and start() is
        # about to run: a stop after that point is reported as arriving after
        # the launch was committed (it is enforced by interrupting the run).
        self._launch_committed: str | None = None
        self._event_reader_lock = threading.Lock()
        self._event_reader: threading.Thread | None = None
        self._active_run = None
        self._active_lease = None
        self.remote_launch_guard = None
        # Installed by the remote client: the ``account_ref`` a remote job's
        # claim carried (None when absent). It reads the durable claim, so a
        # choice survives a restart; it raises when the claim is unknown.
        self.remote_account_ref = None

    @property
    def account_identity(self) -> str | None:
        """The account the next launch would use: the owner's current local pin.

        Re-read from settings on every call, so ``openswap worker account``
        (or the menu bar picker) takes effect for the next job without a
        worker restart. A running job keeps the identity recorded on it at
        STARTING and held by its lease.
        """
        if self._fixed_account_identity is not None:
            return self._fixed_account_identity
        return load_worker_settings(self.backup_root).pinned_account_ref

    def account_ready(self) -> bool:
        """Whether a pin exists and its account is still in the Codex roster."""
        identity = self.account_identity
        if identity is None:
            return False
        if self._fixed_account_identity is not None:
            return True
        return codex_account_in_roster(self.backup_root, identity)

    def allowlist_ready(self) -> bool:
        """Whether any account allowlisted for a per-job choice is in the Codex roster."""
        allowlist = load_worker_settings(self.backup_root).account_allowlist
        if not allowlist:
            return False
        if self._fixed_account_identity is not None:
            return True
        roster = {choice.account_ref for choice in codex_accounts(self.backup_root) or ()}
        return any(entry.identity in roster for entry in allowlist)

    def _resolve_launch_account(self, job: JobRecord) -> tuple[str | None, str | None]:
        """``(identity, None)`` for this launch, or ``(None, diagnostic)``; under the launch lock.

        A remote job whose claim named an ``account_ref`` runs on that
        allowlist entry as the allowlist reads now; any other job runs on the
        current pin. A reference no longer allowlisted, an unreadable choice,
        or no pin when one is needed fails the job: another account is never
        substituted. The roster is checked here too, before anything is
        journaled, so a missing account is reported as never launched; the
        check under the lease's mutation guard still closes the race.
        """
        requested = None
        if job.idempotency_key.startswith("remote:") and self.remote_account_ref is not None:
            try:
                requested = self.remote_account_ref(job.job_id)
            except Exception:
                return None, "provider_auth_unavailable"
            if requested is not None and not isinstance(requested, str):
                return None, "provider_auth_unavailable"
        if requested is None:
            identity = self.account_identity
            if identity is None:
                return None, "provider_unavailable"
        else:
            entry = load_worker_settings(self.backup_root).allowlisted(requested)
            if entry is None:
                return None, "provider_auth_unavailable"
            identity = entry.identity
        if self._fixed_account_identity is None and not codex_account_in_roster(self.backup_root, identity):
            return None, "provider_auth_unavailable"
        return identity, None

    def _quarantine_lease_for_recovered_jobs(self) -> None:
        """Stop trusting a still-``active`` lease left by a crashed worker.

        ``start_epoch`` already marked any job it recovered as INTERRUPTED,
        but a prior process that never reached its own cleanup (killed,
        crashed) leaves its account lease recorded ``active`` forever: no
        supported path ever proves the account is idle again. Flip it to
        ``uncertain`` so mutation guards keep refusing (never auto-release)
        while ``openswap worker lease release`` becomes available to the
        owner once they confirm the process is really gone.
        """
        if not self.recovered_job_ids:
            return
        try:
            lease = self.leases.current()
        except AccountLeaseError:
            return
        if (
            lease is not None
            and lease.state == "active"
            and lease.job_id in self.recovered_job_ids
        ):
            try:
                self.leases.mark_uncertain(lease.token(), "worker_restarted")
            except AccountLeaseError:
                pass

    def submit(self, submission: JobSubmission, *, job_id: str | None = None, remote: bool = False) -> JobRecord:
        """Admit a job; ``job_id`` lets a caller publish the ID before the row exists.

        The ``remote:`` idempotency prefix marks a job admitted by the remote client
        (its launch needs remote authorization), so only that client may use it.
        """
        if submission.idempotency_key.startswith("remote:") != remote:
            raise AdmissionError("the remote: idempotency prefix is reserved for remote jobs")
        with self._admission_lock:
            policy = load_worker_settings(self.backup_root)
            if not policy.enabled:
                raise AdmissionError("local worker is disabled")
            if policy.paused:
                raise AdmissionError("local worker admission is paused")
            if submission.expires_at <= datetime.now(timezone.utc):
                raise AdmissionError("job is expired")
            return self.store.create(
                submission, owner_ref=self.owner_ref, worker_epoch=self.worker_epoch, job_id=job_id,
            )

    def get(self, job_id: str) -> JobRecord:
        return self.store.get(job_id)

    def events(self, job_id: str, *, after_cursor: int = 0, limit: int = 100) -> EventPage:
        return self.store.list_events(job_id, after_cursor=after_cursor, limit=limit)

    def cancel(self, job_id: str) -> JobRecord:
        with self._launch_lock:
            record = self.store.get(job_id)
            return self.store.cancel(
                job_id, worker_epoch=self.worker_epoch, expected_generation=record.generation,
            )

    def status(self) -> WorkerSnapshot:
        policy = load_worker_settings(self.backup_root)
        return _snapshot_from_store(
            self.backup_root, process_state=WorkerProcessState.RUNNING,
            enabled=policy.enabled, paused=policy.paused, store=self.store,
            worker_pid=self.worker_pid,
        )

    def stop(self, job_id: str | None) -> ControlResult:
        with self._launch_lock:
            record = self.store.active()
            if record is None:
                if job_id is None:
                    return ControlResult(True, diagnostic_code="no_active_job")
                try:
                    self.store.get(job_id)
                except KeyError:
                    return ControlResult(False, job_id=job_id, diagnostic_code="job_not_found")
                return ControlResult(True, job_id=job_id, diagnostic_code="job_not_active")
            if job_id is not None and record.job_id != job_id:
                return ControlResult(False, job_id=job_id, diagnostic_code="active_job_mismatch")
            try:
                updated = self.store.cancel(
                    record.job_id, worker_epoch=self.worker_epoch,
                    expected_generation=record.generation,
                )
            except StaleWriteError:
                return ControlResult(False, job_id=record.job_id, diagnostic_code="stale_job_state")
            if self._launch_committed == record.job_id:
                # The provider start was already committed: it cannot be
                # prevented, only interrupted once start() returns.
                return ControlResult(
                    True, job_id=updated.job_id, diagnostic_code="stop_after_launch_committed",
                )
            return ControlResult(True, job_id=updated.job_id, diagnostic_code="stop_requested")

    def set_paused(self, paused: bool) -> ControlResult:
        if type(paused) is not bool:
            return ControlResult(False, diagnostic_code="invalid_pause_value")
        try:
            with self._admission_lock:
                update_worker_settings(self.backup_root, paused=paused)
        except (OSError, RuntimeError, ValueError):
            return ControlResult(False, diagnostic_code="settings_unavailable")
        return ControlResult(True, diagnostic_code="admission_paused" if paused else "admission_open")

    def reconcile_once(self, *, shutdown_event: threading.Event | None = None) -> JobRecord | None:
        """Process at most one job; injectable adapters are for tests only."""
        # A prior provider call (an event read, or a start() abandoned after
        # a stop) may still be blocked. Keep it tracked and refuse new work
        # until it quiesces; late results must never reach another job.
        with self._event_reader_lock:
            if self._event_reader is not None:
                if self._event_reader.is_alive():
                    self.heartbeat()
                    return None
                self._event_reader = None
        with self._admission_lock:
            policy = load_worker_settings(self.backup_root)
            if not policy.enabled or policy.paused:
                return None
            queued = self.store.queue(limit=1)
            if not queued:
                return None
            item = queued[0]
            if item.expires_at <= datetime.now(timezone.utc):
                return self.store.transition(
                    item.job_id, expected_states=(JobState.QUEUED,),
                    new_state=JobState.EXPIRED, worker_epoch=self.worker_epoch,
                    expected_generation=item.generation, diagnostic_code="job_expired",
                )
            claimed = self.store.claim(
                item.job_id, worker_epoch=self.worker_epoch,
                expected_generation=item.generation,
            )

        prepared = self._prepare_run(claimed, shutdown_event)
        if isinstance(prepared, JobRecord):
            return prepared
        # The runtime limit started when start() was called, not when it
        # returned: startup spends the same budget.
        running, run, token, deadline = prepared
        current = running
        provider_cursor = run.provider_event_cursor
        finished_event_candidate = None
        finished_event_proof = None
        try:
            while True:
                self.heartbeat()
                current = self.store.get(running.job_id)
                if not load_worker_settings(self.backup_root).enabled:
                    return self._interrupt_active(current, "worker_shutdown")
                if shutdown_event is not None and shutdown_event.is_set():
                    return self._interrupt_active(current, "worker_shutdown")
                if current.state == JobState.CANCEL_REQUESTED:
                    return self._interrupt_active(current, "cancel_requested")
                if self.monotonic() >= deadline:
                    return self._interrupt_active(current, "runtime_limit_reached")
                events, interrupt_reason = self._read_events_while_monitoring(
                    run, provider_cursor, current, deadline, shutdown_event,
                )
                if interrupt_reason is not None:
                    latest = self.store.get(running.job_id)
                    return self._interrupt_active(latest, interrupt_reason)
                if not isinstance(events, tuple) or len(events) > 100:
                    raise RuntimeError("provider_event_batch_too_large")
                for event in events:
                    if (not isinstance(event, SafeEvent)
                            or event.job_id != current.job_id or type(event.cursor) is not int
                            or event.cursor <= provider_cursor):
                        raise RuntimeError("adapter_event_job_mismatch")
                    if event.kind == SafeEventKind.PROVIDER_FINISHED:
                        final_state = event.state
                        if final_state not in {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED}:
                            raise RuntimeError("invalid_provider_terminal_state")
                    validated_diagnostic = validate_event_fields(
                        kind=event.kind, state=event.state,
                        diagnostic_code=event.diagnostic_code,
                        execution_stopped=event.execution_stopped,
                    )
                    if (
                        event.kind == SafeEventKind.PROVIDER_FINISHED
                        and event.execution_stopped is True
                    ):
                        # Candidate only: lease release still requires the
                        # journal to accept this event under a current fence.
                        finished_event_candidate = event
                    appended = self.store.append_event(
                        current.job_id, kind=event.kind, state=event.state,
                        diagnostic_code=validated_diagnostic,
                        worker_epoch=self.worker_epoch,
                        expected_generation=current.generation,
                        execution_stopped=event.execution_stopped,
                    )
                    if (
                        event.kind == SafeEventKind.PROVIDER_FINISHED
                        and type(appended.execution_stopped) is bool
                        and appended.execution_stopped is True
                    ):
                        # The journal validated the strict stop-proof type and
                        # durably accepted this provider event. Preserve that
                        # proof if a concurrent cancel fences the final state
                        # transition below.
                        finished_event_proof = appended
                    provider_cursor = event.cursor
                    current = self.store.get(current.job_id)
                    if event.kind == SafeEventKind.PROVIDER_FINISHED:
                        if not event.execution_stopped:
                            self.leases.mark_uncertain(token, "execution_uncertain")
                            self._clear_active()
                            return self.store.transition(
                                current.job_id, expected_states=(current.state,),
                                new_state=JobState.INTERRUPTED,
                                worker_epoch=self.worker_epoch,
                                expected_generation=current.generation,
                                diagnostic_code="execution_uncertain",
                            )
                        if current.state not in {JobState.RUNNING, JobState.CANCEL_REQUESTED}:
                            raise RuntimeError("unexpected_provider_terminal_state")
                        if current.state == JobState.CANCEL_REQUESTED:
                            final_state = JobState.CANCELLED
                        final = self.store.transition(
                            current.job_id, expected_states=(current.state,),
                            new_state=final_state, worker_epoch=self.worker_epoch,
                            expected_generation=current.generation,
                            diagnostic_code=event.diagnostic_code,
                        )
                        self.leases.release(token, ReleaseEvidence.CONFIRMED_STOPPED)
                        self._clear_active()
                        return final
                self.sleeper(0.05)
        except Exception as error:
            if finished_event_proof is not None:
                return self._finalize_proven_terminal(token, finished_event_proof)
            if finished_event_candidate is not None and isinstance(error, StaleWriteError):
                retried = None
                try:
                    latest = self.store.get(running.job_id)
                    if latest.state in {JobState.RUNNING, JobState.CANCEL_REQUESTED}:
                        retried = self.store.append_event(
                            latest.job_id, kind=finished_event_candidate.kind,
                            state=finished_event_candidate.state,
                            diagnostic_code=finished_event_candidate.diagnostic_code,
                            worker_epoch=self.worker_epoch,
                            expected_generation=latest.generation,
                            execution_stopped=finished_event_candidate.execution_stopped,
                        )
                except Exception:
                    # If the terminal proof cannot be journaled under a fresh
                    # fence, fall through to ordinary interruption handling;
                    # that path quarantines unless interrupt independently
                    # proves the run stopped.
                    retried = None
                if (
                    retried is not None
                    and type(retried.execution_stopped) is bool
                    and retried.execution_stopped is True
                ):
                    return self._finalize_proven_terminal(token, retried)
            try:
                latest = self.store.get(running.job_id)
            except Exception:
                # If we still own this provider handle but cannot safely write
                # through an unknown journal fence, attempt interruption and
                # update only the durable lease; leave the journal for startup
                # reconciliation. If an interrupt already ran (and cleared the
                # handle) before this failure, never interrupt it again.
                if self._active_run is not None:
                    try:
                        self._interrupt_execution(token, run)
                    finally:
                        self._clear_active()
                raise
            reason = (
                "cancel_requested"
                if latest.state == JobState.CANCEL_REQUESTED
                else "provider_error"
            )
            return self._interrupt_active(latest, reason)

    def _read_events_while_monitoring(
        self, run, provider_cursor: int, record: JobRecord, deadline: float,
        shutdown_event: threading.Event | None,
    ) -> tuple[tuple[SafeEvent, ...] | None, str | None]:
        """Read provider events without blocking the control/deadline driver.

        There is at most one tracked reader. The reader only calls the adapter
        and stores its result; this reconcile thread remains the sole journal
        writer. An unfinished read is retained across interruption and blocks
        admission until it exits.
        """
        completed = threading.Event()
        result: dict[str, object] = {}

        def read() -> None:
            try:
                result["events"] = self.adapter.events(run, after_cursor=provider_cursor)
            except BaseException as error:
                result["error"] = error
            finally:
                completed.set()

        reader = threading.Thread(
            target=read, name=f"openswap-event-reader-{record.job_id}", daemon=True,
        )
        with self._event_reader_lock:
            prior = self._event_reader
            if prior is not None and prior.is_alive():
                return None, "event_reader_busy"
            self._event_reader = reader
            reader.start()

        interrupt_reason = None
        while not completed.wait(0.05):
            self.heartbeat()
            # Let the caller's owned-run cleanup handle unreadable journal
            # state; an unobserved read is never treated as stop proof.
            current = self.store.get(record.job_id)
            if not load_worker_settings(self.backup_root).enabled:
                interrupt_reason = "worker_shutdown"
            elif shutdown_event is not None and shutdown_event.is_set():
                interrupt_reason = "worker_shutdown"
            elif current.state == JobState.CANCEL_REQUESTED:
                interrupt_reason = "cancel_requested"
            elif self.monotonic() >= deadline:
                interrupt_reason = "runtime_limit_reached"
            if interrupt_reason is not None:
                return None, interrupt_reason

        # completion is signaled in the reader's finally block; wait for that
        # already-finished call's thread frame to exit before permitting a new
        # reader. This wait cannot be held by provider I/O because events()
        # has returned, and heartbeat/control checks remain responsive.
        while reader.is_alive():
            reader.join(timeout=0.05)
            if not reader.is_alive():
                break
            self.heartbeat()
            current = self.store.get(record.job_id)
            if not load_worker_settings(self.backup_root).enabled:
                interrupt_reason = "worker_shutdown"
            elif shutdown_event is not None and shutdown_event.is_set():
                interrupt_reason = "worker_shutdown"
            elif current.state == JobState.CANCEL_REQUESTED:
                interrupt_reason = "cancel_requested"
            elif self.monotonic() >= deadline:
                interrupt_reason = "runtime_limit_reached"
            if interrupt_reason is not None:
                return None, interrupt_reason
        with self._event_reader_lock:
            if self._event_reader is reader and not reader.is_alive():
                self._event_reader = None
        if "error" in result:
            error = result["error"]
            if isinstance(error, Exception):
                raise error
            raise RuntimeError("provider_event_read_failed")
        if "events" not in result:
            raise RuntimeError("provider_event_read_failed")
        return result["events"], None

    def _finalize_proven_terminal(self, token, event: SafeEvent) -> JobRecord:
        """Honor a journaled provider stop proof despite a stale state fence."""
        self.leases.release(token, ReleaseEvidence.CONFIRMED_STOPPED)
        self._clear_active()
        for attempt in range(2):
            current = self.store.get(event.job_id)
            if current.state in {
                JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
                JobState.INTERRUPTED, JobState.EXPIRED,
            }:
                return current
            if current.state == JobState.CANCEL_REQUESTED:
                final_state = JobState.CANCELLED
                diagnostic = "cancel_requested"
            elif current.state == JobState.RUNNING:
                final_state = event.state
                diagnostic = event.diagnostic_code
            else:
                raise RuntimeError("proven_provider_terminal_state_unavailable")
            try:
                return self.store.transition(
                    current.job_id, expected_states=(current.state,),
                    new_state=final_state, worker_epoch=self.worker_epoch,
                    expected_generation=current.generation,
                    diagnostic_code=diagnostic,
                )
            except StaleWriteError:
                if attempt == 1:
                    raise
        raise RuntimeError("proven_provider_terminal_state_unavailable")

    def heartbeat(self) -> None:
        self.store.heartbeat(self.worker_pid, self.worker_epoch)

    def mark_stopped(self) -> None:
        """Clear this process's health record after its server thread exits."""
        self.store.mark_stopped(self.worker_pid, self.worker_epoch)

    def _cancel_before_launch(self, current: JobRecord, token=None) -> JobRecord:
        if token is not None:
            self.leases.release(token, ReleaseEvidence.UNLAUNCHED)
            self._clear_active()
        return self.store.transition(
            current.job_id, expected_states=(JobState.CANCEL_REQUESTED,),
            new_state=JobState.CANCELLED, worker_epoch=self.worker_epoch,
            expected_generation=current.generation,
            diagnostic_code="cancel_requested",
        )

    def _prepare_run(self, claimed: JobRecord, shutdown_event: threading.Event | None = None):
        # Provider calls (probe, start) run without the control lock so stop()
        # and cancel() stay responsive; every journal step is taken under it
        # against a freshly read fence. A stop that lands during start() makes
        # the STARTING -> RUNNING write stale, and the started run is then
        # interrupted instead of replayed.
        with self._launch_lock:
            current = self.store.get(claimed.job_id)
            if current.state == JobState.CANCEL_REQUESTED:
                return self._cancel_before_launch(current)
        try:
            availability = self.adapter.probe()
        except Exception:
            availability = ProviderAvailability(False, "provider_unavailable", None)
        with self._launch_lock:
            prepared = self._prepare_launch(claimed, availability, shutdown_event)
        if isinstance(prepared, JobRecord):
            return prepared
        starting, token, workspace = prepared
        try:
            deadline = self.monotonic() + starting.runtime_limit_s
            run, abandon_reason, skipped_reason = self._start_while_monitoring(
                starting, workspace, shutdown_event, deadline, token,
            )
            if (
                abandon_reason is None and skipped_reason is None
                and not isinstance(run, ProviderRun)
            ):
                raise RuntimeError("invalid_provider_run")
        except Exception:
            self.leases.mark_uncertain(token, "launch_uncertain")
            self._clear_active()
            with self._launch_lock:
                latest = self.store.get(starting.job_id)
                return self.store.transition(
                    latest.job_id, expected_states=(latest.state,),
                    new_state=JobState.INTERRUPTED, worker_epoch=self.worker_epoch,
                    expected_generation=latest.generation,
                    diagnostic_code="execution_uncertain",
                )
        if skipped_reason is not None:
            return self._finish_unlaunched(starting, token, skipped_reason)
        if abandon_reason is not None:
            # start() never returned a handle, so whether it launched is
            # unknown and nothing can be interrupted. The monitor already
            # quarantined the lease as START_PENDING_REASON; the tracked start
            # thread blocks admission (and a live-worker lease release) until
            # it exits. Record the job interrupted.
            self._clear_active()
            with self._launch_lock:
                latest = self.store.get(starting.job_id)
                return self.store.transition(
                    latest.job_id, expected_states=(latest.state,),
                    new_state=JobState.INTERRUPTED, worker_epoch=self.worker_epoch,
                    expected_generation=latest.generation,
                    diagnostic_code="execution_uncertain",
                )
        self._active_run = run
        try:
            with self._launch_lock:
                running = self.store.transition(
                    starting.job_id, expected_states=(JobState.STARTING,),
                    new_state=JobState.RUNNING, worker_epoch=self.worker_epoch,
                    expected_generation=starting.generation,
                    provider_session_id=run.session_id,
                )
                self._launch_committed = None
                return self.store.get(running.job_id), run, token, deadline
        except Exception:
            recovered = self._cleanup_started_run(starting, run, token)
            if recovered is not None:
                return recovered
            raise

    def _launch_fence(
        self, job_id: str, shutdown_event: threading.Event | None,
        authorization: object = True,
    ) -> str | None:
        """Why a launch must not start now, read under the control lock.

        ``authorization`` is what the remote launch guard returned outside the
        lock. For a remote job it is re-verified here against settings.json
        alone (no Keychain, no network): ``unpair`` clears the configured URL
        before it removes the key, so a launch the guard authorized moments
        before an unpair finds the URL gone (or changed) and never commits.
        """
        latest = self.store.get(job_id)
        if latest.state == JobState.CANCEL_REQUESTED:
            return "cancel_requested"
        if latest.state != JobState.STARTING:
            # Already finalized (an abandoned launch records the job
            # interrupted): a provider must never start for a finished job.
            return "worker_shutdown"
        if latest.expires_at <= datetime.now(timezone.utc):
            return "job_expired"
        policy = load_worker_settings(self.backup_root)
        if (shutdown_event is not None and shutdown_event.is_set()) or not policy.enabled:
            return "worker_shutdown"
        if latest.idempotency_key.startswith("remote:"):
            if self.remote_launch_guard is None or not (
                authorization is True or isinstance(authorization, RemoteAuthorization)
            ):
                return "worker_shutdown"
            # A worker ID recorded at pairing also catches an unpair followed by a
            # re-pair of the same URL: the new enrollment is a different worker.
            if policy.control_service_url is None or (
                isinstance(authorization, RemoteAuthorization)
                and (policy.control_service_url != authorization.url
                     or policy.control_service_worker_id not in {None, authorization.worker_id})
            ):
                return "unpaired"
        return None

    def _finish_unlaunched(self, starting: JobRecord, token, reason: str) -> JobRecord:
        """Record a launch the start thread declined: nothing was started."""
        self.leases.release(token, ReleaseEvidence.UNLAUNCHED)
        self._clear_active()
        with self._launch_lock:
            latest = self.store.get(starting.job_id)
            if latest.state == JobState.CANCEL_REQUESTED:
                return self._cancel_before_launch(latest)
            new_state, diagnostic = (
                (JobState.EXPIRED, "job_expired") if reason == "job_expired"
                else (JobState.FAILED, "worker_disabled")
            )
            return self.store.transition(
                latest.job_id, expected_states=(JobState.STARTING,),
                new_state=new_state, worker_epoch=self.worker_epoch,
                expected_generation=latest.generation, diagnostic_code=diagnostic,
            )

    def _start_while_monitoring(
        self, starting: JobRecord, workspace, shutdown_event: threading.Event | None,
        deadline: float, token,
    ) -> tuple[ProviderRun | None, str | None, str | None]:
        """Call ``start()`` on a tracked thread so stop, shutdown and the
        runtime limit stay enforceable while it runs.

        Returns ``(run, None, None)`` once start returns, raises its error,
        ``(None, None, reason)`` when the thread declined to launch (the
        cancellation, expiry and shutdown fences are re-read under the
        control lock immediately before ``start()``), or ``(None, reason,
        None)`` when a hung launch is abandoned. A stop gets
        START_CANCEL_GRACE_SECONDS for start to return a handle it can
        interrupt with proof. An abandoned start keeps the lease
        START_PENDING_REASON until the thread exits; a late handle is then
        interrupted best effort, never as stop proof.
        """
        outcome: dict[str, object] = {}
        outcome_lock = threading.Lock()
        completed = threading.Event()

        def start() -> None:
            late = False
            try:
                # Network/Keychain checks may block; do them on the tracked
                # start thread without holding the local control lock. Stop
                # and the runtime deadline stay responsive, and the final
                # local fence below observes anything that changed meanwhile.
                authorization: object = True
                if self.remote_launch_guard is not None:
                    try:
                        authorization = self.remote_launch_guard(starting.job_id)
                    except Exception:
                        authorization = False
                with self._launch_lock:
                    skipped = self._launch_fence(starting.job_id, shutdown_event, authorization)
                    with outcome_lock:
                        # Decided under the same lock the monitor abandons
                        # through: a launch given up on while the guard ran
                        # (stop, deadline or shutdown) never reaches start().
                        abandoned = outcome.get("abandoned")
                        if skipped is None and abandoned:
                            skipped = "job_expired" if abandoned == "runtime_limit_reached" else str(abandoned)
                        if skipped is None:
                            # Committed atomically with the fence: any stop from
                            # here on is reported as after the launch.
                            self._launch_committed = starting.job_id
                        else:
                            outcome["skipped"] = skipped
                if skipped is not None:
                    if abandoned:
                        # The monitor recorded the job interrupted and the lease
                        # start-pending; nothing started, so release it with proof.
                        try:
                            self.leases.release(token, ReleaseEvidence.UNLAUNCHED)
                        except Exception:
                            pass
                    return
                run = self.adapter.start(starting, workspace, worker_epoch=self.worker_epoch)
                with outcome_lock:
                    outcome["run"] = run
                    late = outcome.get("abandoned", False)
                if late and isinstance(run, ProviderRun):
                    try:
                        self.adapter.interrupt(run)
                    except BaseException:
                        pass
            except BaseException as error:
                with outcome_lock:
                    outcome["error"] = error
                    late = outcome.get("abandoned", False)
            finally:
                if late:
                    # The abandoned call has now returned: the lease stays
                    # uncertain, but it may be released on confirmation.
                    try:
                        self.leases.mark_uncertain(token, "launch_uncertain")
                    except Exception:
                        pass
                completed.set()

        thread = threading.Thread(
            target=start, name=f"openswap-provider-start-{starting.job_id}", daemon=True,
        )
        with self._event_reader_lock:
            self._event_reader = thread
            thread.start()

        cancel_grace_until = None
        while not completed.wait(0.05):
            self.heartbeat()
            reason = None
            if not load_worker_settings(self.backup_root).enabled:
                reason = "worker_shutdown"
            elif shutdown_event is not None and shutdown_event.is_set():
                reason = "worker_shutdown"
            elif self.monotonic() >= deadline:
                reason = "runtime_limit_reached"
            elif self.store.get(starting.job_id).state == JobState.CANCEL_REQUESTED:
                if cancel_grace_until is None:
                    cancel_grace_until = self.monotonic() + START_CANCEL_GRACE_SECONDS
                elif self.monotonic() >= cancel_grace_until:
                    reason = "cancel_requested"
            if reason is not None:
                with outcome_lock:
                    if not outcome:
                        # Marked before the flag, under the same lock the start
                        # thread reports through, so its quiesce update always
                        # lands after this one.
                        self.leases.mark_uncertain(token, START_PENDING_REASON)
                        outcome["abandoned"] = reason
                        return None, reason, None
                break  # start returned meanwhile: handle it normally
        # start() has returned, so this join waits only for the thread frame
        # to exit; the slot is then free for the first event read.
        completed.wait()
        thread.join()
        with self._event_reader_lock:
            if self._event_reader is thread:
                self._event_reader = None
        with outcome_lock:
            if "error" in outcome:
                raise outcome["error"]
            if "skipped" in outcome:
                return None, None, outcome["skipped"]
            return outcome.get("run"), None, None

    def _prepare_launch(
        self, claimed: JobRecord, availability: ProviderAvailability,
        shutdown_event: threading.Event | None = None,
    ):
        """Journal and lease steps before launch; runs under the control lock."""
        current = self.store.get(claimed.job_id)
        if current.state == JobState.CANCEL_REQUESTED:
            return self._cancel_before_launch(current)
        # A shutdown or opt-out that arrived while probe() ran records no
        # journal cancellation, so check it here, before any lease or launch.
        if (
            (shutdown_event is not None and shutdown_event.is_set())
            or not load_worker_settings(self.backup_root).enabled
        ):
            return self.store.transition(
                current.job_id, expected_states=(JobState.CLAIMED,),
                new_state=JobState.FAILED, worker_epoch=self.worker_epoch,
                expected_generation=current.generation, diagnostic_code="worker_disabled",
            )
        if not availability.available:
            return self.store.transition(
                current.job_id, expected_states=(JobState.CLAIMED,),
                new_state=JobState.FAILED, worker_epoch=self.worker_epoch,
                expected_generation=current.generation,
                diagnostic_code=availability.diagnostic_code or "provider_unavailable",
            )
        # The account is resolved, recorded on the job and leased under the
        # Codex mutation guard that `openswap worker account` also holds, so a
        # pin or allowlist change either lands before this launch (which then
        # uses it) or waits until the lease is taken; a job never starts on a
        # replaced account. The roster check shares the guard so `codex remove`
        # cannot drop the account in between.
        starting = None
        try:
            with self.leases.mutation_guard() as guard:
                guard.assert_available()
                # The owner's pin, or the job's allowlisted choice, as it reads
                # now; a refusal fails the job before STARTING (never launched).
                identity, refusal = self._resolve_launch_account(current)
                if identity is None:
                    return self.store.transition(
                        current.job_id, expected_states=(JobState.CLAIMED,),
                        new_state=JobState.FAILED, worker_epoch=self.worker_epoch,
                        expected_generation=current.generation, diagnostic_code=refusal,
                    )
                starting = self.store.transition(
                    current.job_id, expected_states=(JobState.CLAIMED,),
                    new_state=JobState.STARTING, worker_epoch=self.worker_epoch,
                    expected_generation=current.generation,
                    pinned_account_ref=identity,
                )
                if self._fixed_account_identity is None and not codex_account_in_roster(
                    self.backup_root, identity,
                ):
                    token = None
                else:
                    token = guard.acquire(
                        job_id=starting.job_id, account_identity=identity,
                        worker_pid=self.worker_pid, worker_epoch=self.worker_epoch,
                        ttl_s=starting.runtime_limit_s + 60,
                    )
        except (LeaseConflictError, LeaseStateError) as error:
            conflict = isinstance(error, LeaseConflictError)
            record = starting if starting is not None else current
            return self.store.transition(
                record.job_id, expected_states=(record.state,),
                new_state=JobState.FAILED if conflict else JobState.INTERRUPTED,
                worker_epoch=self.worker_epoch, expected_generation=record.generation,
                diagnostic_code="lease_conflict" if conflict else "execution_uncertain",
            )
        if token is None:
            # The pinned account was removed from the roster (or the roster is
            # unreadable): nothing was leased or launched.
            return self.store.transition(
                starting.job_id, expected_states=(JobState.STARTING,),
                new_state=JobState.FAILED, worker_epoch=self.worker_epoch,
                expected_generation=starting.generation,
                diagnostic_code="provider_auth_unavailable",
            )
        self._active_lease = token
        try:
            workspace = self._resolve_workspace(starting.workspace_id, starting.job_id)
        except Exception:
            self.leases.release(token, ReleaseEvidence.UNLAUNCHED)
            self._clear_active()
            return self.store.transition(
                starting.job_id, expected_states=(JobState.STARTING,),
                new_state=JobState.FAILED, worker_epoch=self.worker_epoch,
                expected_generation=starting.generation, diagnostic_code="provider_unavailable",
            )
        # Probe, lease and workspace setup take time: re-check expiry right
        # before launch so an expired job never starts.
        if starting.expires_at <= datetime.now(timezone.utc):
            self.leases.release(token, ReleaseEvidence.UNLAUNCHED)
            self._clear_active()
            return self.store.transition(
                starting.job_id, expected_states=(JobState.STARTING,),
                new_state=JobState.EXPIRED, worker_epoch=self.worker_epoch,
                expected_generation=starting.generation, diagnostic_code="job_expired",
            )
        return starting, token, workspace

    def _cleanup_started_run(self, starting: JobRecord, run: ProviderRun, token) -> JobRecord | None:
        """Stop an owned launch if post-start journal work fails; never replay it."""
        stopped = False
        try:
            stopped = self._interrupt_execution(token, run)
        except Exception:
            # A failed lease write cannot turn uncertain execution into proof.
            try:
                self.leases.mark_uncertain(token, "execution_uncertain")
            except Exception:
                pass
        finally:
            self._clear_active()

        try:
            latest = self.store.get(starting.job_id)
        except Exception:
            return None
        if latest.state in {
            JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
            JobState.INTERRUPTED, JobState.EXPIRED,
        }:
            return latest
        if latest.state not in {
            JobState.STARTING, JobState.RUNNING, JobState.CANCEL_REQUESTED,
        }:
            return None
        if stopped and latest.state == JobState.CANCEL_REQUESTED:
            final_state = JobState.CANCELLED
            diagnostic = "cancel_requested"
        elif stopped:
            final_state = JobState.FAILED
            diagnostic = "provider_unavailable"
        else:
            final_state = JobState.INTERRUPTED
            diagnostic = "execution_uncertain"
        try:
            return self.store.transition(
                latest.job_id, expected_states=(latest.state,),
                new_state=final_state, worker_epoch=self.worker_epoch,
                expected_generation=latest.generation,
                diagnostic_code=diagnostic,
            )
        except Exception:
            return None

    def _interrupt_active(self, record: JobRecord, reason: str) -> JobRecord:
        token, run = self._active_lease, self._active_run
        if token is None or run is None:
            if token is not None:
                self.leases.mark_uncertain(token, "execution_uncertain")
            self._clear_active()
            return self.store.transition(
                record.job_id, expected_states=(record.state,),
                new_state=JobState.INTERRUPTED, worker_epoch=self.worker_epoch,
                expected_generation=record.generation,
                diagnostic_code="execution_uncertain",
            )
        # Interruption may overlap a stop request, which advances the journal
        # generation while the provider is being asked to stop. Preserve the
        # result of this one interrupt attempt and finalize only against a
        # freshly loaded fence; never retry interrupt after explicit proof.
        try:
            stopped = self._interrupt_execution(token, run)
        except Exception:
            # A lease persistence failure is not stop proof. Quarantine best
            # effort, and do not let reconcile's outer handler call interrupt
            # a second time for the same owned handle.
            try:
                self.leases.mark_uncertain(token, "execution_uncertain")
            except Exception:
                pass
            stopped = False
        self._clear_active()
        # The lease already records either confirmed stop or uncertainty. A
        # journal read failure here propagates (like a failed terminal write
        # below): returning the stale in-flight record would orphan the row for
        # this process's life, while exiting lets the next start_epoch recover
        # it as interrupted.
        latest = self.store.get(record.job_id)
        terminal_states = {
            JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED,
            JobState.INTERRUPTED, JobState.EXPIRED,
        }
        if latest.state in terminal_states:
            return latest
        if latest.state not in {
            JobState.STARTING, JobState.RUNNING, JobState.CANCEL_REQUESTED,
        }:
            return latest
        if stopped:
            if latest.state == JobState.CANCEL_REQUESTED:
                new_state = JobState.CANCELLED
                diagnostic = "cancel_requested"
            elif reason in {"cancel_requested", "worker_shutdown"}:
                new_state = JobState.CANCELLED
                diagnostic = "cancel_requested" if reason == "cancel_requested" else "worker_disabled"
            elif reason == "runtime_limit_reached":
                new_state = JobState.FAILED
                diagnostic = "runtime_limit_reached"
            else:
                new_state = JobState.FAILED
                diagnostic = "provider_unavailable"
        else:
            new_state = JobState.INTERRUPTED
            diagnostic = "execution_uncertain"
        for attempt in range(2):
            try:
                return self.store.transition(
                    latest.job_id, expected_states=(latest.state,), new_state=new_state,
                    worker_epoch=self.worker_epoch, expected_generation=latest.generation,
                    diagnostic_code=diagnostic,
                )
            except StaleWriteError:
                latest = self.store.get(record.job_id)
                if latest.state in terminal_states or latest.state not in {
                    JobState.STARTING, JobState.RUNNING, JobState.CANCEL_REQUESTED,
                }:
                    return latest
                if latest.state == JobState.CANCEL_REQUESTED and stopped:
                    new_state, diagnostic = JobState.CANCELLED, "cancel_requested"
            # Any other journal failure propagates: the stop/quarantine evidence
            # is already recorded, and returning the stale in-flight record would
            # orphan the job for this process's life. Exiting lets the next
            # start_epoch recover it as interrupted; the run is never
            # re-interrupted or relaunched.
        return latest

    def _interrupt_execution(self, token, run) -> bool:
        """Attempt to stop an owned run and persist only explicit stop proof."""
        try:
            result = self.adapter.interrupt(run)
        except Exception:
            result = None
        if isinstance(result, InterruptResult) and result.execution_stopped is True:
            self.leases.release(token, ReleaseEvidence.CONFIRMED_STOPPED)
            return True
        self.leases.mark_uncertain(token, "execution_uncertain")
        return False

    def _clear_active(self) -> None:
        self._active_lease = None
        self._active_run = None
        self._launch_committed = None

    def _resolve_workspace(self, workspace_id: str, job_id: str) -> ResolvedWorkspace:
        settings = load_worker_settings(self.backup_root)
        workspace = next((item for item in settings.workspaces if item.workspace_id == workspace_id), None)
        if workspace is None:
            raise ValueError("workspace is not registered")
        base_root = workspace.output_root
        base_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _raise_for_output_problem(output_dir_problem(base_root))
        output_root = base_root / job_id
        output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _raise_for_output_problem(output_dir_problem(output_root))
        for source in workspace.readonly_roots:
            problem = readonly_source_problem(source)
            if problem == "unavailable":
                raise ValueError("approved read-only source is unavailable")
            if problem == "unsafe":
                raise ValueError("approved read-only source is unsafe")
            if problem == "not_owned":
                raise ValueError("approved read-only source is not locally owned")
            if problem == "permissions":
                raise ValueError("approved read-only source permissions are unsafe")
        return ResolvedWorkspace(
            workspace_id=workspace_id, output_root=output_root,
            readonly_sources=workspace.readonly_roots,
        )


def output_dir_problem(path: Path) -> str | None:
    """Why ``path`` cannot be a writable research root, or ``None``.

    It must be a real directory (not a symlink) owned by this user with no
    group or other access: ``unavailable``, ``unsafe`` or ``permissions``.
    Shared by launch-time resolution and ``openswap worker workspace add``.
    """
    try:
        info = path.lstat()
    except OSError:
        return "unavailable"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return "unsafe"
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        return "permissions"
    return None


def readonly_source_problem(path: Path) -> str | None:
    """Why ``path`` cannot be an approved read-only source, or ``None``.

    Readable by others is normal for a source checkout; writable by others is
    not, since the source could change during the run: ``unavailable``,
    ``unsafe``, ``not_owned`` or ``permissions``.
    """
    try:
        info = path.lstat()
    except OSError:
        return "unavailable"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return "unsafe"
    if os.name != "nt" and info.st_uid != os.getuid():
        return "not_owned"
    if os.name != "nt" and info.st_mode & 0o022:
        return "permissions"
    return None


def _raise_for_output_problem(problem: str | None) -> None:
    if problem == "permissions":
        raise ValueError("registered workspace permissions are unsafe")
    if problem is not None:
        raise ValueError("registered workspace is unsafe")


WORKER_REFUSED_MANAGED = 2


def _mark_stopped_quietly(runtime) -> None:
    if runtime is None:
        return
    try:
        runtime.mark_stopped()
    except Exception:
        pass


def run_worker(
    backup_root: Path,
    *,
    runtime_factory=WorkerRuntime,
    remote_factory=None,
    managed: bool = True,
    service_loaded=None,
) -> int:
    """Run the default-off worker daemon with a process-lifetime singleton lock.

    ``service_loaded`` (when given) reports whether the per-user LaunchAgent is
    loaded. An unmanaged start (``managed=False``, a manual ``worker run``)
    refuses with ``WORKER_REFUSED_MANAGED`` while it is, and the check runs
    under the same lifecycle lock as the policy check and singleton
    acquisition, so a concurrent enable cannot leave two competing workers.
    """
    from openswap.worker.ipc import serve, socket_path

    store = LocalJobStore(backup_root)
    store._ensure_private_dir()
    lifecycle_lock = FileLock(Path(backup_root) / "worker" / "lifecycle.lock", timeout=3)
    instance_lock = FileLock(Path(backup_root) / "worker" / "instance.lock", timeout=0)
    runtime = None
    server = None
    remote_thread = None
    if not lifecycle_lock.acquire(timeout=3):
        return 1
    if not instance_lock.acquire(timeout=0):
        lifecycle_lock.release()
        return 1
    stop_event = threading.Event()
    try:
        if not load_worker_settings(backup_root).enabled:
            return 0
        if not managed and service_loaded is not None and service_loaded():
            return WORKER_REFUSED_MANAGED
        runtime = runtime_factory(backup_root)
        ready_event = threading.Event()
        server = threading.Thread(
            target=serve,
            args=(socket_path(backup_root), runtime, stop_event),
            kwargs={"ready_event": ready_event},
            name="openswap-worker-ipc", daemon=False,
        )
        server.start()
        if not ready_event.wait(3.0) or not server.is_alive():
            stop_event.set()
            server.join(timeout=3)
            # start_epoch already recorded this process as the live worker;
            # clear it so status and disable do not see a stale worker.
            _mark_stopped_quietly(runtime)
            return 1
        if remote_factory is None:
            from openswap.worker.remote_state import ConfiguredRemote
            remote_factory = ConfiguredRemote
        if remote_factory is not None:
            remote = remote_factory(runtime)
            if remote is not None:
                remote_thread = threading.Thread(target=remote.run, args=(stop_event,),
                                                 name="openswap-worker-remote", daemon=True)
                remote_thread.start()
        lifecycle_lock.release()
        original_handlers = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            try:
                original_handlers[signum] = signal.signal(signum, lambda *_: stop_event.set())
            except ValueError:
                pass  # Embedded/test callers may not run in the main thread.
        try:
            while not stop_event.is_set():
                if not server.is_alive():
                    stop_event.set()
                    return 1
                if not load_worker_settings(backup_root).enabled:
                    # The policy can be disabled outside this process (for
                    # example, by another supported local control path). An
                    # active reconcile observes the same policy and interrupts
                    # with explicit stop proof or leaves the lease quarantined.
                    stop_event.set()
                    break
                runtime.heartbeat()
                runtime.reconcile_once(shutdown_event=stop_event)
                stop_event.wait(0.2)
        finally:
            stop_event.set()
            server.join(timeout=3)
            if server.is_alive():
                server.join()
            runtime.mark_stopped()
            for signum, handler in original_handlers.items():
                signal.signal(signum, handler)
        return 0
    except Exception:
        # An ambiguous in-flight run remains quarantined by the lease store;
        # process shutdown is never treated as proof that it stopped. Only the
        # worker's health record is cleared, so disable can still proceed.
        _mark_stopped_quietly(runtime)
        return 1
    finally:
        stop_event.set()
        if remote_thread is not None:
            remote_thread.join(timeout=6)
        lifecycle_lock.release()
        instance_lock.release()
