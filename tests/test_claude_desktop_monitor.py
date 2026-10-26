"""Default adoption and automatic identity must preserve logins and user edits."""

import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap.claude_desktop import ClaudeDesktopError, DesktopProfiles
from openswap.claude_desktop_identity import AccountDetector, KeyAccessError
from openswap.claude_desktop_menu import ClaudeDesktopMenu
from openswap.claude_desktop_monitor import ProfileIdentityMonitor, REFRESH_INTERVAL

STAMP = "2026-10-26T20:00:00+00:00"
DETAILS = {"email": "same@example.test", "organization": "Acme", "detected_at": STAMP}


@pytest.fixture
def store(tmp_path):
    return DesktopProfiles(tmp_path / "profiles", default_desktop=tmp_path / "existing-Claude")


def existing(store):
    store.default_desktop.mkdir(mode=0o755)
    (store.default_desktop / "config.json").write_text('{"test": "kept"}')


def test_default_is_offered_but_only_added_explicitly(store):
    assert not store.default_available()
    existing(store)
    before = store.default_desktop.stat().st_mode
    assert store.default_available() and not store.root.exists()
    profile = store.add_default()
    assert profile.name == "Default" and profile.kind == "default" and profile.auto_detect
    assert store.paths(profile) == (store.default_desktop, None)
    assert not (store.root / profile.id).exists()
    assert store.default_desktop.stat().st_mode == before
    assert (store.default_desktop / "config.json").read_text() == '{"test": "kept"}'
    assert not store.default_available()
    assert store.add_default() == profile and len(store.list()) == 1


def test_default_archive_and_readoption_keep_id_details_and_files(store):
    existing(store)
    p = store.add_default()
    store.update(p.id, name="Main", **DETAILS)
    store.update(p.id, archived=True)
    assert store.default_available()
    restored = store.add_default()
    assert restored.id == p.id and restored.name == "Main" and restored.email == DETAILS["email"]
    assert store.default_desktop.is_dir() and len(store.list()) == 1


def test_default_label_collision_does_not_alias_an_isolated_profile(store):
    existing(store)
    isolated = store.add("Default")
    default = store.add_default()
    assert default.name == "Default (2)" and default.id != isolated.id
    assert store.paths(isolated)[0] != store.paths(default)[0]


def test_missing_or_linked_default_is_never_recreated_or_followed(store, tmp_path):
    with pytest.raises(ClaudeDesktopError): store.add_default()
    assert not store.root.exists() and not store.default_desktop.exists()
    actual = tmp_path / "elsewhere"
    actual.mkdir()
    store.default_desktop.symlink_to(actual)
    with pytest.raises(ClaudeDesktopError): store.default_available()
    with pytest.raises(ClaudeDesktopError): store.add_default()
    assert list(actual.iterdir()) == []


def test_multiple_default_references_fail_closed(store):
    existing(store)
    store.add_default()
    raw = json.loads(store.registry.read_text())
    raw["profiles"].append({**raw["profiles"][0], "id": "f" * 32, "name": "Second"})
    store.registry.write_text(json.dumps(raw))
    with pytest.raises(ClaudeDesktopError): store.list()


@pytest.mark.parametrize("manual", [True, False])
def test_v4_migration_preserves_manual_details_and_existing_ids(store, manual):
    p = store.add("Work")
    row = asdict(p)
    row.pop("kind")
    row.pop("auto_detect")
    row.update(DETAILS)
    if manual: row["detected_at"] = ""
    store.registry.write_text(json.dumps({"version": 4, "profiles": [row]}))
    loaded = store.get(p.id)
    assert loaded.kind == "managed" and loaded.auto_detect == (not manual)
    store.update(p.id, name="Client")
    assert store.get(p.id).email == DETAILS["email"]
    assert json.loads(store.registry.read_text())["version"] == 5


def menu(store, answers):
    monitor = Mock(statuses={})
    controller = ClaudeDesktopMenu(store=store, prompt=Mock(side_effect=answers), show_error=Mock(),
                                   changed=Mock(), notify=Mock(), monitor=monitor)
    controller.open = Mock()
    return controller


def test_create_flow_offers_existing_then_reuses_it(store):
    existing(store)
    controller = menu(store, [SimpleNamespace(clicked=1, choice=0)])
    controller.add()
    p = store.list()[0]
    assert p.kind == "default"
    controller.open.assert_called_once_with(p.id)
    assert "Use existing Claude profile (Default)" in controller.prompt.call_args.kwargs["choices"]
    assert "OpenSwap may request Keychain access" in controller.prompt.call_args.kwargs["message"]
    controller.monitor.refresh.assert_called_once_with(p.id)


def test_create_flow_new_choice_creates_separate_empty_storage(store):
    existing(store)
    controller = menu(store, [SimpleNamespace(clicked=1, choice=1), SimpleNamespace(clicked=1, text="Client")])
    controller.add()
    p = store.list()[0]
    assert p.kind == "managed" and p.name == "Client" and p.auto_detect
    assert list(store.paths(p)[0].iterdir()) == []
    controller.open.assert_called_once_with(p.id)
    assert controller.prompt.call_args.kwargs["title"] == "Create Claude Desktop profile"
    assert "After sign-in, OpenSwap may request Keychain access" in controller.prompt.call_args.kwargs["message"]


@pytest.mark.parametrize("second", [False, True])
def test_cancel_either_create_step_keeps_existing_login_and_empty_registry(store, second):
    existing(store)
    answers = ([SimpleNamespace(clicked=1, choice=1)] if second else []) + [SimpleNamespace(clicked=0)]
    controller = menu(store, answers)
    controller.add()
    assert store.list() == [] and not store.root.exists()
    assert (store.default_desktop / "config.json").read_text() == '{"test": "kept"}'
    controller.open.assert_not_called()


def test_registered_default_is_not_offered_twice(store):
    existing(store)
    store.add_default()
    controller = menu(store, [SimpleNamespace(clicked=0)])
    controller.add()
    assert "choices" not in controller.prompt.call_args.kwargs


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


def test_quitting_during_request_preserves_pause_until_explicit_retry(store, monkeypatch):
    from openswap import claude_desktop_identity as module
    detector = AccountDetector(store)
    def key():
        assert store.keychain_paused()  # durable before the system dialog
        detector.clear()
        return b"k" * 16
    monkeypatch.setattr(module, "_key", key)
    with pytest.raises(KeyAccessError): detector._get_key()
    assert detector.denied and AccountDetector(store).denied
    assert detector._cached_key is None


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
