"""Outbound protocol client. Transport failure never interrupts local execution."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import ssl
import stat
import threading
import time
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
# Reserved by the protocol: v1 has no approval/resume operation, so it fails closed.
APPROVAL = "waiting_for_approval"
# Validation failures the service (or the local export check) reports for one
# artifact. They never clear on retry, so they end that artifact, not the claim.
ARTIFACT_REJECTIONS = frozenset({"artifact_too_large", "artifact_limit", "artifact_conflict",
                                 "hash_mismatch", "invalid_request"})


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _nonfinite(_):
    raise ValueError("non-finite number")


def _open_artifact(directory: Path, name: str):
    """``(lstat, fd)`` for a regular file ``name`` directly in ``directory``, or ``None``.

    Where the platform supports it, everything is anchored on one descriptor for
    the checked directory, so a provider that swaps the directory for a symlink
    afterwards cannot redirect the read. Windows (no ``dir_fd``) keeps the path
    checks and the inode comparison the caller makes after opening.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd:
        path = directory / name
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise ProtocolError("invalid_request")
        if before.st_size > MAX_ARTIFACT:
            raise ProtocolError("artifact_too_large", 413)
        return before, os.open(path, os.O_RDONLY | nofollow)
    try:
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow)
    except FileNotFoundError:
        return None
    except OSError:
        raise ProtocolError("invalid_request") from None
    try:
        if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
            raise ProtocolError("invalid_request")
        try:
            before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise ProtocolError("invalid_request")
        if before.st_size > MAX_ARTIFACT:
            raise ProtocolError("artifact_too_large", 413)
        try:
            return before, os.open(name, os.O_RDONLY | nofollow, dir_fd=dir_fd)
        except OSError:
            raise ProtocolError("invalid_request") from None
    finally:
        os.close(dir_fd)


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
            # Duplicate keys are refused, never resolved last-wins, before any control decision.
            value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("invalid_response") from None


class RemoteJournal:
    """Claim receipt and upload acknowledgements survive response loss/restart."""
    def __init__(self, runtime, url, key):
        runtime.store._ensure_private_dir()
        self.path = runtime.store.state_dir / "remote.sqlite3"
        # Namespaced by enrollment, not just URL: a replacement device paired against the
        # same service must never inherit bindings the revoked or expired one can no longer read.
        enrollment = hashlib.sha256(key.encode()).hexdigest()
        self.service = hashlib.sha256((url + "\0" + enrollment).encode()).hexdigest()
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
    """Heartbeats on one thread; admission and durable upload replay on another.

    The existing runtime remains the only launch driver. Its final launch fence
    queries this client; a network failure prevents that launch, never kills a
    job that has already started. Tests inject transport/adapters, not binaries.

    ``run`` owns both threads. ``tick`` is the synchronous composition of the two
    halves (``heartbeat_tick`` then ``sync_tick``) for callers that drive the
    client themselves. Connectivity, the worker epoch and the admitted claim are
    the only state shared across threads; the Transport is shared too, which is
    safe because urllib opens one connection per request and pools none.
    """
    def __init__(self, runtime, url: str, key: str, *, transport=None, artifact_names=("result.md",)):
        self.runtime, self.url = runtime, validate_url(url)
        self.transport = transport or Transport(self.url, key)
        self.journal = RemoteJournal(runtime, self.url, key)
        self.stop_event = threading.Event()
        self.worker_epoch = None
        self.state = "offline"
        self.last_seen_at = None
        self._skew = timedelta(0)  # service clock minus local clock, from the last heartbeat
        self._lock = threading.Lock()
        self._admitted = None  # claim whose lease the heartbeat thread keeps renewing
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
                with self._lock:
                    self.worker_epoch = None
            raise

    def _connectivity(self, failure=None):
        """Fold one request outcome from either thread into the shared connectivity state.

        Any failure reports ``offline`` (or ``revoked``); only a heartbeat success reports
        ``online``. Revocation is final for this client, so nothing overrides it.
        """
        with self._lock:
            if self.state == "revoked":
                return
            if failure is None:
                self.state = "online"
            elif isinstance(failure, ProtocolError):
                self.state = ("revoked" if failure.code in {"revoked", "unauthorized", "device_expired"}
                              else "online" if failure.code == "lease_lost" else "offline")
            else:
                self.state = "offline"

    def _server_now(self):
        """Deadlines are the service's: expiry is judged on its clock, not the Mac's."""
        return datetime.now(timezone.utc) + self._skew

    def _job_response(self, operation, claim):
        """``job``/``renew`` for ``claim`` with a real boolean cancel flag and a known state.

        A ``job`` response must also describe that claim (ID and epoch) with a valid
        cursor and expiry; ``renew`` must carry a valid lease. Anything else is a
        malformed response, handled like a transport failure: it never cancels or
        launches local work.
        """
        data = {"job_id": claim.job_id} if operation == "job" else self._fence(claim)
        remote = self.transport.request(operation, data)
        try:
            if type(remote.get("cancel_requested")) is not bool or remote.get("state") not in STATES:
                raise ProtocolError("invalid_response")
            if operation == "job":
                integer(remote["event_cursor"])
                timestamp(remote["expires_at"])
                if remote["job_id"] != claim.job_id or integer(remote["epoch"], 1) != claim.epoch:
                    raise ProtocolError("invalid_response")
            else:
                timestamp(remote["lease_until"])
        except (KeyError, ProtocolError):
            raise ProtocolError("invalid_response") from None
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
        if local.expires_at <= datetime.now(timezone.utc) or claim.submission.job.expires_at <= self._server_now():
            return False
        try:
            remote = self._job_response("renew", claim)
            return (remote["state"] not in TERMINAL and remote["state"] not in {"cancel_requested", APPROVAL}
                    and not remote["cancel_requested"])
        except (ProtocolError, KeyError, ValueError, TypeError):
            return False

    def tick(self):
        """One bounded synchronous pass: heartbeat, then synchronization. No sleeping; tests control time."""
        if self.stop_event.is_set():
            return
        policy = load_worker_settings(self.runtime.backup_root)
        if self.heartbeat_tick(policy):
            self.sync_tick(policy)

    def heartbeat_tick(self, policy=None) -> bool:
        """The liveness half of a tick: register when needed, then heartbeat.

        Returns whether the worker is online. Bounded by two request timeouts and
        never waits on synchronization work, so a slow pass cannot cost liveness.
        """
        if self.stop_event.is_set():
            return False
        policy = policy or load_worker_settings(self.runtime.backup_root)
        if policy.control_service_url != self.url:
            with self._lock:
                self.state = "disabled"
            return False
        if self.state == "revoked":
            return False
        try:
            if self.worker_epoch is None:
                registered = self.transport.request("register", {})
                self.worker_epoch = integer(registered["worker_epoch"], 1)
            sent = datetime.now(timezone.utc)
            heartbeat = self._worker_request("heartbeat")
            seen = timestamp(heartbeat["last_seen_at"])
            with self._lock:
                self.last_seen_at = seen
                # Measured against the send time, the offset can only overstate the
                # service clock by the round trip: deadlines err early, never late.
                self._skew = seen - sent
            self._connectivity()
            return self.state == "online"
        except (ProtocolError, KeyError, TypeError, ValueError, OSError, sqlite3.Error) as exc:
            self._connectivity(exc)
            return False

    def renew_admitted(self):
        """Renew the admitted claim's lease from the renewal thread.

        The lease governs admission only, and a lost lease cannot be revived, so a
        pass blocked in event pages or artifact uploads must not let a claim that
        is waiting for its launch fence expire. ``lease_lost`` ends the renewals;
        the synchronization thread still reconciles that job's outcome.
        """
        claim = self._admitted
        if claim is None or self.worker_epoch is None:
            return
        try:
            self._job_response("renew", claim)
        except ProtocolError as exc:
            if exc.code != "lease_lost":
                raise
            if self._admitted is claim:
                self._admitted = None

    def sync_tick(self, policy=None):
        """The synchronization half of a tick: replay pending bindings, then claim new work.

        Runs only while the heartbeat half reports ``online`` with a known epoch.
        """
        if self.stop_event.is_set():
            return
        policy = policy or load_worker_settings(self.runtime.backup_root)
        with self._lock:
            if self.state != "online" or self.worker_epoch is None or policy.control_service_url != self.url:
                return
        try:
            for binding in self.journal.pending():
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
        except (ProtocolError, KeyError, TypeError, ValueError, OSError, sqlite3.Error, AdmissionError) as exc:
            self._connectivity(exc)

    def _sync(self, binding):
        claim = Claim.from_dict(json.loads(binding["claim"]))
        remote = self._job_response("job", claim)
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
                if (remote["state"] in TERMINAL or remote["state"] == APPROVAL or remote["cancel_requested"]
                        or claim.submission.job.expires_at <= self._server_now()):
                    # Nothing was ever admitted locally, so nothing launched.
                    state = ("interrupted" if remote["state"] == "interrupted" else "cancelled" if remote["cancel_requested"]
                             else "failed" if remote["state"] == APPROVAL else "expired")
                    self.transport.request("reconcile", {**self._fence(claim), "state": state,
                                                         "execution_stopped": False, "unlaunched": True})
                    self.journal.update(claim.job_id, done=1)
                    return
                self._job_response("renew", claim)
                self._admitted = claim
                if self.stop_event.is_set():
                    return
                # The binding is published before admission: the runtime's
                # launch fence may query it the moment the job is queued.
                local_id = local_id or uuid4().hex
                self.journal.update(claim.job_id, local_id=local_id)
                # The local runtime judges expiry on the Mac's clock: hand it the service
                # deadline translated into local time.
                job = claim.submission.job
                local = self.runtime.submit(replace(job, idempotency_key=idem, expires_at=job.expires_at - self._skew),
                                            job_id=local_id)
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
            self._job_response("renew", claim)
            self._admitted = claim
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
            self._admitted = None
            if state == "interrupted" and not (stopped or unlaunched) and self._proof_pending(local):
                return  # provisional: stay pending so later stop proof is still reconciled
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

    def _proof_pending(self, local):
        """Whether stop proof for an uncertain run may still arrive: its account lease is
        still held or quarantined. That lease also blocks new claims, so a retained
        provisional binding never holds back work that could otherwise run."""
        lease = self.runtime.leases.read_current()
        return lease is not None and lease.job_id == local.job_id and lease.state != "released"

    def _proof(self, local):
        if local.state.value == "interrupted":
            # Only the lease can later establish what happened to an uncertain run.
            lease = self.runtime.leases.read_current()
            if lease is not None and lease.job_id == local.job_id and lease.state == "released":
                return lease.reason == "confirmed_stopped", lease.reason == "unlaunched"
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
        opened = _open_artifact(directory, name)
        if opened is None:
            return  # an absent optional result is not an error
        before, fd = opened
        with os.fdopen(fd, "rb") as stream:
            current = os.fstat(stream.fileno())
            if (current.st_ino, current.st_dev) != (before.st_ino, before.st_dev):
                raise ProtocolError("invalid_request")
            content = stream.read(MAX_ARTIFACT + 1)
        if len(content) > MAX_ARTIFACT:
            raise ProtocolError("artifact_too_large", 413)
        self.transport.request("upload", {**self._fence(claim), "artifact": Artifact(name, content).to_dict()})

    def run(self, stop_event: threading.Event):
        """Heartbeat on this thread on an absolute schedule; renew and synchronize on two others.

        Every request is bounded by a 5 s timeout but a synchronization pass is not
        (probe, event pages, artifact uploads), and the service interrupts a live job
        after 15 s without a heartbeat. Heartbeats are due every ``HEARTBEAT_SECONDS``
        from the previous due time, not from when the last one returned, and nothing
        else shares their thread, so consecutive heartbeats reach the service at most
        one period plus one request timeout apart. All threads stop on ``stop_event``
        and are joined before returning.
        """
        self.stop_event = stop_event
        helpers = [threading.Thread(target=self._sync_loop, name="openswap-worker-remote-sync", daemon=True),
                   threading.Thread(target=self._renew_loop, name="openswap-worker-remote-renew", daemon=True)]
        for helper in helpers:
            helper.start()
        try:
            due = time.monotonic()
            while not stop_event.is_set():
                try:
                    self.heartbeat_tick()
                except Exception as exc:
                    self._connectivity(exc)
                due = max(due + HEARTBEAT_SECONDS, time.monotonic())
                stop_event.wait(due - time.monotonic())
        finally:
            for helper in helpers:
                helper.join()

    def _renew_loop(self):
        while not self.stop_event.is_set():
            try:
                if self.state == "online":
                    self.renew_admitted()
            except Exception as exc:
                self._connectivity(exc)
            self.stop_event.wait(HEARTBEAT_SECONDS)

    def _sync_loop(self):
        while not self.stop_event.is_set():
            try:
                self.sync_tick()
            except Exception as exc:
                # A journal or adapter fault must not end synchronization for the
                # process lifetime; the next pass retries at the usual cadence.
                self._connectivity(exc)
            self.stop_event.wait(HEARTBEAT_SECONDS)
