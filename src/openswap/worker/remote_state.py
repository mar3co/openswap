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
    """Reload the URL/key on each poll; unpair prevents new work immediately."""
    def __init__(self, runtime, *, client_factory=RemoteClient, enrollment_loader=load_enrollment):
        self.runtime = runtime
        self.client_factory, self.enrollment_loader = client_factory, enrollment_loader
        self.client = None
        self.identity = None
        self.stop_event = threading.Event()
        runtime.remote_launch_guard = self.launch_allowed

    def launch_allowed(self, job_id):
        if not self.runtime.get(job_id).idempotency_key.startswith("remote:"):
            return True
        if self.stop_event.is_set() or self.client is None:
            return False
        # Re-read authorization in Keychain before every launch; a removed or
        # locked key cannot rely on a copy cached by a prior heartbeat.
        policy = load_worker_settings(self.runtime.backup_root)
        if policy.control_service_url != self.client.url:
            return False
        try:
            enrollment = self.enrollment_loader(self.client.url)
            if enrollment is None or self.identity != (self.client.url, enrollment.worker_id, enrollment.device_key):
                return False
        except ProtocolError:
            return False
        return self.client.launch_allowed(job_id)

    def tick(self):
        root = self.runtime.backup_root
        policy = load_worker_settings(root)
        url = policy.control_service_url
        if self.stop_event.is_set():
            return
        if not policy.enabled or url is None:
            self.client, self.identity = None, None
            return
        try:
            enrollment = self.enrollment_loader(url)
            if enrollment is None:
                raise ProtocolError("device_key_unavailable")
            identity = (url, enrollment.worker_id, enrollment.device_key)
            if identity != self.identity:
                self.client = self.client_factory(self.runtime, url, enrollment.device_key)
                self.identity = identity
                self.client.stop_event = self.stop_event
                # RemoteClient installs its standalone guard; keep the
                # configured guard that also rechecks Keychain availability.
                self.runtime.remote_launch_guard = self.launch_allowed
            self.client.tick()
            save_status(root, url, self.client.state, self.client.last_seen_at)
        except ProtocolError as exc:
            state = "revoked" if exc.code in {"revoked", "device_expired", "unauthorized"} else "offline"
            seen = self.client.last_seen_at if self.client else None
            if self.client is not None and self.client.url == url:
                self.client.state = state
            else:
                self.client, self.identity = None, None
            save_status(root, url, state, seen)

    def run(self, stop_event):
        self.stop_event = stop_event
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # A locked/invalid local policy must not turn a transport
                # thread failure into permission to start cached remote jobs.
                self.client, self.identity = None, None
            stop_event.wait(HEARTBEAT_SECONDS)
