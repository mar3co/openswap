"""Profile identity is optional, isolated, on demand, and never a launch key."""

import json
import os
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import claude_desktop_identity as identity
from openswap.claude_desktop import ClaudeDesktopError, DesktopProfiles
from openswap.claude_desktop_menu import ClaudeDesktopMenu

ACCOUNT = "11111111-1111-4111-8111-111111111111"
ORG_A = "22222222-2222-4222-8222-222222222222"
ORG_B = "33333333-3333-4333-8333-333333333333"
STAMP = "2026-10-26T12:00:00+00:00"


def bootstrap():
    return {"account": {"uuid": ACCOUNT, "email_address": "same@example.test", "memberships": [
        {"organization": {"uuid": ORG_A, "name": "Personal"}},
        {"organization": {"uuid": ORG_B, "name": "Acme"}},
    ]}}


def cookie_store(store, profile, *, org=ORG_B):
    desktop, _ = store.paths(profile)
    (desktop / "config.json").write_text(json.dumps({"lastKnownAccountUuid": ACCOUNT}))
    with sqlite3.connect(desktop / "Cookies") as db:
        db.execute("CREATE TABLE meta(key TEXT, value TEXT)")
        db.execute("INSERT INTO meta VALUES ('version', '24')")
        db.execute("CREATE TABLE cookies(name TEXT, host_key TEXT, value TEXT, encrypted_value BLOB, "
                   "path TEXT DEFAULT '/', top_frame_site_key TEXT DEFAULT '', is_secure INTEGER DEFAULT 1, expires_utc INTEGER DEFAULT 0)")
        db.executemany("INSERT INTO cookies(name, host_key, value, encrypted_value) VALUES (?, '.claude.ai', ?, ?)",
                       [("lastActiveOrg", org, b""), ("sessionKey", "fixture-session", b"")])
    return desktop


@pytest.fixture
def setup(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("The macOS cookie reader requires POSIX no-follow opens and file ownership checks")
    store = DesktopProfiles(tmp_path / "profiles")
    profile = store.add("Work")
    desktop = cookie_store(store, profile)
    app = Mock()
    # Never replace process-wide sys.platform: the real file-lock backend must
    # continue to match the host where it was imported.
    monkeypatch.setattr(identity, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(identity, "_bootstrap", Mock(return_value=bootstrap()))
    monkeypatch.setattr(identity, "_key", Mock(side_effect=AssertionError("No Keychain for plaintext fixtures")))
    return store, profile, desktop, app


def detect(setup):
    store, profile, _, app = setup
    return identity.detect_account(store, profile.id, app=app)


def test_detects_selected_org_not_first_membership_and_never_saves(setup):
    store, profile, desktop, app = setup
    files = {p: p.read_bytes() for p in (store.registry, desktop / "Cookies", desktop / "config.json")}
    result = detect(setup)
    assert result["email"] == "same@example.test" and result["organization"] == "Acme"
    assert set(result) == {"email", "organization", "detected_at", "account_uuid", "organization_uuid", "detected_login"}
    assert result["account_uuid"] == ACCOUNT and result["organization_uuid"] == ORG_B
    assert files == {p: p.read_bytes() for p in files}
    app.validate.assert_called_once()


def test_live_wal_is_read_without_changing_claude_files(setup):
    desktop = setup[2]
    with sqlite3.connect(desktop / "Cookies") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE cookies SET value=? WHERE name='lastActiveOrg'", (ORG_A,))
        writer.commit()
        files = {p: p.read_bytes() for p in desktop.iterdir()}
        assert detect(setup)["organization"] == "Personal"
        assert files == {p: p.read_bytes() for p in files}


def test_network_cookie_store_is_used_only_when_unambiguous(setup):
    desktop = setup[2]
    (desktop / "Network").mkdir()
    (desktop / "Cookies").rename(desktop / "Network/Cookies")
    assert detect(setup)["organization"] == "Acme"
    (desktop / "Cookies").write_bytes((desktop / "Network/Cookies").read_bytes())
    with pytest.raises(identity.DetectionError): detect(setup)


def test_hardlinked_cookie_store_is_rejected(setup, tmp_path):
    os.link(setup[2] / "Cookies", tmp_path / "linked")
    with pytest.raises(identity.DetectionError): detect(setup)
    identity._bootstrap.assert_not_called()


def test_same_email_profiles_resolve_their_own_organization(setup):
    store, profile, _, app = setup
    personal = store.add("Personal")
    cookie_store(store, personal, org=ORG_A)
    results = [identity.detect_account(store, p.id, app=app) for p in (profile, personal)]
    assert [r["organization"] for r in results] == ["Acme", "Personal"]
    for p, result in zip((profile, personal), results):
        store.update(p.id, **result)
    assert [p.email for p in store.list()] == ["same@example.test"] * 2
    assert store.paths(profile) != store.paths(personal)


def test_default_detection_uses_existing_store_without_falling_back_from_managed(setup, tmp_path):
    store, managed, _, app = setup
    store.default_desktop = tmp_path / "existing-Claude"
    store.default_desktop.mkdir()
    default = store.ensure_default()
    desktop = cookie_store(store, default, org=ORG_A)
    before = {p: p.read_bytes() for p in desktop.iterdir()}
    assert identity.detect_account(store, default.id, app=app)["organization"] == "Personal"
    assert identity.detect_account(store, managed.id, app=app)["organization"] == "Acme"
    assert before == {p: p.read_bytes() for p in desktop.iterdir()}


def test_manual_edit_clears_detection_but_lifecycle_preserves_details(setup):
    store, p, desktop, _ = setup
    store.update(p.id, email="same@example.test", organization="Acme", detected_at=STAMP)
    updated = store.update(p.id, name="Client", badge="9", archived=True)
    assert updated.detected_at == STAMP and store.paths(updated)[0] == desktop
    store.update(p.id, archived=False)
    assert store.update(p.id, organization="Another org").detected_at == ""
    cleared = store.update(p.id, email="", organization="")
    assert not cleared.email and not cleared.organization


@pytest.mark.parametrize("version", [1, 2, 3])
def test_metadata_upgrade_keeps_ids_and_data_without_inventing_identity(setup, version):
    store, p, desktop, _ = setup
    row = {"id": p.id, "name": p.name, "archived": False}
    if version == 2:
        row["code_account"] = {"store": "a" * 64, "email": "wrong@example.test", "organization": "CLI org"}
    if version == 3:
        row.update(badge="8", color="purple")
    store.registry.write_text(json.dumps({"version": version, "profiles": [row]}))
    before = (desktop / "Cookies").read_bytes()
    assert store.get(p.id).email == ""
    store.update(p.id, email="new@example.test")
    assert json.loads(store.registry.read_text())["version"] == 6
    assert (desktop / "Cookies").read_bytes() == before
    if version == 3:
        assert store.get(p.id).badge == "8"


@pytest.mark.parametrize("changes", [
    {"email": "bad"}, {"email": "bad\n@example.test"}, {"email": []},
    {"organization": "x\u202ey"}, {"organization": "x" * 129},
    {"detected_at": "not-a-date"}, {"detected_at": STAMP},
    {"email": "a@b", "organization": "X", "detected_at": "2026-10-26"},
])
def test_invalid_details_never_replace_metadata(setup, changes):
    store, p, _, _ = setup
    before = store.registry.read_bytes()
    with pytest.raises(ClaudeDesktopError):
        store.update(p.id, **changes)
    assert store.registry.read_bytes() == before


@pytest.mark.parametrize("sql", [
    "DELETE FROM cookies WHERE name='sessionKey'",
    "UPDATE cookies SET expires_utc=1",
    "UPDATE cookies SET top_frame_site_key='https://other.test'",
    "UPDATE cookies SET host_key='other.test'",
    "UPDATE cookies SET path='/other'",
    "UPDATE cookies SET is_secure=0",
    "UPDATE meta SET value='25'",
    "INSERT INTO cookies SELECT * FROM cookies",
    "UPDATE cookies SET value='bad; injected=x' WHERE name='sessionKey'",
    "UPDATE cookies SET value='not-an-org' WHERE name='lastActiveOrg'",
])
def test_missing_ambiguous_or_invalid_cookie_state_does_not_make_requests(setup, sql):
    with sqlite3.connect(setup[2] / "Cookies") as db:
        db.execute(sql)
    with pytest.raises(identity.DetectionError):
        detect(setup)
    identity._bootstrap.assert_not_called()
    identity._key.assert_not_called()


@pytest.mark.parametrize("path", ["Cookies", "Cookies-wal", "config.json", "Network"])
def test_symlinks_are_not_followed(setup, tmp_path, path):
    desktop = setup[2]
    target = tmp_path / "external"
    target.write_text("untouched")
    (desktop / path).unlink(missing_ok=True)
    (desktop / path).symlink_to(target)
    with pytest.raises(identity.DetectionError):
        detect(setup)
    assert target.read_text() == "untouched"
    identity._bootstrap.assert_not_called()


def test_never_falls_back_to_default_or_cli_identity(setup):
    store, p, desktop, _ = setup
    (desktop / "Cookies").unlink()
    (store.paths(p)[1] / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "wrong@example.test"}}))
    with pytest.raises(identity.DetectionError):
        detect(setup)
    identity._bootstrap.assert_not_called()


def test_archived_and_untrusted_app_do_not_inspect_credentials(setup):
    store, p, _, app = setup
    app.validate.side_effect = ClaudeDesktopError("wrong publisher")
    with pytest.raises(identity.DetectionError):
        detect(setup)
    app.validate.side_effect = None
    store.update(p.id, archived=True)
    with pytest.raises(identity.DetectionError):
        detect(setup)
    identity._bootstrap.assert_not_called()


@pytest.mark.parametrize("changed", ["account", "organization", "session"])
def test_rejects_state_changed_during_request(setup, changed):
    desktop = setup[2]
    def fetch(_):
        if changed == "account":
            (desktop / "config.json").write_text(json.dumps({"lastKnownAccountUuid": ORG_A}))
        else:
            with sqlite3.connect(desktop / "Cookies") as db:
                db.execute("UPDATE cookies SET value=? WHERE name=?", (ORG_A if changed == "organization" else "new", "lastActiveOrg" if changed == "organization" else "sessionKey"))
        return bootstrap()
    identity._bootstrap.side_effect = fetch
    with pytest.raises(identity.DetectionError, match="changed"):
        detect(setup)


@pytest.mark.parametrize("change", ["account", "missing_org", "duplicate_org", "email", "org_name"])
def test_response_requires_matching_account_and_selected_membership(setup, change):
    data = bootstrap()
    if change == "account": data["account"]["uuid"] = ORG_A
    if change == "missing_org": data["account"]["memberships"] = []
    if change == "duplicate_org": data["account"]["memberships"] *= 2
    if change == "email": data["account"]["email_address"] = "invalid"
    if change == "org_name": data["account"]["memberships"][1]["organization"]["name"] = ""
    identity._bootstrap.return_value = data
    with pytest.raises(identity.DetectionError):
        detect(setup)


def test_errors_never_expose_session_or_response(setup):
    identity._bootstrap.side_effect = RuntimeError("fixture-session private response")
    with pytest.raises(identity.DetectionError) as exc:
        detect(setup)
    assert "fixture-session" not in str(exc.value) and "private response" not in str(exc.value)


def test_keychain_failure_does_not_make_request(setup, monkeypatch):
    with sqlite3.connect(setup[2] / "Cookies") as db:
        db.execute("UPDATE cookies SET value='', encrypted_value=? WHERE name='sessionKey'", (b"v10" + b"x" * 16,))
    monkeypatch.setattr(identity, "_key", Mock(side_effect=identity.DetectionError("Denied")))
    with pytest.raises(identity.DetectionError): detect(setup)
    identity._bootstrap.assert_not_called()


def test_keychain_read_uses_shared_app_identified_helper(monkeypatch):
    read = Mock(return_value="fixture-key")
    monkeypatch.setattr(identity.macos_keychain, "get_password", read)
    assert len(identity._key()) == 16
    read.assert_called_once_with("Claude Safe Storage", "Claude Key", native=True)
    read.side_effect = identity.macos_keychain.KeychainError("private error")
    with pytest.raises(identity.KeyAccessError, match="paused"): identity._key()


@pytest.mark.skipif(sys.platform != "darwin", reason="CommonCrypto is macOS-only")
def test_cookie_decryption_matches_independent_openssl_vector():
    # Fixture encrypted independently with OpenSSL AES-128-CBC, not this reader.
    key = identity.hashlib.pbkdf2_hmac("sha1", b"fixture-key", b"saltysalt", 1003, 16)
    encrypted = bytes.fromhex("763130d2d701f97ec9261543e965699887e30dbdd4ebe4687d7e52d5fc65d5309c77b2e985d80bb1a0a0cf2f37e25bd4c683b1")
    assert identity._decrypt(encrypted, ".claude.ai", key) == "fixture-session"
    for data, host, password in [(encrypted, "claude.ai", key), (encrypted, ".claude.ai", b"x" * 16),
                                  (b"v11" + encrypted[3:], ".claude.ai", key), (encrypted[:-1], ".claude.ai", key)]:
        with pytest.raises(identity.DetectionError): identity._decrypt(data, host, password)


def test_http_request_is_fixed_bounded_and_refuses_redirects(monkeypatch):
    response = Mock(status=200)
    response.geturl.return_value = identity.BOOTSTRAP_URL
    response.read.return_value = json.dumps(bootstrap()).encode()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    factory = Mock(return_value=opener)
    monkeypatch.setattr(identity.urllib.request, "build_opener", factory)
    identity._bootstrap({"sessionKey": "fixture"})
    req = opener.open.call_args.args[0]
    assert req.full_url == identity.BOOTSTRAP_URL and req.get_method() == "GET"
    assert opener.open.call_args.kwargs == {"timeout": 5}
    response.read.assert_called_once_with(identity.MAX_RESPONSE + 1)
    assert factory.call_args.args[0].redirect_request(None, None, 302, None, None, "https://other.test") is None
    response.read.return_value = b"x" * (identity.MAX_RESPONSE + 1)
    with pytest.raises(identity.DetectionError): identity._bootstrap({"sessionKey": "fixture"})


@pytest.fixture
def controller(tmp_path, monkeypatch):
    # Run only fake detector work inline, then exercise the normal UI-thread poll.
    import openswap.claude_desktop_menu as menu
    monkeypatch.setattr(menu.threading, "Thread", lambda *, target, **_: SimpleNamespace(start=target))
    return ClaudeDesktopMenu(store=DesktopProfiles(tmp_path / "profiles"), prompt=Mock(), show_error=Mock(),
                             changed=Mock(), notify=Mock(), detector=Mock(return_value={
                                 "email": "same@example.test", "organization": "Acme", "detected_at": STAMP}))


def answer(clicked, email="", organization="", checked=True):
    return SimpleNamespace(clicked=clicked, values=(email, organization), checked=checked)


def test_detect_then_save_is_explicit_and_keeps_profile_identity(controller):
    p = controller.store.add("Work")
    before = controller.store.registry.read_bytes()
    controller.prompt.side_effect = [answer(-1), answer(1, "same@example.test", "Acme")]
    controller.perform("details", p.id)
    assert controller.busy and controller.store.registry.read_bytes() == before
    controller.poll()
    saved = controller.store.get(p.id)
    assert saved.name == "Work" and saved.email == "same@example.test" and saved.detected_at == STAMP
    assert not controller.busy


def test_detect_then_cancel_does_not_save(controller):
    p = controller.store.add("Work")
    before = controller.store.registry.read_bytes()
    controller.prompt.side_effect = [answer(-1), answer(0)]
    controller.perform("details", p.id)
    controller.poll()
    assert controller.store.registry.read_bytes() == before


def test_editing_detected_result_saves_manual_labels(controller):
    p = controller.store.add("Work")
    controller.prompt.side_effect = [answer(-1), answer(1, "same@example.test", "Other org")]
    controller.perform("details", p.id)
    controller.poll()
    saved = controller.store.get(p.id)
    assert saved.organization == "Other org" and saved.detected_at == ""


def test_invalid_manual_email_keeps_draft_open(controller):
    p = controller.store.add("Work")
    controller.prompt.side_effect = [answer(1, "invalid", "Acme"), answer(0)]
    controller.perform("details", p.id)
    assert controller.prompt.call_args.kwargs["fields"] == [("Email (optional)", "invalid"), ("Organization (optional)", "Acme")]
    assert controller.store.get(p.id).email == ""


def test_cancel_detected_details_and_failed_detection_preserve_saved_labels(controller):
    p = controller.store.add("Work")
    controller.store.update(p.id, email="kept@example.test", organization="Kept")
    before = controller.store.registry.read_bytes()
    controller.prompt.side_effect = [answer(-1, "draft@example.test", "Draft"), answer(0)]
    controller.detector.side_effect = RuntimeError("sensitive")
    controller.perform("details", p.id)
    controller.poll()
    assert controller.store.registry.read_bytes() == before
    args = controller.prompt.call_args.kwargs
    assert args["fields"][0][1] == "draft@example.test" and "sensitive" not in args["message"]


@pytest.mark.parametrize("gate", ["disabled", "busy", "blocked"])
def test_details_respect_gates(controller, gate):
    controller.enabled = lambda: gate != "disabled"
    controller.blocked = lambda: gate == "blocked"
    controller.busy = gate == "busy"
    controller.perform("details", "unused")
    controller.prompt.assert_not_called()
    controller.detector.assert_not_called()


@pytest.mark.parametrize("change", ["disabled", "archived"])
def test_disable_or_archive_during_detection_does_not_reopen_dialog(controller, change):
    p = controller.store.add("Work")
    controller.prompt.return_value = answer(-1)
    controller.perform("details", p.id)
    if change == "archived": controller.store.update(p.id, archived=True)
    else: controller.enabled = lambda: False
    controller.poll()
    controller.prompt.assert_called_once()
    assert controller.store.get(p.id, include_archived=True).email == ""


def test_status_and_settings_never_detect_accounts(controller):
    controller.store.add("Work")
    controller._app_factory = Mock(return_value=Mock(profile_states=Mock(return_value={}), validate=Mock(return_value={"version": "test"})))
    controller.settings_state()
    controller.refresh_status()
    controller.poll()
    controller.detector.assert_not_called()
