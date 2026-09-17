from __future__ import annotations

import json
import sys
from unittest.mock import Mock

import pytest

from openswap.cli import main


def run_cli(args: list[str]) -> int:
    sys.argv = ["openswap", *args]
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.fixture
def desktop_backend(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    engine = Mock(name="CodexEngine")
    backend = Mock(name="DesktopSwitcher")
    constructor = Mock(return_value=backend)
    monkeypatch.setattr("openswap.codex.engine.CodexEngine", Mock(return_value=engine))
    monkeypatch.setattr("openswap.codex.desktop.DesktopSwitcher", constructor)
    return constructor, backend


def test_desktop_status_human_is_explicitly_experimental(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    constructor, backend = desktop_backend
    backend.preflight.return_value = {
        "experimental": True,
        "target": {"number": "2", "email": "work@example.test"},
        "running": True,
        "warning": "Relaunch does not verify the desktop identity.",
    }

    assert run_cli(["codex", "desktop", "status", "2"]) == 0

    constructor.assert_called_once()
    backend.preflight.assert_called_once_with("2")
    out = capsys.readouterr().out
    assert "Experimental" in out
    assert "preflight completed" in out
    assert "work@example.test" in out
    assert "ChatGPT: running" in out
    assert "local and remote work" in out
    assert "quit and relaunched" in out


def test_desktop_status_json_preserves_backend_payload(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    _constructor, backend = desktop_backend
    payload = {"status": "blocked", "experimental": True, "reasons": ["busy"]}
    backend.preflight.return_value = payload

    assert run_cli(["codex", "desktop", "status", "2", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == payload
    backend.preflight.assert_called_once_with("2")


@pytest.mark.parametrize("flag", ["--confirm-restart", "--confirm-idle"])
def test_desktop_switch_requires_both_confirmations(
    desktop_backend: tuple[Mock, Mock], flag: str
) -> None:
    _constructor, backend = desktop_backend

    assert run_cli(["codex", "desktop", "switch", "2", flag]) == 2

    backend.switch.assert_not_called()


def test_desktop_switch_forwards_confirmations_and_requires_manual_verification(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    _constructor, backend = desktop_backend
    backend.switch.return_value = {
        "status": "awaiting_verification",
        "experimental": True,
        "target": {"number": "2"},
    }

    assert run_cli(
        [
            "codex",
            "desktop",
            "switch",
            "2",
            "--confirm-restart",
            "--confirm-idle",
        ]
    ) == 0

    backend.switch.assert_called_once_with(
        "2", confirm_restart=True, confirm_idle=True
    )
    out = capsys.readouterr().out
    assert "awaiting verification" in out
    assert "manually verify" in out
    assert "Chat, Work, and Codex" in out
    assert "authenticated" not in out


def test_desktop_switch_json_never_relabels_backend_result_as_verified(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    _constructor, backend = desktop_backend
    payload = {
        "status": "awaiting_verification",
        "experimental": True,
        "desktopAuthenticated": False,
    }
    backend.switch.return_value = payload

    assert run_cli(
        [
            "codex",
            "desktop",
            "switch",
            "2",
            "--confirm-restart",
            "--confirm-idle",
            "--json",
        ]
    ) == 0

    assert json.loads(capsys.readouterr().out) == payload


def test_desktop_help_states_scope_and_manual_verification(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert run_cli(["codex", "desktop", "--help"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Experimental macOS" in out
    assert "shared" in out
    assert "local and remote work" in out
    assert "Chat, Work, and Codex" in out
    assert "recovery-status" in out
    assert "recover" in out


def test_desktop_target_must_be_a_positive_account_number(
    desktop_backend: tuple[Mock, Mock]
) -> None:
    _constructor, backend = desktop_backend

    assert run_cli(["codex", "desktop", "status", "work"]) == 2
    assert run_cli(["codex", "desktop", "status", "0"]) == 2
    backend.preflight.assert_not_called()


def test_desktop_backend_error_uses_existing_codex_error_policy(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    from openswap.codex.desktop import DesktopSwitchError

    _constructor, backend = desktop_backend
    backend.preflight.side_effect = DesktopSwitchError("desktop preflight refused")

    assert run_cli(["codex", "desktop", "status", "2"]) == 1
    captured = capsys.readouterr()
    assert "desktop preflight refused" in captured.err
    assert captured.out == ""


def test_desktop_recovery_status_human_is_sanitized(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    _constructor, backend = desktop_backend
    backend.recovery_status.return_value = {
        "status": "pending",
        "pending": True,
        "experimental": True,
        "from": {"number": "1"},
        "to": {"number": "2"},
        "warning": "Keep ChatGPT stopped until recovery completes.",
    }

    assert run_cli(["codex", "desktop", "recovery-status"]) == 0

    backend.recovery_status.assert_called_once_with()
    out = capsys.readouterr().out
    assert "recovery status: pending" in out
    assert "Keep ChatGPT stopped" in out
    assert "auth.json" not in out
    assert "credential" not in out.lower()


def test_desktop_recovery_status_json_preserves_sanitized_payload(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    _constructor, backend = desktop_backend
    payload = {
        "status": "none",
        "experimental": True,
        "pending": False,
    }
    backend.recovery_status.return_value = {
        **payload,
        "journal": {"live": "must-never-print"},
    }

    assert run_cli(["codex", "desktop", "recovery-status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize("flag", ["--confirm-restart", "--confirm-idle"])
def test_desktop_recover_requires_both_confirmations(
    desktop_backend: tuple[Mock, Mock], flag: str
) -> None:
    _constructor, backend = desktop_backend

    assert run_cli(["codex", "desktop", "recover", flag]) == 2
    backend.recover.assert_not_called()


def test_desktop_recover_forwards_confirmations_without_claiming_authentication(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    _constructor, backend = desktop_backend
    backend.recover.return_value = {
        "status": "recovered_awaiting_verification",
        "experimental": True,
        "restored": {"number": "1"},
        "warning": "ChatGPT remains stopped; launch it and verify manually.",
    }

    assert run_cli(
        [
            "codex",
            "desktop",
            "recover",
            "--confirm-restart",
            "--confirm-idle",
        ]
    ) == 0

    backend.recover.assert_called_once_with(
        confirm_restart=True, confirm_idle=True
    )
    out = capsys.readouterr().out
    assert "recovered_awaiting_verification" in out
    assert "may remain stopped" in out
    assert "does not verify" in out
    assert "authenticated" not in out


def test_desktop_recover_json_preserves_backend_payload(
    desktop_backend: tuple[Mock, Mock], capsys: pytest.CaptureFixture[str]
) -> None:
    _constructor, backend = desktop_backend
    payload = {
        "status": "recovered_awaiting_verification",
        "experimental": True,
        "restored": {"number": "1"},
        "warning": "Verify manually.",
    }
    backend.recover.return_value = {
        **payload,
        "journal": {"live": "must-never-print"},
        "restored": {"number": "1", "rawCredential": "must-never-print"},
    }

    assert run_cli(
        [
            "codex",
            "desktop",
            "recover",
            "--confirm-restart",
            "--confirm-idle",
            "--json",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == payload
