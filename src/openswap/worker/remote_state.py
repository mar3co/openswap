"""Safe durable connectivity status and configured transport lifecycle."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import time

from openswap.settings import atomic_write_json, load_worker_settings
from openswap.worker.models import RemoteConnectivity
from openswap.worker.protocol import HEARTBEAT_SECONDS, MISSED_HEARTBEATS, ProtocolError, timestamp
from openswap.worker.remote import RemoteClient
from openswap.worker.pairing import load_enrollment


def _identity(url, worker_id):
    # Scoped to the paired worker as well as the URL: a re-pair of the same URL
    # must not inherit the previous enrollment's online/revoked/expired status.
    return hashlib.sha256((url + "\0" + (worker_id or "")).encode()).hexdigest()


STATUS_FILE = ("worker", "remote-status.json")


def save_status(root, url, state, seen, worker_id=None, received=None):
    """``seen`` is the service's timestamp, kept for display; ``received`` is the Mac's
    time for that heartbeat (default ``seen``), which alone decides freshness so a
    skewed service clock cannot make a live connection read as offline."""
    received = received or seen
    atomic_write_json(root.joinpath(*STATUS_FILE), {
        "service": _identity(url, worker_id), "state": state,
        "last_seen_at": seen.isoformat() if seen else None,
        "received_at": received.isoformat() if received else None,
    })


def read_status(root, *, now=None):
    """Read without Keychain access, mutations, sockets, or directory creation."""
    policy = load_worker_settings(root)
    if not policy.enabled or policy.control_service_url is None:
        return RemoteConnectivity.DISABLED, None
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads(root.joinpath(*STATUS_FILE).read_text(encoding="utf-8"))
        if data["service"] != _identity(policy.control_service_url, policy.control_service_worker_id):
            return RemoteConnectivity.OFFLINE, None
        state = RemoteConnectivity(data["state"])
        seen = timestamp(data["last_seen_at"]) if data["last_seen_at"] else None
        received = timestamp(data["received_at"]) if data.get("received_at") else seen
        if state == RemoteConnectivity.ONLINE and (received is None or not -HEARTBEAT_SECONDS <= (now - received).total_seconds() < HEARTBEAT_SECONDS * MISSED_HEARTBEATS):
            state = RemoteConnectivity.OFFLINE
        return state, seen
    except (OSError, KeyError, TypeError, ValueError, ProtocolError):
        return RemoteConnectivity.OFFLINE, None


class ConfiguredRemote:
    """Reload the URL/key on each poll; unpair prevents new work immediately.

    ``run`` mirrors ``RemoteClient.run``: this thread re-reads the enrollment
    and heartbeats on an absolute schedule, a second renews the admitted claim,
    and a third synchronizes (probe, event pages, artifact uploads). The
    heartbeat and synchronization threads write ``remote-status.json`` from the
    client's shared connectivity after each pass. The client (and its worker epoch) is rebuilt only when the
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
        self._client_lock = threading.Lock()  # client and identity change together
        runtime.remote_launch_guard = self.launch_allowed

    def launch_allowed(self, job_id):
        if not self.runtime.get(job_id).idempotency_key.startswith("remote:"):
            return True
        with self._client_lock:
            client, identity = self.client, self.identity
        if self.stop_event.is_set() or client is None:
            return False
        # Re-read authorization in Keychain before every launch; a removed or
        # locked key cannot rely on a copy cached by a prior heartbeat.
        policy = load_worker_settings(self.runtime.backup_root)
        if policy.control_service_url != client.url:
            return False
        try:
            enrollment = self.enrollment_loader(client.url)
            # The captured client must still be the active one for this exact enrollment:
            # an unpair and re-pair of the same URL while the loader ran replaces both.
            with self._client_lock:
                current = self.client is client and self.identity == identity
            if enrollment is None or not current or identity != (client.url, enrollment):
                return False
        except ProtocolError:
            return False
        return client.launch_allowed(job_id)

    def _set_client(self, client, identity):
        with self._client_lock:
            self.client, self.identity = client, identity

    def _save(self, client):
        with self._status_lock:
            seen = client.last_seen_at
            # The heartbeat's local send time: the service timestamp less the measured offset.
            received = seen - getattr(client, "_skew", timedelta(0)) if seen else None
            save_status(self.runtime.backup_root, client.url, client.state, seen,
                        client.worker_id or None, received)

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
            self._set_client(None, None)
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
                self._set_client(None, None)
                with self._status_lock:
                    save_status(root, url, state, seen, policy.control_service_worker_id)
            return None
        if enrollment is None:
            # Unpaired (the key is gone) while settings still name the URL.
            seen = self.client.last_seen_at if self.client is not None else None
            self._set_client(None, None)
            with self._status_lock:
                save_status(root, url, "offline", seen, policy.control_service_worker_id)
            return None
        # Enrollment compares by value (a re-pair changes it) but its
        # repr omits the key, so the identity is safe to print.
        identity = (url, enrollment)
        if identity != self.identity:
            self._set_client(None, None)  # never drive a stale enrollment
            client = self.client_factory(self.runtime, url, enrollment.device_key,
                                         worker_id=enrollment.worker_id)
            client.stop_event = self.stop_event
            # RemoteClient installs its standalone guard; keep the
            # configured guard that also rechecks Keychain availability.
            self.runtime.remote_launch_guard = self.launch_allowed
            self._set_client(client, identity)
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
            client.heartbeat_tick()
        except Exception as exc:
            client._connectivity(exc)
        self._save(client)

    def _renew_pass(self):
        """Renew the admitted claim and apply queued cancellations, like ``RemoteClient._renew_loop``;
        cancellations are applied even when nothing needs renewing."""
        client = self.client
        if client is None:
            return
        try:
            if client.state == "online":
                client.renew_admitted()
            client.enforce_cancellations()
        except Exception as exc:
            client._connectivity(exc)

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
        helpers = [threading.Thread(target=self._loop, args=(self._sync_pass,),
                                    name="openswap-worker-remote-sync", daemon=True),
                   threading.Thread(target=self._loop, args=(self._renew_pass,),
                                    name="openswap-worker-remote-renew", daemon=True)]
        for helper in helpers:
            helper.start()
        try:
            due = time.monotonic()
            while not stop_event.is_set():
                try:
                    self._heartbeat_pass()
                except Exception as exc:
                    self._offline(exc)
                # Due on an absolute schedule, like RemoteClient.run.
                due = max(due + HEARTBEAT_SECONDS, time.monotonic())
                stop_event.wait(due - time.monotonic())
        finally:
            for helper in helpers:
                helper.join()

    def _loop(self, one_pass):
        while not self.stop_event.is_set():
            try:
                one_pass()
            except Exception as exc:
                self._offline(exc)
            self.stop_event.wait(HEARTBEAT_SECONDS)
