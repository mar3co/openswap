import json
from pathlib import Path
import sys

import pytest
from openswap.codex.engine import CodexEngine, CodexAuthError, CodexSwitchError
from openswap.exceptions import ValidationError
from openswap.engine.protocol import AccountEngine
from openswap.json_output import USAGE_API_KEY, USAGE_NO_CREDENTIALS
from tests.test_codex_auth import _auth

def _engine(tmp_path, limits=None, codex_bin="/opt/codex"):
    calls = []
    def read_limits(home, *, codex_bin, **kw):
        calls.append(Path(home))
        if isinstance(limits, Exception):
            raise limits
        return limits or {"primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 1788265323},
                          "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 1788765541}}
    home = tmp_path / "codex-home"
    eng = CodexEngine(backup_dir=tmp_path / "backup", home=home,
                      codex_bin=lambda: codex_bin, read_limits=read_limits, clock=lambda: 1_788_000_000.0)
    eng._test_calls = calls
    return eng, home

def _login(home: Path, **kw) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(_auth(**kw))

def test_satisfies_protocol(tmp_path):
    eng, _ = _engine(tmp_path)
    assert isinstance(eng, AccountEngine) and eng.provider == "codex"

def test_add_captures_live_login_into_slot_dir(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    num = eng.add_account()
    assert num == "1"
    slot = eng.slots_dir / "1" / "auth.json"
    assert slot.read_text() == (home / "auth.json").read_text()
    if sys.platform != "win32":
        assert oct(slot.stat().st_mode & 0o777) == "0o600"
    assert eng.current_account_number() == "1"
    assert eng.live_identity() == ("a@x.com", "acc-a")
    assert eng.slot_identity("1") == ("a@x.com", "acc-a")

def test_add_refuses_missing_and_duplicate(tmp_path):
    eng, home = _engine(tmp_path)
    with pytest.raises(CodexAuthError, match="codex login"):
        eng.add_account()
    _login(home)
    eng.add_account()
    with pytest.raises(CodexAuthError, match="already"):
        eng.add_account()

def test_switch_to_writes_live_and_captures_outgoing(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a", refresh="rt-a1")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    # Codex refreshed the live (b) token since we captured it:
    _login(home, email="b@x.com", account_id="acc-b", refresh="rt-b2")
    result = eng.switch_to("1", json_output=True)
    assert result["switched"] is True
    assert result["to"] == {"number": "1", "email": "a@x.com"}
    assert result["from"] == {"number": "2", "email": "b@x.com"}
    assert "rt-a1" in (home / "auth.json").read_text()
    assert "rt-b2" in (eng.slots_dir / "2" / "auth.json").read_text()   # newest generation kept
    assert eng.current_account_number() == "1"

def test_switch_to_same_slot_is_already_active(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    assert eng.switch_to("1", json_output=True)["reason"] == "already-active"

def test_switch_refuses_unmanaged_live_login_unless_forced(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a"); eng.add_account()
    _login(home, email="stranger@x.com", account_id="acc-s")
    with pytest.raises(CodexSwitchError, match="not managed"):
        eng.switch_to("1", json_output=True)
    assert eng.switch_to("1", json_output=True, force=True)["switched"] is True

def test_rotate_skips_disabled_and_wraps(tmp_path):
    eng, home = _engine(tmp_path)
    for e in ("a", "b", "c"):
        _login(home, email=f"{e}@x.com", account_id=f"acc-{e}"); eng.add_account()
    eng.switch_to("1", json_output=True)
    eng.set_account_disabled("2", True)
    assert eng.switch(json_output=True)["to"]["number"] == "3"
    assert eng.switch(json_output=True)["to"]["number"] == "1"
    assert eng.switchable_account_numbers() == ["1", "3"]

def test_snapshot_reads_idle_slot_from_its_own_home(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a"); eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b"); eng.add_account()
    snap = eng.accounts_snapshot(fetch=None)
    assert [a.number for a in snap.accounts] == ["1", "2"]
    assert snap.active_number == "2"
    assert all(a.provider == "codex" for a in snap.accounts)
    assert snap.accounts[0].usage.last_good["five_hour"]["pct"] == 10.0
    assert snap.accounts[0].org_name == "plus" and snap.accounts[0].org_uuid == "acc-a"
    assert set(eng._test_calls) == {eng.slots_dir / "1", home}

def test_snapshot_store_only_never_calls_codex(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    eng.accounts_snapshot(fetch=set())
    assert eng._test_calls == []

def test_api_key_slot_is_switchable_but_has_no_bars(tmp_path):
    eng, home = _engine(tmp_path)
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk"}))
    eng.add_account()
    snap = eng.accounts_snapshot()
    assert eng.account_kind_for("1") == "api_key"
    assert snap.accounts[0].usage.sentinel == USAGE_API_KEY
    assert snap.accounts[0].switchable is True
    assert eng._test_calls == []          # never asks app-server for an API key

def test_missing_slot_file_is_no_credentials(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    (eng.slots_dir / "1" / "auth.json").unlink()
    snap = eng.accounts_snapshot()
    assert snap.accounts[0].usage.sentinel == USAGE_NO_CREDENTIALS
    assert snap.accounts[0].switchable is False
    assert eng.switchable_account_numbers() == []

def test_snapshot_records_failure_and_keeps_last_good(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    good = eng.accounts_snapshot().accounts[0].usage.last_good
    eng._read_limits = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    eng._usage_store.clock = lambda: 1_788_001_000.0   # past SERVE_TTL_S
    snap = eng.accounts_snapshot()
    assert snap.accounts[0].usage.last_good == good
    assert snap.accounts[0].usage.last_error

def test_codex_not_installed_is_reported_not_raised(tmp_path):
    eng, home = _engine(tmp_path, codex_bin=None)
    _login(home); eng.add_account()
    snap = eng.accounts_snapshot()
    assert snap.accounts[0].usage.last_error == "codex-not-installed"

def test_freshen_backup_is_ok(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    assert eng.freshen_backup("1", "a@x.com") == "ok"

def test_alias_and_remove(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com"); eng.add_account()
    eng.set_alias("1", "work")
    assert eng.list_aliases() == [("1", "work", "a@x.com")]
    assert eng.resolve_account("work")[0] == "1"
    eng.remove_account("work", assume_yes=True)
    assert eng.accounts_snapshot(fetch=set()).accounts == ()
    assert not (eng.slots_dir / "1").exists()


def test_snapshot_does_not_clobber_slot_when_live_is_another_account(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a", refresh="rt-a")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b", refresh="rt-b")
    eng.add_account()
    slot2 = (eng.slots_dir / "2" / "auth.json").read_text()
    # Out-of-band login as slot 1; roster still says 2 is active.
    _login(home, email="a@x.com", account_id="acc-a", refresh="rt-a-new")
    snap = eng.accounts_snapshot()
    assert (eng.slots_dir / "2" / "auth.json").read_text() == slot2
    assert "rt-a-new" in (eng.slots_dir / "1" / "auth.json").read_text()
    assert eng.current_account_number() == "1"
    assert snap.active_number == "1"
    assert snap.accounts[0].is_active is True
    assert snap.accounts[1].is_active is False


def test_snapshot_does_not_clobber_slot_when_live_is_unmanaged(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a", refresh="rt-a")
    eng.add_account()
    slot1 = (eng.slots_dir / "1" / "auth.json").read_text()
    _login(home, email="stranger@x.com", account_id="acc-s", refresh="rt-s")
    snap = eng.accounts_snapshot()
    assert (eng.slots_dir / "1" / "auth.json").read_text() == slot1
    assert eng.current_account_number() is None
    assert eng.has_live_login() is True
    assert snap.active_number is None
    assert snap.accounts[0].is_active is False


def test_already_active_repairs_stale_roster(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    data = eng._read_roster()
    data["activeAccountNumber"] = "1"
    eng._write_roster(data)
    assert eng.current_account_number() == "2"
    result = eng.switch_to("2", json_output=True)
    assert result["reason"] == "already-active"
    assert str(eng._read_roster()["activeAccountNumber"]) == "2"


def test_remove_prompts_unless_assume_yes(tmp_path, monkeypatch):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com")
    eng.add_account()
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "n")
    eng.remove_account("1")
    assert (eng.slots_dir / "1" / "auth.json").exists()
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "y")
    eng.remove_account("1")
    assert not (eng.slots_dir / "1").exists()


def test_switch_strategy_forwards_force(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    _login(home, email="stranger@x.com", account_id="acc-s")
    with pytest.raises(CodexSwitchError, match="not managed"):
        eng.switch(json_output=True)
    result = eng.switch(json_output=True, force=True)
    assert result["switched"] is True


def test_capture_live_does_not_overwrite_newer_slot(tmp_path):
    eng, home = _engine(tmp_path)
    _login(
        home,
        email="a@x.com",
        account_id="acc-a",
        refresh="rt-old",
        last_refresh="2026-01-01T00:00:00Z",
    )
    eng.add_account()
    (eng.slots_dir / "1" / "auth.json").write_text(
        _auth(
            email="a@x.com",
            account_id="acc-a",
            refresh="rt-new",
            last_refresh="2026-09-10T00:00:00Z",
        )
    )
    assert eng._capture_live("1") is True
    slot = (eng.slots_dir / "1" / "auth.json").read_text()
    assert "rt-new" in slot
    assert "rt-old" not in slot


def test_failed_live_usage_does_not_capture_unchanged_live(tmp_path):
    # Same last_refresh so a generation check alone would still copy live.
    eng, home = _engine(tmp_path)
    _login(
        home,
        email="a@x.com",
        account_id="acc-a",
        refresh="rt-live",
        last_refresh="2026-09-10T00:00:00Z",
    )
    eng.add_account()
    (eng.slots_dir / "1" / "auth.json").write_text(
        _auth(
            email="a@x.com",
            account_id="acc-a",
            refresh="rt-slot",
            last_refresh="2026-09-10T00:00:00Z",
        )
    )
    _login(
        home,
        email="a@x.com",
        account_id="acc-a",
        refresh="rt-live",
        last_refresh="2026-09-10T00:00:00Z",
    )
    eng._read_limits = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    eng.accounts_snapshot()
    assert "rt-slot" in (eng.slots_dir / "1" / "auth.json").read_text()


def test_switch_does_not_capture_outgoing_over_newer_slot(tmp_path):
    eng, home = _engine(tmp_path)
    _login(
        home,
        email="a@x.com",
        account_id="acc-a",
        refresh="rt-a1",
        last_refresh="2026-01-01T00:00:00Z",
    )
    eng.add_account()
    _login(
        home,
        email="b@x.com",
        account_id="acc-b",
        refresh="rt-b1",
        last_refresh="2026-01-01T00:00:00Z",
    )
    eng.add_account()
    (eng.slots_dir / "2" / "auth.json").write_text(
        _auth(
            email="b@x.com",
            account_id="acc-b",
            refresh="rt-b2",
            last_refresh="2026-09-10T00:00:00Z",
        )
    )
    _login(
        home,
        email="b@x.com",
        account_id="acc-b",
        refresh="rt-b1",
        last_refresh="2026-01-01T00:00:00Z",
    )
    eng.switch_to("1", json_output=True)
    assert "rt-b2" in (eng.slots_dir / "2" / "auth.json").read_text()


def test_swap_exchanges_slot_dirs_and_roster_emails(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    a_text = (eng.slots_dir / "1" / "auth.json").read_text()
    b_text = (eng.slots_dir / "2" / "auth.json").read_text()
    num_a, num_b = eng.swap_accounts("1", "2")
    assert (num_a, num_b) == ("1", "2")
    data = eng._read_roster()
    assert data["accounts"]["1"]["email"] == "b@x.com"
    assert data["accounts"]["2"]["email"] == "a@x.com"
    assert (eng.slots_dir / "1" / "auth.json").read_text() == b_text
    assert (eng.slots_dir / "2" / "auth.json").read_text() == a_text
    assert data["sequence"] == [1, 2]


def test_swap_updates_active_account_number(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    assert str(eng._read_roster()["activeAccountNumber"]) == "2"
    eng.swap_accounts("1", "2")
    assert str(eng._read_roster()["activeAccountNumber"]) == "1"


def test_swap_missing_auth_json_is_empty_slot_not_abort(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    (eng.slots_dir / "1" / "auth.json").unlink()
    eng.swap_accounts("1", "2")
    data = eng._read_roster()
    assert data["accounts"]["1"]["email"] == "b@x.com"
    assert data["accounts"]["2"]["email"] == "a@x.com"
    assert (eng.slots_dir / "1" / "auth.json").exists()
    assert not (eng.slots_dir / "2" / "auth.json").exists()


def test_move_to_empty_frees_old_slot(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    src, dest, swapped = eng.move_account("2", "5")
    assert (src, dest, swapped) == ("2", "5", False)
    data = eng._read_roster()
    assert "2" not in data["accounts"]
    assert data["accounts"]["5"]["email"] == "b@x.com"
    assert data["accounts"]["1"]["email"] == "a@x.com"
    assert data["sequence"] == [1, 5]
    assert not (eng.slots_dir / "2").exists()
    assert (eng.slots_dir / "5" / "auth.json").exists()
    assert str(data["activeAccountNumber"]) == "5"


def test_move_to_occupied_is_swap(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    src, dest, swapped = eng.move_account("1", "2")
    assert (src, dest, swapped) == ("1", "2", True)
    data = eng._read_roster()
    assert data["accounts"]["1"]["email"] == "b@x.com"
    assert data["accounts"]["2"]["email"] == "a@x.com"


def test_move_cap_rejects_huge_slot(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    eng.add_account()
    with pytest.raises(ValidationError, match="out of range"):
        eng.move_account("1", "100")
    data = eng._read_roster()
    assert "1" in data["accounts"]
    assert "100" not in data["accounts"]
