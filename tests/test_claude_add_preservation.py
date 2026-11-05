"""Adding a Claude login must distinguish saved credentials from config labels."""

import json

import pytest

from openswap.credentials import ActiveCredentials
from openswap.exceptions import CredentialReadError
from openswap.menubar_display import claude_add_unsaved_live_message
from openswap.worker.leases import AccountLeaseStore, LeaseConflictError, stable_account_identity
from tests.test_engine_abi import ADS_ORG, EMAIL, _two_org_engine
from tests.test_identity_drift import _set_live_credential, _unmanaged_config
from tests.test_menubar import _claude_add_app


@pytest.mark.parametrize("config_drift", [False, True])
def test_saved_credential_is_recognized_without_recapturing_wrong_identity(temp_home, config_drift):
    engine = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    if config_drift:
        _unmanaged_config(temp_home)
    # The live access token differs, but the saved refresh token is current.
    paths = [temp_home / ".claude.json", engine.sequence_file]
    paths.extend(engine.configs_dir.iterdir())
    paths.extend(engine.credentials_dir.iterdir())
    before = {path: path.read_bytes() for path in paths}

    assert engine.current_login_is_saved() is True

    assert {path: path.read_bytes() for path in paths} == before


def test_same_email_does_not_make_a_different_organization_login_saved(temp_home):
    engine = _two_org_engine(temp_home)
    config = temp_home / ".claude.json"
    data = json.loads(config.read_text())
    data["oauthAccount"]["organizationUuid"] = "another-org"
    config.write_text(json.dumps(data))

    assert engine.current_account_number() is None
    assert engine.current_login_is_saved() is False


@pytest.mark.parametrize("credentials", ["", '{"claudeAiOauth": {}}'])
def test_empty_credentials_never_count_as_a_saved_login(temp_home, credentials, monkeypatch):
    engine = _two_org_engine(temp_home)
    monkeypatch.setattr(engine, "_read_capture_credentials", lambda: credentials)
    monkeypatch.setattr(engine, "_read_account_credentials_ex", lambda *_: (credentials, False))
    assert engine.current_login_is_saved() is False


def test_degraded_live_read_cannot_confirm_saved_login(temp_home, monkeypatch):
    engine = _two_org_engine(temp_home)
    saved = engine._read_account_credentials("1", EMAIL)
    monkeypatch.setattr(engine, "_read_active_credentials", lambda: ActiveCredentials(saved, True, degraded=True))
    with pytest.raises(CredentialReadError):
        engine.current_login_is_saved()


def test_unreadable_backups_do_not_masquerade_as_unsaved(temp_home, monkeypatch):
    engine = _two_org_engine(temp_home)
    monkeypatch.setattr(engine, "_read_account_credentials_ex", lambda *_: ("", True))
    assert engine.current_login_is_saved() is None


def test_unreadable_unrelated_slot_does_not_hide_a_saved_copy(temp_home, monkeypatch):
    engine = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    read = engine._read_account_credentials_ex
    monkeypatch.setattr(
        engine, "_read_account_credentials_ex",
        lambda num, email: ("", True) if num == "1" else read(num, email),
    )
    assert engine.current_login_is_saved() is True


def test_saved_copy_matches_its_config_regardless_of_case(temp_home, monkeypatch):
    engine = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    config = {"oauthAccount": {"emailAddress": EMAIL.upper(), "organizationUuid": ADS_ORG.upper()}}
    monkeypatch.setattr(engine, "_read_account_config", lambda *_: json.dumps(config))
    assert engine.current_login_is_saved() is True


@pytest.mark.parametrize("config", ["", "{}", '{"oauthAccount":{"emailAddress":"other@example.com"}}'])
def test_saved_token_needs_a_matching_config_to_be_restorable(temp_home, monkeypatch, config):
    engine = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    monkeypatch.setattr(engine, "_read_account_config", lambda *_: config)
    assert engine.current_login_is_saved() is False


def test_saved_login_still_respects_worker_lease(temp_home):
    engine = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    AccountLeaseStore(engine.backup_dir, "claude").acquire(
        job_id="job-1", account_identity=stable_account_identity("claude", "acct-1"),
        worker_pid=123, worker_epoch=7, ttl_s=60,
    )
    with pytest.raises(LeaseConflictError):
        engine.current_login_is_saved()


def _add_app():
    app = _claude_add_app()
    app.live = ("a@x.com", "different-org")
    app.switcher._get_current_identity_triple = lambda **_: (*app.live, "uuid")
    app._claude_bin = lambda: "/bin/claude"
    app.dialogs = []

    def alert(**kwargs):
        app.dialogs.append(kwargs)
        return 0

    app._alert = alert
    app.starts = []
    app._start_claude_login = lambda *args, **kwargs: app.starts.append((args, kwargs))
    return app


def test_add_skips_warning_for_saved_credential_with_unmatched_config():
    app = _add_app()
    app.switcher.current_login_is_saved = lambda: True

    app.on_add_claude_login(None)

    assert app.dialogs == []
    assert len(app.starts) == 1
    assert app.added == []  # No capture under the unrelated config identity.
    assert "saved accounts stay in OpenSwap" in app.notes[0].body


def test_add_uses_real_saved_credential_when_config_names_an_unsaved_organization(temp_home):
    app = _add_app()
    engine = _two_org_engine(temp_home)
    _set_live_credential(temp_home, "rt-2")
    _unmanaged_config(temp_home)
    app.switcher = engine
    roster_before = engine.sequence_file.read_bytes()
    config_before = (temp_home / ".claude.json").read_bytes()

    app.on_add_claude_login(None)

    assert app.dialogs == []
    assert len(app.starts) == 1
    assert engine.sequence_file.read_bytes() == roster_before
    assert (temp_home / ".claude.json").read_bytes() == config_before


def test_add_explains_same_email_different_organization_and_cancel_keeps_login():
    app = _add_app()
    app.on_add_claude_login(None)

    dialog = app.dialogs[0]
    assert dialog["title"] == "Add another Claude Code account?"
    assert dialog["ok"] == "Continue to sign in"
    assert "saved accounts stay in OpenSwap" in dialog["message"]
    assert "different organization" in dialog["message"]
    assert "couldn't match the current login" in dialog["message"]
    assert "signs it out" not in dialog["message"]
    assert app.starts == app.added == []


def test_add_explains_failed_backup_without_claiming_saved_account_is_lost():
    app = _add_app()
    app.live = ("a@x.com", "org-1")
    from openswap.exceptions import ConfigError
    app.add_error = ConfigError("backup unavailable")
    app.on_add_claude_login(None)

    assert "saved accounts stay in OpenSwap" in app.dialogs[0]["message"]
    assert "latest version" in app.dialogs[0]["message"]
    assert "backup unavailable" in app.dialogs[0]["message"]
    assert app.starts == []


def test_add_confirms_instead_of_blocking_when_some_saved_login_is_unreadable():
    app = _add_app()
    app.switcher.current_login_is_saved = lambda: None

    app.on_add_claude_login(None)

    assert "couldn't check every saved login" in app.dialogs[0]["message"]
    assert app.alerts == [] and app.starts == []


def _case_drifted_add(temp_home, live_org):
    """Roster rows say ``EMAIL.upper()``; the live config says ``EMAIL`` with an unsaved credential."""
    app = _add_app()
    engine = _two_org_engine(temp_home)
    data = json.loads(engine.sequence_file.read_text())
    for account in data["accounts"].values():
        account["email"] = EMAIL.upper()
        account["organizationUuid"] = account["organizationUuid"].upper()
    engine.sequence_file.write_text(json.dumps(data))
    config = temp_home / ".claude.json"
    live = json.loads(config.read_text())
    live["oauthAccount"]["organizationUuid"] = live_org
    config.write_text(json.dumps(live))
    _set_live_credential(temp_home, "rt-unsaved")
    app.switcher = engine
    app.on_add_claude_login(None)
    return app.dialogs[0]["message"]


def test_add_names_the_saved_organization_when_only_the_case_differs(temp_home):
    message = _case_drifted_add(temp_home, "another-org")
    assert "is saved for Ads Online, a personal account" in message
    assert "different organization" in message


def test_add_calls_a_case_drifted_saved_account_older_not_unsaved(temp_home):
    message = _case_drifted_add(temp_home, ADS_ORG)
    assert "saved copy is older" in message
    assert "different organization" not in message
    assert "isn't saved" not in message


def test_nameless_organization_is_not_called_personal():
    message = claude_add_unsaved_live_message("a@x.com", "org-2", {"org-1": ""})
    assert "saved for an unnamed organization" in message
    assert "personal" not in message
