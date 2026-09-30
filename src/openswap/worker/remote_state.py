"""Safe durable connectivity status and configured transport lifecycle."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import threading

from openswap.settings import atomic_write_json, load_worker_settings
from openswap.worker.models import RemoteConnectivity
from openswap.worker.protocol import HEARTBEAT_SECONDS, MISSED_HEARTBEATS, ProtocolError, timestamp
from openswap.worker.remote import RemoteClient
from openswap.worker.pairing import load_enrollment


def _identity(url):
    return hashlib.sha256(url.encode()).hexdigest()


def save_status(root, url, state, seen):
    atomic_write_json(root / "worker" / "remote-status.json", {
        "service": _identity(url), "state": state,
        "last_seen_at": seen.isoformat() if seen else None,
    })


def read_status(root, *, now=None):
    """Read without Keychain access, mutations, sockets, or directory creation."""
    policy = load_worker_settings(root)
    if not policy.enabled or policy.control_service_url is None:
        return RemoteConnectivity.DISABLED, None
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads((root / "worker" / "remote-status.json").read_text(encoding="utf-8"))
        if data["service"] != _identity(policy.control_service_url):
            return RemoteConnectivity.OFFLINE, None
        state = RemoteConnectivity(data["state"])
        seen = timestamp(data["last_seen_at"]) if data["last_seen_at"] else None
        if state == RemoteConnectivity.ONLINE and (seen is None or not -HEARTBEAT_SECONDS <= (now - seen).total_seconds() < HEARTBEAT_SECONDS * MISSED_HEARTBEATS):
            state = RemoteConnectivity.OFFLINE
        return state, seen
    except (OSError, KeyError, TypeError, ValueError, ProtocolError):
        return RemoteConnectivity.OFFLINE, None


class ConfiguredRemote:
    """Reload the URL/key on each poll; unpair prevents new work immediately.

    ``run`` mirrors ``RemoteClient.run``: this thread re-reads the enrollment,
    heartbeats and renews the admitted claim at the fixed cadence, while a
    second thread synchronizes (probe, event pages, artifact uploads). Both
    threads write ``remote-status.json`` from the client's shared connectivity
    after each pass. The client (and its worker epoch) is rebuilt only when the
    enrollment identity changes (URL, worker ID or key) or the enrollment is
    gone/expired locally; a locked Keychain, a transport fault or a failed status
    write keeps it and reports ``offline``, so nothing re-registers (which would
    interrupt the service-side jobs of the registration it replaced).
    """
    def __init__(self, runtime, *, client_factory=RemoteClient, enrollment_loader=load_enrollment):
        self.runtime = runtime
        self.client_factory, self.enrollment_loader = client_factory, enrollment_loader
        self.client = None
        self.identity = None
        self.stop_event = threading.Event()
        self._status_lock = threading.Lock()  # a status write reflects the state read for it
        runtime.remote_launch_guard = self.launch_allowed

    def launch_allowed(self, job_id):
        if not self.runtime.get(job_id).idempotency_key.startswith("remote:"):
            return True
        client = self.client
        if self.stop_event.is_set() or client is None:
            return False
        # Re-read authorization in Keychain before every launch; a removed or
        # locked key cannot rely on a copy cached by a prior heartbeat.
        policy = load_worker_settings(self.runtime.backup_root)
        if policy.control_service_url != client.url:
            return False
        try:
            enrollment = self.enrollment_loader(client.url)
            if enrollment is None or self.identity != (client.url, enrollment):
                return False
        except ProtocolError:
            return False
        return client.launch_allowed(job_id)

    def _save(self, client):
        with self._status_lock:
            save_status(self.runtime.backup_root, client.url, client.state, client.last_seen_at)

    def _resolve(self):
        """Re-read policy and enrollment; return the client to drive this pass, or None.

        Writes the status for every outcome that skips the client's own pass.
        """
        root = self.runtime.backup_root
        policy = load_worker_settings(root)
        url = policy.control_service_url
        if self.stop_event.is_set():
            return None
        if not policy.enabled or url is None:
            self.client, self.identity = None, None
            return None
        try:
            enrollment = self.enrollment_loader(url)
        except ProtocolError as exc:
            # A locally expired enrollment is not a revocation: the owner
            # simply has to re-pair. It ends this client like an unpair does.
            state = ("revoked" if exc.code in {"revoked", "unauthorized"}
                     else "expired" if exc.code == "device_expired" else "offline")
            client = self.client
            if client is not None and client.url == url and state == "offline":
                # A locked Keychain is transient: keep the registration, report offline.
                client._connectivity(exc)
                self._save(client)
            else:
                seen = client.last_seen_at if client is not None else None
                self.client, self.identity = None, None
                with self._status_lock:
                    save_status(root, url, state, seen)
            return None
        if enrollment is None:
            # Unpaired (the key is gone) while settings still name the URL.
            seen = self.client.last_seen_at if self.client is not None else None
            self.client, self.identity = None, None
            with self._status_lock:
                save_status(root, url, "offline", seen)
            return None
        # Enrollment compares by value (a re-pair changes it) but its
        # repr omits the key, so the identity is safe to print.
        identity = (url, enrollment)
        if identity != self.identity:
            self.client, self.identity = None, None  # never drive a stale enrollment
            client = self.client_factory(self.runtime, url, enrollment.device_key,
                                         worker_id=enrollment.worker_id)
            client.stop_event = self.stop_event
            # RemoteClient installs its standalone guard; keep the
            # configured guard that also rechecks Keychain availability.
            self.runtime.remote_launch_guard = self.launch_allowed
            self.client, self.identity = client, identity
        return self.client

    def tick(self):
        """One synchronous pass (heartbeat, then synchronization); tests control time."""
        client = self._resolve()
        if client is None:
            return
        client.tick()
        self._save(client)

    def _heartbeat_pass(self):
        client = self._resolve()
        if client is None:
            return
        try:
            if client.heartbeat_tick():
                client.renew_admitted()
        except Exception as exc:
            client._connectivity(exc)
        self._save(client)

    def _sync_pass(self):
        client = self.client
        if client is None:
            return
        try:
            client.sync_tick()
        except Exception as exc:
            # A journal or adapter fault must not end synchronization for the
            # process lifetime; the next pass retries at the usual cadence.
            client._connectivity(exc)
        self._save(client)

    def _offline(self, exc):
        """A pass failed outside the client (for example the status write): keep the
        client but report offline, which also closes the launch fence until a
        heartbeat succeeds and the status is written again."""
        client = self.client
        if client is not None:
            client._connectivity(exc)

    def run(self, stop_event):
        self.stop_event = stop_event
        sync = threading.Thread(target=self._sync_loop, name="openswap-worker-remote-sync", daemon=True)
        sync.start()
        try:
            while not stop_event.is_set():
                try:
                    self._heartbeat_pass()
                except Exception as exc:
                    self._offline(exc)
                stop_event.wait(HEARTBEAT_SECONDS)
        finally:
            sync.join()

    def _sync_loop(self):
        while not self.stop_event.is_set():
            try:
                self._sync_pass()
            except Exception as exc:
                self._offline(exc)
            self.stop_event.wait(HEARTBEAT_SECONDS)
