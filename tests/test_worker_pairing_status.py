from datetime import datetime, timedelta, timezone
import json
import threading
import time

import pytest

from tests.test_worker_remote import remote_setup

from openswap import macos_keychain
from openswap.settings import configure_worker_service, load_worker_settings, update_worker_settings
from openswap.worker import cli, pairing
from openswap.worker.models import RemoteConnectivity
from openswap.worker.protocol import ProtocolError
from openswap.worker.remote_state import ConfiguredRemote, read_status, save_status
from openswap.worker.runtime import read_worker_snapshot
from openswap.menubar_display import _remote_tasks_status_copy


@pytest.fixture
def keychain(monkeypatch):
    values = {}
    monkeypatch.setattr(pairing, "require_macos", lambda: None)
    monkeypatch.setattr(macos_keychain, "get_password", lambda service, account: values.get((service, account)))
    monkeypatch.setattr(macos_keychain, "set_password", lambda service, account, value: values.__setitem__((service, account), value))
    monkeypatch.setattr(macos_keychain, "delete_password", lambda service, account: values.pop((service, account), None))
    return values


class PairTransport:
    def request(self, operation, data):
        assert operation == "pair" and data == {"code": "one-use"}
        return {"worker_id": "worker", "device_key": "synthetic-device-key",
                "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}


def test_pair_and_unpair_keychain_only(tmp_path, keychain):
    assert pairing.pair(tmp_path, "http://localhost:8765", "one-use", transport=PairTransport()) == "worker"
    policy = load_worker_settings(tmp_path)
    assert policy.control_service_url == "http://localhost:8765" and not policy.enabled
    assert pairing.load_enrollment(policy.control_service_url).worker_id == "worker"
    assert all(service == "openswap" for service, _ in keychain)
    assert "synthetic-device-key" not in (tmp_path / "settings.json").read_text()
    pairing.unpair(tmp_path)
    assert not keychain and load_worker_settings(tmp_path).control_service_url is None


def test_pair_settings_failure_cleans_key(tmp_path, keychain, monkeypatch):
    monkeypatch.setattr(pairing, "configure_worker_service", lambda *_: (_ for _ in ()).throw(OSError("secret path")))
    with pytest.raises(ProtocolError, match="pairing_settings_unavailable"):
        pairing.pair(tmp_path, "http://localhost", "one-use", transport=PairTransport())
    assert not keychain


def test_pair_requires_unpair_before_changing_backend(tmp_path, keychain):
    pairing.pair(tmp_path, "http://localhost", "one-use", transport=PairTransport())
    with pytest.raises(ProtocolError, match="unpair_before_pairing"):
        pairing.pair(tmp_path, "https://another.example", "one-use", transport=PairTransport())
    assert len(keychain) == 1


def test_locked_keychain_and_expired_enrollment_fail_closed(tmp_path, keychain, monkeypatch):
    url = "http://localhost"
    pairing.pair(tmp_path, url, "one-use", transport=PairTransport())
    saved = json.loads(keychain[("openswap", pairing.account_name(url))])
    saved["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    keychain[("openswap", pairing.account_name(url))] = json.dumps(saved)
    with pytest.raises(ProtocolError, match="device_expired"):
        pairing.load_enrollment(url)
    monkeypatch.setattr(macos_keychain, "get_password", lambda *_: (_ for _ in ()).throw(macos_keychain.KeychainError("secret")))
    with pytest.raises(ProtocolError, match="device_key_unavailable"):
        pairing.load_enrollment(url)


def test_non_macos_explicitly_unsupported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(pairing.sys, "platform", "linux")
    assert cli.main(["pair", "http://localhost", "one-use"], backup_root=tmp_path) == 1
    assert "unsupported" in capsys.readouterr().err
    assert cli.main(["unpair"], backup_root=tmp_path) == 1
    assert not (tmp_path / "settings.json").exists()


def test_pair_cli_does_not_print_key(tmp_path, keychain, monkeypatch, capsys):
    monkeypatch.setattr(pairing, "Transport", lambda *_: PairTransport())
    assert cli.main(["pair", "http://localhost", "one-use"], backup_root=tmp_path) == 0
    output = capsys.readouterr().out
    assert "worker" in output and "synthetic-device-key" not in output
    assert cli.main(["unpair"], backup_root=tmp_path) == 0
    assert not keychain


def test_status_is_read_only_and_remote_time_is_separate(tmp_path, monkeypatch):
    monkeypatch.setattr(macos_keychain, "get_password", lambda *_: pytest.fail("status must not read Keychain"))
    assert read_worker_snapshot(tmp_path).remote_connectivity == RemoteConnectivity.DISABLED
    assert not (tmp_path / "worker").exists()
    update_worker_settings(tmp_path, enabled=True)
    configure_worker_service(tmp_path, "https://control.example")
    assert read_status(tmp_path) == (RemoteConnectivity.OFFLINE, None)
    assert not (tmp_path / "worker").exists()
    seen = datetime.now(timezone.utc)
    save_status(tmp_path, "https://control.example", "online", seen)
    assert read_status(tmp_path, now=seen) == (RemoteConnectivity.ONLINE, seen)
    assert read_status(tmp_path, now=seen + timedelta(seconds=16)) == (RemoteConnectivity.OFFLINE, seen)
    snapshot = read_worker_snapshot(tmp_path)
    assert snapshot.remote_connectivity == RemoteConnectivity.OFFLINE  # no live worker
    assert snapshot.remote_last_seen_at == seen and snapshot.last_seen_at is None
    save_status(tmp_path, "https://control.example", "revoked", seen)
    assert read_worker_snapshot(tmp_path).remote_connectivity == RemoteConnectivity.REVOKED
    configure_worker_service(tmp_path, "https://new.example")
    assert read_status(tmp_path) == (RemoteConnectivity.OFFLINE, None)


def test_corrupt_status_fails_closed(tmp_path):
    update_worker_settings(tmp_path, enabled=True)
    configure_worker_service(tmp_path, "https://control.example")
    worker = tmp_path / "worker"
    worker.mkdir()
    (worker / "remote-status.json").write_text('{broken')
    assert read_status(tmp_path) == (RemoteConnectivity.OFFLINE, None)


def test_cli_and_menu_show_service_connectivity_and_last_seen():
    seen = "2026-09-30T00:00:00Z"
    snapshot = {"enabled": True, "paused": False, "process_state": "running", "provider": {"available": False},
                "remote_connectivity": "revoked", "remote_last_seen_at": seen}
    assert "service: revoked" in cli._format_status(snapshot)
    assert seen in cli._format_status(snapshot)
    menu = _remote_tasks_status_copy(snapshot, enabled=True, paused=False)
    assert "service revoked" in menu and "last seen" in menu


def test_configured_client_defaults_off_without_keychain(tmp_path):
    class Runtime:
        backup_root = tmp_path
    runtime = Runtime()
    configured = ConfiguredRemote(runtime, enrollment_loader=lambda _: pytest.fail("default off must not read a key"))
    configured.tick()
    assert configured.client is None and not (tmp_path / "worker").exists()


def test_configured_client_hot_reload_and_keychain_lock(remote_setup):
    remote, runtime, _, _, _, paired, transport = remote_setup
    enrollment = pairing.Enrollment(paired["worker_id"], paired["device_key"], datetime.now(timezone.utc) + timedelta(days=30))
    locked = [False]
    clients = []
    def load(url):
        if locked[0]:
            raise ProtocolError("device_key_unavailable")
        return enrollment
    def factory(*_, **__):
        clients.append(remote)
        return remote
    configured = ConfiguredRemote(runtime, client_factory=factory, enrollment_loader=load)
    configured.tick()
    epoch = remote.worker_epoch
    assert read_status(runtime.backup_root)[0] == RemoteConnectivity.ONLINE
    locked[0] = True
    configured.tick()
    assert read_status(runtime.backup_root)[0] == RemoteConnectivity.OFFLINE
    locked[0] = False
    configured.tick()
    assert remote.worker_epoch == epoch and len(clients) == 1
    configure_worker_service(runtime.backup_root, None)
    configured.tick()
    assert configured.client is None


def test_repair_same_url_does_not_reuse_prior_enrollment_bindings(remote_setup):
    from tests.test_worker_remote import submit, StoreTransport
    from openswap.worker.remote import RemoteClient
    remote, runtime, _, store, _, _, _ = remote_setup
    submit(remote_setup)
    remote.tick()
    assert remote.journal.pending()
    paired = store.request("pair", {"code": store.issue_code()})
    transport = StoreTransport(store, paired["device_key"])
    repaired = RemoteClient(runtime, remote.url, paired["device_key"], worker_id=paired["worker_id"], transport=transport)
    assert repaired.journal.pending() == []
    assert paired["device_key"] not in json.dumps([remote.journal.service, repaired.journal.service])
    assert repaired.journal.service != remote.journal.service
    result = runtime.reconcile_once()
    assert result.state.value == "failed"  # the old enrollment's queued row cannot launch


def test_blocked_remote_authorization_does_not_hold_control_lock(remote_setup):
    from tests.test_worker_remote import submit
    remote, runtime, adapter, _, _, _, _ = remote_setup
    submit(remote_setup)
    remote.tick()
    local = runtime.store.queue()[0]
    guard_entered, release_guard = threading.Event(), threading.Event()
    def guard(_):
        guard_entered.set()
        assert release_guard.wait(3)
        return True
    runtime.remote_launch_guard = guard
    results = []
    runner = threading.Thread(target=lambda: results.append(runtime.reconcile_once()))
    runner.start()
    assert guard_entered.wait(2)
    # This would wait for the network timeout if the guard held launch_lock.
    assert runtime.stop(local.job_id).accepted
    release_guard.set()
    runner.join(2)
    assert not runner.is_alive()
    assert results[0].state.value == "cancelled" and adapter.starts == 0


def test_orphaned_keychain_item_is_replaced_or_removed(tmp_path, keychain):
    """Settings lost the URL (reset, or pair failed after the Keychain write): recovery must not need a manual Keychain edit."""
    url = "http://localhost:8765"
    account = ("openswap", pairing.account_name(url))
    keychain[account] = json.dumps({"worker_id": "orphan", "device_key": "stale-key",
                                    "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
    # An explicit unpair removes the item although settings name no URL.
    pairing.unpair(tmp_path, url)
    assert not keychain
    keychain[account] = json.dumps({"worker_id": "orphan", "device_key": "stale-key",
                                    "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
    # Re-pairing replaces the orphan instead of refusing forever.
    assert pairing.pair(tmp_path, url, "one-use", transport=PairTransport()) == "worker"
    assert json.loads(keychain[account])["worker_id"] == "worker" and len(keychain) == 1
    assert load_worker_settings(tmp_path).control_service_url == url
    # A live enrollment still needs an unpair first, at the same or another URL.
    with pytest.raises(ProtocolError, match="unpair_before_pairing"):
        pairing.pair(tmp_path, url, "one-use", transport=PairTransport())
    with pytest.raises(ProtocolError, match="unpair_before_pairing"):
        pairing.pair(tmp_path, "https://another.example", "one-use", transport=PairTransport())


def test_explicit_unpair_of_another_url_keeps_the_live_enrollment(tmp_path, keychain):
    pairing.pair(tmp_path, "http://localhost:8765", "one-use", transport=PairTransport())
    other = ("openswap", pairing.account_name("https://old.example"))
    keychain[other] = json.dumps({"worker_id": "old", "device_key": "old-key",
                                  "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
    pairing.unpair(tmp_path, "https://old.example/")
    assert other not in keychain and len(keychain) == 1
    assert load_worker_settings(tmp_path).control_service_url == "http://localhost:8765"
    pairing.unpair(tmp_path, "HTTP://localhost:8765/")  # normalized to the configured origin
    assert not keychain and load_worker_settings(tmp_path).control_service_url is None


def test_unpair_cli_accepts_an_explicit_url(tmp_path, keychain, capsys):
    account = ("openswap", pairing.account_name("http://localhost:8765"))
    keychain[account] = json.dumps({"worker_id": "orphan", "device_key": "stale-key",
                                    "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
    assert cli.main(["unpair", "http://localhost:8765"], backup_root=tmp_path) == 0
    assert not keychain and "stale-key" not in capsys.readouterr().out
    assert cli.main(["unpair", "ftp://nope"], backup_root=tmp_path) == 1
    assert "https_required" in capsys.readouterr().err


def test_enrollment_and_identity_never_print_the_key():
    enrollment = pairing.Enrollment("worker", "synthetic-device-key", datetime.now(timezone.utc))
    assert "synthetic-device-key" not in repr(enrollment) and "synthetic-device-key" not in str((enrollment,))
    assert enrollment.device_key == "synthetic-device-key"

    class Runtime:
        backup_root = None
        remote_launch_guard = None
    configured = ConfiguredRemote(Runtime(), enrollment_loader=lambda _: enrollment)
    configured.identity = ("https://control.example", enrollment)
    assert "synthetic-device-key" not in repr(configured.identity)
    assert configured.identity == ("https://control.example",
                                   pairing.Enrollment("worker", "synthetic-device-key", enrollment.expires_at))
    assert configured.identity != ("https://control.example", pairing.Enrollment("worker", "rotated", enrollment.expires_at))


def test_locally_expired_enrollment_shows_expired_not_revoked(remote_setup):
    remote, runtime, _, _, _, paired, _ = remote_setup
    expired = pairing.Enrollment(paired["worker_id"], paired["device_key"], datetime.now(timezone.utc) - timedelta(seconds=1))

    def load(url):
        if expired.expires_at <= datetime.now(timezone.utc):
            raise ProtocolError("device_expired", 401)
        return expired
    configured = ConfiguredRemote(runtime, client_factory=lambda *_, **__: remote, enrollment_loader=load)
    configured.tick()
    assert read_status(runtime.backup_root)[0] == RemoteConnectivity.EXPIRED
    assert read_worker_snapshot(runtime.backup_root).remote_connectivity == RemoteConnectivity.EXPIRED
    snapshot = {"enabled": True, "paused": False, "process_state": "running", "provider": {"available": False},
                "remote_connectivity": "expired", "remote_last_seen_at": None}
    assert "service: expired" in cli._format_status(snapshot)
    assert "service expired" in _remote_tasks_status_copy(snapshot, enabled=True, paused=False)


def test_service_reported_device_expiry_stops_claims_as_expired(remote_setup):
    remote, _, _, store, ticks, _, transport = remote_setup
    ticks[0] += 31 * 86400
    remote.tick()
    assert remote.state == "expired"
    before = len(transport.calls)
    remote.tick()
    assert len(transport.calls) == before  # no further requests until a new enrollment


def test_pair_and_unpair_normalize_hostname_case(tmp_path, keychain):
    """Pairing, unpairing, status and the journal all hash the normalized origin."""
    pairing.pair(tmp_path, "https://Control.Example", "one-use", transport=PairTransport())
    assert load_worker_settings(tmp_path).control_service_url == "https://control.example"
    assert ("openswap", pairing.account_name("https://control.example")) in keychain
    assert pairing.account_name("HTTPS://Control.Example/") == pairing.account_name("https://control.example")
    seen = datetime.now(timezone.utc)
    save_status(tmp_path, "https://control.example", "online", seen, "worker")
    update_worker_settings(tmp_path, enabled=True)
    assert read_status(tmp_path, now=seen) == (RemoteConnectivity.ONLINE, seen)
    pairing.unpair(tmp_path, "https://control.example")
    assert not keychain and load_worker_settings(tmp_path).control_service_url is None


def _enrollment(paired):
    return pairing.Enrollment(paired["worker_id"], paired["device_key"], datetime.now(timezone.utc) + timedelta(days=30))


def test_configured_run_heartbeats_and_synchronizes_on_two_threads(remote_setup, monkeypatch):
    from tests.test_worker_remote import BlockingTransport, StoreTransport, submit
    from openswap.worker import remote_state
    from openswap.worker.remote import RemoteClient
    remote, runtime, adapter, store, _, paired, _ = remote_setup
    job_id = submit(remote_setup)
    monkeypatch.setattr(remote_state, "HEARTBEAT_SECONDS", 0.05)
    configure_worker_service(runtime.backup_root, remote.url, paired["worker_id"])  # as pairing records it
    enrollment = _enrollment(paired)  # one value: the identity compares by value
    recording = BlockingTransport(StoreTransport(store, paired["device_key"]), set(), threading.Event())
    clients = []

    def factory(runtime, url, key, *, worker_id):
        clients.append(RemoteClient(runtime, url, key, worker_id=worker_id, transport=recording))
        return clients[-1]

    configured = ConfiguredRemote(runtime, client_factory=factory, enrollment_loader=lambda _: enrollment)
    stop = threading.Event()
    thread = threading.Thread(target=configured.run, args=(stop,), daemon=True)  # a failure must not hang pytest
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not runtime.store.queue():
            time.sleep(0.02)
        assert runtime.store.queue(), "the synchronization thread never claimed the job"
        # The heartbeat thread writes the status right after the heartbeat that
        # admitted synchronization; read it at that heartbeat's local receipt time
        # since the shortened cadence also shortens the staleness window.
        status = lambda: read_status(  # noqa: E731
            runtime.backup_root, now=clients[0].last_seen_at - clients[0]._skew)[0]
        while time.monotonic() < deadline and status() != RemoteConnectivity.ONLINE:
            time.sleep(0.02)
        assert status() == RemoteConnectivity.ONLINE
        by_thread = lambda op: {name for o, _, name in recording.stamps if o == op}  # noqa: E731
        assert by_thread("register") == {thread.name} and thread.name in by_thread("heartbeat")
        assert by_thread("poll") == {"openswap-worker-remote-sync"}
        assert runtime.reconcile_once().state.value == "succeeded"
        deadline = time.monotonic() + 15  # each phase gets its own bound on a slow runner
        while time.monotonic() < deadline and clients[0].journal.pending():
            time.sleep(0.02)
        assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "succeeded"
        assert len(clients) == 1 and configured.client is clients[0]
        # Unpairing (the URL is cleared first) ends the client from the heartbeat thread.
        configure_worker_service(runtime.backup_root, None)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and configured.client is not None:
            time.sleep(0.02)
        assert configured.client is None and read_status(runtime.backup_root)[0] == RemoteConnectivity.DISABLED
    finally:
        stop.set()
        thread.join(2)
    assert not thread.is_alive()
    assert not any(t.name == "openswap-worker-remote-sync" and t.is_alive() for t in threading.enumerate())
    assert adapter.starts == 1


def test_status_reflects_a_synchronization_thread_failure(remote_setup):
    remote, runtime, _, _, _, paired, transport = remote_setup
    enrollment = _enrollment(paired)  # one value: the identity compares by value
    configured = ConfiguredRemote(runtime, client_factory=lambda *_, **__: remote, enrollment_loader=lambda _: enrollment)
    configured.tick()
    assert read_status(runtime.backup_root)[0] == RemoteConnectivity.ONLINE
    transport.reject["poll"] = ProtocolError("service_unavailable", 503)
    configured._sync_pass()  # what the second thread runs each cadence
    assert remote.state == "offline" and read_status(runtime.backup_root)[0] == RemoteConnectivity.OFFLINE
    configured._heartbeat_pass()
    assert read_status(runtime.backup_root)[0] == RemoteConnectivity.ONLINE


def test_status_write_failure_keeps_the_registration(remote_setup, monkeypatch):
    """A failed remote-status.json write must not rebuild the client: re-registering
    would interrupt the service-side jobs of the registration it replaces."""
    from openswap.worker import remote_state
    remote, runtime, _, _, _, paired, transport = remote_setup
    enrollment = _enrollment(paired)  # one value: the identity compares by value
    configured = ConfiguredRemote(runtime, client_factory=lambda *_, **__: remote, enrollment_loader=lambda _: enrollment)
    configured.tick()
    epoch, registrations = remote.worker_epoch, transport.calls.count("register")
    monkeypatch.setattr(remote_state, "HEARTBEAT_SECONDS", 0.01)
    failures = []

    def failing_write(*args, **kwargs):
        failures.append(args)
        raise OSError("status file unavailable")

    monkeypatch.setattr(remote_state, "save_status", failing_write)
    stop = threading.Event()
    thread = threading.Thread(target=configured.run, args=(stop,), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(failures) < 4:
            time.sleep(0.01)
    finally:
        stop.set()
        thread.join(2)
    assert not thread.is_alive() and len(failures) >= 4
    assert configured.client is remote and remote.worker_epoch == epoch
    assert transport.calls.count("register") == registrations
    assert remote.state == "offline"  # unwritable status closes the launch fence...
    monkeypatch.undo()
    configured.tick()  # ...until the next successful pass, still without re-registering
    assert read_status(runtime.backup_root)[0] == RemoteConnectivity.ONLINE
    assert transport.calls.count("register") == registrations


def _claimed_row(setup, idem):
    from tests.test_worker_remote import submit
    remote, runtime, _, _, _, _, _ = setup
    submit(setup, idem)
    remote.tick()
    return runtime.store.queue()[0]


def test_unpair_before_the_launch_commit_never_starts(remote_setup, keychain):
    """The guard authorized the launch; unpair landed before the commit point."""
    from openswap.worker.models import RemoteAuthorization
    remote, runtime, adapter, _, _, _, _ = remote_setup
    root, url = runtime.backup_root, remote.url
    keychain[("openswap", pairing.account_name(url))] = "{}"
    local = _claimed_row(remote_setup, "one")

    def guard(job_id):
        assert remote.launch_allowed(job_id) == RemoteAuthorization(url, remote.worker_id)
        pairing.unpair(root)  # clears worker.controlServiceUrl, then deletes the key
        return True

    runtime.remote_launch_guard = guard
    result = runtime.reconcile_once()
    assert (result.state.value, result.diagnostic_code) == ("failed", "worker_disabled")
    assert adapter.starts == 0 and not keychain and load_worker_settings(root).control_service_url is None
    lease = runtime.leases.read_current()
    assert lease.job_id == local.job_id and (lease.state, lease.reason) == ("released", "unlaunched")

    # A token for another enrollment's URL is refused the same way.
    configure_worker_service(root, url)
    local = _claimed_row(remote_setup, "two")
    runtime.remote_launch_guard = lambda _: RemoteAuthorization("https://other.example", "someone")
    result = runtime.reconcile_once()
    assert (result.job_id, result.state.value, result.diagnostic_code) == (local.job_id, "failed", "worker_disabled")
    assert adapter.starts == 0

    # The verified token for the configured URL launches as before.
    local = _claimed_row(remote_setup, "three")
    runtime.remote_launch_guard = remote.launch_allowed
    assert runtime.reconcile_once().state.value == "succeeded" and adapter.starts == 1


@pytest.mark.parametrize("spelling, origin", [
    ("https://Example.com:443", "https://example.com"), ("https://example.com:443/", "https://example.com"),
    ("http://localhost:80", "http://localhost"), ("http://[::1]:80", "http://[::1]"),
    ("https://example.com:", "https://example.com"), ("https://example.com:8443", "https://example.com:8443"),
])
def test_default_ports_hash_to_one_enrollment(spelling, origin):
    from openswap.worker.protocol import validate_url
    assert validate_url(spelling) == origin
    assert pairing.account_name(spelling) == pairing.account_name(origin)


def test_unpair_with_the_default_port_spelled_out(tmp_path, keychain):
    pairing.pair(tmp_path, "https://control.example", "one-use", transport=PairTransport())
    assert pairing.unpair(tmp_path, "https://control.example:443") is True
    assert not keychain and load_worker_settings(tmp_path).control_service_url is None


def test_pairing_records_the_worker_id_and_unpair_clears_it(tmp_path, keychain):
    pairing.pair(tmp_path, "http://localhost:8765", "one-use", transport=PairTransport())
    assert load_worker_settings(tmp_path).control_service_worker_id == "worker"
    pairing.unpair(tmp_path)
    assert load_worker_settings(tmp_path).control_service_worker_id is None
    assert "controlServiceWorkerId" not in (tmp_path / "settings.json").read_text()


def test_repair_of_the_same_url_refuses_the_old_enrollments_authorization(remote_setup):
    """Unpair then re-pair the same URL between the guard and the commit point."""
    from openswap.worker.models import RemoteAuthorization
    remote, runtime, adapter, _, _, _, _ = remote_setup
    configure_worker_service(runtime.backup_root, remote.url, "old-worker")
    local = _claimed_row(remote_setup, "one")

    def guard(job_id):
        configure_worker_service(runtime.backup_root, remote.url, "new-worker")  # the re-pair
        return RemoteAuthorization(remote.url, "old-worker")

    runtime.remote_launch_guard = guard
    result = runtime.reconcile_once()
    assert (result.job_id, result.state.value, result.diagnostic_code) == (local.job_id, "failed", "worker_disabled")
    assert adapter.starts == 0


def test_configured_guard_refuses_a_client_replaced_during_the_keychain_read(remote_setup):
    remote, runtime, adapter, store, _, paired, _ = remote_setup
    old = _enrollment(paired)
    configured = ConfiguredRemote(runtime, client_factory=lambda *_, **__: remote, enrollment_loader=lambda _: old)
    configured.tick()
    local = _claimed_row(remote_setup, "one")
    replacement = pairing.Enrollment("new-worker", "new-key", old.expires_at)

    def repaired(url):
        # Unpair and re-pair land while the guard waits on the Keychain: the heartbeat
        # thread installs a new client for the new enrollment.
        configured._set_client(object(), (url, replacement))
        return replacement

    configured.enrollment_loader = repaired
    assert configured.launch_allowed(local.job_id) is False


def test_unpair_cli_reports_when_the_configured_service_stays_active(tmp_path, keychain, capsys):
    pairing.pair(tmp_path, "http://localhost:8765", "one-use", transport=PairTransport())
    keychain[("openswap", pairing.account_name("https://old.example"))] = "{}"
    assert cli.main(["unpair", "https://old.example"], backup_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "remote access disabled" not in out and "unchanged" in out
    assert load_worker_settings(tmp_path).control_service_url == "http://localhost:8765"
    assert cli.main(["unpair"], backup_root=tmp_path) == 0
    assert "remote access disabled" in capsys.readouterr().out


def test_repair_of_the_same_url_does_not_inherit_the_old_status(tmp_path, keychain):
    pairing.pair(tmp_path, "https://control.example", "one-use", transport=PairTransport())
    update_worker_settings(tmp_path, enabled=True)
    seen = datetime.now(timezone.utc)
    save_status(tmp_path, "https://control.example", "revoked", seen, "worker")
    assert read_status(tmp_path, now=seen)[0] == RemoteConnectivity.REVOKED
    pairing.unpair(tmp_path)

    class Replacement:
        def request(self, operation, data):
            return {"worker_id": "replacement", "device_key": "another-key",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()}
    pairing.pair(tmp_path, "https://control.example", "one-use", transport=Replacement())
    assert read_status(tmp_path, now=seen) == (RemoteConnectivity.OFFLINE, None)


def test_pairing_transactions_are_serialized(tmp_path, keychain):
    entered, release, order = threading.Event(), threading.Event(), []

    class Slow:
        def request(self, operation, data):
            order.append("first-network")
            entered.set()
            release.wait(5)
            return PairTransport().request(operation, data)
    first = threading.Thread(target=pairing.pair, args=(tmp_path, "http://localhost:8765", "one-use"),
                             kwargs={"transport": Slow()})
    first.start()
    assert entered.wait(5)
    second = threading.Thread(target=lambda: order.append(("unpair", pairing.unpair(tmp_path))))
    second.start()
    time.sleep(0.3)
    assert order == ["first-network"]  # unpair waits for the whole pairing transaction
    release.set()
    first.join(5)
    second.join(5)
    assert order == ["first-network", ("unpair", True)]
    assert not keychain and load_worker_settings(tmp_path).control_service_url is None


def test_busy_pairing_lock_is_reported(tmp_path, keychain, monkeypatch):
    from openswap.locking import FileLock
    monkeypatch.setattr(pairing, "_pairing_lock", lambda root: FileLock(root / ".worker-pairing.lock", timeout=0.1))
    with FileLock(tmp_path / ".worker-pairing.lock"):
        with pytest.raises(ProtocolError, match="pairing_in_progress"):
            pairing.unpair(tmp_path)


def test_unpair_migrates_legacy_backup_before_writing(temp_home, keychain, monkeypatch, capsys):
    legacy = temp_home / ".claude-swap-backup"
    legacy.mkdir()
    (legacy / "accounts.json").write_text('{"kept":true}', encoding="utf-8")
    target = temp_home / "Library" / "Application Support" / "OpenSwap"
    monkeypatch.setattr(cli.paths, "get_legacy_backup_root", lambda: legacy)
    assert cli.main(["unpair", "https://old.example"], backup_root=target) == 0
    assert not legacy.exists()
    assert (target / "accounts.json").read_text(encoding="utf-8") == '{"kept":true}'


def test_configured_renewal_loop_drains_cancellations(remote_setup):
    remote, runtime, adapter, store, _, paired, _ = remote_setup
    enrollment = _enrollment(paired)
    configured = ConfiguredRemote(runtime, client_factory=lambda *_, **__: remote, enrollment_loader=lambda _: enrollment)
    configured.tick()
    local = _claimed_row(remote_setup, "one")
    remote._cancels.add(remote.journal.binding(local.job_id)["remote_id"])  # queued by a heartbeat
    remote.state = "offline"  # nothing to renew: cancellations are applied anyway
    configured._renew_pass()
    assert runtime.store.get(local.job_id).state.value in {"cancelled", "cancel_requested"}


def test_status_freshness_uses_local_receipt_time_under_clock_skew(tmp_path):
    configure_worker_service(tmp_path, "https://control.example", "worker")
    update_worker_settings(tmp_path, enabled=True)
    now = datetime.now(timezone.utc)
    service_time = now + timedelta(minutes=10)  # the service clock runs ten minutes ahead
    save_status(tmp_path, "https://control.example", "online", service_time, "worker", now)
    assert read_status(tmp_path, now=now) == (RemoteConnectivity.ONLINE, service_time)
    assert read_status(tmp_path, now=now + timedelta(seconds=20))[0] == RemoteConnectivity.OFFLINE


def test_configured_status_stays_online_when_the_service_clock_is_ahead(remote_setup):
    remote, runtime, _, _, ticks, paired, _ = remote_setup
    configure_worker_service(runtime.backup_root, remote.url, paired["worker_id"])
    enrollment = _enrollment(paired)
    configured = ConfiguredRemote(runtime, client_factory=lambda *_, **__: remote, enrollment_loader=lambda _: enrollment)
    remote.worker_id = paired["worker_id"]
    ticks[0] += 600  # service ten minutes ahead of the Mac
    configured.tick()
    assert read_status(runtime.backup_root)[0] == RemoteConnectivity.ONLINE


def test_renewal_pairing_drops_the_old_keys_status(tmp_path, keychain):
    pairing.pair(tmp_path, "https://control.example", "one-use", transport=PairTransport())
    update_worker_settings(tmp_path, enabled=True)
    seen = datetime.now(timezone.utc)
    save_status(tmp_path, "https://control.example", "expired", seen, "worker")
    assert read_status(tmp_path, now=seen)[0] == RemoteConnectivity.EXPIRED
    pairing.unpair(tmp_path)
    pairing.pair(tmp_path, "https://control.example", "one-use", transport=PairTransport())  # same worker ID
    assert read_status(tmp_path, now=seen) == (RemoteConnectivity.OFFLINE, None)


def test_pairing_that_changes_nothing_keeps_the_configured_status(tmp_path, keychain):
    pairing.pair(tmp_path, "https://control.example", "one-use", transport=PairTransport())
    update_worker_settings(tmp_path, enabled=True)
    seen = datetime.now(timezone.utc)
    save_status(tmp_path, "https://control.example", "revoked", seen, "worker")
    with pytest.raises(ProtocolError, match="unpair_before_pairing"):
        pairing.pair(tmp_path, "https://control.example", "one-use", transport=PairTransport())
    assert read_status(tmp_path, now=seen) == (RemoteConnectivity.REVOKED, seen)
    keychain[("openswap", pairing.account_name("https://old.example"))] = "orphan"
    assert pairing.unpair(tmp_path, "https://old.example") is False  # an unrelated orphan only
    assert read_status(tmp_path, now=seen) == (RemoteConnectivity.REVOKED, seen)
    pairing.unpair(tmp_path)
    assert not (tmp_path / "worker" / "remote-status.json").exists()

