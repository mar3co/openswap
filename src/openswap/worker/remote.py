"""Outbound protocol client. Transport failure never interrupts local execution."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import logging
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
from openswap.worker.attention import log_limited
from openswap.worker.journal import AdmissionError, JournalError
from openswap.worker.models import JobState, RemoteAuthorization, SafeEventKind
from openswap.worker.protocol import (
    AdvertisedAccount, Artifact, Claim, HEARTBEAT_SECONDS, MAX_ARTIFACT, MAX_BODY, MAX_REPORTED_FOLDERS,
    ProtocolError, ROTATE_BEFORE_SECONDS,
    ReportedFolder, TERMINAL, capability_features, fields, integer, text, timestamp, validate_url,
)

STATES = frozenset(state.value for state in JobState)
# The protocol caps heartbeat cancel_job_ids; a longer list is a malformed response.
logger = logging.getLogger(__name__)
MAX_CANCEL_IDS = 100
# Reserved by the protocol: v1 has no approval/resume operation, so it fails closed.
APPROVAL = "waiting_for_approval"
# Local diagnostics the protocol does not list, and the listed code each is
# sent as: the service's diagnostic list is closed. `worker status` and the
# local journal keep the specific code.
WIRE_DIAGNOSTICS = {"workspace_refused": "provider_unavailable", "origin_refused": "provider_unavailable"}
# What this worker advertises with `capabilities` after each registration. The
# set never changes at runtime in this release.
WORKER_FEATURES = ("origin", "key_rotation", "attention")
# After a failed rotation (lost response, refused, Keychain failure) the next
# attempt waits this long; the old key keeps working meanwhile.
ROTATION_BACKOFF_SECONDS = 3600.0
# How often the readiness report rescans folders of repos when nothing changed.
FOLDER_RESCAN_SECONDS = 30.0
# The session attention thread reads the session files this often (a "No" or
# Esc at the Mac fires no hook); it retries a failed report every
# HEARTBEAT_SECONDS, and reports a change at once.
ATTENTION_WAKE_SECONDS = 0.5
# A service that refuses session notices as forbidden is asked again this rarely.
ATTENTION_FORBIDDEN_SECONDS = 900.0
# Hooks go into config homes created since the worker started this often.
ATTENTION_INSTALL_SECONDS = 30.0
# A report the service refuses as too large shrinks, but never below this.
ATTENTION_MIN_BUDGET = 8192
# Once the service has taken this many reports since, a shrunk budget is whole again.
ATTENTION_BUDGET_RECOVERY = 5
# Validation failures the service (or the local export check) reports for one
# artifact. They never clear on retry, so they end that artifact, not the claim.
ARTIFACT_REJECTIONS = frozenset({"artifact_too_large", "artifact_limit", "artifact_conflict",
                                 "hash_mismatch", "invalid_request"})
# Every documented service error code. Error bodies are untrusted: any other
# text is reported as ``service_unavailable`` rather than surfaced.
SERVICE_ERRORS = frozenset({"revoked", "unauthorized", "device_expired", "lease_lost", "stale_epoch", "offline_worker",
                            "invalid_code", "invalid_request", "forbidden", "not_found", "invalid_state",
                            "unsupported_version", "idempotency_conflict", "cursor_conflict", "queue_full",
                            "body_too_large", "service_unavailable", "rotation_too_soon", *ARTIFACT_REJECTIONS})
# Connectivity states that only a new enrollment (re-pair) can leave.
FINAL = frozenset({"revoked", "expired"})


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
        self._key = key
        # A key rotation swaps the key while other threads send requests: each
        # request reads it once, so it carries one key or the other in full.
        self._key_lock = threading.Lock()
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
        key = self.key
        if key:
            headers["Authorization"] = f"Bearer {key}"
        request = Request(f"{self.url}/v1/{operation}", encoded, headers, method="POST")
        try:
            with self.opener.open(request, timeout=5) as response:
                result = self._read(response)
        except HTTPError as exc:
            with exc:
                try:
                    result = self._read(exc)
                except ProtocolError:
                    result = {}  # not the service's own answer (a proxy's page): its status still counts
            code = result.get("error", "service_unavailable")
            raise ProtocolError(code if code in SERVICE_ERRORS else "service_unavailable", exc.code) from None
        except (URLError, TimeoutError, OSError, http.client.HTTPException):
            raise ProtocolError("service_unavailable", 503) from None
        return result

    @property
    def key(self) -> str | None:
        with self._key_lock:
            return self._key

    @key.setter
    def key(self, value: str | None) -> None:
        with self._key_lock:
            self._key = value

    def set_key(self, value: str) -> None:
        """Swap the key in place (a rotation); requests already sent keep the old one."""
        self.key = value

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
    def __init__(self, runtime, url, enrollment):
        runtime.store._ensure_private_dir()
        self.path = runtime.store.state_dir / "remote.sqlite3"
        # Bindings are scoped to one enrollment at one service: a replacement device
        # paired against the same URL never inherits bindings the old one can no longer read.
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
            # Account choice extension: whether this enrollment ever advertised a
            # non-empty account set. Claims may carry account_ref only after that.
            db.execute("CREATE TABLE IF NOT EXISTS account_choice (service TEXT PRIMARY KEY, "
                       "advertised INTEGER NOT NULL DEFAULT 0)")
            # Capabilities extension: the registration (worker epoch) whose
            # `capabilities` the service last acknowledged, and the features it
            # accepted. Claims may carry `origin` only while that is the
            # current registration and "origin" was accepted.
            db.execute("CREATE TABLE IF NOT EXISTS capabilities (service TEXT PRIMARY KEY, "
                       "worker_epoch INTEGER NOT NULL, features TEXT NOT NULL)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def accounts_advertised(self) -> bool:
        with closing(self.connect()) as db:
            row = db.execute("SELECT advertised FROM account_choice WHERE service=?", (self.service,)).fetchone()
        return bool(row and row["advertised"])

    def mark_accounts_advertised(self):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO account_choice(service,advertised) VALUES (?,1) "
                       "ON CONFLICT(service) DO UPDATE SET advertised=1", (self.service,))

    def capabilities(self) -> tuple[int, tuple[str, ...]] | None:
        """``(worker_epoch, accepted features)`` last acknowledged for this enrollment, or None."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT worker_epoch,features FROM capabilities WHERE service=?",
                             (self.service,)).fetchone()
        if row is None:
            return None
        try:
            return int(row["worker_epoch"]), capability_features(json.loads(row["features"]))
        except (ValueError, TypeError, ProtocolError):
            return None

    def record_capabilities(self, worker_epoch: int, features) -> None:
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO capabilities(service,worker_epoch,features) VALUES (?,?,?) "
                       "ON CONFLICT(service) DO UPDATE SET worker_epoch=excluded.worker_epoch, "
                       "features=excluded.features", (self.service, worker_epoch, json.dumps(list(features))))

    def remember(self, claim):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO bindings(service,remote_id,claim) VALUES (?,?,?)",
                       (self.service, claim.job_id, json.dumps(claim.to_dict())))

    def pending(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM bindings WHERE service=? AND done=0 ORDER BY rowid", (self.service,)).fetchall()

    def by_remote(self, remote_id):
        """The binding for a remote ID whether or not it is done: cancellation must
        reach a local run for as long as it executes."""
        with closing(self.connect()) as db:
            return db.execute("SELECT * FROM bindings WHERE service=? AND remote_id=?", (self.service, remote_id)).fetchone()

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


def offerable(backup_root, workspace, workspaces) -> bool:
    """Whether to advertise ``workspace``: the worker would not refuse its jobs at launch.

    A workspace that breaks the folder rules (see ``cli.workspace_refusal``)
    is left out of the readiness report, so the service stops offering a
    folder every job would fail in; once it is fixed the report changes and
    is sent again. A check that fails leaves out only that workspace.
    """
    try:
        from openswap.worker.cli import workspace_refusal

        return workspace_refusal(backup_root, workspace, workspaces) is None
    except Exception:
        return False


def run_attention(runtime, stop_event: threading.Event, current) -> None:
    """The session attention thread: keep ``runtime.attention`` current and
    report it through ``current()``, the client to report through now (None
    while there is none; a configured worker replaces its client on a re-pair).
    """
    from openswap.settings import load_attention_settings

    store = runtime.attention
    next_pass = next_install = 0.0
    pending = False
    while not stop_event.is_set():
        # Cleared only once seen set: a notice landing after a timed-out
        # wait wakes the next one at once.
        if store.changed.wait(ATTENTION_WAKE_SECONDS):
            store.changed.clear()
            pending = True
        try:
            client = current()
            store.refresh()  # session files and Codex's log: answers and exits that fire no hook
            store.tick()
            if client is not None:
                store.apply_replies(client._server_now().timestamp())
            now = time.monotonic()
            if now >= next_pass:
                next_pass = now + HEARTBEAT_SECONDS
                switches = load_attention_settings(runtime.backup_root)
                if switches.readable and not switches.notices:
                    store.clear()  # even with no service to tell
                pending = True
            if now >= next_install:
                next_install = now + ATTENTION_INSTALL_SECONDS
                store.install()  # config homes and profiles added while the worker runs
            if store.changed.is_set():
                store.changed.clear()
                pending = True
            if pending:
                # Asked again: an unpair or re-pair during this pass changes it.
                client = current()
                # Kept until a client could report it: none yet at start or during a
                # re-pair, or one not yet registered or offline.
                if client is not None and client.sync_attention():
                    pending = False
        except Exception as exc:  # noqa: BLE001 - the next pass tries again
            log_limited("attention_loop", "Session notices pass failed: %s", exc)
            continue


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
    def __init__(self, runtime, url: str, key: str, *, worker_id: str = "", transport=None,
                 artifact_names=("result.md",)):
        self.runtime, self.url, self.worker_id = runtime, validate_url(url), worker_id
        self.transport = transport or Transport(self.url, key)
        # Keyed by the stable worker ID, so a key renewal (same worker) keeps its bindings
        # while a replacement enrollment (new worker) starts clean. Without an ID, a digest
        # of the key stands in.
        self.journal = RemoteJournal(runtime, self.url, worker_id or hashlib.sha256(key.encode()).hexdigest())
        self.stop_event = threading.Event()
        self.worker_epoch = None
        self.state = "offline"
        self.last_seen_at = None
        self._skew = timedelta(0)  # service clock minus local clock, from the last heartbeat
        self._lock = threading.Lock()
        self._admitted = None  # claim whose lease the renewal thread keeps renewing
        self._cancels = set()  # remote IDs the service asked to cancel, enforced off the heartbeat thread
        self.artifact_names = tuple(artifact_names)
        if len(self.artifact_names) > 8:
            raise ValueError("too many explicit artifacts")
        for name in self.artifact_names:
            Artifact.from_dict(Artifact(name, b"").to_dict())
        # Optional account choice extension. ``_accounts_sent`` is the (worker
        # epoch, fingerprint) the service last acknowledged, so a change is
        # detected locally without polling the service; a 404
        # 404 ``unsupported_version`` (or a legacy ``not_found``) stops sending until the next registration.
        self._accounts_sent = None
        self._accounts_offered = False  # the acknowledged set was non-empty
        self._accounts_unsupported_epoch = None
        self._accounts_advertised = self.journal.accounts_advertised()
        # Optional readiness report extension, tracked the same way.
        self._readiness_sent = None
        self._readiness_unsupported_epoch = None
        # Optional capabilities extension: sent once per registration.
        # ``_origin_epoch`` is the registration whose "origin" the service
        # accepted (durable in the remote journal); a claim may carry `origin`
        # only while it is the current one.
        self._capabilities_sent = None
        self._capabilities_unsupported_epoch = None
        recorded = self.journal.capabilities()
        self._origin_epoch = recorded[0] if recorded is not None and "origin" in recorded[1] else None
        # Optional key rotation extension. ``key_expires_at`` is the current key's
        # expiry and ``key_store(previous_key, worker_id, device_key, expires_at)``
        # saves a rotated key durably and then calls ``adopt_key``; both are set by
        # whoever owns the enrollment (``ConfiguredRemote``). A client without a
        # store has nowhere durable to put a new key, so it never rotates.
        self.key_expires_at = None
        self.key_store = None
        self._rotation_epoch = recorded[0] if recorded is not None and "key_rotation" in recorded[1] else None
        self._rotation_unsupported_epoch = None
        # Optional session attention extension: sent only for a registration whose
        # "attention" the service accepted, whenever the report changes.
        self._attention_epoch = recorded[0] if recorded is not None and "attention" in recorded[1] else None
        self._attention_sent = None
        self._attention_unsupported_epoch = None
        # How reports are shaped after the service refused one (``_attention_refused``).
        self._attention_shape = _ReportShape()
        self._rotation_retry_at = None  # time.monotonic() before which no rotation is asked for
        self.runtime.remote_launch_guard = self.launch_allowed
        self.runtime.remote_account_ref = self.requested_account_ref

    def _fence(self, claim):
        return {"job_id": claim.job_id, "epoch": claim.epoch, "worker_epoch": self.worker_epoch}

    def _idempotency_key(self, claim):
        # Fixed length whatever the remote ID's size (IDs may be 200 characters).
        return "remote:" + hashlib.sha256((self.journal.service + "\0" + claim.job_id).encode()).hexdigest()

    def _worker_request(self, operation, extra=None):
        """``heartbeat``/``poll``: a stale worker epoch means another registration
        superseded this one, so re-register on the next tick instead of reporting online."""
        try:
            return self.transport.request(operation, {"worker_epoch": self.worker_epoch, **(extra or {})})
        except ProtocolError as exc:
            if exc.code == "stale_epoch":
                with self._lock:
                    self.worker_epoch = None
            raise

    def _connectivity(self, failure=None):
        """Fold one request outcome from either thread into the shared connectivity state.

        Any failure reports ``offline`` (or ``revoked``/``expired``); only a heartbeat
        success reports ``online``. Revocation and device expiry are final for this
        client (only a new enrollment restores access), so nothing overrides them.
        """
        with self._lock:
            if self.state in FINAL:
                return
            if failure is None:
                self.state = "online"
            elif isinstance(failure, ProtocolError):
                self.state = ("revoked" if failure.code in {"revoked", "unauthorized"}
                              else "expired" if failure.code == "device_expired"
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

    def launch_allowed(self, local_id: str) -> bool | RemoteAuthorization:
        """``True`` for local jobs; for a remote job, the authorization the service
        just granted (URL and worker ID), or ``False``. The runtime re-checks the
        token's URL against settings at the commit point."""
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
        claim = self._stored_claim(binding)
        if local.expires_at <= datetime.now(timezone.utc) or claim.submission.job.expires_at <= self._server_now():
            return False
        try:
            remote = self._job_response("renew", claim)
            if remote["state"] in TERMINAL or remote["state"] in {"cancel_requested", APPROVAL} or remote["cancel_requested"]:
                return False
            return RemoteAuthorization(self.url, self.worker_id)
        except (ProtocolError, KeyError, ValueError, TypeError):
            return False

    @staticmethod
    def _stored_claim(binding):
        """A claim already validated when it was received (with or without account_ref or origin)."""
        return Claim.from_dict(json.loads(binding["claim"]), allow_account_ref=True, allow_origin=True)

    def requested_account_ref(self, local_id: str) -> str | None:
        """The ``account_ref`` the claim behind a local job carried, or None when absent.

        The runtime resolves it against the current allowlist under its launch
        lock. Raises ``LookupError`` when no claim is bound to the job, so the
        runtime fails it rather than guessing an account.
        """
        binding = self.journal.binding(local_id)
        if binding is None:
            raise LookupError("no claim is bound to this job")
        return self._stored_claim(binding).submission.account_ref

    def origin_accepted(self) -> bool:
        """Whether this registration advertised "origin" and the service accepted it."""
        epoch = self.worker_epoch
        return epoch is not None and self._origin_epoch == epoch

    def sync_capabilities(self) -> None:
        """Send ``capabilities`` once after each new registration.

        Never raises: a failure is retried on the next synchronization pass,
        and claims and heartbeats go on meanwhile (a claim carrying ``origin``
        is malformed until the service acknowledges). Only a 404
        (``unsupported_version``, or ``not_found`` from reference servers that
        predate the extension) records that the backend has no capabilities
        support, until the next registration.
        """
        try:
            epoch = self.worker_epoch
            if epoch is None or epoch in (self._capabilities_sent, self._capabilities_unsupported_epoch):
                return
            try:
                response = self.transport.request(
                    "capabilities", {"worker_epoch": epoch, "features": list(WORKER_FEATURES)})
            except ProtocolError as exc:
                if exc.status == 404 and exc.code in {"unsupported_version", "not_found"}:
                    self._capabilities_unsupported_epoch = epoch
                elif exc.code in {"revoked", "unauthorized", "device_expired"}:
                    self._connectivity(exc)
                return
            try:
                accepted = capability_features(fields(response, {"features"})["features"])
            except ProtocolError:
                return  # malformed acknowledgement: resend on the next pass
            if not set(accepted) <= set(WORKER_FEATURES):
                return  # a feature this worker never advertised: malformed, resend
            self.journal.record_capabilities(epoch, accepted)
            self._origin_epoch = epoch if "origin" in accepted else None
            self._rotation_epoch = epoch if "key_rotation" in accepted else None
            self._attention_epoch = epoch if "attention" in accepted else None
            self._capabilities_sent = epoch
        except Exception:
            return

    def rotation_accepted(self) -> bool:
        """Whether this registration advertised "key_rotation" and the service accepted it."""
        epoch = self.worker_epoch
        return epoch is not None and self._rotation_epoch == epoch != self._rotation_unsupported_epoch

    def key_renewal(self) -> bool | None:
        """For status: whether the key renews itself (True), will not (False: the backend
        lacks rotation), or None while this registration has not found out yet."""
        epoch = self.worker_epoch
        if epoch is None:
            return None
        if epoch in (self._rotation_unsupported_epoch, self._capabilities_unsupported_epoch):
            return False
        if self._capabilities_sent == epoch:
            return self.rotation_accepted()
        return None

    def adopt_key(self, device_key: str, expires_at: datetime) -> None:
        """Send every later request with a rotated key. Called by ``key_store`` once the
        key is saved; never re-registers, so the worker epoch and claims are kept."""
        set_key = getattr(self.transport, "set_key", None)
        if callable(set_key):
            set_key(device_key)
        else:
            self.transport.key = device_key
        self.key_expires_at = expires_at

    def rotate_key_if_due(self) -> bool:
        """Swap the device key for a new one once ``ROTATE_BEFORE_SECONDS`` or less of it is left.

        At most one request per call, only after a registration whose
        ``key_rotation`` the service accepted, and never within
        ``ROTATION_BACKOFF_SECONDS`` of a failure. Never raises. A lost
        response, a refusal or a failed save keeps the old key, which the
        service still accepts because the new one is only pending; the retry
        replaces that pending key. A 404 stops it until the next registration.
        Returns whether a new key was adopted.
        """
        try:
            epoch, expires, store = self.worker_epoch, self.key_expires_at, self.key_store
            if (store is None or expires is None or not self.worker_id or not self.rotation_accepted()
                    or (self._rotation_retry_at is not None and time.monotonic() < self._rotation_retry_at)):
                return False
            if (expires - self._server_now()).total_seconds() > ROTATE_BEFORE_SECONDS:
                return False
            previous_key = self.transport.key
            try:
                response = self.transport.request("rotate_key", {"worker_epoch": epoch})
            except ProtocolError as exc:
                if exc.status == 404 and exc.code in {"unsupported_version", "not_found"}:
                    self._rotation_unsupported_epoch = epoch
                    return False
                if exc.code in {"revoked", "unauthorized", "device_expired"}:
                    self._connectivity(exc)
                self._rotation_retry_at = time.monotonic() + ROTATION_BACKOFF_SECONDS
                return False
            try:
                data = fields(response, {"worker_id", "device_key", "expires_at"})
                worker_id, device_key = text(data["worker_id"]), text(data["device_key"])
                expires_at = timestamp(data["expires_at"])
                if worker_id != self.worker_id or device_key == previous_key or expires_at <= self._server_now():
                    raise ProtocolError("invalid_response")
            except ProtocolError:
                self._rotation_retry_at = time.monotonic() + ROTATION_BACKOFF_SECONDS
                return False
            if not store(previous_key, worker_id, device_key, expires_at):
                self._rotation_retry_at = time.monotonic() + ROTATION_BACKOFF_SECONDS
                return False
            self._rotation_retry_at = None
            return True
        except Exception:
            self._rotation_retry_at = time.monotonic() + ROTATION_BACKOFF_SECONDS
            return False

    def _advertisement(self, policy) -> tuple[AdvertisedAccount, ...]:
        """The allowlist as advertised: opaque references, labels and the default flag only."""
        return tuple(
            AdvertisedAccount(entry.account_ref, entry.label, entry.identity == policy.pinned_account_ref)
            for entry in policy.account_allowlist
        )

    def sync_accounts(self, policy=None) -> None:
        """Send ``accounts`` after a new registration and whenever the allowlist changed.

        Change is detected by comparing a local fingerprint with the one the
        service last acknowledged for this worker epoch. Never raises: a
        failure is retried on the next synchronization pass, and claims and
        heartbeats go on meanwhile. Only a 404 (``unsupported_version``, or
        ``not_found`` from older reference servers) records that the backend
        offers no account choice, until the next registration.
        """
        try:
            epoch = self.worker_epoch
            if epoch is None or self._accounts_unsupported_epoch == epoch:
                return
            policy = policy or load_worker_settings(self.runtime.backup_root)
            entries = self._advertisement(policy)
            body = [entry.to_dict() for entry in entries]
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if self._accounts_sent == (epoch, fingerprint):
                return
            try:
                response = self.transport.request("accounts", {"worker_epoch": epoch, "accounts": body})
            except ProtocolError as exc:
                # Backends without the extension refuse an unknown operation with
                # 404: `unsupported_version` per the spec, or `not_found` from the
                # reference server releases that predate it.
                if exc.status == 404 and exc.code in {"unsupported_version", "not_found"}:
                    self._accounts_unsupported_epoch = epoch
                    self._accounts_offered = False
                elif exc.code in {"revoked", "unauthorized", "device_expired"}:
                    self._connectivity(exc)
                return
            try:
                if integer(fields(response, {"account_count"})["account_count"]) != len(entries):
                    return  # malformed acknowledgement: resend on the next pass
            except ProtocolError:
                return
            if entries and not self._accounts_advertised:
                self.journal.mark_accounts_advertised()
                self._accounts_advertised = True
            self._accounts_sent = (epoch, fingerprint)
            self._accounts_offered = bool(entries)
        except Exception:
            return

    def _readiness(self, policy) -> dict:
        """The readiness report: approved folder IDs with their labels, and the execution mode."""
        from openswap.worker.adapter import execution_mode

        from openswap.worker.cli import launchable_workspaces

        # A folder of repos is reported as its repos, scanned again when the
        # settings change and at least every FOLDER_RESCAN_SECONDS (the scan
        # runs git for each repo, so not on every tick); at most the
        # protocol's 20 folders.
        cached = getattr(self, "_folders_cache", None)
        now = time.monotonic()
        if cached is not None and cached[0] == policy.workspaces and now - cached[1] < FOLDER_RESCAN_SECONDS:
            folders = cached[2]
        else:
            launchable = launchable_workspaces(self.runtime.backup_root, policy.workspaces)
            folders = [ReportedFolder(w.workspace_id, w.display_label).to_dict()
                       for w in launchable
                       if offerable(self.runtime.backup_root, w, launchable)][:MAX_REPORTED_FOLDERS]
            self._folders_cache = (policy.workspaces, now, folders)
        # The pinned account's provider decides (a Claude pin reports Claude's opt-in).
        mode_of = getattr(self.runtime, "execution_mode", None)
        if callable(mode_of):
            try:
                mode = mode_of()
            except Exception:
                mode = "disabled"
            return {"folders": folders, "execution": "live" if mode == "live" else "disabled"}
        return {"folders": folders, "execution": execution_mode(getattr(self.runtime, "adapter", None))}

    def sync_readiness(self, policy=None) -> None:
        """Send ``readiness`` after a new registration and whenever folders or the mode changed.

        Like ``sync_accounts``: a local fingerprint per worker epoch, never
        raises, retried on the next pass, and a 404 (``unsupported_version``
        or a legacy ``not_found``) stops it until the next registration.
        """
        try:
            epoch = self.worker_epoch
            if epoch is None or self._readiness_unsupported_epoch == epoch:
                return
            policy = policy or load_worker_settings(self.runtime.backup_root)
            body = self._readiness(policy)
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if self._readiness_sent == (epoch, fingerprint):
                return
            try:
                response = self.transport.request("readiness", {"worker_epoch": epoch, **body})
            except ProtocolError as exc:
                if exc.status == 404 and exc.code in {"unsupported_version", "not_found"}:
                    self._readiness_unsupported_epoch = epoch
                elif exc.code in {"revoked", "unauthorized", "device_expired"}:
                    self._connectivity(exc)
                return
            try:
                if integer(fields(response, {"folder_count"})["folder_count"]) != len(body["folders"]):
                    return  # malformed acknowledgement: resend on the next pass
            except ProtocolError:
                return
            self._readiness_sent = (epoch, fingerprint)
        except Exception:
            return

    def sync_attention(self) -> bool:
        """Send ``attention`` whenever the open items, the closes to report or the
        owner's switches changed, for a registration that advertised it.

        Like ``sync_readiness``: never raises, a failure is retried on the next
        pass, and a 404 stops it until the next registration. Closes are
        dropped from the store only once the service accepted them. A refused
        report is reshaped (``_attention_refused``) so the closes always get
        through. An unreadable settings file is not "off": the open items stay
        and waiting closes still go, without details or replies.

        Returns False when this registration cannot report yet (not online, not
        registered or advertised, unsupported, or waiting out a refusal), so the
        caller keeps the change; True once it had its turn, sent or not.
        """
        try:
            store = getattr(self.runtime, "attention", None)
            epoch = self.worker_epoch
            if (store is None or not store.ready or epoch is None or self.state != "online"
                    or self._attention_epoch != epoch or self._attention_unsupported_epoch == epoch
                    or time.monotonic() < self._attention_shape.retry_at):
                return False
            from openswap.settings import load_attention_settings
            from openswap.worker.attention import approved_folder_label

            switches = load_attention_settings(self.runtime.backup_root)
            store.note_problem("settings", None if switches.readable else
                               "OpenSwap can't read its settings file, so session notices send nothing new.")
            if switches.readable and not switches.notices:
                store.clear()
            notices = switches.notices or not switches.readable
            workspaces = load_worker_settings(self.runtime.backup_root).workspaces
            shape = self._attention_shape
            shape.forget(store.open_ids())
            body, closed_ids = store.report(
                notices=notices, details=switches.details and switches.readable,
                replies=switches.replies and switches.readable,
                folder_label=lambda cwd: approved_folder_label(workspaces, cwd),
                budget=shape.budget, plain=frozenset(shape.plain - {shape.trying}), bare=frozenset(shape.bare),
                closes_only=shape.closes_only)
            if not switches.readable and not body["items"] and not closed_ids:
                return True  # nothing to keep the service up to date with
            fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
            if not closed_ids and self._attention_sent == (epoch, fingerprint):
                shape.accepted()  # the very report the service took last: what it would say again
                return True
            try:
                response = self.transport.request("attention", {"worker_epoch": epoch, **body})
            except ProtocolError as exc:
                if exc.status == 404 and exc.code in {"unsupported_version", "not_found"}:
                    self._attention_unsupported_epoch = epoch
                elif exc.code in {"revoked", "unauthorized", "device_expired"}:
                    self._connectivity(exc)
                else:
                    self._attention_refused(store, exc, body)
                return True
            try:
                if integer(fields(response, {"item_count"})["item_count"]) != len(body["items"]):
                    return True  # malformed acknowledgement: resend on the next pass
            except ProtocolError:
                return True
            store.acknowledge(closed_ids, body["items"])
            shape.took()
            store.note_problem("refused", None)
            # Without the closes the same open set needs no resend.
            body["closed"] = []
            self._attention_sent = (epoch, hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest())
        except Exception as exc:  # noqa: BLE001 - retried on the next pass
            log_limited("attention_sync", "Session notices could not be reported: %s", exc)
            return True
        return True

    def _attention_refused(self, store, exc: ProtocolError, body: dict) -> None:
        """The service refused a report. 403: ask again only every
        ``ATTENTION_FORBIDDEN_SECONDS`` and say why. 413: the next ones are smaller
        (``_ReportShape.too_large``).
        400: the next one leaves out what may be the cause, an item's detail
        first (then each is tried again alone), then its labels, and as a last
        resort every item, so the closes get through. Anything else is retried
        on the next pass."""
        shape = self._attention_shape
        log_limited(f"attention_{exc.status}_{exc.code}", "Control service refused the session notice report: "
                    "%s %s", exc.status, exc.code)
        if exc.status == 403:
            shape.retry_at = time.monotonic() + ATTENTION_FORBIDDEN_SECONDS
            store.note_problem("refused", "The control service refuses session notices from this Mac "
                               f"({exc.code}); OpenSwap asks again every {int(ATTENTION_FORBIDDEN_SECONDS // 60)} "
                               "minutes.")
        elif exc.status == 413:
            shape.too_large(len(json.dumps(body, allow_nan=False).encode()))
        elif exc.status == 400 and exc.code == "invalid_request":
            shape.refused(body)  # the service's own refusal of the report, never a proxy's error page

    def _attention_loop(self):
        run_attention(self.runtime, self.stop_event, lambda: self)

    def accounts_offered(self) -> bool:
        """Whether the service acknowledged a non-empty account set for this registration."""
        sent = self._accounts_sent
        return self._accounts_offered and sent is not None and sent[0] == self.worker_epoch

    def account_ready(self) -> bool:
        """Whether to claim new work.

        Claim when a job could resolve an account (the pin is present), or once
        this enrollment has advertised account choices and the service has
        acknowledged the current set. In that second case the service may hold
        jobs carrying a choice the owner has since withdrawn; claiming them
        lets the runtime fail them before launch (``unlaunched=true``) instead
        of leaving them queued until they expire. Nothing is ever substituted.
        """
        if self.runtime.account_ready():
            return True
        sent = self._accounts_sent
        return self._accounts_advertised and sent is not None and sent[0] == self.worker_epoch

    def tick(self):
        """One bounded synchronous pass: heartbeat, then synchronization. No sleeping; tests control time."""
        if self.stop_event.is_set():
            return
        policy = load_worker_settings(self.runtime.backup_root)
        if self.heartbeat_tick(policy):
            self.enforce_cancellations()
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
        if self.state in FINAL:
            return False  # only a new enrollment (re-pair) can restore access
        store, acks = None, []
        try:
            if self.worker_epoch is None:
                registered = self.transport.request("register", {})
                self.worker_epoch = integer(registered["worker_epoch"], 1)
            sent = datetime.now(timezone.utc)
            # Session attention replies ride on the heartbeat. Outcomes go out on
            # any registration of a service that accepted "attention" (kept until
            # a heartbeat carrying them succeeds); replies come in only for the
            # registration that advertised it.
            store = getattr(self.runtime, "attention", None)
            acks = store.take_acks() if store is not None and self._attention_epoch is not None else []
            try:
                heartbeat = self._worker_request("heartbeat", {"attention_acks": acks} if acks else None)
            except ProtocolError as exc:
                if not (acks and exc.code == "invalid_request"):
                    raise
                # Liveness and replies never wait on outcomes the service refuses:
                # this heartbeat goes again without them, and only if that one is
                # taken are they the cause (tried again apart: ``acks_refused``).
                heartbeat = self._worker_request("heartbeat")
                store.acks_refused(acks)
                acks = []
            seen = timestamp(heartbeat["last_seen_at"])
            cancel_ids = heartbeat["cancel_job_ids"]
            if (not isinstance(cancel_ids, list) or len(cancel_ids) > MAX_CANCEL_IDS
                    or not all(isinstance(i, str) and 0 < len(i) <= 200 for i in cancel_ids)):
                raise ProtocolError("invalid_response")
            with self._lock:
                self.last_seen_at = seen
                # Measured against the send time, the offset can only overstate the
                # service clock by the round trip: deadlines err early, never late.
                self._skew = seen - sent
                # Only queued here: runtime control calls never delay the next heartbeat.
                self._cancels.update(cancel_ids)
            if store is not None:
                store.acks_sent(acks)
                if self._attention_epoch is not None and self._attention_epoch == self.worker_epoch:
                    # Only queued here too: the attention thread applies them.
                    store.queue_replies(heartbeat.get("attention_replies"))
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
            remote = self._job_response("renew", claim)
        except ProtocolError as exc:
            if exc.code != "lease_lost":
                raise
            if self._admitted is claim:
                self._admitted = None
            return
        if remote["cancel_requested"]:
            with self._lock:
                self._cancels.add(claim.job_id)

    def enforce_cancellations(self):
        """Apply the cancellations heartbeat and renewal reported to their local runs.

        Runs on the renewal thread (and in ``tick``), so a synchronization pass
        blocked on the network cannot hold an owner's cancel back. A remote ID
        with no running local job is dropped; the service keeps listing it until
        its outcome is confirmed, so nothing is lost.
        """
        with self._lock:
            pending, self._cancels = self._cancels, set()
        for remote_id in pending:
            binding = self.journal.by_remote(remote_id)
            if binding is None or not binding["local_id"]:
                continue
            try:
                local = self.runtime.get(binding["local_id"])
            except KeyError:
                continue
            if local.state.value not in TERMINAL:
                self.runtime.cancel(local.job_id)
            elif binding["done"]:
                self._reconcile_ended(binding, local)

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
        # Optional capabilities: bounded and never raises. It precedes the
        # claim because a claim may carry `origin` only once it is accepted.
        self.sync_capabilities()
        # Optional account choice: bounded and never raises. It precedes the
        # claim because account_ready() gates polling on it.
        self.sync_accounts(policy)
        try:
            self._sync_jobs(policy)
        finally:
            # The optional readiness report goes last, so a slow or stalled
            # readiness route never delays result delivery or task pickup.
            with self._lock:
                online = self.state == "online"
            if online and not self.stop_event.is_set():
                self.sync_readiness(policy)
                # Optional key rotation, last for the same reason: bounded by one
                # request and a Keychain write, and never raises.
                self.rotate_key_if_due()

    def _sync_jobs(self, policy):
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
                availability = self.runtime.provider_availability()
            except Exception:
                return  # an unavailable provider claims nothing; the heartbeat still counts
            if not availability.available or not self.account_ready():
                return
            response = self._worker_request("poll")
            if response["claim"] is not None:
                # account_ref is accepted only once this enrollment advertised accounts,
                # and origin only once this registration's "origin" was accepted;
                # otherwise either is a malformed claim, as before the extensions.
                claim = Claim.from_dict(response["claim"], allow_account_ref=self._accounts_advertised,
                                        allow_origin=self.origin_accepted())
                self.journal.remember(claim)  # persist before local admission
                pending = self.journal.pending()
                if pending:  # a re-offered claim that already ended here has nothing to sync
                    self._sync(pending[0])
        except (ProtocolError, KeyError, TypeError, ValueError, OSError, sqlite3.Error, AdmissionError) as exc:
            self._connectivity(exc)

    def _sync(self, binding):
        claim = self._stored_claim(binding)
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
                    if remote["state"] == "succeeded":
                        # Only a worker holding stop proof can confirm a success, and the
                        # service has one: there is nothing to restate, so the binding ends.
                        self.journal.update(claim.job_id, done=1)
                        return
                    # Nothing was ever admitted locally, so nothing launched. An outcome the
                    # service already holds (say, a ``failed`` whose acknowledgement was lost)
                    # is replayed as is, so the retry cannot conflict with it. An owner's cancel
                    # outranks a provisional interruption: nothing ran either way.
                    state = (remote["state"] if remote["state"] in TERMINAL and remote["state"] != "interrupted"
                             else "cancelled" if remote["cancel_requested"] else "interrupted" if remote["state"] == "interrupted"
                             else "failed" if remote["state"] == APPROVAL else "expired")
                    self._reconcile(claim, state, stopped=False, unlaunched=True)
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
                                            job_id=local_id, remote=True, origin=claim.submission.origin)
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
        if not self._forward_events(claim, local_id, binding["cursor"]):
            if self._admitted is claim:
                self._admitted = None  # the binding ended; nothing is left to renew
            return
        local = self.runtime.get(local_id)
        if local.state.value in TERMINAL:
            stopped, unlaunched = self._proof(local)
            state = local.state.value if stopped or unlaunched else "interrupted"
            self._reconcile(claim, state, stopped=stopped, unlaunched=unlaunched)
            if state == "succeeded" and self.upload_results(claim, local):
                # Relay the rejection diagnostics before acknowledging the claim.
                self._forward_events(claim, local_id, self.journal.binding(local_id)["cursor"])
            self._admitted = None
            if state == "interrupted" and not (stopped or unlaunched) and self._proof_pending(local):
                return  # provisional: stay pending so later stop proof is still reconciled
            self.journal.update(claim.job_id, done=1)

    def _reconcile(self, claim, state, *, stopped, unlaunched):
        """Report an outcome; only an acknowledgement for exactly this job and state counts,
        so a malformed reply leaves the binding pending for a retry instead of retiring it."""
        ack = self.transport.request("reconcile", {**self._fence(claim), "state": state,
                                                   "execution_stopped": stopped, "unlaunched": unlaunched})
        if ack.get("job_id") != claim.job_id or ack.get("state") != state:
            raise ProtocolError("invalid_response")

    def _forward_events(self, claim, local_id, cursor) -> bool:
        """Upload local events after ``cursor``; returns whether the claim is still live.

        ``cursor_conflict`` means the service holds a different history for this
        job. No replay can repair that, so the binding ends instead of being
        retried forever as "offline" and blocking every later claim.
        """
        # One page per tick keeps requests bounded; terminal jobs are not
        # acknowledged until every event page is durable at the service.
        page = self.runtime.events(local_id, after_cursor=cursor, limit=200)
        while page.events:
            if self.stop_event.is_set():
                raise ProtocolError("service_unavailable", 503)
            self._worker_request("heartbeat")
            events = [replace(event, job_id=claim.job_id,
                              diagnostic_code=WIRE_DIAGNOSTICS.get(event.diagnostic_code, event.diagnostic_code)
                              ).to_dict() for event in page.events]
            try:
                response = self.transport.request("events", {**self._fence(claim), "after_cursor": cursor, "events": events})
            except ProtocolError as exc:
                if exc.code != "cursor_conflict":
                    raise
                self._abandon(claim, local_id)
                return False
            ack = integer(response["next_cursor"])
            if ack != page.next_cursor:
                raise ProtocolError("invalid_response")
            cursor = ack
            self.journal.update(claim.job_id, cursor=cursor)
            if self.runtime.get(local_id).state.value not in TERMINAL:
                break
            page = self.runtime.events(local_id, after_cursor=cursor, limit=200)
        return True

    def _reconcile_ended(self, binding, local):
        """Report a proven outcome for a binding that ended without one (a
        ``cursor_conflict``), so a cancelled run does not stay listed forever.

        Best effort: the service keeps listing the job until its outcome is
        confirmed, so a failure here is retried on a later heartbeat.
        """
        stopped, unlaunched = self._proof(local)
        if not (stopped or unlaunched):
            return
        claim = self._stored_claim(binding)
        try:
            self._reconcile(claim, local.state.value, stopped=stopped, unlaunched=unlaunched)
        except ProtocolError:
            pass

    def _abandon(self, claim, local_id):
        """End a binding whose service history diverged; local execution is untouched.

        The service re-offers an active job on every poll until it is
        reconciled, so the outcome is reported as uncertain (``interrupted``
        with nothing proven) first. An unreachable service, an acknowledgement
        for anything but exactly that, or a ``stale_epoch`` defers it to the next
        tick; any other definitive answer ends the binding, and the local journal
        records a ``remote_sync_conflict`` diagnostic. A superseded worker epoch
        is left to the next heartbeat to detect and re-register: the same error
        can mean only this claim's epoch is stale, which re-registering would not fix.
        """
        try:
            self._reconcile(claim, "interrupted", stopped=False, unlaunched=False)
        except ProtocolError as exc:
            if exc.code in {"service_unavailable", "invalid_response", "stale_epoch"}:
                raise
        self._diagnose(self.runtime.get(local_id), "remote_sync_conflict")
        self.journal.update(claim.job_id, done=1)

    def _diagnose(self, local, code, *, required=False):
        """Journal a diagnostic for ``local``. A terminal job takes it whichever epoch
        ended it (a restart leaves the old one on the row). ``required`` lets a
        failed write propagate, so the binding stays pending instead of retiring
        without the diagnostic."""
        try:
            if local.state.value in TERMINAL:
                self.runtime.store.append_terminal_diagnostic(
                    local.job_id, diagnostic_code=code, worker_epoch=self.runtime.worker_epoch,
                    expected_generation=local.generation)
            else:
                self.runtime.store.append_event(
                    local.job_id, kind=SafeEventKind.DIAGNOSTIC, diagnostic_code=code,
                    worker_epoch=self.runtime.worker_epoch, expected_generation=local.generation)
        except JournalError:
            if required:
                raise

    def _proof_pending(self, local):
        """Whether stop proof for an uncertain run may still arrive: its account lease is
        still held or quarantined. That lease also blocks new claims, so a retained
        provisional binding never holds back work that could otherwise run."""
        lease = self.runtime.leases.for_job(local.job_id)
        return lease is not None and lease.state != "released"

    def _proof(self, local):
        if local.state.value == "interrupted":
            # Only the lease can later establish what happened to an uncertain run.
            lease = self.runtime.leases.for_job(local.job_id)
            if lease is not None and lease.state == "released":
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
        lease = self.runtime.leases.for_job(local.job_id)
        if lease is not None and lease.state == "released":
            return stopped or lease.reason == "confirmed_stopped", lease.reason == "unlaunched"
        # A pinned account missing from the roster at launch fails the job from
        # STARTING with no lease: nothing ran. The same code reported after a
        # launch (a RUNNING event exists) is not unlaunched.
        account_missing = (
            local.state.value == "failed" and local.diagnostic_code == "provider_auth_unavailable"
        )
        return stopped, not launched and (local.pinned_account_ref is None or account_missing)

    def upload_results(self, claim, local) -> bool:
        """Upload the explicit artifact list; returns whether any artifact was refused.

        A refused artifact (oversized, symlinked, replaced mid-read, over the
        per-job limit, or conflicting with a stored copy) would be refused on
        every retry, so it is skipped and journaled as ``artifact_rejected``
        rather than holding the claim, and every other claim, pending forever.
        Transport, lease and authorization failures still propagate for retry, as
        does a failure to journal the diagnostic, so the claim never completes
        without either the artifact or its ``artifact_rejected`` record.
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
                # One record per job: a pass retried after a later failure must not repeat it.
                if not self._has_diagnostic(local.job_id, "artifact_rejected"):
                    self._diagnose(local, "artifact_rejected", required=True)
        return rejected

    def _has_diagnostic(self, local_id, code):
        cursor = 0
        while True:
            page = self.runtime.events(local_id, after_cursor=cursor, limit=200)
            if not page.events:
                return False
            if any(e.diagnostic_code == code for e in page.events):
                return True
            cursor = page.next_cursor

    def _upload_artifact(self, claim, local, name):
        from openswap.worker.cli import results_folder

        # The results folder, even if the task's repo is gone since it ran:
        # a repo from a folder of repos keeps its ID for good.
        policy = load_worker_settings(self.runtime.backup_root)
        folder = results_folder(self.runtime.backup_root, local.workspace_id, policy.workspaces)
        if folder is None:
            raise ProtocolError("invalid_request")
        directory = folder / local.job_id
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
        artifact = Artifact(name, content).to_dict()
        ack = self.transport.request("upload", {**self._fence(claim), "artifact": artifact})
        # The claim completes only once the service confirms exactly what was sent.
        if any(ack.get(field) != artifact[field] for field in ("name", "size", "sha256")):
            raise ProtocolError("invalid_response")

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
        if getattr(self.runtime, "attention", None) is not None:
            # Its own thread, so a long synchronization pass never delays a notice.
            helpers.append(threading.Thread(target=self._attention_loop, name="openswap-worker-remote-attention",
                                            daemon=True))
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
                self.enforce_cancellations()
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


class _ReportShape:
    """How the next ``attention`` report is cut after the service refused one."""

    def __init__(self):
        from openswap.worker.attention import REPORT_BUDGET

        self.full = self.budget = REPORT_BUDGET
        self.taken = 0  # reports the service took since the budget last shrank
        self.restored = False  # made whole once: a later 413 shrinks it for good
        self.plain: set[str] = set()  # items sent without detail
        self.bare: set[str] = set()  # items sent with generic labels too
        self.suspects: list[str] = []  # items whose detail is tried again, one report at a time
        self.trying = ""  # the suspect whose detail the current report carries
        self.doubted = ""  # the suspect a refusal pointed at, until a report without its detail tells
        self.closes_only = False
        self.items_at = 0.0  # time.monotonic() when items are tried again after ``closes_only``
        self.retry_at = 0.0  # time.monotonic() before which no report is sent

    def forget(self, open_ids: set[str]) -> None:
        self.plain &= open_ids
        self.bare &= open_ids
        self.suspects = [i for i in self.suspects if i in open_ids]
        if self.trying not in open_ids:
            self.trying = ""
        if self.doubted not in open_ids:
            self.doubted = ""
        if not self.trying and not self.doubted and self.suspects:
            self.trying = self.suspects.pop(0)
        if self.closes_only and time.monotonic() >= self.items_at:
            self.closes_only = False

    def only_closes(self) -> None:
        """Send only the closes for a while: the service refuses every item."""
        self.closes_only = True
        self.items_at = time.monotonic() + ATTENTION_FORBIDDEN_SECONDS

    def refused(self, body: dict) -> None:
        items = body.get("items", [])
        if self.trying:
            # This item's detail may be the difference: the next report, without
            # it, tells (``accepted``), never this refusal alone.
            self.doubted, self.trying = self.trying, ""
            return
        if self.doubted:
            # Refused without its detail too: not its fault. Tried again later.
            self.suspects.append(self.doubted)
            self.doubted = ""
        detailed = [e["id"] for e in items if "detail" in e]
        if detailed:
            self.plain.update(detailed)
            self.suspects.extend(i for i in detailed if i not in self.suspects)
        elif any(e["id"] not in self.bare for e in items):
            self.bare.update(e["id"] for e in items)
            logger.warning("Session notices sent with generic labels: the control service refused the report")
        else:
            self.only_closes()

    def too_large(self, sent: int) -> None:
        """The service refused a report of ``sent`` bytes as too large."""
        if self.budget <= ATTENTION_MIN_BUDGET:
            self.only_closes()
        self.taken = 0
        self.budget = max(ATTENTION_MIN_BUDGET, min(self.budget, sent) // 2)

    def took(self) -> None:
        """The service took a report it was sent."""
        self.accepted()
        if self.budget < self.full and not self.restored:
            self.taken += 1
            if self.taken >= ATTENTION_BUDGET_RECOVERY:
                self.budget, self.taken, self.restored = self.full, 0, True

    def accepted(self) -> None:
        if self.trying:
            self.plain.discard(self.trying)
            self.trying = ""
        if self.doubted:
            # Taken without it right after it was refused with it: its detail it was.
            logger.warning("Session notice %s: its detail was refused by the control service; sent without it",
                           self.doubted)
            self.doubted = ""
