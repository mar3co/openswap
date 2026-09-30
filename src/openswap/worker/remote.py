"""Outbound protocol client. Transport failure never interrupts local execution."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import ssl
import stat
import threading
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from openswap.settings import load_worker_settings
from openswap.worker.journal import AdmissionError, JournalError
from openswap.worker.models import JobState, SafeEventKind
from openswap.worker.protocol import (
    Artifact, Claim, HEARTBEAT_SECONDS, MAX_ARTIFACT, MAX_BODY, ProtocolError,
    TERMINAL, integer, timestamp, validate_url,
)

STATES = frozenset(state.value for state in JobState)
# Validation failures the service (or the local export check) reports for one
# artifact. They never clear on retry, so they end that artifact, not the claim.
ARTIFACT_REJECTIONS = frozenset({"artifact_too_large", "artifact_limit", "artifact_conflict",
                                 "hash_mismatch", "invalid_request"})


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ProtocolError("redirect_refused")


class Transport:
    def __init__(self, url: str, key: str | None = None):
        self.url = validate_url(url)
        self.key = key
        handlers = [ProxyHandler({}), NoRedirect()]
        if self.url.startswith("https:"):
            import truststore
            handlers.append(HTTPSHandler(context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)))
        self.opener = build_opener(*handlers)

    def request(self, operation: str, data: dict) -> dict:
        encoded = json.dumps(data, allow_nan=False).encode()
        if len(encoded) > MAX_BODY:
            raise ProtocolError("body_too_large", 413)
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        request = Request(f"{self.url}/v1/{operation}", encoded, headers, method="POST")
        try:
            with self.opener.open(request, timeout=5) as response:
                result = self._read(response)
        except HTTPError as exc:
            with exc:
                result = self._read(exc)
            code = result.get("error", "service_unavailable")
            # Error bodies are untrusted: never expose arbitrary service text.
            allowed = {"revoked", "unauthorized", "device_expired", "lease_lost", "stale_epoch", "offline_worker",
                       "invalid_code", "invalid_request", "forbidden", "not_found", "invalid_state",
                       *ARTIFACT_REJECTIONS}
            raise ProtocolError(code if code in allowed else "service_unavailable", exc.code) from None
        except (URLError, TimeoutError, OSError):
            raise ProtocolError("service_unavailable", 503) from None
        return result

    @staticmethod
    def _read(response):
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ProtocolError("body_too_large", 413)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("invalid_response") from None


class RemoteJournal:
    """Claim receipt and upload acknowledgements survive response loss/restart."""
    def __init__(self, runtime, url, worker_id):
        runtime.store._ensure_private_dir()
        self.path = runtime.store.state_dir / "remote.sqlite3"
        # Bindings are scoped to one enrollment at one service. The worker ID
        # is per enrollment and not secret, so nothing key-derived is on disk.
        self.service = hashlib.sha256((url + "\0" + worker_id).encode()).hexdigest()
        if self.path.is_symlink():
            raise ValueError("unsafe remote journal")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS bindings (service TEXT, remote_id TEXT, claim TEXT NOT NULL, "
                       "local_id TEXT, cursor INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0, "
                       "PRIMARY KEY(service,remote_id))")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def remember(self, claim):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO bindings(service,remote_id,claim) VALUES (?,?,?)",
                       (self.service, claim.job_id, json.dumps(claim.to_dict())))

    def pending(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM bindings WHERE service=? AND done=0 ORDER BY rowid", (self.service,)).fetchall()

    def binding(self, local_id):
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM bindings WHERE service=? AND local_id=?", (self.service, local_id)).fetchone()

    def update(self, remote_id, **changes):
        if not changes or changes.keys() - {"local_id", "cursor", "done"}:
            raise ValueError("invalid journal update")
        with closing(self.connect()) as db, db:
            names = list(changes)
            db.execute("UPDATE bindings SET " + ",".join(name + "=?" for name in names)
                       + " WHERE service=? AND remote_id=?", (*[changes[n] for n in names], self.service, remote_id))


class RemoteClient:
    """One daemon thread for heartbeats, admission and durable upload replay.

    The existing runtime remains the only launch driver. Its final launch fence
    queries this client; a network failure prevents that launch, never kills a
    job that has already started. Tests inject transport/adapters, not binaries.
    """
    def __init__(self, runtime, url: str, key: str, *, worker_id: str = "", transport=None,
                 artifact_names=("result.md",)):
        self.runtime, self.url = runtime, validate_url(url)
        self.transport = transport or Transport(self.url, key)
        self.journal = RemoteJournal(runtime, self.url, worker_id)
        self.stop_event = threading.Event()
        self.worker_epoch = None
        self.state = "offline"
        self.last_seen_at = None
        self.artifact_names = tuple(artifact_names)
        if len(self.artifact_names) > 8:
            raise ValueError("too many explicit artifacts")
        for name in self.artifact_names:
            Artifact.from_dict(Artifact(name, b"").to_dict())
        self.runtime.remote_launch_guard = self.launch_allowed

    def _fence(self, claim):
        return {"job_id": claim.job_id, "epoch": claim.epoch, "worker_epoch": self.worker_epoch}

    def _idempotency_key(self, claim):
        # Fixed length whatever the remote ID's size (IDs may be 200 characters).
        return "remote:" + hashlib.sha256((self.journal.service + "\0" + claim.job_id).encode()).hexdigest()

    def _worker_request(self, operation):
        """``heartbeat``/``poll``: a stale worker epoch means another registration
        superseded this one, so re-register on the next tick instead of reporting online."""
        try:
            return self.transport.request(operation, {"worker_epoch": self.worker_epoch})
        except ProtocolError as exc:
            if exc.code == "stale_epoch":
                self.worker_epoch = None
            raise

    def _job_response(self, operation, data):
        """``job``/``renew`` with a real boolean cancel flag and a known state.

        Anything else is a malformed response, handled like a transport failure:
        it never cancels or launches local work.
        """
        remote = self.transport.request(operation, data)
        if type(remote.get("cancel_requested")) is not bool or remote.get("state") not in STATES:
            raise ProtocolError("invalid_response")
        return remote

    def launch_allowed(self, local_id: str) -> bool:
        # No client lock is held during runtime control calls: the launch lock
        # and the independent heartbeat driver cannot deadlock each other.
        local = self.runtime.get(local_id)
        if not local.idempotency_key.startswith("remote:"):
            return True
        binding = self.journal.binding(local_id)
        policy = load_worker_settings(self.runtime.backup_root)
        if (binding is None or binding["done"] or self.state != "online" or not self.worker_epoch
                or policy.control_service_url != self.url or not policy.enabled or policy.paused):
            return False
        claim = Claim.from_dict(json.loads(binding["claim"]))
        if min(local.expires_at, claim.submission.job.expires_at) <= datetime.now(timezone.utc):
            return False
        try:
            remote = self._job_response("renew", self._fence(claim))
            return remote["state"] not in TERMINAL and remote["state"] != "cancel_requested" and not remote["cancel_requested"]
        except (ProtocolError, KeyError, ValueError, TypeError):
            return False

    def tick(self):
        """One bounded transport pass. No sleeping; tests control readiness/time."""
        if self.stop_event.is_set():
            return
        policy = load_worker_settings(self.runtime.backup_root)
        if policy.control_service_url != self.url:
            self.state = "disabled"
            return
        if self.state in {"revoked", "expired"}:
            return  # only a new enrollment (re-pair) can restore access
        try:
            if self.worker_epoch is None:
                registered = self.transport.request("register", {})
                self.worker_epoch = integer(registered["worker_epoch"], 1)
            heartbeat = self._worker_request("heartbeat")
            self.last_seen_at = timestamp(heartbeat["last_seen_at"])
            self.state = "online"
            pending = self.journal.pending()
            for binding in pending:
                self._sync(binding)
            # Pending work, including an expired remote lease on a running job,
            # blocks new claims until its outcome/events/artifacts are acknowledged.
            if self.stop_event.is_set() or self.journal.pending() or not policy.enabled or policy.paused:
                return
            if self.runtime.store.active() is not None or self.runtime.store.queue():
                return
            lease = self.runtime.leases.read_current()
            if lease is not None and lease.state != "released":
                return
            try:
                availability = self.runtime.adapter.probe()
            except Exception:
                return  # an unavailable provider claims nothing; the heartbeat still counts
            if not availability.available or self.runtime.account_identity is None:
                return
            response = self._worker_request("poll")
            if response["claim"] is not None:
                claim = Claim.from_dict(response["claim"])
                self.journal.remember(claim)  # persist before local admission
                self._sync(self.journal.pending()[0])
        except ProtocolError as exc:
            self.state = ("revoked" if exc.code in {"revoked", "unauthorized"}
                          else "expired" if exc.code == "device_expired"
                          else "online" if exc.code == "lease_lost" else "offline")
        except (KeyError, TypeError, ValueError, OSError, sqlite3.Error, AdmissionError):
            self.state = "offline"

    def _sync(self, binding):
        claim = Claim.from_dict(json.loads(binding["claim"]))
        remote = self._job_response("job", {"job_id": claim.job_id})
        local_id = binding["local_id"]
        local = None
        if local_id:
            try:
                local = self.runtime.get(local_id)
            except KeyError:
                pass  # the ID was reserved but admission never completed
        if local is None:
            # Deterministic identity recovers a mapping from before the ID was
            # reserved ahead of admission, without a second launch.
            idem = self._idempotency_key(claim)
            local = self.runtime.store.get_by_idempotency_key(idem)
            if local is None:
                if remote["state"] in TERMINAL or claim.submission.job.expires_at <= datetime.now(timezone.utc) or remote["cancel_requested"]:
                    # Nothing was ever admitted locally, so nothing launched.
                    state = "interrupted" if remote["state"] == "interrupted" else "cancelled" if remote["cancel_requested"] else "expired"
                    self.transport.request("reconcile", {**self._fence(claim), "state": state,
                                                         "execution_stopped": False, "unlaunched": True})
                    self.journal.update(claim.job_id, done=1)
                    return
                self._job_response("renew", self._fence(claim))
                if self.stop_event.is_set():
                    return
                # The binding is published before admission: the runtime's
                # launch fence may query it the moment the job is queued.
                local_id = local_id or uuid4().hex
                self.journal.update(claim.job_id, local_id=local_id)
                local = self.runtime.submit(replace(claim.submission.job, idempotency_key=idem), job_id=local_id)
            if local.job_id != local_id:
                local_id = local.job_id
                self.journal.update(claim.job_id, local_id=local_id)
        if remote["cancel_requested"] and local.state.value not in TERMINAL:
            self.runtime.cancel(local_id)
            local = self.runtime.get(local_id)
        # An old queued row recovered after registration must not be launched
        # against a server outcome of interrupted/expired/cancelled.
        if local.state.value == "queued" and remote["state"] in TERMINAL:
            self.runtime.cancel(local_id)
            local = self.runtime.get(local_id)
        if local.state.value not in TERMINAL:
            self._job_response("renew", self._fence(claim))
        self._forward_events(claim, local_id, binding["cursor"])
        local = self.runtime.get(local_id)
        if local.state.value in TERMINAL:
            stopped, unlaunched = self._proof(local)
            state = local.state.value if stopped or unlaunched else "interrupted"
            self.transport.request("reconcile", {**self._fence(claim), "state": state,
                                                 "execution_stopped": stopped, "unlaunched": unlaunched})
            if state == "succeeded" and self.upload_results(claim, local):
                # Relay the rejection diagnostics before acknowledging the claim.
                self._forward_events(claim, local_id, self.journal.binding(local_id)["cursor"])
            self.journal.update(claim.job_id, done=1)

    def _forward_events(self, claim, local_id, cursor):
        # One page per tick keeps requests bounded; terminal jobs are not
        # acknowledged until every event page is durable at the service.
        page = self.runtime.events(local_id, after_cursor=cursor, limit=200)
        while page.events:
            if self.stop_event.is_set():
                raise ProtocolError("service_unavailable", 503)
            self._worker_request("heartbeat")
            events = [replace(event, job_id=claim.job_id).to_dict() for event in page.events]
            response = self.transport.request("events", {**self._fence(claim), "after_cursor": cursor, "events": events})
            ack = integer(response["next_cursor"])
            if ack != page.next_cursor:
                raise ProtocolError("invalid_response")
            cursor = ack
            self.journal.update(claim.job_id, cursor=cursor)
            if self.runtime.get(local_id).state.value not in TERMINAL:
                break
            page = self.runtime.events(local_id, after_cursor=cursor, limit=200)

    def _proof(self, local):
        if local.state.value == "interrupted":
            return False, False
        if local.state.value == "failed" and local.diagnostic_code == "lease_conflict":
            return False, True  # no account lease was ever acquired: nothing launched
        cursor, launched, stopped = 0, False, False
        while True:
            page = self.runtime.events(local.job_id, after_cursor=cursor, limit=200)
            if not page.events:
                break
            launched |= any(e.state and e.state.value == "running" for e in page.events)
            stopped |= any(e.execution_stopped is True for e in page.events)
            cursor = page.next_cursor
        lease = self.runtime.leases.read_current()
        if lease is not None and lease.job_id == local.job_id and lease.state == "released":
            return stopped or lease.reason == "confirmed_stopped", lease.reason == "unlaunched"
        return stopped, not launched and local.pinned_account_ref is None

    def upload_results(self, claim, local) -> bool:
        """Upload the explicit artifact list; returns whether any artifact was refused.

        A refused artifact (oversized, symlinked, replaced mid-read, over the
        per-job limit, or conflicting with a stored copy) would be refused on
        every retry, so it is skipped and journaled as ``artifact_rejected``
        rather than holding the claim, and every other claim, pending forever.
        Transport, lease and authorization failures still propagate for retry.
        """
        rejected = False
        for name in self.artifact_names:  # explicit list; never discover files
            if self.stop_event.is_set():
                raise ProtocolError("service_unavailable", 503)
            self._worker_request("heartbeat")
            try:
                self._upload_artifact(claim, local, name)
            except ProtocolError as exc:
                if exc.code not in ARTIFACT_REJECTIONS:
                    raise
                rejected = True
                try:
                    self.runtime.store.append_event(
                        local.job_id, kind=SafeEventKind.DIAGNOSTIC, diagnostic_code="artifact_rejected",
                        worker_epoch=self.runtime.worker_epoch, expected_generation=local.generation)
                except JournalError:
                    pass  # a job from an earlier worker epoch cannot take new events
        return rejected

    def _upload_artifact(self, claim, local, name):
        policy = load_worker_settings(self.runtime.backup_root)
        workspace = next((w for w in policy.workspaces if w.workspace_id == local.workspace_id), None)
        if workspace is None:
            raise ProtocolError("invalid_request")
        directory = workspace.output_root / local.job_id
        if directory.is_symlink() or directory.resolve() != directory:
            raise ProtocolError("invalid_request")
        path = directory / name
        try:
            before = path.lstat()
        except FileNotFoundError:
            return  # an absent optional result is not an error
        if not stat.S_ISREG(before.st_mode):
            raise ProtocolError("invalid_request")
        if before.st_size > MAX_ARTIFACT:
            raise ProtocolError("artifact_too_large", 413)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            current = os.fstat(stream.fileno())
            if (current.st_ino, current.st_dev) != (before.st_ino, before.st_dev):
                raise ProtocolError("invalid_request")
            content = stream.read(MAX_ARTIFACT + 1)
        if len(content) > MAX_ARTIFACT:
            raise ProtocolError("artifact_too_large", 413)
        self.transport.request("upload", {**self._fence(claim), "artifact": Artifact(name, content).to_dict()})

    def run(self, stop_event: threading.Event):
        self.stop_event = stop_event
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # A journal or adapter fault must not end heartbeating for the
                # process lifetime; the next tick retries at the usual cadence.
                self.state = "offline"
            stop_event.wait(HEARTBEAT_SECONDS)
