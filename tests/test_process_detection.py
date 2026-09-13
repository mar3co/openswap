"""Tests for Claude Code process detection."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from openswap.process_detection import (
    ClaudeSession,
    CodexProcess,
    IdeInstance,
    classify_codex_argv,
    get_claude_dir,
    get_running_codex_instances,
    get_running_instances,
    is_codex_comm,
    is_pid_alive,
    list_ide_instances,
    list_sessions,
    parse_process_table,
)
from openswap.printer import abbreviate_path, entrypoint_label, format_age


# --- get_claude_dir ---


class TestGetClaudeDir:
    def test_default_path(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            result = get_claude_dir()
            assert result == Path.home() / ".claude"

    def test_respects_env_var(self, tmp_path):
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(tmp_path)}):
            assert get_claude_dir() == tmp_path


# --- is_pid_alive ---


class TestIsPidAlive:
    # The os.kill(pid, 0) semantics below are the POSIX branch; on Windows
    # is_pid_alive() dispatches to _is_pid_alive_windows() and never calls
    # os.kill. Pin the platform so these exercise the intended path on any host.
    def test_alive_pid(self):
        with patch("openswap.process_detection.sys.platform", "linux"), \
             patch("os.kill") as mock_kill:
            mock_kill.return_value = None
            assert is_pid_alive(12345) is True
            mock_kill.assert_called_once_with(12345, 0)

    def test_dead_pid(self):
        with patch("openswap.process_detection.sys.platform", "linux"), \
             patch("os.kill", side_effect=OSError("No such process")):
            assert is_pid_alive(12345) is False

    def test_permission_error_means_alive(self):
        with patch("openswap.process_detection.sys.platform", "linux"), \
             patch("os.kill", side_effect=PermissionError("Operation not permitted")):
            assert is_pid_alive(12345) is True

    def test_windows_dispatches_to_ctypes_impl(self):
        """On win32, is_pid_alive() delegates to the ctypes-based helper."""
        with patch("openswap.process_detection.sys.platform", "win32"), \
             patch(
                 "openswap.process_detection._is_pid_alive_windows",
                 return_value=True,
             ) as mock_win:
            assert is_pid_alive(12345) is True
            mock_win.assert_called_once_with(12345)

    def test_current_process_is_alive(self):
        """Smoke test on the real platform branch (win32 or POSIX)."""
        assert is_pid_alive(os.getpid()) is True

    def test_invalid_pid_zero(self):
        assert is_pid_alive(0) is False

    def test_invalid_pid_one(self):
        assert is_pid_alive(1) is False

    def test_negative_pid(self):
        assert is_pid_alive(-1) is False


# --- list_sessions ---


def _write_session(sessions_dir: Path, pid: int, **overrides) -> Path:
    """Write a session PID file with sensible defaults."""
    data = {
        "pid": pid,
        "sessionId": f"session-{pid}",
        "cwd": "/home/user/project",
        "startedAt": int(time.time() * 1000),
        "kind": "interactive",
        "entrypoint": "cli",
    }
    data.update(overrides)
    path = sessions_dir / f"{pid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestListSessions:
    def test_reads_valid_sessions(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, entrypoint="cli", cwd="/home/user/app")
        _write_session(sessions_dir, 1002, entrypoint="claude-vscode", cwd="/home/user/web")

        with patch("openswap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert len(result) == 2
        pids = {s.pid for s in result}
        assert pids == {1001, 1002}

    def test_filters_dead_pids(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)
        _write_session(sessions_dir, 1002)

        def alive(pid):
            return pid == 1001

        with patch("openswap.process_detection.is_pid_alive", side_effect=alive):
            result = list_sessions(tmp_path)

        assert len(result) == 1
        assert result[0].pid == 1001

    def test_missing_sessions_dir(self, tmp_path):
        assert list_sessions(tmp_path) == []

    @pytest.mark.parametrize(
        "write_bad_file",
        [
            lambda p: p.write_text("not json{{{", encoding="utf-8"),
            lambda p: p.write_bytes(b"\xff\xfe{\"pid\": 1}"),
            lambda p: p.write_text(json.dumps({"pid": 2**31}), encoding="utf-8"),
            lambda p: p.write_text("[" * 2000 + "]" * 2000, encoding="utf-8"),
            lambda p: p.write_text("[]", encoding="utf-8"),
        ],
        ids=["invalid_json", "invalid_utf8", "pid_overflows_c_long",
             "json_nested_too_deep", "json_is_an_array"],
    )
    def test_corrupt_session_file_is_skipped(self, tmp_path, write_bad_file):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        write_bad_file(sessions_dir / "9999.json")

        result = list_sessions(tmp_path)
        assert result == []

    def test_missing_pid_field(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "9999.json").write_text(
            json.dumps({"sessionId": "abc", "cwd": "/tmp"}), encoding="utf-8"
        )

        result = list_sessions(tmp_path)
        assert result == []

    def test_optional_status_field(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001, status="busy")

        with patch("openswap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert result[0].status == "busy"

    def test_status_absent(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)

        with patch("openswap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        assert result[0].status is None

    def test_session_fields(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(
            sessions_dir, 5000,
            sessionId="sess-abc",
            cwd="/projects/foo",
            startedAt=1700000000000,
            kind="bg",
            entrypoint="claude-desktop",
        )

        with patch("openswap.process_detection.is_pid_alive", return_value=True):
            result = list_sessions(tmp_path)

        s = result[0]
        assert s.pid == 5000
        assert s.session_id == "sess-abc"
        assert s.cwd == "/projects/foo"
        assert s.started_at == 1700000000000
        assert s.kind == "bg"
        assert s.entrypoint == "claude-desktop"


# --- list_ide_instances ---


def _write_ide_lock(ide_dir: Path, port: int, **overrides) -> Path:
    """Write an IDE lockfile with sensible defaults."""
    data = {
        "pid": port + 1000,
        "workspaceFolders": ["/home/user/project"],
        "ideName": "Visual Studio Code",
        "transport": "ws",
    }
    data.update(overrides)
    path = ide_dir / f"{port}.lock"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestListIdeInstances:
    def test_reads_valid_lockfiles(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, ideName="Visual Studio Code")
        _write_ide_lock(ide_dir, 45001, ideName="Cursor")

        with patch("openswap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert len(result) == 2
        names = {i.ide_name for i in result}
        assert names == {"Visual Studio Code", "Cursor"}

    def test_filters_dead_pids(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, pid=2001)
        _write_ide_lock(ide_dir, 45001, pid=2002)

        with patch("openswap.process_detection.is_pid_alive", side_effect=lambda p: p == 2001):
            result = list_ide_instances(tmp_path)

        assert len(result) == 1
        assert result[0].pid == 2001

    def test_missing_ide_dir(self, tmp_path):
        assert list_ide_instances(tmp_path) == []

    @pytest.mark.parametrize(
        "write_bad_file",
        [
            lambda p: p.write_text("broken", encoding="utf-8"),
            lambda p: p.write_text(json.dumps({"pid": 2**31}), encoding="utf-8"),
            lambda p: p.write_text("[" * 2000 + "]" * 2000, encoding="utf-8"),
            lambda p: p.write_text("[]", encoding="utf-8"),
        ],
        ids=["invalid_json", "pid_overflows_c_long", "json_nested_too_deep",
             "json_is_an_array"],
    )
    def test_corrupt_json(self, tmp_path, write_bad_file):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        write_bad_file(ide_dir / "9999.lock")

        assert list_ide_instances(tmp_path) == []

    def test_missing_pid_field(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        (ide_dir / "9999.lock").write_text(
            json.dumps({"ideName": "VS Code"}), encoding="utf-8"
        )

        assert list_ide_instances(tmp_path) == []

    def test_port_from_filename(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 12345)

        with patch("openswap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert result[0].port == 12345

    def test_workspace_folders(self, tmp_path):
        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000, workspaceFolders=["/a", "/b"])

        with patch("openswap.process_detection.is_pid_alive", return_value=True):
            result = list_ide_instances(tmp_path)

        assert result[0].workspace_folders == ["/a", "/b"]


# --- get_running_instances ---


class TestGetRunningInstances:
    def test_returns_both(self, tmp_path):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        _write_session(sessions_dir, 1001)

        ide_dir = tmp_path / "ide"
        ide_dir.mkdir()
        _write_ide_lock(ide_dir, 45000)

        with patch("openswap.process_detection.is_pid_alive", return_value=True):
            sessions, ides = get_running_instances(tmp_path)

        assert len(sessions) == 1
        assert len(ides) == 1

    def test_empty_when_no_dirs(self, tmp_path):
        sessions, ides = get_running_instances(tmp_path)
        assert sessions == []
        assert ides == []


# --- Display helpers (in switcher.py) ---


class TestEntrypointLabel:
    @pytest.mark.parametrize(
        "entrypoint,expected",
        [
            ("cli", "CLI"),
            ("claude-vscode", "VS Code"),
            ("claude-desktop", "Desktop"),
            ("sdk-cli", "SDK"),
            ("mcp", "MCP"),
            ("unknown-thing", "unknown-thing"),
        ],
    )
    def test_known_and_unknown(self, entrypoint, expected):
        assert entrypoint_label(entrypoint) == expected


class TestAbbreviatePath:
    def test_replaces_home(self):
        home = str(Path.home())
        assert abbreviate_path(f"{home}/projects/foo") == "~/projects/foo"

    def test_non_home_path_unchanged(self):
        assert abbreviate_path("/opt/data/bar") == "/opt/data/bar"

    def test_home_root(self):
        home = str(Path.home())
        assert abbreviate_path(home) == "~"


class TestFormatAge:
    def test_just_now(self):
        now_ms = int(time.time() * 1000)
        assert format_age(now_ms) == "just now"

    def test_minutes(self):
        ms = int((time.time() - 300) * 1000)  # 5 minutes ago
        assert format_age(ms) == "5m ago"

    def test_hours(self):
        ms = int((time.time() - 7200) * 1000)  # 2 hours ago
        assert format_age(ms) == "2h ago"

    def test_days(self):
        ms = int((time.time() - 172800) * 1000)  # 2 days ago
        assert format_age(ms) == "2d ago"


# --- Codex SCAN (process table; never real ps) ---


class TestIsCodexComm:
    def test_basename_codex_and_exe_only(self):
        assert is_codex_comm("codex") is True
        assert is_codex_comm("codex.exe") is True
        assert is_codex_comm("/usr/local/bin/codex") is True
        assert is_codex_comm("C:\\Tools\\codex.exe") is True
        assert is_codex_comm("CODEX.EXE") is True
        assert is_codex_comm("codex-cli") is False
        assert is_codex_comm("node") is False
        assert is_codex_comm("claude") is False
        assert is_codex_comm("") is False


class TestClassifyCodexArgv:
    def test_tui_bare_resume_fork(self):
        assert classify_codex_argv(["codex"]) == "tui"
        assert classify_codex_argv(["/opt/homebrew/bin/codex"]) == "tui"
        assert classify_codex_argv(["codex", "resume"]) == "tui"
        assert classify_codex_argv(["codex", "resume", "sess-1"]) == "tui"
        assert classify_codex_argv(["codex", "fork"]) == "tui"
        # --sandbox takes a MODE; resume after the mode is still TUI.
        assert classify_codex_argv(
            ["codex", "--sandbox", "workspace-write", "resume"]
        ) == "tui"

    def test_tui_skips_value_taking_flags(self):
        assert classify_codex_argv(["codex", "-m", "o3"]) == "tui"
        assert classify_codex_argv(["codex", "--sandbox", "workspace-write"]) == "tui"
        assert classify_codex_argv(["codex", "-C", "/tmp"]) == "tui"
        assert classify_codex_argv(["codex", "--model=o3"]) == "tui"
        # Value skipped, then no subcommand remains (resume was the MODE).
        assert classify_codex_argv(["codex", "--sandbox", "resume"]) == "tui"

    def test_exec(self):
        assert classify_codex_argv(["codex", "exec", "hi"]) == "exec"
        assert classify_codex_argv(["codex", "e", "hi"]) == "exec"
        assert classify_codex_argv(["/usr/bin/codex", "exec"]) == "exec"
        assert classify_codex_argv(["codex", "-m", "o3", "exec", "hi"]) == "exec"

    def test_app_server(self):
        assert classify_codex_argv(["codex", "app-server"]) == "app-server"
        assert classify_codex_argv(["codex", "app-server", "--listen"]) == "app-server"
        assert classify_codex_argv(["codex", "-m", "o3", "app-server"]) == "app-server"

    def test_app(self):
        assert classify_codex_argv(["codex", "app"]) == "app"

    def test_other(self):
        assert classify_codex_argv(["codex", "login"]) == "other"
        assert classify_codex_argv(["codex", "mcp"]) == "other"
        assert classify_codex_argv(["codex", "exec-not"]) == "other"


class TestParseProcessTable:
    def test_filters_non_codex(self):
        rows = [
            (10, "node", ["node", "server"]),
            (11, "codex", ["codex", "exec", "hi"]),
            (12, "claude", ["claude"]),
            (13, "codex-cli", ["codex-cli"]),
        ]
        got = parse_process_table(rows)
        assert [p.pid for p in got] == [11]
        assert got[0].kind == "exec"
        assert got[0].argv == ["codex", "exec", "hi"]
        assert isinstance(got[0], CodexProcess)

    def test_pid_zero_and_one_dropped(self):
        rows = [
            (0, "codex", ["codex"]),
            (1, "codex", ["codex"]),
            (2, "codex", ["codex"]),
        ]
        got = parse_process_table(rows)
        assert [p.pid for p in got] == [2]


class TestGetRunningCodexInstances:
    def test_tui_only(self):
        rows = [
            (100, "codex", ["codex"]),
            (101, "codex", ["codex", "resume", "abc"]),
            (102, "codex", ["codex", "fork"]),
            (103, "codex", ["codex", "exec", "hi"]),
            (104, "codex", ["codex", "e", "hi"]),
            (105, "codex", ["codex", "app-server"]),
            (106, "codex", ["codex", "app"]),
            (107, "codex", ["codex", "login"]),
            (108, "node", ["node", "foo"]),
        ]
        got = get_running_codex_instances(ps=lambda: rows)
        assert [p.pid for p in got] == [100, 101, 102]
        assert all(p.kind == "tui" for p in got)

    def test_empty(self):
        assert get_running_codex_instances(ps=lambda: []) == []

    def test_injected_ps_never_calls_real_ps(self):
        with patch("openswap.process_detection.subprocess.run") as run:
            got = get_running_codex_instances(
                ps=lambda: [(9, "codex", ["codex", "resume"])]
            )
        run.assert_not_called()
        assert len(got) == 1
        assert got[0].pid == 9
        assert got[0].kind == "tui"

    def test_pid_zero_and_one_dropped(self):
        rows = [
            (0, "codex", ["codex"]),
            (1, "codex", ["codex"]),
            (42, "codex", ["codex"]),
        ]
        got = get_running_codex_instances(ps=lambda: rows)
        assert [p.pid for p in got] == [42]
