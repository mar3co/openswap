"""Automatic Desktop -> Code onboarding with isolated auth and exact identity."""

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import claude_desktop_code as code
from openswap.claude_desktop import DesktopProfiles, ClaudeDesktopError
from openswap.claude_login import LoginResult
from openswap.menubar_display import desktop_profile_usage, desktop_profile_height
from openswap.switcher import ClaudeAccountSwitcher


ACCOUNT = "11111111-1111-4111-8111-111111111111"
ORG = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
DETAILS = dict(email="same@example.test", organization="Acme", account_uuid=ACCOUNT,
               organization_uuid=ORG, detected_login="f" * 64, detected_at="2026-10-26T20:00:00+00:00")
CREDS = json.dumps({"claudeAiOauth": {"accessToken": "fixture-access", "refreshToken": "fixture-refresh",
                                   "expiresAt": 99999999999999, "scopes": ["user:profile", "user:inference"]}})


class Login:
    def __init__(self, binary, email, *, env, on_done):
        self.binary, self.email, self.env, self.done = binary, email, env, on_done
        self.started = threading.Event()
        self.hold = False
        self.result = LoginResult(0, False, False, "")

    def start(self):
        self.started.set()
        if not self.hold:
            self.done(self.result)

    def cancel(self):
        self.done(LoginResult(None, False, True, ""))


@pytest.fixture
def setup(temp_home, tmp_path, monkeypatch):
    engine = ClaudeAccountSwitcher()
    engine._setup_directories()
    engine._init_sequence_file()
    # Real encrypted backup writes, but any attempt to activate the new login fails.
    monkeypatch.setattr(engine, "_write_credentials", Mock(side_effect=AssertionError("active login changed")))
    monkeypatch.setattr(engine, "switch_to", Mock(side_effect=AssertionError("account switched")))
    store = DesktopProfiles(tmp_path / "desktop")
    p = store.add("Work")
    p = store.update(p.id, **DETAILS)
    calls = []

    def login(*args, **kw):
        run = Login(*args, **kw)
        calls.append(run)
        return run

    oracle = Mock(return_value={"uuid": ACCOUNT, "organizationUuid": ORG, "email": DETAILS["email"]})
    monkeypatch.setattr(code.oauth, "fetch_oauth_profile", oracle)
    connector = code.DesktopCodeConnector(store, engine, resolve_binary=Mock(return_value="/fake/claude"),
        login_factory=login, observe=Mock(return_value="f" * 64),
        read_credentials=Mock(return_value=CREDS), cleanup=Mock(), notify=Mock(), clock=lambda: 100)
    return SimpleNamespace(engine=engine, store=store, profile=p, connector=connector, calls=calls, oracle=oracle)


def finish(s):
    deadline = time.monotonic() + 4
    while s.connector._active and time.monotonic() < deadline:
        s.connector.poll(enabled=True)
        time.sleep(.005)
    assert s.connector._active is None, "onboarding worker did not finish"
    return s.store.get(s.profile.id, include_archived=True)


def seed(s, *, uuid=ACCOUNT, org=ORG, email=DETAILS["email"], number="7"):
    data = s.engine.sequence_data()
    data["activeAccountNumber"] = 7
    data["sequence"].append(int(number))
    data["accounts"][number] = {"email": email, "uuid": uuid, "organizationUuid": org,
                                "organizationName": "Old label", "alias": "Keep me", "disabled": True}
    s.engine._write_json(s.engine.sequence_file, data)
    s.engine._write_account_credentials(number, email, CREDS)
    return data


def test_add_automatically_saves_verified_code_account_without_switching(setup, monkeypatch):
    s = setup
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-inherit")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-inherit")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/do-not-touch")
    before = s.engine.sequence_data()["activeAccountNumber"]
    assert s.connector.poll(enabled=True)
    p = finish(s)
    assert p.code_setup_state == "connected" and len(s.calls) == 1
    row = s.engine.account_identity("1")
    assert row == {"email": p.email, "uuid": ACCOUNT, "organizationUuid": ORG}
    assert s.engine.sequence_data()["activeAccountNumber"] == before
    assert s.engine.read_account_credentials("1", p.email) == CREDS
    run = s.calls[0]
    assert run.email == p.email
    env = run.env
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"] == env["CLAUDE_SECURESTORAGE_CONFIG_DIR"]
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(s.store.root / ".code-login-"))
    assert not Path(env["CLAUDE_CONFIG_DIR"]).exists()
    s.connector.cleanup.assert_called_once_with(Path(env["CLAUDE_CONFIG_DIR"]))
    assert s.connector.state(p) == {"code_status": "In Claude Code", "code_slot": "1"}


def test_existing_account_links_without_login_or_changing_alias_disabled_or_active(setup):
    s = setup
    before = seed(s)
    assert s.connector.poll(enabled=True)
    assert not s.calls and s.engine.sequence_data() == before
    p = s.store.get(s.profile.id)
    assert p.code_setup_state == "connected"
    assert s.connector.state(p)["code_slot"] == "7"


@pytest.mark.parametrize("change, state", [({"uuid": OTHER}, "mismatch"), ({"organizationUuid": OTHER}, "mismatch"),
                                           ({"email": "different@example.test"}, "mismatch"), ({"uuid": None}, "mismatch"),
                                           ({"organizationUuid": None}, "failed")])
def test_wrong_or_incomplete_provider_identity_never_adds_an_account(setup, change, state):
    s = setup
    s.oracle.return_value.update(change)
    s.connector.poll(enabled=True)
    # An omitted field with no readable completed login is unverifiable, not a different account.
    assert finish(s).code_setup_state == state
    assert s.engine.sequence_data()["accounts"] == {}
    assert s.connector.cleanup.call_count == 1


WANTED = (ACCOUNT, ORG, DETAILS["email"])
EMAIL = DETAILS["email"]


@pytest.mark.parametrize("found, authorized, outcome", [
    ((ACCOUNT, ORG, EMAIL), None, None),
    ((ACCOUNT, ORG, EMAIL), WANTED, None),
    ((ACCOUNT, ORG, EMAIL), (ACCOUNT, OTHER, EMAIL), code.IdentityMismatch),
    ((ACCOUNT, None, None), WANTED, None),
    ((ACCOUNT, None, None), None, ClaudeDesktopError),
    ((ACCOUNT, None, None), (ACCOUNT, OTHER, EMAIL), code.IdentityMismatch),
    ((None, ORG, EMAIL), WANTED, code.IdentityMismatch),
    ((ACCOUNT, OTHER, EMAIL), WANTED, code.IdentityMismatch),
    ((OTHER, ORG, EMAIL), None, code.IdentityMismatch),
])
def test_verification_truth_table(setup, found, authorized, outcome):
    s = setup
    s.oracle.return_value = dict(zip(("uuid", "organizationUuid", "email"), found))
    if outcome is None:
        code.verify_credentials(CREDS, s.profile, authorized_identity=authorized)
        return
    with pytest.raises(outcome) as info:
        code.verify_credentials(CREDS, s.profile, authorized_identity=authorized)
    assert isinstance(info.value, code.IdentityMismatch) is (outcome is code.IdentityMismatch)


def test_unavailable_verification_is_not_treated_as_a_match(setup):
    s = setup
    s.oracle.return_value = None
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "failed"
    assert s.engine.sequence_data()["accounts"] == {}


def test_verification_requires_a_current_detected_identity(setup):
    s = setup
    profile = s.store.update(s.profile.id, detected_at="")
    with pytest.raises(code.ProfileChanged):
        code.verify_credentials(CREDS, profile)
    s.oracle.assert_not_called()


def test_late_completion_does_not_notify_for_a_changed_profile(setup):
    s = setup
    s.connector._results.put((s.profile, code.setup_key(s.profile, s.engine), "connected"))
    s.store.update(s.profile.id, email="new@example.test")
    s.connector.poll(enabled=True)
    s.connector.notify.assert_not_called()
    assert not s.store.get(s.profile.id).code_setup_state


@pytest.mark.parametrize("result", [LoginResult(None, False, True, "secret output"), LoginResult(None, True, False, ""),
                                    LoginResult(1, False, False, "secret output")])
def test_cancellation_timeout_and_failure_do_not_reopen_browser_after_restart(setup, result):
    s = setup
    factory = s.connector.login_factory

    def login(*args, **kw):
        run = factory(*args, **kw)
        run.result = result
        return run

    s.connector.login_factory = login
    s.connector.poll(enabled=True)
    p = finish(s)
    assert p.code_setup_state in ("sign_in", "failed")
    assert "secret" not in s.store.registry.read_text()
    s.connector._next_scan = 0
    s.connector.poll(enabled=True)
    restarted = code.DesktopCodeConnector(s.store, s.engine, resolve_binary=Mock(return_value="/fake/claude"), login_factory=Mock())
    restarted.poll(enabled=True)
    restarted.login_factory.assert_not_called()
    assert len(s.calls) == 1 and s.engine.sequence_data()["accounts"] == {}
    assert s.connector.state(p)["code_action"] == "connect_code"


def test_explicit_continue_retries_and_completed_account_is_not_duplicated(setup):
    s = setup
    s.connector.resolve_binary.return_value = None
    s.connector.poll(enabled=True)
    assert s.store.get(s.profile.id).code_setup_state == "install"
    s.connector.resolve_binary.return_value = "/fake/claude"
    s.connector.retry(s.profile.id)
    s.connector.poll(enabled=True)
    finish(s)
    s.connector.retry(s.profile.id)
    s.connector.poll(enabled=True)
    assert len(s.calls) == 1 and len(s.engine.sequence_data()["accounts"]) == 1


@pytest.mark.parametrize("edit", ["archive", "manual", "organization", "disable", "cancel"])
def test_changes_during_browser_authorization_invalidate_the_result(setup, edit):
    s = setup
    created = threading.Event()
    factory = s.connector.login_factory

    def login(*args, **kw):
        run = factory(*args, **kw)
        run.hold = True
        created.set()
        return run

    s.connector.login_factory = login
    s.connector.poll(enabled=True)
    assert created.wait(3) and s.calls[0].started.wait(3)
    if edit == "archive": s.store.update(s.profile.id, archived=True)
    elif edit == "manual": s.store.update(s.profile.id, email="manual@example.test")
    elif edit == "organization": s.store.update(s.profile.id, **{**DETAILS, "organization_uuid": OTHER})
    elif edit == "disable": s.connector.poll(enabled=False)
    else: s.connector.cancel(s.profile.id)
    finish(s)
    assert not s.engine.sequence_data()["accounts"]
    s.connector.cleanup.assert_called_once()


def test_login_cookie_change_between_verification_and_save_rejects_stale_result(setup):
    s = setup

    def verify(_):
        s.connector.observe.return_value = "another-login"
        return {"uuid": ACCOUNT, "organizationUuid": ORG, "email": DETAILS["email"]}

    s.oracle.side_effect = verify
    s.connector.poll(enabled=True)
    p = finish(s)
    # The browser already opened once: resume only on an explicit click.
    assert p.code_setup_state == "sign_in" and not s.engine.sequence_data()["accounts"]
    s.connector._next_scan = 0
    s.connector.poll(enabled=True)
    assert len(s.calls) == 1 and s.connector.state(p)["code_action"] == "connect_code"


def guard_factory(s, on_enter):
    class Store(code.AccountLeaseStore):
        @contextmanager
        def mutation_guard(self, **kwargs):
            with super().mutation_guard(**kwargs) as guard:
                on_enter()
                yield guard
    return Store


def test_registry_lock_held_during_the_engine_wait_does_not_lose_a_finished_login(setup, monkeypatch):
    s = setup

    def ui_edit_holds_registry_lock():
        with s.store.lock():
            pass

    monkeypatch.setattr(code, "AccountLeaseStore", guard_factory(s, ui_edit_holds_registry_lock))
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "connected"


def test_profile_archived_between_verification_and_commit_is_not_committed(setup, monkeypatch):
    s = setup
    monkeypatch.setattr(code, "AccountLeaseStore", guard_factory(s, lambda: s.store.update(s.profile.id, archived=True)))
    s.connector.poll(enabled=True)
    finish(s)
    assert s.engine.sequence_data()["accounts"] == {}


def test_legacy_link_rechecks_the_profile_inside_the_engine_lock(setup, monkeypatch):
    s = setup
    seed(s, uuid="")
    backfill = s.engine.backfill_account_uuid

    def archive_then_backfill(*args, **kwargs):
        s.store.update(s.profile.id, archived=True)
        return backfill(*args, **kwargs)

    monkeypatch.setattr(s.engine, "backfill_account_uuid", archive_then_backfill)
    s.connector.poll(enabled=True)
    finish(s)
    assert s.engine.account_identity("7")["uuid"] == ""


def test_same_email_in_two_organizations_creates_two_distinct_accounts(setup):
    s = setup
    seed(s, org=OTHER)
    s.connector.poll(enabled=True)
    finish(s)
    rows = s.engine.sequence_data()["accounts"]
    assert len(rows) == 2
    assert {r["organizationUuid"] for r in rows.values()} == {ORG, OTHER}
    assert s.engine.sequence_data()["activeAccountNumber"] == 7


def test_duplicate_profiles_share_one_code_account_and_one_authorization(setup):
    s = setup
    other = s.store.add("Another window")
    s.store.update(other.id, **DETAILS)
    s.connector.poll(enabled=True)
    finish(s)
    s.connector._next_scan = 0
    s.connector.poll(enabled=True)
    assert len(s.engine.sequence_data()["accounts"]) == 1 and len(s.calls) == 1
    assert s.store.get(other.id).code_setup_state == "connected"


def test_existing_legacy_identity_is_verified_before_linking_without_browser(setup):
    s = setup
    seed(s, uuid="")
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "connected"
    assert not s.calls and s.engine.account_identity("7")["uuid"] == ACCOUNT


def test_legacy_credential_changed_during_verification_is_not_relabelled(setup):
    s = setup
    seed(s, uuid="")

    def verify(_):
        s.engine._write_account_credentials("7", DETAILS["email"], "other credential")
        return {"uuid": ACCOUNT, "organizationUuid": ORG, "email": DETAILS["email"]}

    s.oracle.side_effect = verify
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "failed"
    assert s.engine.account_identity("7")["uuid"] == ""


def test_concurrent_add_is_deduplicated_at_commit(setup):
    s = setup

    def verify(_):
        code.save_verified_account(s.engine, s.profile, CREDS)
        return {"uuid": ACCOUNT, "organizationUuid": ORG, "email": DETAILS["email"]}

    s.oracle.side_effect = verify
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "connected"
    assert len(s.engine.sequence_data()["accounts"]) == 1


@pytest.mark.parametrize("change", [{"email": "manual@example.test"}, {"auto_detect": False}, {"archived": True}, {"detected_at": ""}])
def test_unverified_or_disabled_profiles_never_launch_authorization(setup, change):
    s = setup
    # Clearing detection also clears the authenticated IDs.
    s.store.update(s.profile.id, **change)
    s.connector.poll(enabled=True)
    assert not s.calls and s.connector._active is None


def test_disabled_feature_and_busy_ui_do_not_start_login(setup):
    s = setup
    s.connector.poll(enabled=False)
    s.connector.poll(enabled=True, idle=False)
    assert not s.calls
    s.connector.poll(enabled=True)
    finish(s)
    assert len(s.calls) == 1


def test_removed_code_account_does_not_reappear_or_reopen_browser(setup):
    s = setup
    seed(s)
    s.connector.poll(enabled=True)
    data = s.engine.sequence_data()
    data["accounts"], data["sequence"] = {}, []
    s.engine._write_json(s.engine.sequence_file, data)
    s.connector._next_scan = 0
    s.connector.poll(enabled=True)
    assert not s.calls and s.connector.state(s.store.get(s.profile.id))["code_status"] == "Code account removed"


def test_unsafe_email_cannot_become_a_credential_filename(setup):
    s = setup
    p = s.store.update(s.profile.id, **{**DETAILS, "email": "../oops@example.test"})
    with pytest.raises(ClaudeDesktopError): code.save_verified_account(s.engine, p, CREDS)
    assert s.engine.sequence_data()["accounts"] == {}


def test_usage_is_shared_only_for_the_exact_code_slot_and_org(setup):
    p = {**asdict(setup.profile), "code_slot": "2", "code_status": "In Claude Code"}
    card = {"num": "2", "provider": "claude", "identity": (p["email"], ORG),
            "windows": [{"label": "5h", "pct": 23}], "stale": False}
    assert desktop_profile_usage(p, [card]) == "Usage: 5h 23%"
    assert desktop_profile_usage(p, [{**card, "stale": True}]) == "Last known: 5h 23%"
    for changed in ({"num": "3"}, {"provider": "codex"}, {"identity": (p["email"], OTHER)}, {"identity": None}):
        assert desktop_profile_usage(p, [{**card, **changed}]) == ""
    assert desktop_profile_usage({**p, "archived": True}, [card]) == ""
    assert desktop_profile_height({**p, "code_usage": "Usage: 5h 23%"}) == 116


def test_stale_detected_identity_cannot_start_code_authorization(setup):
    s = setup
    s.connector.observe.return_value = "new Desktop login, not yet detected"
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == ""
    assert not s.calls and s.engine.sequence_data()["accounts"] == {}


@pytest.mark.skipif(os.name == "nt", reason="Completed Desktop auth reads use the macOS/POSIX no-follow reader")
def test_personal_account_can_use_completed_oauth_org_when_profile_omits_it(setup):
    s = setup
    s.oracle.return_value["organizationUuid"] = None
    factory = s.connector.login_factory

    def login(*args, **kwargs):
        run = factory(*args, **kwargs)
        config = {"oauthAccount": {"accountUuid": ACCOUNT, "organizationUuid": ORG, "emailAddress": DETAILS["email"]}}
        (Path(run.env["CLAUDE_CONFIG_DIR"]) / ".claude.json").write_text(json.dumps(config))
        return run

    s.connector.login_factory = login
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "connected"
    assert s.engine.account_identity("1")["organizationUuid"] == ORG


def test_local_oauth_identity_cannot_override_server_org_disagreement(setup):
    s = setup
    s.oracle.return_value["organizationUuid"] = OTHER
    with pytest.raises(code.IdentityMismatch):
        code.verify_credentials(CREDS, s.profile, authorized_identity=code.identity(s.profile))


def test_old_unknown_slot_gets_fresh_credentials_without_duplicate_or_selection_change(setup):
    s = setup
    seed(s, uuid="")
    s.oracle.side_effect = [None, {"uuid": ACCOUNT, "organizationUuid": ORG, "email": DETAILS["email"]}]
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "connected"
    data = s.engine.sequence_data()
    assert list(data["accounts"]) == ["7"] and data["activeAccountNumber"] == 7
    assert data["accounts"]["7"]["uuid"] == ACCOUNT
    assert data["accounts"]["7"]["alias"] == "Keep me" and data["accounts"]["7"]["disabled"]
    assert len(s.calls) == 1
    assert json.loads(s.engine.read_account_config("7", DETAILS["email"]))["oauthAccount"]["accountUuid"] == ACCOUNT


def test_relinking_a_legacy_slot_keeps_its_full_config_backup(setup):
    s = setup
    seed(s, uuid="")
    backup = json.dumps({"oauthAccount": {"emailAddress": DETAILS["email"], "organizationUuid": ORG}, "projects": {"/work": {}}})
    s.engine._write_account_config("7", DETAILS["email"], backup)
    s.oracle.side_effect = [None, {"uuid": ACCOUNT, "organizationUuid": ORG, "email": DETAILS["email"]}]
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "connected"
    assert s.engine.account_identity("7")["uuid"] == ACCOUNT
    assert s.engine.read_account_config("7", DETAILS["email"]) == backup


def test_usage_quarantine_failure_after_commit_still_reports_the_added_account(setup, monkeypatch, caplog):
    s = setup
    monkeypatch.setattr(s.engine._usage_store, "clear_dead_token", Mock(side_effect=OSError("locked " + DETAILS["email"])))
    with caplog.at_level("WARNING", logger="openswap"):
        s.connector.poll(enabled=True)
        assert finish(s).code_setup_state == "connected"
    assert s.engine.account_identity("1")["uuid"] == ACCOUNT
    s.connector.notify.assert_called_once()
    [record] = [r for r in caplog.records if "usage quarantine" in r.getMessage()]
    assert "OSError" in record.getMessage() and DETAILS["email"] not in record.getMessage()


def test_duplicate_roster_match_reports_itself_without_blocking_other_profiles(setup):
    s = setup
    seed(s)
    seed(s, number="8")
    other = s.store.add("Client")
    s.store.update(other.id, **{**DETAILS, "organization_uuid": OTHER})
    s.oracle.return_value["organizationUuid"] = OTHER
    s.connector.poll(enabled=True)
    finish(s)
    assert len(s.calls) == 1 and s.store.get(other.id).code_setup_state == "connected"
    assert s.connector.state(s.store.get(s.profile.id)) == {
        "code_status": "More than one saved Code account matches. Resolve the duplicate in Claude Code."}


def real_keychain(monkeypatch, outcome):
    """The production Keychain delete, with ``security`` answering as ``outcome`` says."""
    from openswap import macos_keychain
    from openswap.models import Platform
    monkeypatch.setattr(Platform, "detect", classmethod(lambda cls: Platform.MACOS))
    calls = []

    def delete_password(service, account):
        calls.append(service)
        if outcome == "locked":
            raise macos_keychain.KeychainError("security delete-generic-password failed (rc=36): User interaction is not allowed.")
        if outcome == "timeout":
            raise macos_keychain.KeychainError("security delete-generic-password timed out after 10s")
        # rc 0 and rc 44 (absent) both return without raising

    monkeypatch.setattr(macos_keychain, "delete_password", delete_password)
    return calls


@pytest.mark.parametrize("outcome", ["deleted", "locked", "timeout"])
def test_first_enabled_poll_sweeps_orphaned_logins_off_the_ui_thread_but_never_live_ones(setup, monkeypatch, outcome):
    s = setup
    s.connector.cleanup = code.delete_login_keychain_item
    deletes = real_keychain(monkeypatch, outcome)
    stale, live = (s.store.root / f".code-login-{name}" for name in ("stale", "live"))
    for folder in (stale, live):
        folder.mkdir()
        (folder / ".claude.json").write_text("{}")
        (folder / "nested").mkdir()
        (folder / "nested" / ".credentials.json").write_text("{}")
    os.utime(stale, (s.connector._started - 100,) * 2)
    os.utime(live, (s.connector._started + 100,) * 2)
    s.connector.poll(enabled=False)
    assert stale.exists() and deletes == []
    assert s.connector.poll(enabled=True)
    s.connector._sweeper.join(4)
    assert s.connector._sweeper.name == "claude-desktop-code-sweep" and not s.connector._sweeper.is_alive()
    from openswap.session import keychain_service_name
    assert deletes == [keychain_service_name(stale)]
    assert live.is_dir() and (live / "nested" / ".credentials.json").exists()
    if outcome == "deleted":
        assert not stale.exists()
    else:
        # The item may survive: the empty folder keeps its name for the next sweep.
        assert stale.is_dir() and list(stale.iterdir()) == []
    assert finish(s).code_setup_state == "connected"


@pytest.mark.parametrize("outcome", ["deleted", "locked"])
def test_finished_login_keeps_its_empty_scratch_folder_until_the_keychain_item_is_confirmed_gone(setup, monkeypatch, outcome):
    s = setup
    s.connector.cleanup = code.delete_login_keychain_item
    deletes = real_keychain(monkeypatch, outcome)
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "connected"
    scratch = Path(s.calls[0].env["CLAUDE_CONFIG_DIR"])
    assert len(deletes) == 1
    if outcome == "deleted":
        assert not scratch.exists()
    else:
        assert scratch.is_dir() and list(scratch.iterdir()) == []


def test_profile_change_before_commit_leaves_a_legacy_slot_byte_for_byte_unchanged(setup, monkeypatch):
    s = setup
    seed(s, uuid="")
    # An unusable legacy credential sends setup through fresh authorization.
    s.engine._write_account_credentials("7", DETAILS["email"], "legacy credential")
    backup = json.dumps({"oauthAccount": {"emailAddress": DETAILS["email"]}, "projects": {"/work": {}}})
    s.engine._write_account_config("7", DETAILS["email"], backup)
    monkeypatch.setattr(code, "AccountLeaseStore", guard_factory(s, lambda: s.store.update(s.profile.id, archived=True)))
    s.connector.poll(enabled=True)
    finish(s)
    assert len(s.calls) == 1
    assert s.engine.read_backup_credentials("7", DETAILS["email"]) == ("legacy credential", False)
    assert s.engine.read_account_config("7", DETAILS["email"]) == backup
    assert s.engine.account_identity("7")["uuid"] == ""


def test_roster_write_failure_cleans_only_new_backup_and_preserves_active(setup, monkeypatch):
    s = setup
    before = seed(s, org=OTHER)
    writer = s.engine._write_json

    def fail(path, data):
        if path == s.engine.sequence_file and len(data["accounts"]) > 1:
            raise OSError("disk full")
        return writer(path, data)

    monkeypatch.setattr(s.engine, "_write_json", fail)
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "failed"
    assert s.engine.sequence_data() == before
    assert s.engine.read_account_credentials("7", DETAILS["email"]) == CREDS
    assert not list(s.engine.configs_dir.glob('*-8-*'))


def test_lease_refusal_does_not_change_accounts(setup, monkeypatch):
    from contextlib import contextmanager
    s = setup

    @contextmanager
    def refuse():
        yield SimpleNamespace(assert_available=Mock(side_effect=ClaudeDesktopError("Worker holds account lease")))

    monkeypatch.setattr(code, "AccountLeaseStore", lambda *_: SimpleNamespace(mutation_guard=refuse))
    s.connector.poll(enabled=True)
    assert finish(s).code_setup_state == "failed"
    assert s.engine.sequence_data()["accounts"] == {}


def test_invalid_detected_ids_and_setup_journal_are_rejected(setup):
    s = setup
    before = s.store.registry.read_bytes()
    for fields in ({"account_uuid": "not-a-uuid"}, {"detected_login": "not-a-fingerprint"},
                   {"code_setup_key": "f" * 64}, {"code_setup_state": "connected"}):
        with pytest.raises(ClaudeDesktopError): s.store.update(s.profile.id, **fields)
        assert s.store.registry.read_bytes() == before


def test_archiving_retains_the_saved_code_account(setup):
    s = setup
    s.connector.poll(enabled=True)
    finish(s)
    before = s.engine.sequence_data()
    p = s.store.update(s.profile.id, archived=True)
    assert s.connector.state(p) == {}
    s.connector._next_scan = 0
    s.connector.poll(enabled=True)
    assert s.engine.sequence_data() == before
