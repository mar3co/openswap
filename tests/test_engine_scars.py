"""Scar tests for the rewritten Engine façade (issue #3).

Drive shipped public methods: leftover Keychain copy, kickoff isolation,
unreadable capture, consume CAS, hot-path structure, widget schema.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from openswap.credentials import (
    LEGACY_BACKUP_SECURITY_SERVICE,
    SECURITY_SERVICE,
    ActiveCredentials,
)
from openswap.engine import Engine
from openswap.exceptions import CredentialReadError
from openswap.kickoff import invoke_kickoff
from openswap.macos_keychain import KeychainError
from openswap.migrations import STATE_FILENAME, migrate_claude_swap_backup_items, run_migrations
from openswap.models import Platform
from openswap.oauth import RefreshOutcome
from openswap.session import SessionManager
from openswap.switcher import ClaudeAccountSwitcher
from openswap.widget_snapshot import SCHEMA_VERSION

from tests.test_engine_abi import (
    ADS_ORG,
    EMAIL,
    PERSONAL_ORG,
    _make_live,
    _seed_oauth,
    _two_org_engine,
)


def test_engine_shim_is_the_implementation() -> None:
    assert ClaudeAccountSwitcher is Engine
    src = inspect.getsource(inspect.getmodule(ClaudeAccountSwitcher))
    # The implementation class lives in engine.engine, not the shim.
    import openswap.switcher as shim

    shim_src = inspect.getsource(shim)
    assert "class ClaudeAccountSwitcher" not in shim_src
    assert "Engine as ClaudeAccountSwitcher" in shim_src


def test_widget_schema_stays_1() -> None:
    assert SCHEMA_VERSION == 1


def test_hot_path_modules_do_not_call_engine_privates() -> None:
    banned = {
        "_get_current_account",
        "_account_kind",
        "_get_sequence_data",
        "_invalidate_session_credentials",
        "_read_account_credentials_ex",
    }
    root = Path(__file__).resolve().parents[1] / "src" / "openswap"
    for name in (
        "menubar.py",
        "autoswitch.py",
        "kickoff.py",
        "cli.py",
        "session.py",
    ):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in banned:
                raise AssertionError(f"{name} still calls {node.attr}")


def test_macos_hot_path_does_not_import_keyring() -> None:
    from openswap.engine import live, slots, snapshot, consume, switch

    for mod in (live, slots, snapshot, consume, switch):
        src = inspect.getsource(mod)
        assert "import keyring" not in src


def test_leftover_claude_swap_backup_copied_once(
    temp_home: Path, block_real_keychain
) -> None:
    s = _two_org_engine(temp_home)
    s.platform = Platform.MACOS
    username = f"account-2-{EMAIL}"
    leftover = json.dumps({"claudeAiOauth": {"accessToken": "at-legacy"}})
    block_real_keychain.set_password(
        LEGACY_BACKUP_SECURITY_SERVICE, username, leftover
    )
    assert block_real_keychain.get_password(SECURITY_SERVICE, username) is None
    assert migrate_claude_swap_backup_items(s) is True
    assert block_real_keychain.get_password(SECURITY_SERVICE, username) == leftover
    # Second run does not overwrite a present destination.
    block_real_keychain.set_password(SECURITY_SERVICE, username, "already-there")
    assert migrate_claude_swap_backup_items(s) is True
    assert block_real_keychain.get_password(SECURITY_SERVICE, username) == "already-there"


def test_leftover_copy_off_macos_is_not_applied(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    s.platform = Platform.LINUX
    assert migrate_claude_swap_backup_items(s) is False


def test_leftover_copy_missing_roster_is_not_applied(temp_home: Path) -> None:
    s = Engine()
    s.platform = Platform.MACOS
    s._setup_directories()
    s.sequence_file.unlink(missing_ok=True)
    assert migrate_claude_swap_backup_items(s) is False


def test_leftover_copy_keychain_error_is_not_applied(
    temp_home: Path, monkeypatch
) -> None:
    s = _two_org_engine(temp_home)
    s.platform = Platform.MACOS

    def boom(*_a, **_k):
        raise KeychainError("locked")

    monkeypatch.setattr("openswap.migrations.macos_keychain.get_password", boom)
    assert migrate_claude_swap_backup_items(s) is False


def test_leftover_copy_not_marked_without_roster(temp_home: Path) -> None:
    s = Engine()
    s.platform = Platform.MACOS
    s._setup_directories()
    s.sequence_file.unlink(missing_ok=True)
    run_migrations(s)
    state_path = s.backup_dir / STATE_FILENAME
    applied = {}
    if state_path.exists():
        applied = json.loads(state_path.read_text(encoding="utf-8")).get("applied") or {}
    assert "claude_swap_backup_to_openswap" not in applied


def test_idle_kickoff_sets_config_dir_and_does_not_rewrite_default(
    temp_home: Path,
) -> None:
    s = _two_org_engine(temp_home)
    claude_json = temp_home / ".claude.json"
    before = claude_json.read_text(encoding="utf-8")
    mgr = SessionManager(s)
    from openswap.session import session_dir_for

    session_dir = session_dir_for(s.backup_dir, "2", EMAIL)
    session_dir.mkdir(parents=True, exist_ok=True)
    with patch.object(mgr, "_is_session_valid", return_value=True):
        got, num, email = mgr.setup_session("2", share=True, share_history=False)
    assert num == "2"
    assert email == EMAIL
    assert got == session_dir
    assert claude_json.read_text(encoding="utf-8") == before

    captured: dict = {}

    def fake_which(name: str):
        return "/opt/fake/claude" if name == "claude" else None

    def fake_run(argv, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    invoke_kickoff(
        session_dir,
        which=fake_which,
        run=fake_run,
        environ={"PATH": "/usr/bin"},
    )
    assert captured["env"]["CLAUDE_CONFIG_DIR"] == str(session_dir)

    captured_live: dict = {}

    def fake_run_live(argv, **kwargs):
        captured_live["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    invoke_kickoff(
        None,
        which=fake_which,
        run=fake_run_live,
        environ={"PATH": "/usr/bin", "CLAUDE_CONFIG_DIR": "/tmp/other"},
    )
    assert "CLAUDE_CONFIG_DIR" not in captured_live["env"]


def test_degraded_live_is_not_captured(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    degraded = ActiveCredentials(
        value="{}", keychain_unavailable=True, degraded=True
    )
    with patch.object(s, "_read_active_credentials", return_value=degraded):
        with pytest.raises(CredentialReadError):
            s.add_account()
    # Outgoing/idle slot backup unchanged.
    blob = s.read_account_credentials("2", EMAIL)
    assert "rt-2" in blob


def test_consume_same_snapshot_posts_refresh_once(temp_home: Path) -> None:
    s = _two_org_engine(temp_home)
    creds = s.read_account_credentials("1", EMAIL)
    successor = json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "at-new",
                "refreshToken": "rt-new",
                "expiresAt": 9999999999000,
            }
        }
    )
    calls = {"n": 0}

    def fake_refresh(blob, timeout_s=10.0):
        calls["n"] += 1
        return RefreshOutcome(credentials=successor, error=None)

    with patch(
        "openswap.oauth.try_refresh_oauth_credentials", side_effect=fake_refresh
    ):
        s.consume_backup_grant("1", EMAIL, creds)
        s.consume_backup_grant("1", EMAIL, creds)
    assert calls["n"] == 1


def test_cli_list_status_switch_construct_engine() -> None:
    src = (
        Path(__file__).resolve().parents[1] / "src" / "openswap" / "cli.py"
    ).read_text(encoding="utf-8")
    assert "from openswap.engine import Engine" in src
    assert "ClaudeAccountSwitcher = Engine" in src
