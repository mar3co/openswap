from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import queue
from unittest.mock import MagicMock, call, patch

import pytest


PROBE_PATH = Path(__file__).parents[1] / "tools" / "probe_codex_auth_reload.py"
SPEC = importlib.util.spec_from_file_location("probe_codex_auth_reload", PROBE_PATH)
assert SPEC is not None and SPEC.loader is not None
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_account_type_only_exposes_type() -> None:
    result = {"account": {"type": "chatgpt", "email": "secret@example.test"}}
    assert probe.account_type(result) == "chatgpt"
    assert probe.account_type({"account": None}) is None
    assert probe.account_type({"account": {"type": "arbitrary secret"}}) == "unknown"


def test_safe_enum_never_returns_unrecognized_protocol_data() -> None:
    assert probe.safe_enum("apikey", probe.AUTH_MODES) == "apikey"
    assert probe.safe_enum("untrusted protocol detail", probe.AUTH_MODES) == "unknown"
    assert probe.safe_enum(123, probe.AUTH_MODES) == "unknown"


def test_child_environment_does_not_inherit_auth_or_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "real-secret")
    monkeypatch.setenv("CODEX_HOME", "/real/codex/home")
    monkeypatch.setenv("CODEX_CONFIG", "/real/config.toml")
    env = probe.isolated_child_env(tmp_path)
    assert "OPENAI_API_KEY" not in env
    assert "CODEX_CONFIG" not in env
    assert env["CODEX_HOME"] == str(tmp_path / "codex")
    assert set(env) == {
        "CODEX_HOME",
        "HOME",
        "USERPROFILE",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "TMPDIR",
        "LANG",
    }


def test_popen_is_fully_isolated_and_forces_file_store(tmp_path: Path) -> None:
    fake_process = MagicMock()
    fake_process.stdout = []
    fake_process.poll.return_value = 0
    with patch.object(probe.subprocess, "Popen", return_value=fake_process) as popen:
        server = probe.JsonRpcProcess(tmp_path / "codex", tmp_path, 0.1)
        server.reader.join(timeout=0.1)

    args, kwargs = popen.call_args
    assert args[0][-2:] == ["--config", 'cli_auth_credentials_store="file"']
    assert kwargs["cwd"] == tmp_path
    assert kwargs["start_new_session"] is (os.name != "nt")
    assert "HTTP_PROXY" not in kwargs["env"]
    assert "HTTPS_PROXY" not in kwargs["env"]
    assert "ALL_PROXY" not in kwargs["env"]
    assert "OPENAI_API_KEY" not in kwargs["env"]


def test_synthetic_auth_is_published_atomically(tmp_path: Path) -> None:
    auth_file = tmp_path / "auth.json"
    real_replace = os.replace
    replacements: list[tuple[Path, Path]] = []

    def recording_replace(source: Path, destination: Path) -> None:
        replacements.append((source, destination))
        real_replace(source, destination)

    with patch.object(probe.os, "replace", side_effect=recording_replace):
        probe.write_synthetic_auth_atomically(auth_file)

    assert replacements == [(tmp_path / ".auth.json.probe-tmp", auth_file)]
    assert json.loads(auth_file.read_text()) == {"OPENAI_API_KEY": "sk-fabricated-file"}
    if os.name != "nt":
        assert auth_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_cleanup_normal_exit_kills_leftover_group() -> None:
    server = object.__new__(probe.JsonRpcProcess)
    server.timeout = 0.1
    server.proc = MagicMock(pid=123)
    server.proc.poll.return_value = None
    with patch.object(probe.os, "killpg") as killpg:
        server._stop_process()
    assert killpg.call_args_list == [
        call(123, probe.signal.SIGTERM),
        call(123, probe.signal.SIGKILL),
    ]
    server.proc.wait.assert_called_once_with(timeout=0.1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_cleanup_timeout_kills_group_and_waits_again() -> None:
    server = object.__new__(probe.JsonRpcProcess)
    server.timeout = 0.1
    server.proc = MagicMock(pid=456)
    server.proc.poll.return_value = None
    server.proc.wait.side_effect = [probe.subprocess.TimeoutExpired("codex", 0.1), 0]
    with patch.object(probe.os, "killpg") as killpg:
        server._stop_process()
    assert killpg.call_args_list == [
        call(456, probe.signal.SIGTERM),
        call(456, probe.signal.SIGKILL),
        call(456, probe.signal.SIGKILL),
    ]
    assert server.proc.wait.call_count == 2


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_cleanup_already_exited_still_kills_leftover_group() -> None:
    server = object.__new__(probe.JsonRpcProcess)
    server.timeout = 0.1
    server.proc = MagicMock(pid=789)
    server.proc.poll.return_value = 0
    with patch.object(probe.os, "killpg") as killpg:
        server._stop_process()
    killpg.assert_called_once_with(789, probe.signal.SIGKILL)
    server.proc.wait.assert_not_called()


@pytest.mark.parametrize("value", [-1.0, math.nan, math.inf, -math.inf])
def test_observe_seconds_must_be_finite_and_non_negative(value: float) -> None:
    with pytest.raises(probe.ProbeError, match="finite and non-negative"):
        probe.validate_observe_seconds(value)


def test_receive_response_times_out_without_blocking() -> None:
    server = object.__new__(probe.JsonRpcProcess)
    server.timeout = 0.01
    server.messages = queue.Queue()
    with pytest.raises(probe.ProbeError, match="timed out"):
        server.receive_response(42)


def test_resolve_binary_returns_absolute_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "codex"
    binary.write_text("")
    monkeypatch.setattr(probe.shutil, "which", lambda _value: str(binary))
    assert probe.resolve_binary("codex") == binary.resolve()
