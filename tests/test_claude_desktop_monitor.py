"""Default discovery and automatic identity must preserve logins and user edits."""

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap.claude_desktop import ClaudeDesktopError, DesktopProfiles
from openswap.claude_desktop_identity import AccountDetector, KeyAccessError
from openswap.claude_desktop_menu import ClaudeDesktopMenu
from openswap.claude_desktop_monitor import ProfileIdentityMonitor, REFRESH_INTERVAL

STAMP = "2026-10-26T20:00:00+00:00"
DETAILS = {"email": "same@example.test", "organization": "Acme", "detected_at": STAMP}
ACCOUNT = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
LOGIN, NEW_LOGIN = "a" * 64, "b" * 64
IDENTITY = {**DETAILS, "account_uuid": ACCOUNT, "organization_uuid": ORG, "detected_login": LOGIN}


@pytest.fixture
def store(tmp_path):
    return DesktopProfiles(tmp_path / "profiles", default_desktop=tmp_path / "existing-Claude")


def existing(store):
    store.default_desktop.mkdir(mode=0o755)
    (store.default_desktop / "config.json").write_text('{"test": "kept"}')


def test_default_is_discovered_once_without_copying_or_changing_claude_data(store):
    assert store.ensure_default() is None and not store.root.exists()
    existing(store)
    before = store.default_desktop.stat().st_mode
    profile = store.ensure_default()
    assert profile.name == "Default" and profile.kind == "default" and profile.auto_detect
    assert store.paths(profile) == (store.default_desktop, None)
    assert not (store.root / profile.id).exists()
    assert store.default_desktop.stat().st_mode == before
    assert (store.default_desktop / "config.json").read_text() == '{"test": "kept"}'
    saved = store.registry.read_bytes()
    assert store.ensure_default() == profile and len(store.list()) == 1
    assert store.registry.read_bytes() == saved


def test_default_archive_stays_archived_until_explicit_restore(store):
    existing(store)
    p = store.ensure_default()
    store.update(p.id, name="Main", **DETAILS)
    archived = store.update(p.id, archived=True, auto_detect=False)
    assert store.ensure_default() == archived and store.list() == []
    restored = store.update(p.id, archived=False)
    assert restored.id == p.id and restored.name == "Main" and restored.email == DETAILS["email"]
    assert not restored.auto_detect
    assert store.default_desktop.is_dir() and len(store.list()) == 1


def test_default_label_collision_does_not_alias_an_isolated_profile(store):
    existing(store)
    isolated = store.add("Default")
    default = store.ensure_default()
    assert default.name == "Default (2)" and default.id != isolated.id
    assert store.paths(isolated)[0] != store.paths(default)[0]


def test_missing_or_linked_default_is_never_recreated_or_followed(store, tmp_path):
    assert store.ensure_default() is None
    assert not store.root.exists() and not store.default_desktop.exists()
    actual = tmp_path / "elsewhere"
    actual.mkdir()
    store.default_desktop.symlink_to(actual)
    with pytest.raises(ClaudeDesktopError): store.ensure_default()
    assert list(actual.iterdir()) == []


def test_multiple_default_references_fail_closed(store):
    existing(store)
    store.ensure_default()
    raw = json.loads(store.registry.read_text())
    raw["profiles"].append({**raw["profiles"][0], "id": "f" * 32, "name": "Second"})
    store.registry.write_text(json.dumps(raw))
    with pytest.raises(ClaudeDesktopError): store.list()


def menu(store, answers):
    monitor = Mock(statuses={})
    controller = ClaudeDesktopMenu(store=store, prompt=Mock(side_effect=answers), show_error=Mock(),
                                   changed=Mock(), notify=Mock(), monitor=monitor)
    controller.open = Mock()
    return controller


def test_showing_desktop_discovers_default_first_without_opening_or_prompting(store):
    existing(store)
    extra = store.add("Work")
    controller = menu(store, [])
    state = controller.settings_state()
    default = store.ensure_default()
    assert [p["id"] for p in state["items"]] == [default.id, extra.id]
    controller.prompt.assert_not_called()
    controller.open.assert_not_called()
    controller.monitor.detector.assert_not_called()
    saved = store.registry.read_bytes()
    controller.settings_state()
    assert store.registry.read_bytes() == saved


def test_default_discovery_waits_for_enabled_feature_and_existing_directory(store):
    controller = menu(store, [])
    controller.enabled = lambda: False
    existing(store)
    assert controller.settings_state()["items"] == [] and not store.root.exists()
    controller.enabled = lambda: True
    assert controller.settings_state()["items"][0]["kind"] == "default"


def test_default_appears_when_claude_is_first_used_later(store):
    controller = menu(store, [])
    assert controller.settings_state()["items"] == []
    existing(store)
    assert controller.settings_state()["items"][0]["name"] == "Default"


def test_add_profile_always_creates_additional_empty_storage_in_one_step(store):
    existing(store)
    controller = menu(store, [SimpleNamespace(clicked=1, text="Client", checked=True)])
    controller.guide_seen = lambda: True
    default = controller.settings_state()["items"][0]
    controller.add()
    p = store.get("Client")
    assert p.kind == "managed" and p.auto_detect and p.id != default["id"]
    assert list(store.paths(p)[0].iterdir()) == []
    controller.open.assert_called_once_with(p.id, setup_shortcuts=True)
    assert controller.prompt.call_count == 1
    assert "choices" not in controller.prompt.call_args.kwargs
    assert "add it to Claude Code" in controller.prompt.call_args.kwargs["message"]
    assert "Keychain access" in controller.prompt.call_args.kwargs["message"]
    assert "Allow OpenSwap to open Claude links" in controller.prompt.call_args.kwargs["message"]


def test_cancel_add_keeps_default_and_does_not_create_another_profile(store):
    existing(store)
    controller = menu(store, [SimpleNamespace(clicked=0)])
    controller.guide_seen = lambda: True
    default = controller.settings_state()["items"][0]
    saved = store.registry.read_bytes()
    controller.add()
    assert store.registry.read_bytes() == saved
    assert len(store.list()) == 1 and store.list()[0].id == default["id"]
    assert (store.default_desktop / "config.json").read_text() == '{"test": "kept"}'
    controller.open.assert_not_called()


def test_unreadable_default_does_not_block_additional_profiles(store, tmp_path):
    actual = tmp_path / "elsewhere"
    actual.mkdir()
    store.default_desktop.symlink_to(actual)
    controller = menu(store, [SimpleNamespace(clicked=1, text="Work", checked=True)])
    controller.guide_seen = lambda: True
    state = controller.settings_state()
    assert state["default_error"] and "storage_error" not in state
    controller.add()
    assert store.get("Work").kind == "managed" and list(actual.iterdir()) == []


def test_renamed_archived_default_is_not_duplicated_or_unarchived_by_ui(store):
    existing(store)
    p = store.ensure_default()
    store.update(p.id, name="Home", archived=True, auto_detect=False, **DETAILS)
    controller = menu(store, [])
    state = controller.settings_state()
    assert len(state["items"]) == 1 and state["archived"] == 1
    assert state["items"][0]["name"] == "Home" and not state["items"][0]["auto_detect"]
    assert state["items"][0]["email"] == DETAILS["email"]


@pytest.fixture
def watcher(store, monkeypatch):
    from openswap import claude_desktop_monitor as module
    monkeypatch.setattr(module.threading, "Thread", lambda *, target, **_: SimpleNamespace(start=target))
    clock = [0.0]
    fingerprint = {"value": "session-1", "stamp": 1}
    detector = Mock(return_value=dict(DETAILS), denied=False)
    monitor = ProfileIdentityMonitor(store, detector=detector, clock=lambda: clock[0],
                                     observe=lambda *_: fingerprint["value"], stamp=lambda *_: fingerprint["stamp"])
    p = store.add("Work")
    return monitor, store, p, clock, fingerprint, detector


def cycle(watcher, seconds=11):
    monitor, _, _, clock, _, _ = watcher
    clock[0] += seconds
    monitor.poll(enabled=True)
    return monitor.poll(enabled=True)


def test_new_login_fills_details_automatically_without_renaming(watcher):
    monitor, store, p, _, state, detector = watcher
    state["value"] = ""
    cycle(watcher)
    detector.assert_not_called()
    assert monitor.statuses[p.id] == "Waiting for sign-in"
    state.update(value="session-1", stamp=2)
    assert cycle(watcher)
    saved = store.get(p.id)
    assert saved.name == "Work" and saved.email == DETAILS["email"] and saved.organization == "Acme"
    assert saved.detected_at == STAMP and monitor.statuses[p.id] == ""


def test_same_email_org_change_and_logout_update_only_labels(watcher):
    monitor, store, p, _, state, detector = watcher
    cycle(watcher)
    detector.return_value = {**DETAILS, "organization": "Second org"}
    state.update(value="session-2", stamp=2)
    cycle(watcher)
    assert store.get(p.id).organization == "Second org"
    state.update(value="", stamp=3)
    cycle(watcher)
    saved = store.get(p.id)
    assert saved.email == saved.organization == saved.detected_at == ""
    assert saved.name == "Work" and saved.id == p.id
    assert monitor.statuses[p.id] == "Waiting for sign-in"


def test_unchanged_login_and_unrelated_cookie_writes_do_not_repeat_requests(watcher):
    _, _, _, _, state, detector = watcher
    cycle(watcher)
    cycle(watcher)
    state["stamp"] += 1
    cycle(watcher)
    assert detector.call_count == 1
    cycle(watcher, REFRESH_INTERVAL)
    assert detector.call_count == 2


def test_failure_backoff_and_changed_login_never_keep_wrong_detected_labels(watcher):
    monitor, store, p, _, state, detector = watcher
    cycle(watcher)
    detector.side_effect = RuntimeError("private response")
    state.update(value="session-2", stamp=2)
    cycle(watcher)
    assert store.get(p.id).email == "" and monitor.statuses[p.id] == "Account details unavailable"
    cycle(watcher)
    assert detector.call_count == 2
    detector.side_effect = None
    cycle(watcher, 61)
    assert store.get(p.id).email == DETAILS["email"]


def test_keychain_denial_pauses_requests_until_explicit_retry(watcher):
    monitor, _, p, _, state, detector = watcher
    def denied(*args, **kwargs):
        detector.denied = True
        raise KeyAccessError("Denied")
    detector.side_effect = denied
    cycle(watcher)
    assert monitor.statuses[p.id] == "Account detection paused"
    state.update(value="other", stamp=2)
    cycle(watcher, 100)
    cycle(watcher, 100)
    assert detector.call_count == 1
    detector.denied = False
    detector.side_effect = None
    monitor.refresh(p.id)
    cycle(watcher)
    assert detector.call_count == 2


@pytest.mark.parametrize("change", ["archived", "manual", "disabled", "files"])
def test_late_results_cannot_overwrite_archives_manual_edits_or_changed_logins(watcher, change):
    monitor, store, p, _, state, _ = watcher
    monitor.poll(enabled=True)  # worker result queued, not applied
    if change == "archived": store.update(p.id, archived=True)
    if change == "manual": store.update(p.id, email="manual@example.test", auto_detect=False)
    if change == "files": state.update(value="other", stamp=2)
    monitor.poll(enabled=change != "disabled")
    assert store.get(p.id, include_archived=True).email == ("manual@example.test" if change == "manual" else "")


@pytest.mark.parametrize("gate", ["disabled", "busy", "archived", "manual"])
def test_background_gates_do_not_read_signins(watcher, gate):
    monitor, store, p, _, _, detector = watcher
    monitor.observe = Mock(side_effect=AssertionError("read a gated profile"))
    if gate == "archived": store.update(p.id, archived=True)
    if gate == "manual": store.update(p.id, auto_detect=False)
    monitor.poll(enabled=gate != "disabled", idle=gate != "busy")
    monitor.poll(enabled=gate != "disabled", idle=gate != "busy")
    monitor.observe.assert_not_called()
    detector.assert_not_called()


def test_unreadable_profile_surfaces_status_without_overwriting_details(watcher):
    monitor, store, p, _, _, detector = watcher
    monitor.stamp = Mock(side_effect=OSError("unreadable"))
    cycle(watcher)
    assert monitor.statuses[p.id] == "Account details unavailable"
    assert store.get(p.id).email == ""
    detector.assert_not_called()


def persisted_watcher(store, monkeypatch, details):
    """A monitor with no memory of a profile whose identity is already on disk."""
    from openswap import claude_desktop_monitor as module
    monkeypatch.setattr(module.threading, "Thread", lambda *, target, **_: SimpleNamespace(start=target))
    p = store.update(store.add("Work").id, **details)
    clock, login = [0.0], {"value": LOGIN}
    detector = Mock(denied=False, side_effect=lambda store, _, expected_fingerprint: {**IDENTITY, "detected_login": expected_fingerprint})
    monitor = ProfileIdentityMonitor(store, detector=detector, clock=lambda: clock[0],
                                     observe=lambda *_: login["value"], stamp=lambda *_: 1)
    return monitor, store, p, clock, login, detector


def fail_detection(detector, failure):
    if failure == "denied":
        detector.denied = True
    else:
        detector.side_effect = {"error": RuntimeError("offline"), "keychain": KeyAccessError("Denied")}[failure]


@pytest.mark.parametrize("failure", ["error", "keychain", "denied"])
@pytest.mark.parametrize("memory", ["restart", "refresh"])
def test_failed_check_keeps_identity_until_the_persisted_login_changes(store, monkeypatch, failure, memory):
    watcher = persisted_watcher(store, monkeypatch, IDENTITY)
    monitor, store, p, _, login, detector = watcher
    if memory == "refresh":
        cycle(watcher)
        monitor.refresh()
    success = detector.side_effect
    fail_detection(detector, failure)
    cycle(watcher, 100)
    saved = store.get(p.id)
    assert (saved.email, saved.account_uuid, saved.detected_login) == (DETAILS["email"], ACCOUNT, LOGIN)
    assert monitor.statuses[p.id] == ("Account details unavailable" if failure == "error" else "Account detection paused")
    login["value"] = NEW_LOGIN
    cycle(watcher, 100)
    saved = store.get(p.id)
    assert saved.email == saved.account_uuid == saved.detected_login == ""
    detector.denied, detector.side_effect = False, success
    cycle(watcher, 100)
    assert store.get(p.id).detected_login == NEW_LOGIN


def test_labels_without_a_persisted_login_do_not_survive_a_failed_check(store, monkeypatch):
    watcher = persisted_watcher(store, monkeypatch, DETAILS)
    _, store, p, _, _, detector = watcher
    fail_detection(detector, "error")
    cycle(watcher)
    assert store.get(p.id).email == ""


def test_detector_caches_key_and_denial_without_repeated_prompts(monkeypatch):
    from openswap import claude_desktop_identity as module
    key = Mock(return_value=b"k" * 16)
    monkeypatch.setattr(module, "_key", key)
    detector = AccountDetector()
    assert detector._get_key() == detector._get_key() == b"k" * 16
    assert key.call_count == 1
    detector.clear()
    key.side_effect = KeyAccessError("Denied")
    for _ in range(2):
        with pytest.raises(KeyAccessError): detector._get_key()
    assert key.call_count == 2
    detector.retry()
    key.side_effect = None
    assert detector._get_key() == b"k" * 16 and key.call_count == 3


def test_denial_survives_restart_feature_toggle_and_adding_profiles(store, monkeypatch):
    from openswap import claude_desktop_identity as module
    key = Mock(side_effect=KeyAccessError("Denied"))
    monkeypatch.setattr(module, "_key", key)
    first = AccountDetector(store)
    with pytest.raises(KeyAccessError): first._get_key()
    assert store.keychain_paused()
    if os.name != "nt":
        assert (store.root / "keychain-paused").stat().st_mode & 0o777 == 0o600
    store.add("Another profile")
    first.clear()
    restarted = AccountDetector(store)
    for detector in (first, restarted):
        with pytest.raises(KeyAccessError): detector._get_key()
    assert key.call_count == 1
    restarted.retry()
    key.side_effect = None
    key.return_value = b"k" * 16
    assert restarted._get_key() == b"k" * 16
    assert not store.keychain_paused() and not AccountDetector(store).denied
    assert key.call_count == 2


@pytest.mark.parametrize("granted", [True, False])
def test_feature_toggle_during_prompt_persists_a_pause_only_for_a_real_denial(store, monkeypatch, granted):
    from openswap import claude_desktop_identity as module
    detector = AccountDetector(store)
    prompts = []
    def key():
        assert store.keychain_paused()  # durable before the system dialog, so a quit mid-dialog stays paused
        prompts.append(None)
        if len(prompts) == 1:
            detector.clear()
        if not granted:
            raise KeyAccessError("Denied")
        return b"k" * 16
    monkeypatch.setattr(module, "_key", key)
    with pytest.raises(KeyAccessError): detector._get_key()
    assert detector._cached_key is None
    assert store.keychain_paused() is detector.denied is AccountDetector(store).denied is (not granted)
    if granted:
        assert detector._get_key() == b"k" * 16 and len(prompts) == 2


def test_pause_marker_uses_exclusive_create_when_nofollow_is_unavailable(store, monkeypatch):
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    store.pause_keychain(True)
    assert store.keychain_paused()
    store.pause_keychain(False)
    assert not store.keychain_paused()


def test_unreadable_pause_marker_cannot_trigger_prompt(store, tmp_path, monkeypatch):
    from openswap import claude_desktop_identity as module
    store.add("Work")
    target = tmp_path / "elsewhere"
    target.touch()
    (store.root / "keychain-paused").symlink_to(target)
    key = Mock()
    monkeypatch.setattr(module, "_key", key)
    detector = AccountDetector(store)
    assert detector.denied
    with pytest.raises(KeyAccessError): detector._get_key()
    with pytest.raises(ClaudeDesktopError): detector.retry()
    key.assert_not_called()


def test_suspending_during_permission_prompt_discards_late_key(monkeypatch):
    from openswap import claude_desktop_identity as module
    detector = AccountDetector()
    def key():
        detector.clear()  # must not acquire the held key lock or block the UI
        return b"k" * 16
    monkeypatch.setattr(module, "_key", key)
    with pytest.raises(module.DetectionError): detector._get_key()
    assert detector._cached_key is None
