"""Tests for Codex auth.json export/import envelopes."""

from __future__ import annotations

import json
import os
import sys

import pytest

from openswap.exceptions import TransferError
from openswap.codex.transfer import export_accounts, import_accounts
from tests.test_codex_auth import _auth
from tests.test_codex_engine import _engine, _login


def _two_oauth_slots(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-1", refresh="rt-a")
    eng.add_account(alias="work")
    _login(home, email="b@x.com", account_id="acc-2", refresh="rt-b")
    eng.add_account()
    return eng, home


def _add_api_key(eng, key, alias=None):
    eng.home.mkdir(parents=True, exist_ok=True)
    (eng.home / "auth.json").write_text(json.dumps({
        "auth_mode": "apiKey",
        "OPENAI_API_KEY": key,
        "tokens": None,
    }))
    return eng.add_account(alias=alias)


def _api_key_for_slot(eng, number):
    return json.loads(eng._slot_text(str(number)))["OPENAI_API_KEY"]


def test_export_import_round_trip_two_oauth_slots(tmp_path):
    src, _home = _two_oauth_slots(tmp_path)
    out = tmp_path / "codex.openswap"
    export_accounts(src, str(out))

    envelope = json.loads(out.read_text())
    assert envelope["version"] == 1
    assert envelope["provider"] == "codex"
    assert envelope["encrypted"] is False
    by_email = {a["email"]: a for a in envelope["accounts"]}
    assert set(by_email) == {"a@x.com", "b@x.com"}
    assert by_email["a@x.com"]["accountId"] == "acc-1"
    assert by_email["a@x.com"]["alias"] == "work"
    assert by_email["a@x.com"]["auth"]["tokens"]["refresh_token"] == "rt-a"
    assert by_email["b@x.com"]["auth"]["tokens"]["refresh_token"] == "rt-b"
    assert "credentials" not in by_email["a@x.com"]
    assert "config" not in by_email["a@x.com"]

    dst, dst_home = _engine(tmp_path / "dst")
    import_accounts(dst, str(out))

    roster = dst._read_roster()
    assert roster["accounts"]["1"]["email"] == "a@x.com"
    assert roster["accounts"]["1"]["accountId"] == "acc-1"
    assert roster["accounts"]["1"]["alias"] == "work"
    assert roster["accounts"]["2"]["email"] == "b@x.com"
    assert json.loads(dst._slot_text("1"))["tokens"]["refresh_token"] == "rt-a"
    assert json.loads(dst._slot_text("2"))["tokens"]["refresh_token"] == "rt-b"
    assert not (dst_home / "auth.json").exists()


def test_export_import_round_trip_multiple_api_keys(tmp_path):
    src, _home = _engine(tmp_path)
    _add_api_key(src, "sk-first", alias="first")
    _add_api_key(src, "sk-second", alias="second")
    out = tmp_path / "keys.openswap"
    export_accounts(src, str(out))

    dst, _dst_home = _engine(tmp_path / "dst")
    import_accounts(dst, str(out))

    roster = dst._read_roster()
    assert set(roster["accounts"]) == {"1", "2"}
    assert _api_key_for_slot(dst, "1") == "sk-first"
    assert _api_key_for_slot(dst, "2") == "sk-second"
    assert roster["accounts"]["1"]["kind"] == "api_key"
    assert roster["accounts"]["2"]["kind"] == "api_key"


@pytest.mark.parametrize("force", [False, True])
def test_different_api_key_does_not_replace_existing_slot(tmp_path, force):
    src, _home = _engine(tmp_path / "src")
    _add_api_key(src, "sk-imported")
    out = tmp_path / "key.openswap"
    export_accounts(src, str(out))

    dst, _dst_home = _engine(tmp_path / "dst")
    _add_api_key(dst, "sk-local")
    import_accounts(dst, str(out), force=force)

    assert set(dst._read_roster()["accounts"]) == {"1", "2"}
    assert _api_key_for_slot(dst, "1") == "sk-local"
    assert _api_key_for_slot(dst, "2") == "sk-imported"


@pytest.mark.parametrize("force", [False, True])
def test_same_api_key_matches_existing_despite_json_formatting(tmp_path, force):
    src, _home = _engine(tmp_path / "src")
    _add_api_key(src, "sk-same")
    out = tmp_path / "key.openswap"
    export_accounts(src, str(out))

    dst, _dst_home = _engine(tmp_path / "dst")
    _add_api_key(dst, "sk-same")
    # Change stored bytes without changing the semantic credential.
    dst._write_slot("1", '{\n  "tokens": null, "OPENAI_API_KEY": "sk-same", "auth_mode": "apiKey"\n}')
    before = dst._slot_text("1")
    import_accounts(dst, str(out), force=force)

    assert set(dst._read_roster()["accounts"]) == {"1"}
    if force:
        assert json.loads(dst._slot_text("1"))["OPENAI_API_KEY"] == "sk-same"
    else:
        assert dst._slot_text("1") == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "forged@example.com"),
        ("accountId", "forged-account"),
        ("kind", "oauth"),
    ],
)
def test_reject_metadata_mismatch_before_mutation(tmp_path, field, value):
    src, _home = _engine(tmp_path / "src")
    _add_api_key(src, "sk-imported")
    out = tmp_path / "key.openswap"
    export_accounts(src, str(out))
    envelope = json.loads(out.read_text())
    envelope["accounts"][0][field] = value
    out.write_text(json.dumps(envelope))

    dst, _dst_home = _engine(tmp_path / "dst")
    _add_api_key(dst, "sk-local")
    roster_before = dst._read_roster()
    slot_before = dst._slot_text("1")
    with pytest.raises(TransferError, match="does not match auth"):
        import_accounts(dst, str(out), force=True)

    assert dst._read_roster() == roster_before
    assert dst._slot_text("1") == slot_before


def test_import_derives_optional_metadata_and_accepts_stale_plan(tmp_path):
    src, _home = _two_oauth_slots(tmp_path / "src")
    out = tmp_path / "oauth.openswap"
    export_accounts(src, str(out), account="1")
    envelope = json.loads(out.read_text())
    account = envelope["accounts"][0]
    account.pop("accountId")
    account.pop("kind")
    account["planType"] = "stale-plan"
    out.write_text(json.dumps(envelope))

    dst, _dst_home = _engine(tmp_path / "dst")
    import_accounts(dst, str(out))

    record = dst._read_roster()["accounts"]["1"]
    assert record["accountId"] == "acc-1"
    assert record["kind"] == "oauth"
    assert record["planType"] == "plus"


def test_different_api_key_with_existing_alias_drops_imported_alias(tmp_path, capsys):
    src, _home = _engine(tmp_path / "src")
    _add_api_key(src, "sk-imported", alias="work")
    out = tmp_path / "key.openswap"
    export_accounts(src, str(out))

    dst, _dst_home = _engine(tmp_path / "dst")
    _add_api_key(dst, "sk-local", alias="work")
    import_accounts(dst, str(out))

    assert "dropping the imported alias" in capsys.readouterr().err
    roster = dst._read_roster()
    assert roster["accounts"]["1"]["alias"] == "work"
    assert "alias" not in roster["accounts"]["2"]


def test_active_slot_export_uses_live_bytes_when_live_is_newer(tmp_path):
    eng, home = _engine(tmp_path)
    _login(
        home,
        email="a@x.com",
        account_id="acc-1",
        refresh="rt-slot",
        last_refresh="2026-01-01T00:00:00Z",
    )
    eng.add_account()
    _login(
        home,
        email="a@x.com",
        account_id="acc-1",
        refresh="rt-live-new",
        last_refresh="2026-09-12T00:00:00Z",
    )
    out = tmp_path / "live.openswap"
    export_accounts(eng, str(out))
    envelope = json.loads(out.read_text())
    assert envelope["accounts"][0]["auth"]["tokens"]["refresh_token"] == "rt-live-new"


def test_skip_existing_without_force(tmp_path, capsys):
    eng, _home = _two_oauth_slots(tmp_path)
    out = tmp_path / "codex.openswap"
    export_accounts(eng, str(out))
    slot_before = eng._slot_text("1")

    import_accounts(eng, str(out), force=False)
    err = capsys.readouterr().err
    assert "Skipped a@x.com" in err
    assert "use --force" in err
    assert eng._slot_text("1") == slot_before
    assert set(eng._read_roster()["accounts"]) == {"1", "2"}


def test_force_overwrites_existing_slot(tmp_path, capsys):
    eng, _home = _two_oauth_slots(tmp_path)
    out = tmp_path / "codex.openswap"
    export_accounts(eng, str(out), account="1")
    envelope = json.loads(out.read_text())
    envelope["accounts"][0]["number"] = 9
    envelope["accounts"][0]["auth"]["tokens"]["refresh_token"] = "rt-forced"
    out.write_text(json.dumps(envelope))

    import_accounts(eng, str(out), force=True)
    err = capsys.readouterr().err
    assert "Overwrote a@x.com (slot 1)" in err
    assert json.loads(eng._slot_text("1"))["tokens"]["refresh_token"] == "rt-forced"
    assert json.loads(eng._slot_text("2"))["tokens"]["refresh_token"] == "rt-b"
    assert set(eng._read_roster()["accounts"]) == {"1", "2"}


def test_import_reuses_exported_number_else_next_free(tmp_path):
    src, _home = _engine(tmp_path)
    _login(src.home, email="a@x.com", account_id="acc-1")
    src.add_account()
    out = tmp_path / "a.openswap"
    export_accounts(src, str(out))

    dst, _dst_home = _engine(tmp_path / "dst")
    _login(dst.home, email="b@x.com", account_id="acc-b")
    dst.add_account()
    import_accounts(dst, str(out))
    roster = dst._read_roster()
    assert roster["accounts"]["1"]["email"] == "b@x.com"
    assert roster["accounts"]["2"]["email"] == "a@x.com"


def test_alias_collision_drops_imported_alias(tmp_path, capsys):
    src, _home = _engine(tmp_path)
    _login(src.home, email="a@x.com", account_id="acc-1")
    src.add_account(alias="work")
    out = tmp_path / "a.openswap"
    export_accounts(src, str(out))

    dst, _dst_home = _engine(tmp_path / "dst")
    _login(dst.home, email="local@x.com", account_id="acc-local")
    dst.add_account(alias="work")
    import_accounts(dst, str(out))
    err = capsys.readouterr().err
    assert "dropping the imported alias" in err
    roster = dst._read_roster()
    imported = next(n for n, rec in roster["accounts"].items() if rec["email"] == "a@x.com")
    assert "alias" not in roster["accounts"][imported]
    assert roster["accounts"]["1"]["alias"] == "work"


def test_reject_encrypted(tmp_path):
    eng, _home = _two_oauth_slots(tmp_path)
    out = tmp_path / "enc.openswap"
    export_accounts(eng, str(out))
    envelope = json.loads(out.read_text())
    envelope["encrypted"] = True
    out.write_text(json.dumps(envelope))
    with pytest.raises(TransferError, match="encrypted exports are not supported"):
        import_accounts(eng, str(out))


def test_reject_missing_provider(tmp_path):
    eng, _home = _two_oauth_slots(tmp_path)
    out = tmp_path / "noprov.openswap"
    export_accounts(eng, str(out))
    envelope = json.loads(out.read_text())
    del envelope["provider"]
    out.write_text(json.dumps(envelope))
    with pytest.raises(TransferError, match="provider"):
        import_accounts(eng, str(out))


def test_reject_claude_envelope(tmp_path):
    eng, _home = _engine(tmp_path)
    out = tmp_path / "claude.openswap"
    out.write_text(json.dumps({
        "version": 1,
        "exportedAt": "2026-01-01T00:00:00Z",
        "exportedFrom": "macos",
        "swapVersion": "0.0.0",
        "encrypted": False,
        "activeAccountNumber": 1,
        "accounts": [{
            "number": 1,
            "email": "a@x.com",
            "uuid": "u",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2026-01-01T00:00:00Z",
            "credentials": {"claudeAiOauth": {"refreshToken": "rt"}},
            "config": {"oauthAccount": {"emailAddress": "a@x.com"}},
        }],
    }))
    with pytest.raises(TransferError):
        import_accounts(eng, str(out))
    assert eng._read_roster().get("accounts") in ({}, None) or not eng._read_roster().get("accounts")


def test_export_stdout_is_json_only(tmp_path, capsys):
    eng, _home = _two_oauth_slots(tmp_path)
    export_accounts(eng, "-")
    captured = capsys.readouterr()
    envelope = json.loads(captured.out)
    assert envelope["provider"] == "codex"
    assert len(envelope["accounts"]) == 2
    assert "Exported" not in captured.out
    assert "Exported" not in captured.err


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only chmod check")
def test_export_file_is_0600(tmp_path):
    eng, _home = _two_oauth_slots(tmp_path)
    out = tmp_path / "perm.openswap"
    export_accounts(eng, str(out))
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
