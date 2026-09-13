import json
import sys
import pytest
from openswap import paths
from openswap.cli import main
from tests.test_codex_auth import _auth

def run_cli(args: list[str]) -> int:
    """Drive ``main()`` like a shell would; return the exit code (0 when it returns)."""
    sys.argv = ["openswap", *args]
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0

def test_codex_add_list_switch_roundtrip(temp_home, capsys, monkeypatch):
    home = temp_home / ".codex"; home.mkdir()
    (home / "auth.json").write_text(_auth(email="a@x.com", account_id="acc-a"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("openswap.codex.engine.read_rate_limits", lambda *a, **k: {"primary": {"usedPercent": 3, "windowDurationMins": 300}})
    monkeypatch.setattr("shutil.which", lambda name: "/opt/codex" if name == "codex" else None)
    run_cli(["codex", "add"])                       # helper: sets argv, calls main, returns exit code
    (home / "auth.json").write_text(_auth(email="b@x.com", account_id="acc-b"))
    run_cli(["codex", "add", "--alias", "b"])
    assert run_cli(["codex", "switch", "1"]) == 0
    assert "a@x.com" in (home / "auth.json").read_text()
    out = capsys.readouterr().out
    assert "a@x.com" in out and "b@x.com" in out
    assert run_cli(["codex", "switch", "1"]) == 2   # already active

def _one_codex_account(temp_home, monkeypatch):
    home = temp_home / ".codex"; home.mkdir()
    (home / "auth.json").write_text(_auth(email="c@x.com", account_id="acc-c"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("openswap.codex.engine.read_rate_limits", lambda *a, **k: {"primary": {"usedPercent": 3, "windowDurationMins": 300}})
    monkeypatch.setattr("shutil.which", lambda name: "/opt/codex" if name == "codex" else None)
    assert run_cli(["codex", "add"]) == 0

def test_list_shows_codex_section_only_when_present(temp_home, capsys, monkeypatch):
    run_cli(["list"])
    assert "Codex" not in capsys.readouterr().out
    _one_codex_account(temp_home, monkeypatch)
    capsys.readouterr()
    run_cli(["list"])
    out = capsys.readouterr().out
    assert "Codex" in out and "c@x.com" in out

def test_list_json_schema_unchanged(temp_home, capsys, monkeypatch):
    _one_codex_account(temp_home, monkeypatch)
    capsys.readouterr()
    run_cli(["list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "codex" not in payload and "provider" not in payload
    assert all("c@x.com" != row.get("email") for row in payload.get("accounts", []))

def test_codex_list_json_has_provider(temp_home, capsys, monkeypatch):
    _one_codex_account(temp_home, monkeypatch)
    capsys.readouterr()
    run_cli(["codex", "list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "codex"
    assert payload["accounts"][0]["email"] == "c@x.com"
    assert payload["accounts"][0]["organizationName"] == "plus"


def test_codex_remove_cancel_does_not_claim_success(temp_home, capsys, monkeypatch):
    _one_codex_account(temp_home, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "n")
    assert run_cli(["codex", "remove", "1"]) == 0
    out = capsys.readouterr().out
    assert "Cancelled" in out
    assert "Removed" not in out
    assert (paths.get_backup_root() / "codex" / "slots" / "1" / "auth.json").exists()


def _two_codex_accounts(temp_home, monkeypatch):
    home = temp_home / ".codex"
    home.mkdir()
    (home / "auth.json").write_text(_auth(email="a@x.com", account_id="acc-a", refresh="rt-a"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(
        "openswap.codex.engine.read_rate_limits",
        lambda *a, **k: {"primary": {"usedPercent": 3, "windowDurationMins": 300}},
    )
    monkeypatch.setattr("shutil.which", lambda name: "/opt/codex" if name == "codex" else None)
    assert run_cli(["codex", "add", "--alias", "work"]) == 0
    (home / "auth.json").write_text(_auth(email="b@x.com", account_id="acc-b", refresh="rt-b"))
    assert run_cli(["codex", "add"]) == 0
    return home


def test_codex_export_import_roundtrip(temp_home, capsys, monkeypatch):
    _two_codex_accounts(temp_home, monkeypatch)
    capsys.readouterr()
    out = temp_home / "codex.openswap"
    assert run_cli(["codex", "export", str(out)]) == 0
    envelope = json.loads(out.read_text())
    assert envelope["provider"] == "codex"
    assert {a["email"] for a in envelope["accounts"]} == {"a@x.com", "b@x.com"}
    assert run_cli(["codex", "import", str(out)]) == 0
    assert run_cli(["codex", "import", str(out), "--force"]) == 0
    slot = paths.get_backup_root() / "codex" / "slots" / "1" / "auth.json"
    assert "rt-a" in slot.read_text()
