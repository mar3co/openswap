from datetime import datetime, timedelta, timezone
import json
import threading

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
