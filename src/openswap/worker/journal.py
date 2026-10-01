"""Private SQLite journal for local worker jobs and safe events.

Task text and local owner references are stored only in this private database;
the public read methods return redacted snapshots and allowlisted events.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Iterable
from uuid import uuid4

from openswap.worker.models import (
    EventPage,
    JobRecord,
    JobState,
    JobSubmission,
    SafeEvent,
    SafeEventKind,
)

SCHEMA_VERSION = 1
MAX_PENDING_JOBS = 20
MAX_EVENT_PAGE = 200
_JOB_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_ACTIVE_STATES = (JobState.CLAIMED, JobState.STARTING, JobState.RUNNING, JobState.CANCEL_REQUESTED)
_TERMINAL_STATES = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED, JobState.EXPIRED}
_ALLOWED_TRANSITIONS = {
    JobState.QUEUED: {JobState.CLAIMED, JobState.CANCELLED, JobState.EXPIRED},
    JobState.CLAIMED: {JobState.STARTING, JobState.CANCEL_REQUESTED, JobState.FAILED, JobState.INTERRUPTED},
    # EXPIRED from STARTING only before launch: a job that expired while it was
    # being prepared never starts.
    JobState.STARTING: {
        JobState.RUNNING, JobState.CANCEL_REQUESTED, JobState.FAILED, JobState.INTERRUPTED,
        JobState.EXPIRED,
    },
    JobState.RUNNING: {JobState.CANCEL_REQUESTED, JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.INTERRUPTED},
    JobState.CANCEL_REQUESTED: {JobState.CANCELLED, JobState.FAILED, JobState.INTERRUPTED},
}
_SAFE_DIAGNOSTICS = {
    "live_adapter_disabled", "provider_unavailable", "job_expired",
    "cancel_requested", "execution_uncertain", "lease_conflict",
    "worker_restarted", "invalid_transition", "worker_disabled",
    "runtime_limit_reached",
    "provider_auth_unavailable", "provider_rate_limited",
    "artifact_rejected",
}


class JournalError(RuntimeError):
    """A safe local journal error; callers should not expose SQLite details."""


class StaleWriteError(JournalError):
    """A state/event write lost its expected-state or epoch fence."""


class AdmissionError(JournalError):
    """The bounded local queue cannot accept another job."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp is not timezone-aware")
    return parsed


def _safe_diagnostic(code: str | None) -> str | None:
    if code is None:
        return None
    if not isinstance(code, str) or code not in _SAFE_DIAGNOSTICS:
        raise ValueError("diagnostic_code is not allowlisted")
    return code


def validate_event_fields(
    *,
    kind: SafeEventKind,
    state: JobState | None,
    diagnostic_code: str | None,
    execution_stopped: bool,
) -> str | None:
    """Validate and normalize the shared safe-event data contract."""
    if (
        not isinstance(kind, SafeEventKind)
        or (state is not None and not isinstance(state, JobState))
        or state == JobState.WAITING_FOR_APPROVAL
        or type(execution_stopped) is not bool
        or (execution_stopped and kind != SafeEventKind.PROVIDER_FINISHED)
    ):
        raise ValueError("event kind or state is unsupported")
    return _safe_diagnostic(diagnostic_code)


class LocalJobStore:
    """Transactional job/event store rooted at ``<backup_root>/worker``."""

    def __init__(self, backup_root: Path, *, max_pending: int = MAX_PENDING_JOBS):
        if type(max_pending) is not int or not 1 <= max_pending <= MAX_PENDING_JOBS:
            raise ValueError("max_pending is outside the local worker limit")
        self.state_dir = Path(backup_root) / "worker"
        self.path = self.state_dir / "jobs.sqlite3"
        self.max_pending = max_pending

    def _connect(self) -> sqlite3.Connection:
        self._ensure_private_dir()
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            if self.path.exists() and self.path.stat().st_size:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )}
                if "metadata" in tables:
                    schema = connection.execute(
                        "SELECT value FROM metadata WHERE key='schema_version'"
                    ).fetchone()
                    if schema is None or schema[0] != str(SCHEMA_VERSION):
                        raise JournalError("local worker journal schema is unsupported")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 2000")
            connection.execute("PRAGMA journal_mode = WAL")
            self._initialize(connection)
            if os.name != "nt":
                os.chmod(self.path, 0o600)
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(str(self.path) + suffix)
                    if sidecar.exists():
                        os.chmod(sidecar, 0o600)
            return connection
        except JournalError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error):
            if connection is not None:
                connection.close()
            raise JournalError("local worker journal is unavailable") from None

    def _ensure_private_dir(self) -> None:
        try:
            self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.state_dir.lstat()
        except OSError:
            raise JournalError("local worker state directory is unavailable") from None
        if self.state_dir.is_symlink() or not self.state_dir.is_dir():
            raise JournalError("local worker state directory is unsafe")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise JournalError("local worker state directory permissions are unsafe")

    @staticmethod
    def _initialize(db: sqlite3.Connection) -> None:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                owner_ref TEXT NOT NULL,
                provider TEXT NOT NULL,
                task TEXT NOT NULL,
                capability_profile TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                runtime_limit_s REAL NOT NULL,
                pinned_account_ref TEXT,
                provider_session_id TEXT,
                worker_epoch INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                diagnostic_code TEXT,
                event_cursor INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_state_created ON jobs(state, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs((1))
                WHERE state IN ('claimed','starting','running','cancel_requested');
            CREATE TABLE IF NOT EXISTS events (
                job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                cursor INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                kind TEXT NOT NULL,
                state TEXT,
                diagnostic_code TEXT,
                execution_stopped INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(job_id, cursor)
            );
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('schema_version', '1');
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('worker_epoch', '0');
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('worker_pid', '');
            INSERT OR IGNORE INTO metadata(key, value) VALUES ('last_seen_at', '');
            """
        )
        row = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None or row["value"] != str(SCHEMA_VERSION):
            raise JournalError("local worker journal schema is unsupported")

    def start_epoch(self, worker_pid: int) -> tuple[int, tuple[str, ...]]:
        """Fence prior processes and mark uncertain active rows interrupted."""
        if type(worker_pid) is not int or worker_pid <= 0:
            raise ValueError("worker_pid must be a positive integer")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            epoch = int(db.execute("SELECT value FROM metadata WHERE key='worker_epoch'").fetchone()[0]) + 1
            db.execute("UPDATE metadata SET value=? WHERE key='worker_epoch'", (str(epoch),))
            rows = db.execute(
                "SELECT job_id, state, generation FROM jobs WHERE state IN (?, ?, ?, ?)",
                tuple(state.value for state in _ACTIVE_STATES),
            ).fetchall()
            recovered: list[str] = []
            now = _stamp(_now())
            queued_rows = db.execute(
                "SELECT job_id,generation FROM jobs WHERE state=?",
                (JobState.QUEUED.value,),
            ).fetchall()
            for row in queued_rows:
                db.execute(
                    "UPDATE jobs SET worker_epoch=?, generation=? WHERE job_id=? AND generation=?",
                    (epoch, row["generation"] + 1, row["job_id"], row["generation"]),
                )
            for row in rows:
                self._append_event_tx(db, row["job_id"], SafeEventKind.STATE_CHANGED,
                                      JobState.INTERRUPTED, "worker_restarted", now)
                db.execute(
                    "UPDATE jobs SET state=?, updated_at=?, worker_epoch=?, generation=?, diagnostic_code=? "
                    "WHERE job_id=? AND generation=?",
                    (JobState.INTERRUPTED.value, now, epoch, row["generation"] + 1,
                     "worker_restarted", row["job_id"], row["generation"]),
                )
                recovered.append(row["job_id"])
            db.execute("UPDATE metadata SET value=? WHERE key='worker_pid'", (str(worker_pid),))
            db.execute("UPDATE metadata SET value=? WHERE key='last_seen_at'", (now,))
            db.commit()
            return epoch, tuple(recovered)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def heartbeat(self, worker_pid: int, epoch: int) -> None:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._assert_epoch_tx(db, epoch)
            db.execute("UPDATE metadata SET value=? WHERE key='worker_pid'", (str(worker_pid),))
            db.execute("UPDATE metadata SET value=? WHERE key='last_seen_at'", (_stamp(_now()),))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_stopped(self, worker_pid: int, epoch: int) -> None:
        """Clear live-process health after shutdown, fenced to this epoch."""
        if type(worker_pid) is not int or worker_pid <= 0:
            raise ValueError("worker_pid must be a positive integer")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._assert_epoch_tx(db, epoch)
            row = db.execute(
                "SELECT value FROM metadata WHERE key='worker_pid'"
            ).fetchone()
            if row is None or row["value"] != str(worker_pid):
                raise StaleWriteError("worker health belongs to another process")
            db.execute("UPDATE metadata SET value='' WHERE key='worker_pid'")
            db.execute("UPDATE metadata SET value='' WHERE key='last_seen_at'")
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def health(self) -> tuple[int | None, datetime | None]:
        db = self._connect()
        try:
            rows = dict((r["key"], r["value"]) for r in db.execute(
                "SELECT key, value FROM metadata WHERE key IN ('worker_pid', 'last_seen_at')"
            ))
            pid = int(rows["worker_pid"]) if rows.get("worker_pid") else None
            seen = _parse_stamp(rows["last_seen_at"]) if rows.get("last_seen_at") else None
            return pid, seen
        finally:
            db.close()

    def current_epoch(self) -> int:
        """The epoch ``start_epoch`` last recorded (bumped once per worker start)."""
        db = self._connect()
        try:
            row = db.execute("SELECT value FROM metadata WHERE key='worker_epoch'").fetchone()
            return int(row["value"]) if row is not None else 0
        finally:
            db.close()

    def create(
        self,
        submission: JobSubmission,
        *,
        owner_ref: str,
        worker_epoch: int,
        job_id: str | None = None,
        now: datetime | None = None,
    ) -> JobRecord:
        if not owner_ref or len(owner_ref) > 256 or type(worker_epoch) is not int or worker_epoch < 0:
            raise ValueError("local owner and epoch are required")
        job_id = job_id or uuid4().hex
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ValueError("job_id must be a local opaque identifier")
        timestamp = _stamp(now or _now())
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._assert_epoch_tx(db, worker_epoch)
            existing = db.execute("SELECT job_id FROM jobs WHERE idempotency_key=?", (submission.idempotency_key,)).fetchone()
            if existing:
                prior_row = db.execute("SELECT * FROM jobs WHERE job_id=?", (existing["job_id"],)).fetchone()
                prior = self._record(prior_row)
                if (
                    prior.owner_ref != owner_ref
                    or prior.provider != submission.provider
                    or prior.task != submission.task
                    or prior.capability_profile != submission.capability_profile
                    or prior.workspace_id != submission.workspace_id
                    or prior.expires_at != submission.expires_at
                    or prior.runtime_limit_s != submission.runtime_limit_s
                ):
                    raise AdmissionError("idempotency key is already bound to a different request")
                db.commit()
                return prior
            queued = db.execute("SELECT COUNT(*) AS n FROM jobs WHERE state=?", (JobState.QUEUED.value,)).fetchone()["n"]
            if queued >= self.max_pending:
                raise AdmissionError("local worker queue is full")
            db.execute(
                "INSERT INTO jobs(job_id,idempotency_key,owner_ref,provider,task,capability_profile,workspace_id,state,"
                "created_at,updated_at,expires_at,runtime_limit_s,worker_epoch,generation) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (job_id, submission.idempotency_key, owner_ref, submission.provider, submission.task,
                 submission.capability_profile, submission.workspace_id, JobState.QUEUED.value,
                 timestamp, timestamp, _stamp(submission.expires_at), submission.runtime_limit_s,
                 worker_epoch),
            )
            self._append_event_tx(db, job_id, SafeEventKind.STATE_CHANGED, JobState.QUEUED, None, timestamp)
            db.commit()
            return self.get(job_id)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim(self, job_id: str, *, worker_epoch: int, expected_generation: int) -> JobRecord:
        return self.transition(
            job_id, expected_states=(JobState.QUEUED,), new_state=JobState.CLAIMED,
            worker_epoch=worker_epoch, expected_generation=expected_generation,
        )

    def transition(
        self,
        job_id: str,
        *,
        expected_states: Iterable[JobState],
        new_state: JobState,
        worker_epoch: int,
        expected_generation: int,
        diagnostic_code: str | None = None,
        pinned_account_ref: str | None = None,
        provider_session_id: str | None = None,
    ) -> JobRecord:
        if new_state == JobState.WAITING_FOR_APPROVAL:
            raise ValueError("approval state is unsupported in Phase 2")
        diagnostic_code = _safe_diagnostic(diagnostic_code)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._assert_epoch_tx(db, worker_epoch)
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["worker_epoch"] != worker_epoch or row["generation"] != expected_generation:
                raise StaleWriteError("job write lost its generation fence")
            old_state = JobState(row["state"])
            if old_state not in tuple(expected_states):
                raise StaleWriteError("job write lost its expected-state fence")
            if new_state not in _ALLOWED_TRANSITIONS.get(old_state, set()):
                raise StaleWriteError("job state transition is not allowed")
            if new_state in _ACTIVE_STATES and old_state not in _ACTIVE_STATES:
                active = db.execute(
                    "SELECT job_id FROM jobs WHERE state IN (?, ?, ?, ?) AND job_id<>? LIMIT 1",
                    (*tuple(state.value for state in _ACTIVE_STATES), job_id),
                ).fetchone()
                if active is not None:
                    raise AdmissionError("another local worker job is active")
            now = _stamp(_now())
            generation = expected_generation + 1
            cursor = db.execute(
                "UPDATE jobs SET state=?, updated_at=?, generation=?, diagnostic_code=?, "
                "pinned_account_ref=COALESCE(?,pinned_account_ref), "
                "provider_session_id=COALESCE(?,provider_session_id) "
                "WHERE job_id=? AND worker_epoch=? AND generation=?",
                (new_state.value, now, generation, diagnostic_code, pinned_account_ref,
                 provider_session_id, job_id, worker_epoch, expected_generation),
            )
            if cursor.rowcount != 1:
                raise StaleWriteError("job write lost its generation fence")
            self._append_event_tx(db, job_id, SafeEventKind.STATE_CHANGED, new_state, diagnostic_code, now)
            db.commit()
            return self.get(job_id)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def append_event(
        self,
        job_id: str,
        *,
        kind: SafeEventKind,
        worker_epoch: int,
        expected_generation: int,
        state: JobState | None = None,
        diagnostic_code: str | None = None,
        execution_stopped: bool = False,
    ) -> SafeEvent:
        diagnostic_code = validate_event_fields(
            kind=kind, state=state, diagnostic_code=diagnostic_code,
            execution_stopped=execution_stopped,
        )
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self._assert_epoch_tx(db, worker_epoch)
            row = db.execute("SELECT worker_epoch,generation FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["worker_epoch"] != worker_epoch or row["generation"] != expected_generation:
                raise StaleWriteError("event write lost its generation fence")
            timestamp = _stamp(_now())
            cursor = self._append_event_tx(
                db, job_id, kind, state, diagnostic_code, timestamp, execution_stopped,
            )
            db.commit()
            return SafeEvent(job_id, cursor, _parse_stamp(timestamp), kind, state,
                             diagnostic_code, execution_stopped)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def cancel(self, job_id: str, *, worker_epoch: int, expected_generation: int) -> JobRecord:
        record = self.get(job_id)
        if record.state in _TERMINAL_STATES:
            return record
        if record.state == JobState.CANCEL_REQUESTED:
            if record.worker_epoch != worker_epoch or record.generation != expected_generation:
                raise StaleWriteError("cancel request lost its generation fence")
            return record
        if record.state == JobState.QUEUED:
            return self.transition(
                job_id, expected_states=(JobState.QUEUED,), new_state=JobState.CANCELLED,
                worker_epoch=worker_epoch, expected_generation=expected_generation,
            )
        return self.transition(
            job_id, expected_states=(JobState.CLAIMED, JobState.STARTING, JobState.RUNNING),
            new_state=JobState.CANCEL_REQUESTED, worker_epoch=worker_epoch,
            expected_generation=expected_generation, diagnostic_code="cancel_requested",
        )

    def get(self, job_id: str) -> JobRecord:
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return self._record(row)
        finally:
            db.close()

    def get_by_idempotency_key(self, idempotency_key: str) -> JobRecord | None:
        db = self._connect()
        try:
            row = db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            return self._record(row) if row else None
        finally:
            db.close()

    def list_events(self, job_id: str, *, after_cursor: int = 0, limit: int = 100) -> EventPage:
        if type(after_cursor) is not int or after_cursor < 0:
            raise ValueError("after_cursor must be a nonnegative integer")
        if type(limit) is not int or not 1 <= limit <= MAX_EVENT_PAGE:
            raise ValueError("event page size is outside the local limit")
        db = self._connect()
        try:
            if db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is None:
                raise KeyError(job_id)
            rows = db.execute(
                "SELECT * FROM events WHERE job_id=? AND cursor>? ORDER BY cursor LIMIT ?",
                (job_id, after_cursor, limit),
            ).fetchall()
            events = tuple(SafeEvent(
                job_id=job_id,
                cursor=row["cursor"],
                timestamp=_parse_stamp(row["timestamp"]),
                kind=SafeEventKind(row["kind"]),
                state=JobState(row["state"]) if row["state"] else None,
                diagnostic_code=row["diagnostic_code"],
                execution_stopped=bool(row["execution_stopped"]),
            ) for row in rows)
            return EventPage(events, events[-1].cursor if events else after_cursor)
        finally:
            db.close()

    def queue(self, *, limit: int = MAX_PENDING_JOBS) -> tuple[JobRecord, ...]:
        db = self._connect()
        try:
            rows = db.execute(
                "SELECT * FROM jobs WHERE state=? ORDER BY created_at, job_id LIMIT ?",
                (JobState.QUEUED.value, min(max(limit, 0), self.max_pending)),
            ).fetchall()
            return tuple(self._record(row) for row in rows)
        finally:
            db.close()

    def active(self) -> JobRecord | None:
        db = self._connect()
        try:
            placeholders = ",".join("?" for _ in _ACTIVE_STATES)
            row = db.execute(
                f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY updated_at LIMIT 1",
                tuple(state.value for state in _ACTIVE_STATES),
            ).fetchone()
            return self._record(row) if row else None
        finally:
            db.close()

    @staticmethod
    def _assert_epoch_tx(db: sqlite3.Connection, epoch: int) -> None:
        row = db.execute("SELECT value FROM metadata WHERE key='worker_epoch'").fetchone()
        if row is None or int(row["value"]) != epoch:
            raise StaleWriteError("worker write lost its epoch fence")

    @staticmethod
    def _append_event_tx(
        db: sqlite3.Connection,
        job_id: str,
        kind: SafeEventKind,
        state: JobState | None,
        diagnostic_code: str | None,
        timestamp: str,
        execution_stopped: bool = False,
    ) -> int:
        row = db.execute("SELECT event_cursor FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        cursor = row["event_cursor"] + 1
        db.execute(
            "INSERT INTO events(job_id,cursor,timestamp,kind,state,diagnostic_code,execution_stopped) "
            "VALUES(?,?,?,?,?,?,?)",
            (job_id, cursor, timestamp, kind.value, state.value if state else None,
             diagnostic_code, int(execution_stopped)),
        )
        db.execute("UPDATE jobs SET event_cursor=? WHERE job_id=?", (cursor, job_id))
        return cursor

    @staticmethod
    def _record(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"], idempotency_key=row["idempotency_key"],
            owner_ref=row["owner_ref"], provider=row["provider"], task=row["task"],
            capability_profile=row["capability_profile"], workspace_id=row["workspace_id"],
            state=JobState(row["state"]), created_at=_parse_stamp(row["created_at"]),
            updated_at=_parse_stamp(row["updated_at"]), expires_at=_parse_stamp(row["expires_at"]),
            runtime_limit_s=row["runtime_limit_s"], pinned_account_ref=row["pinned_account_ref"],
            provider_session_id=row["provider_session_id"], worker_epoch=row["worker_epoch"],
            generation=row["generation"], diagnostic_code=row["diagnostic_code"],
            event_cursor=row["event_cursor"],
        )
