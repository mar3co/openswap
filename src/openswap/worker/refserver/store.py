"""Durable single-owner protocol service, independent of provider accounts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from uuid import uuid4
from datetime import datetime, timezone
from contextlib import closing

from openswap.worker.models import SafeEventKind
from openswap.worker.protocol import (
    Artifact, DEVICE_TTL_SECONDS, HEARTBEAT_SECONDS, LEASE_SECONDS, MAX_ARTIFACTS,
    MISSED_HEARTBEATS, ProtocolError, Submission, TERMINAL, advertised_accounts, event_from_dict,
    execution_mode, fields, integer, reported_folders, text, stamp as wire_stamp,
)

# Jobs whose cancel flag a reconnecting worker must still enforce.
ENFORCE_CANCEL = ("claimed", "starting", "running", "cancel_requested", "interrupted")
# Heartbeat lists at most this many: live jobs first, then the newest unconfirmed interruptions.
MAX_CANCEL_IDS = 100


def stamp(seconds: float) -> str:
    return wire_stamp(datetime.fromtimestamp(seconds, timezone.utc))


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class ControlStore:
    # How long a request waits for its turn. Shorter than the client's 5 s request
    # timeout, so a request whose caller has given up is refused, not run late.
    request_wait_seconds = 2.5

    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        # Requests from this process queue here instead of in SQLite's busy handler,
        # which polls with growing sleeps: under steady writes from several threads
        # one waiter can lose the lock for its whole busy timeout, and a client whose
        # socket timeout is just as long then gives up first.
        self._requests = threading.Lock()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise ValueError("unsafe database path")
        if os.name != "nt":
            info = self.path.parent.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("database needs an owner-private directory")
            if self.path.exists() and (self.path.stat().st_uid != os.getuid() or self.path.stat().st_mode & 0o077):
                raise ValueError("database needs owner-private permissions")
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, expiry REAL NOT NULL, device TEXT);
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, key_hash TEXT UNIQUE NOT NULL, expiry REAL NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, epoch INTEGER NOT NULL DEFAULT 0, seen REAL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, device TEXT NOT NULL, idem TEXT NOT NULL, payload TEXT NOT NULL,
                    state TEXT NOT NULL, expiry REAL NOT NULL, epoch INTEGER NOT NULL DEFAULT 0,
                    lease REAL, cursor INTEGER NOT NULL DEFAULT 0, cancel INTEGER NOT NULL DEFAULT 0,
                    confirmed INTEGER NOT NULL DEFAULT 0, UNIQUE(device,idem));
                CREATE TABLE IF NOT EXISTS events (
                    job TEXT NOT NULL, cursor INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(job,cursor));
                CREATE TABLE IF NOT EXISTS artifacts (
                    job TEXT NOT NULL, name TEXT NOT NULL, hash TEXT NOT NULL, content BLOB NOT NULL,
                    PRIMARY KEY(job,name));
                CREATE TABLE IF NOT EXISTS advertised_accounts (
                    device TEXT NOT NULL, account_ref TEXT NOT NULL, label TEXT NOT NULL,
                    is_default INTEGER NOT NULL, position INTEGER NOT NULL,
                    PRIMARY KEY(device,account_ref));
                CREATE TABLE IF NOT EXISTS account_choice_epochs (
                    device TEXT PRIMARY KEY, epoch INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS readiness (
                    device TEXT PRIMARY KEY, folders TEXT NOT NULL, execution TEXT NOT NULL);
            ''')
            if "confirmed" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0")
            if "device" not in {r[1] for r in db.execute("PRAGMA table_info(codes)")}:
                db.execute("ALTER TABLE codes ADD COLUMN device TEXT")
        if os.name != "nt":
            os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def issue_code(self, worker_id: str | None = None) -> str:
        """A one-use pairing code; with ``worker_id``, a renewal code for that worker.

        Pairing with a renewal code rotates the existing worker's key and expiry and
        keeps its ID, so its jobs, events and artifacts stay reachable; the old key
        stops working. A revoked worker cannot be renewed.
        """
        code = "pair_" + secrets.token_urlsafe(24)
        with closing(self.connect()) as db, db:
            if worker_id is not None:
                device = db.execute("SELECT revoked FROM devices WHERE id=?", (worker_id,)).fetchone()
                if device is None:
                    raise ProtocolError("not_found", 404)
                if device["revoked"]:
                    raise ProtocolError("revoked", 403)
            db.execute("DELETE FROM codes WHERE expiry<=?", (self.clock(),))
            db.execute("INSERT INTO codes VALUES (?,?,?)", (digest(code), self.clock() + 600, worker_id))
        return code

    def advertised_accounts(self, worker_id: str) -> list[dict]:
        """The worker's advertised accounts, in the order it sent them (for the operator and tests)."""
        with closing(self.connect()) as db:
            rows = db.execute("SELECT account_ref,label,is_default FROM advertised_accounts WHERE device=? "
                              "ORDER BY position", (worker_id,)).fetchall()
        return [{"account_ref": r[0], "label": r[1], "default": bool(r[2])} for r in rows]

    def readiness(self, worker_id: str) -> dict | None:
        """The worker's readiness report (folders in sent order and execution mode), or None
        when its current registration has not reported (for the operator and tests)."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT folders,execution FROM readiness WHERE device=?", (worker_id,)).fetchone()
        return None if row is None else {"folders": json.loads(row[0]), "execution": row[1]}

    def revoke(self, worker_id: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("UPDATE devices SET revoked=1 WHERE id=?", (worker_id,)).rowcount:
                raise ProtocolError("not_found", 404)
            db.execute("DELETE FROM advertised_accounts WHERE device=?", (worker_id,))
            db.execute("DELETE FROM account_choice_epochs WHERE device=?", (worker_id,))
            db.execute("DELETE FROM readiness WHERE device=?", (worker_id,))
            db.execute("UPDATE jobs SET state='interrupted' WHERE device=? AND state NOT IN "
                       "('queued','succeeded','failed','cancelled','interrupted','expired')", (worker_id,))

    def _sweep(self, db, now):
        db.execute("UPDATE jobs SET state='expired' WHERE state='queued' AND expiry<=?", (now,))
        db.execute("UPDATE jobs SET state='interrupted' WHERE state IN "
                   "('claimed','starting','running','cancel_requested') AND device IN "
                   "(SELECT id FROM devices WHERE seen IS NULL OR seen<=?)",
                   (now - HEARTBEAT_SECONDS * MISSED_HEARTBEATS,))

    @staticmethod
    def _device(db, key, now):
        """Authenticate a device key; read-only, so it runs before any write lock or sweep."""
        if not key or len(key) > 200:
            raise ProtocolError("unauthorized", 401)
        device = db.execute("SELECT * FROM devices WHERE key_hash=?", (digest(key),)).fetchone()
        if device is None:
            raise ProtocolError("unauthorized", 401)
        if device["revoked"]:
            raise ProtocolError("revoked", 403)
        if device["expiry"] <= now:
            raise ProtocolError("device_expired", 401)
        return device

    def request(self, operation: str, value: object, key: str | None = None) -> dict:
        # A refused request has not touched the database, so a retry is safe.
        if not self._requests.acquire(timeout=self.request_wait_seconds):
            raise ProtocolError("service_unavailable", 503)
        try:
            return self._request(operation, value, key)
        finally:
            self._requests.release()

    def _request(self, operation: str, value: object, key: str | None = None) -> dict:
        db = self.connect()
        try:
            now = self.clock()
            if operation != "pair":
                self._device(db, key, now)
            self._sweep(db, now)
            db.commit()
            db.execute("BEGIN IMMEDIATE")
            if operation == "pair":
                data = fields(value, {"code"})
                hashed = digest(text(data["code"]))
                row = db.execute("SELECT expiry,device FROM codes WHERE hash=?", (hashed,)).fetchone()
                if row is None or row[0] <= now:
                    raise ProtocolError("invalid_code")
                db.execute("DELETE FROM codes WHERE hash=?", (hashed,))
                device_key = secrets.token_urlsafe(32)
                if row[1] is not None:
                    # Renewal: rotate the key in place; a worker revoked since the code was issued stays revoked.
                    worker_id = row[1]
                    # Like registration, rotation clears liveness: only a heartbeat with the new key restores it.
                    if not db.execute("UPDATE devices SET key_hash=?,expiry=?,seen=NULL WHERE id=? AND revoked=0",
                                      (digest(device_key), now + DEVICE_TTL_SECONDS, worker_id)).rowcount:
                        raise ProtocolError("invalid_code")
                else:
                    worker_id = uuid4().hex
                    db.execute("INSERT INTO devices(id,key_hash,expiry) VALUES (?,?,?)",
                               (worker_id, digest(device_key), now + DEVICE_TTL_SECONDS))
                result = {"worker_id": worker_id, "device_key": device_key, "expires_at": stamp(now + DEVICE_TTL_SECONDS)}
            else:
                # Re-read inside the write transaction so a concurrent revocation cannot race the check.
                device = self._device(db, key, now)
                result = self._authorized(db, operation, value, device, now)
            db.commit()
            return result
        finally:
            db.close()

    @staticmethod
    def _epoch(data, device):
        if integer(data["worker_epoch"], 1) != device["epoch"]:
            raise ProtocolError("stale_epoch", 409)

    @staticmethod
    def _job(db, job_id, device):
        job = db.execute("SELECT * FROM jobs WHERE id=?", (text(job_id),)).fetchone()
        if job is None or job["device"] != device["id"]:
            raise ProtocolError("not_found", 404)  # another owner's job is indistinguishable from none
        return job

    def _fence(self, data, job, device):
        self._epoch(data, device)
        if integer(data["epoch"], 1) != job["epoch"]:
            raise ProtocolError("stale_epoch", 409)

    @staticmethod
    def _summary(job):
        return {"job_id": job["id"], "state": job["state"], "epoch": job["epoch"],
                "event_cursor": job["cursor"], "expires_at": stamp(job["expiry"]),
                "cancel_requested": bool(job["cancel"])}

    def _authorized(self, db, op, value, device, now):
        if op == "register":
            fields(value, set())
            epoch = device["epoch"] + 1
            # Only heartbeat counts as liveness; a registration alone leaves the worker offline,
            # even when the previous incarnation's heartbeat is still fresh.
            db.execute("UPDATE devices SET epoch=?,seen=NULL WHERE id=?", (epoch, device["id"]))
            # A new registration starts with no advertised accounts; the worker resends them.
            db.execute("DELETE FROM advertised_accounts WHERE device=?", (device["id"],))
            db.execute("DELETE FROM account_choice_epochs WHERE device=?", (device["id"],))
            # ...and has not reported its folders or execution mode either.
            db.execute("DELETE FROM readiness WHERE device=?", (device["id"],))
            db.execute("UPDATE jobs SET state='interrupted' WHERE device=? AND state IN "
                       "('claimed','starting','running','cancel_requested')", (device["id"],))
            return {"worker_id": device["id"], "worker_epoch": epoch,
                    "heartbeat_seconds": HEARTBEAT_SECONDS, "lease_seconds": LEASE_SECONDS}
        if op in {"heartbeat", "poll"}:
            data = fields(value, {"worker_epoch"})
            self._epoch(data, device)
            if op == "heartbeat":
                db.execute("UPDATE devices SET seen=? WHERE id=?", (now, device["id"]))
                cancelled = db.execute(
                    "SELECT id FROM jobs WHERE device=? AND cancel=1 AND confirmed=0 AND state IN (?,?,?,?,?) "
                    "ORDER BY state='interrupted', rowid DESC LIMIT ?", (device["id"], *ENFORCE_CANCEL, MAX_CANCEL_IDS))
                return {"last_seen_at": stamp(now), "cancel_job_ids": [r[0] for r in cancelled]}
            if device["seen"] is None or device["seen"] <= now - HEARTBEAT_SECONDS * MISSED_HEARTBEATS:
                raise ProtocolError("offline_worker", 409)
            active = db.execute("SELECT * FROM jobs WHERE device=? AND epoch>0 AND state NOT IN "
                                "('succeeded','failed','cancelled','interrupted','expired')", (device["id"],)).fetchone()
            if active is not None:
                if active["lease"] <= now:
                    raise ProtocolError("lease_lost", 409)
                job = active
            else:
                # A job carrying account_ref is offered only once this registration has
                # sent `accounts`, so a worker without the extension never receives it.
                sent = db.execute("SELECT 1 FROM account_choice_epochs WHERE device=? AND epoch=?",
                                  (device["id"], device["epoch"])).fetchone() is not None
                job = db.execute("SELECT * FROM jobs WHERE device=? AND state='queued'"
                                 + ("" if sent else " AND json_extract(payload,'$.account_ref') IS NULL")
                                 + " ORDER BY rowid LIMIT 1", (device["id"],)).fetchone()
                if job is None:
                    return {"claim": None}
                db.execute("UPDATE jobs SET state='claimed',epoch=epoch+1,lease=? WHERE id=?",
                           (now + LEASE_SECONDS, job["id"]))
                job = self._job(db, job["id"], device)
            return {"claim": {"job_id": job["id"], "epoch": job["epoch"], "lease_until": stamp(job["lease"]),
                              "submission": json.loads(job["payload"])}}
        if op == "accounts":
            # Optional account choice extension: atomically replace the advertised set.
            data = fields(value, {"worker_epoch", "accounts"})
            self._epoch(data, device)
            entries = advertised_accounts(data["accounts"])
            db.execute("DELETE FROM advertised_accounts WHERE device=?", (device["id"],))
            db.executemany("INSERT INTO advertised_accounts VALUES (?,?,?,?,?)",
                           [(device["id"], e.account_ref, e.label, int(e.default), i) for i, e in enumerate(entries)])
            db.execute("INSERT OR REPLACE INTO account_choice_epochs VALUES (?,?)", (device["id"], device["epoch"]))
            return {"account_count": len(entries)}
        if op == "readiness":
            # Optional readiness report extension: atomically replace the report.
            data = fields(value, {"worker_epoch", "folders", "execution"})
            self._epoch(data, device)
            folders = reported_folders(data["folders"])
            mode = execution_mode(data["execution"])
            db.execute("INSERT OR REPLACE INTO readiness VALUES (?,?,?)",
                       (device["id"], json.dumps([f.to_dict() for f in folders]), mode))
            return {"folder_count": len(folders)}
        if op == "submit":
            submission = Submission.from_dict(value, allow_account_ref=True)
            if submission.worker_id != device["id"]:
                raise ProtocolError("forbidden", 403)
            # account_ref, when present, is part of the normalized idempotency payload.
            payload = json.dumps(submission.to_dict(), sort_keys=True)
            prior = db.execute("SELECT * FROM jobs WHERE device=? AND idem=?",
                               (device["id"], submission.job.idempotency_key)).fetchone()
            if prior is not None:
                if payload != prior["payload"]:
                    raise ProtocolError("idempotency_conflict", 409)
                return {"job_id": prior["id"], "state": prior["state"]}
            # A new admission may name only an account the worker currently advertises.
            if submission.account_ref is not None and db.execute(
                    "SELECT 1 FROM advertised_accounts WHERE device=? AND account_ref=?",
                    (device["id"], submission.account_ref)).fetchone() is None:
                raise ProtocolError("invalid_request")
            if device["seen"] is None or device["seen"] <= now - HEARTBEAT_SECONDS * MISSED_HEARTBEATS:
                raise ProtocolError("offline_worker", 409)
            expiry = submission.job.expires_at.timestamp()
            if not now < expiry <= now + 86400:
                raise ProtocolError("invalid_request")
            count = db.execute("SELECT count(*) FROM jobs WHERE device=? AND state='queued'", (device["id"],)).fetchone()[0]
            if count >= 20:
                raise ProtocolError("queue_full", 409)
            job_id = uuid4().hex
            db.execute("INSERT INTO jobs(id,device,idem,payload,state,expiry) VALUES (?,?,?,?,'queued',?)",
                       (job_id, device["id"], submission.job.idempotency_key, payload, expiry))
            return {"job_id": job_id, "state": "queued"}
        schemas = {
            "job": ({"job_id"}, set()), "cancel": ({"job_id"}, set()),
            "renew": ({"worker_epoch", "job_id", "epoch"}, set()),
            "reconcile": ({"worker_epoch", "job_id", "epoch", "state", "execution_stopped", "unlaunched"}, set()),
            "events": ({"job_id", "after_cursor"}, {"worker_epoch", "epoch", "events"}),
            "upload": ({"worker_epoch", "job_id", "epoch", "artifact"}, set()),
            "artifacts": ({"job_id"}, {"name"}),
        }
        if op not in schemas:
            raise ProtocolError("unsupported_version", 404)  # an unknown operation
        data = fields(value, *schemas[op])
        job = self._job(db, data["job_id"], device)
        if op == "job":
            return self._summary(job)
        if op == "cancel":
            state = job["state"]
            if state not in TERMINAL or (state == "interrupted" and not job["confirmed"]):
                state = "interrupted" if state == "interrupted" else "cancelled" if state == "queued" else "cancel_requested"
                db.execute("UPDATE jobs SET state=?,cancel=1 WHERE id=?", (state, job["id"]))
            return {"job_id": job["id"], "state": state}
        if op in {"renew", "reconcile", "upload"}:
            self._fence(data, job, device)
        if op == "renew":
            if job["lease"] is None or job["lease"] <= now or job["state"] in TERMINAL:
                raise ProtocolError("lease_lost", 409)
            db.execute("UPDATE jobs SET lease=? WHERE id=?", (now + LEASE_SECONDS, job["id"]))
            return {"lease_until": stamp(now + LEASE_SECONDS), "state": job["state"],
                    "cancel_requested": bool(job["cancel"])}
        if op == "reconcile":
            state = data["state"]
            stopped, unlaunched = data["execution_stopped"], data["unlaunched"]
            if (not isinstance(state, str) or state not in TERMINAL or type(stopped) is not bool
                    or type(unlaunched) is not bool or (state == "succeeded" and (not stopped or unlaunched))
                    or (state != "interrupted" and not (stopped or unlaunched))):
                raise ProtocolError("invalid_state")
            # A provisional interruption (heartbeat loss, re-registration, revocation) still yields to the
            # journal's true outcome; once a worker confirms any outcome with proof it is final.
            if job["state"] in TERMINAL and (job["state"] != "interrupted" or job["confirmed"]) and state != job["state"]:
                raise ProtocolError("invalid_state")
            db.execute("UPDATE jobs SET state=?,confirmed=max(confirmed,?) WHERE id=?",
                       (state, int(stopped or unlaunched), job["id"]))
            return {"job_id": job["id"], "state": state}
        if op == "events":
            after = integer(data["after_cursor"])
            if (set(data) & {"worker_epoch", "epoch", "events"}) and not {"worker_epoch", "epoch", "events"} <= data.keys():
                raise ProtocolError("invalid_request")
            if "events" in data:
                self._fence(data, job, device)
                events = data["events"]
                if not isinstance(events, list) or len(events) > 200:
                    raise ProtocolError("invalid_request")
                cursor = job["cursor"]
                for raw in events:
                    event = event_from_dict(raw)
                    if event.job_id != job["id"]:
                        raise ProtocolError("invalid_request")
                    payload = json.dumps(event.to_dict(), sort_keys=True)
                    prior = db.execute("SELECT payload FROM events WHERE job=? AND cursor=?", (job["id"], event.cursor)).fetchone()
                    if prior is not None:
                        if prior[0] != payload:
                            raise ProtocolError("cursor_conflict", 409)
                        continue
                    if event.cursor != cursor + 1:
                        raise ProtocolError("cursor_conflict", 409)
                    db.execute("INSERT INTO events VALUES (?,?,?)", (job["id"], event.cursor, payload))
                    cursor = event.cursor
                    # Only state_changed moves the service-side job; other kinds merely record a state.
                    if (event.kind == SafeEventKind.STATE_CHANGED and event.state is not None
                            and event.state.value in {"starting", "running", "cancel_requested"}):
                        order = {"claimed": 0, "starting": 1, "running": 2, "cancel_requested": 3}
                        current = self._job(db, job["id"], device)["state"]
                        if event.state.value == "cancel_requested" and current == "interrupted":
                            # The state is not moved back, but the persistent flag still records the request.
                            db.execute("UPDATE jobs SET cancel=1 WHERE id=? AND confirmed=0", (job["id"],))
                        if current in order and order[event.state.value] >= order[current]:
                            db.execute("UPDATE jobs SET state=?,cancel=max(cancel,?) WHERE id=?",
                                       (event.state.value, int(event.state.value == "cancel_requested"), job["id"]))
                db.execute("UPDATE jobs SET cursor=? WHERE id=?", (cursor, job["id"]))
            rows = db.execute("SELECT payload,cursor FROM events WHERE job=? AND cursor>? ORDER BY cursor LIMIT 200", (job["id"], after)).fetchall()
            return {"events": [json.loads(r[0]) for r in rows], "next_cursor": rows[-1][1] if rows else after}
        if op == "upload":
            if job["state"] != "succeeded":
                raise ProtocolError("invalid_state")
            artifact = Artifact.from_dict(data["artifact"])
            content_hash = digest_bytes(artifact.content)
            prior = db.execute("SELECT hash FROM artifacts WHERE job=? AND name=?", (job["id"], artifact.name)).fetchone()
            if prior is not None:
                if prior[0] != content_hash:
                    raise ProtocolError("artifact_conflict", 409)
            else:
                if db.execute("SELECT count(*) FROM artifacts WHERE job=?", (job["id"],)).fetchone()[0] >= MAX_ARTIFACTS:
                    raise ProtocolError("artifact_limit", 413)
                db.execute("INSERT INTO artifacts VALUES (?,?,?,?)", (job["id"], artifact.name, content_hash, artifact.content))
            return {"name": artifact.name, "size": len(artifact.content), "sha256": content_hash}
        if "name" in data:
            row = db.execute("SELECT name,content FROM artifacts WHERE job=? AND name=?", (job["id"], text(data["name"], 100))).fetchone()
            if row is None:
                raise ProtocolError("not_found", 404)
            return {"artifact": Artifact(row[0], row[1]).to_dict()}
        rows = db.execute("SELECT name,hash,length(content) FROM artifacts WHERE job=? ORDER BY name", (job["id"],))
        return {"artifacts": [{"name": r[0], "sha256": r[1], "size": r[2]} for r in rows]}


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()
