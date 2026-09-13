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
