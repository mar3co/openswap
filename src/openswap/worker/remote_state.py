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
from openswap.worker.pairing import Enrollment, load_enrollment, store_rotated_key


def _identity(url, worker_id):
    # Scoped to the paired worker as well as the URL: a re-pair of the same URL
    # must not inherit the previous enrollment's online/revoked/expired status.
    return hashlib.sha256((url + "\0" + (worker_id or "")).encode()).hexdigest()


STATUS_FILE = ("worker", "remote-status.json")


def save_status(root, url, state, seen, worker_id=None, received=None, *, key_renews=None, key_expires_at=None):
    """``seen`` is the service's timestamp, kept for display; ``received`` is the Mac's
    time for that heartbeat (default ``seen``), which alone decides freshness so a
    skewed service clock cannot make a live connection read as offline.

    ``key_renews`` is whether the registration rotates its key itself (None: not
    known yet) and ``key_expires_at`` the current key's expiry; neither is secret."""
    received = received or seen
    atomic_write_json(root.joinpath(*STATUS_FILE), {
        "service": _identity(url, worker_id), "state": state,
        "last_seen_at": seen.isoformat() if seen else None,
        "received_at": received.isoformat() if received else None,
        "key_renews": key_renews if isinstance(key_renews, bool) else None,
        "key_expires_at": key_expires_at.isoformat() if key_expires_at else None,
    })


def _kept_key(root, url, worker_id) -> dict:
    """The key fields already saved for this enrollment, as ``save_status`` keywords.

    Paths that write a status without a client (an expired or unreadable key, a
    missing Keychain item) keep what the last client learned, so the expiry and
    its guidance stay visible exactly when they matter."""
    try:
        data = json.loads(root.joinpath(*STATUS_FILE).read_text(encoding="utf-8"))
        if data["service"] != _identity(url, worker_id):
            return {}
        renews, expires = data.get("key_renews"), data.get("key_expires_at")
        return {"key_renews": renews if isinstance(renews, bool) else None,
                "key_expires_at": timestamp(expires) if expires else None}
    except (OSError, KeyError, TypeError, ValueError, AttributeError, ProtocolError):
        return {}


def read_key_status(root):
    """``(renews, expires_at)`` for the configured enrollment's key, from the saved
    status: ``renews`` is True (rotates itself), False (the backend lacks rotation)
    or None (unknown). Like ``read_status``: no Keychain, sockets or writes.
    Unlike it, a disabled worker still reports its key: the key keeps aging
    while the worker is off, so its expiry warning must stay visible. A worker the
    owner removed (saved state ``revoked``) reports nothing: its key can never be
    renewed or reconnected, only unpaired."""
    policy = load_worker_settings(root)
    if policy.control_service_url is None:
        return None, None
    try:
        data = json.loads(root.joinpath(*STATUS_FILE).read_text(encoding="utf-8"))
        if data["service"] != _identity(policy.control_service_url, policy.control_service_worker_id):
            return None, None
        if data.get("state") == RemoteConnectivity.REVOKED.value:
            return None, None
        renews = data.get("key_renews")
        expires = data.get("key_expires_at")
        return (renews if isinstance(renews, bool) else None), (timestamp(expires) if expires else None)
    except (OSError, KeyError, TypeError, ValueError, AttributeError, ProtocolError):
        return None, None


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

    A key rotation (``RemoteClient.rotate_key_if_due``) is the one key change
    that keeps the client: ``_store_rotated`` saves the new key to the Keychain,
    then swaps the client's transport key and the identity in one step under
    the client lock. ``_resolve`` and the launch check accept, besides the
    current identity, only the exact key a rotation is saving now and the exact
    key a rotation that ended during their Keychain read replaced. They never
    wait for a rotation, and any other key (an unpair and re-pair, even of the
    same worker) rebuilds the client or refuses the launch.
    """
    def __init__(self, runtime, *, client_factory=RemoteClient, enrollment_loader=load_enrollment,
                 key_saver=store_rotated_key):
        self.runtime = runtime
        self.client_factory, self.enrollment_loader, self.key_saver = client_factory, enrollment_loader, key_saver
        self.client = None
        self.identity = None
        self.stop_event = threading.Event()
        self._status_lock = threading.Lock()  # a status write reflects the state read for it
        self._client_lock = threading.Lock()  # client and identity change together
        # Key rotation bookkeeping, guarded by the client lock: a generation
        # bumped whenever a rotation ends, the identities rotations are saving
        # now, and the one the last adopted rotation replaced.
        self._key_generation = 0
        self._incoming = set()
        self._replaced = None
        runtime.remote_launch_guard = self.launch_allowed

    def launch_allowed(self, job_id):
        if not self.runtime.get(job_id).idempotency_key.startswith("remote:"):
            return True
        with self._client_lock:
            client, generation = self.client, self._key_generation
        if self.stop_event.is_set() or client is None:
            return False
        # Re-read authorization in Keychain before every launch; a removed or
        # locked key cannot rely on a copy cached by a prior heartbeat.
        policy = load_worker_settings(self.runtime.backup_root)
        if policy.control_service_url != client.url:
            return False
        try:
            enrollment = self.enrollment_loader(client.url)
        except ProtocolError:
            return False
        if enrollment is None:
            return False
        # The captured client must still be the active one, and the Keychain must
        # hold its enrollment (see ``_rotation_match``). An unpair and re-pair of
        # the same URL while the loader ran changes the client or the enrollment,
        # and is refused. A rotation never makes a launch wait: it is matched,
        # not waited out.
        with self._client_lock:
            current = self.client is client
            ours = (client.url, enrollment)
            matches = ours == self.identity or self._rotation_match(ours, generation)
        if not current or not matches:
            return False
        return client.launch_allowed(job_id)

    def _rotation_match(self, identity, generation):
        """Whether ``identity``, read from the Keychain while the key generation was
        ``generation``, is a key a rotation in this process accounts for: the one
        it is saving now, or the one a rotation that ended since then replaced.
        Called with the client lock held."""
        return identity in self._incoming or (
            self._key_generation != generation and identity == self._replaced)

    def _store_rotated(self, client, previous_key, worker_id, device_key, expires_at):
        """``key_store`` for ``client``: save a rotated key, then adopt it in place.

        Returns whether the client adopted it. Nothing is adopted when the
        client is no longer the active one, or when ``key_saver`` wrote nothing
        (an unpair or re-pair won) or failed: the client keeps its old key.
        """
        enrollment = Enrollment(worker_id, device_key, expires_at)
        with self._client_lock:
            if self.client is not client or self.stop_event.is_set():
                return False
            self._incoming.add((client.url, enrollment))
        try:
            try:
                saved = self.key_saver(self.runtime.backup_root, client.url, enrollment, previous_key=previous_key)
            except ProtocolError:
                return False
            if not saved:
                return False
            with self._client_lock:
                if self.client is not client:
                    return False  # the next pass builds a client for whatever the Keychain holds
                client.adopt_key(device_key, expires_at)
                self._replaced, self.identity = self.identity, (client.url, enrollment)
            return True
        finally:
            with self._client_lock:
                self._key_generation += 1
                self._incoming.discard((client.url, enrollment))

    def _set_client(self, client, identity):
        with self._client_lock:
            self.client, self.identity, self._replaced = client, identity, None

    def _save(self, client):
        with self._status_lock:
            seen = client.last_seen_at
            # The heartbeat's local send time: the service timestamp less the measured offset.
            received = seen - getattr(client, "_skew", timedelta(0)) if seen else None
            renewal = getattr(client, "key_renewal", None)
            renews = renewal() if callable(renewal) else None
            if renews is None:
                # Not found out yet by this registration (a restart while the
                # service is unreachable): keep what an earlier one learned.
                renews = _kept_key(self.runtime.backup_root, client.url,
                                   client.worker_id or None).get("key_renews")
            save_status(self.runtime.backup_root, client.url, client.state, seen,
                        client.worker_id or None, received, key_renews=renews,
                        key_expires_at=getattr(client, "key_expires_at", None))

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
        with self._client_lock:
            generation = self._key_generation
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
                    save_status(root, url, state, seen, policy.control_service_worker_id,
                                **_kept_key(root, url, policy.control_service_worker_id))
            return None
        if enrollment is None:
            # Unpaired (the key is gone) while settings still name the URL.
            seen = self.client.last_seen_at if self.client is not None else None
            self._set_client(None, None)
            with self._status_lock:
                save_status(root, url, "offline", seen, policy.control_service_worker_id,
                            **_kept_key(root, url, policy.control_service_worker_id))
            return None
        # Enrollment compares by value (a re-pair changes it) but its
        # repr omits the key, so the identity is safe to print.
        identity = (url, enrollment)
        with self._client_lock:
            current = self.client
            same = identity == self.identity
            rotated = current is not None and self._rotation_match(identity, generation)
        if same:
            return current
        if rotated:
            # A key rotation in this process saved a new key around this read:
            # keep the registration, and look again on the next pass.
            return current
        self._set_client(None, None)  # never drive a stale enrollment
        client = self.client_factory(self.runtime, url, enrollment.device_key,
                                     worker_id=enrollment.worker_id)
        client.stop_event = self.stop_event
        # The key's expiry and a durable home for a rotated key: only a
        # configured client rotates its key.
        client.key_expires_at = enrollment.expires_at
        client.key_store = (lambda previous, worker_id, key, expires, client=client:
                            self._store_rotated(client, previous, worker_id, key, expires))
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
