"""Detect running Claude Code and Codex instances.

Claude: session PID files (~/.claude/sessions/{pid}.json) and IDE lockfiles
(~/.claude/ide/{port}.lock). Codex has no session files — SCAN an injected
process table (or macOS/Linux ``ps``) and keep only TUI processes.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from openswap.paths import get_claude_config_home

logger = logging.getLogger(__name__)


@dataclass
class ClaudeSession:
    """A running Claude Code session from ~/.claude/sessions/{pid}.json."""

    pid: int
    session_id: str
    cwd: str
    started_at: int  # epoch milliseconds
    kind: str  # "interactive", "bg", "daemon", "daemon-worker"
    entrypoint: str  # "cli", "claude-vscode", "claude-desktop", "sdk-cli", "mcp"
    status: str | None = None  # "busy", "idle", "waiting"


@dataclass
class IdeInstance:
    """A running IDE instance from ~/.claude/ide/{port}.lock."""

    port: int  # from filename
    pid: int
    ide_name: str  # "Visual Studio Code", "Cursor", "Windsurf"
    workspace_folders: list[str] = field(default_factory=list)


def get_claude_dir() -> Path:
    """Return the Claude config directory, respecting CLAUDE_CONFIG_DIR."""
    return get_claude_config_home()


def is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Cross-platform:
    - macOS/Linux/WSL: os.kill(pid, 0)
    - Windows: ctypes OpenProcess
    """
    if pid <= 1:
        return False

    if sys.platform == "win32":
        return _is_pid_alive_windows(pid)

    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        # EPERM means the process exists but we lack permission
        return True
    except OSError:
        return False


def _is_pid_alive_windows(pid: int) -> bool:
    """Windows-specific PID liveness check using ctypes."""
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    except Exception:
        return False


def scan_sessions(claude_dir: Path | None = None) -> tuple[list[ClaudeSession], int]:
    """Live sessions, and how many records could NOT be read.

    Two kinds of caller read this directory and they need opposite things from
    an unparseable record:

    - A SCAN (a listing, a status display) wants it skipped. One bad file must
      not take out the whole listing.
    - A GUARD wants to know. ``0 live`` and ``0 readable`` are the same list,
      and only the first is safe to act on -- the callers gate ``_bootstrap``
      (which deletes a profile's Keychain entry and overwrites
      ``.credentials.json``) and account removal, so reading "could not tell"
      as "nobody there" runs them underneath a live instance.

    So the count is returned rather than swallowed, and ``list_sessions``
    below is the scan-shaped view that drops it.
    """
    sessions_dir = (claude_dir or get_claude_dir()) / "sessions"
    if not sessions_dir.is_dir():
        return [], 0

    sessions = []
    unreadable = 0
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data["pid"]
            if not is_pid_alive(pid):
                continue
            sessions.append(ClaudeSession(
                pid=pid,
                session_id=data.get("sessionId", ""),
                cwd=data.get("cwd", ""),
                started_at=data.get("startedAt", 0),
                kind=data.get("kind", ""),
                entrypoint=data.get("entrypoint", ""),
                status=data.get("status"),
            ))
        except (
            json.JSONDecodeError,   # malformed JSON
            KeyError,               # required field missing
            TypeError,              # field has the wrong type (e.g. pid not an int)
            AttributeError,         # valid JSON that is not an object: a
                                    # top-level array reaches `.get` as a list.
                                    # Also how a too-deep nesting lands where
                                    # the parser's recursion limit is high
                                    # enough not to raise -- it differs per
                                    # machine, so BOTH outcomes must be inert.
            ValueError,             # includes UnicodeDecodeError from read_text
            OverflowError,          # pid too large for os.kill's C long (is_pid_alive)
            RecursionError,         # pathologically nested JSON in json.loads
            OSError,
        ) as exc:
            unreadable += 1
            logger.debug("Skipping session file %s: %s", path, exc)
    return sessions, unreadable


def list_sessions(claude_dir: Path | None = None) -> list[ClaudeSession]:
    """Live sessions. A record that cannot be read is SKIPPED.

    SCAN USE ONLY. The returned list cannot distinguish "no live sessions"
    from "no readable records", so anything gating a destructive step must
    call :func:`scan_sessions` and treat a non-zero count as live.
    """
    return scan_sessions(claude_dir)[0]


def list_ide_instances(claude_dir: Path | None = None) -> list[IdeInstance]:
    """Read IDE lockfiles and return only those with alive processes."""
    ide_dir = (claude_dir or get_claude_dir()) / "ide"
    if not ide_dir.is_dir():
        return []

    instances = []
    for path in ide_dir.glob("*.lock"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if pid is None or not is_pid_alive(pid):
                continue
            port = int(path.stem)
            instances.append(IdeInstance(
                port=port,
                pid=pid,
                ide_name=data.get("ideName", "Unknown IDE"),
                workspace_folders=data.get("workspaceFolders", []),
            ))
        except (
            json.JSONDecodeError,   # malformed JSON
            KeyError,               # required field missing
            TypeError,              # field has the wrong type (e.g. pid not an int)
            AttributeError,         # valid JSON that is not an object: a
                                    # top-level array reaches `.get` as a list.
                                    # Also how a too-deep nesting lands where
                                    # the parser's recursion limit is high
                                    # enough not to raise -- it differs per
                                    # machine, so BOTH outcomes must be inert.
            ValueError,             # includes UnicodeDecodeError from read_text
            OverflowError,          # pid too large for os.kill's C long (is_pid_alive)
            RecursionError,         # pathologically nested JSON in json.loads
            OSError,
        ) as exc:
            logger.debug("Skipping IDE lockfile %s: %s", path, exc)
    return instances


def get_running_instances(
    claude_dir: Path | None = None,
) -> tuple[list[ClaudeSession], list[IdeInstance]]:
    """Return all running Claude Code sessions and IDE instances."""
    resolved = claude_dir or get_claude_dir()
    return list_sessions(resolved), list_ide_instances(resolved)


# --- Codex SCAN (process table; display only, never a destructive guard) ---


@dataclass
class CodexProcess:
    pid: int
    argv: list[str]
    kind: str  # tui | exec | app-server | app | other
    cwd: str = ""


_TUI_SUBCOMMANDS = {"resume", "fork"}
_EXEC_SUBCOMMANDS = {"exec", "e"}
# Codex global options that consume the following token (from `codex --help`).
_CODEX_VALUE_OPTIONS = frozenset(
    {
        "-m",
        "--model",
        "-s",
        "--sandbox",
        "-C",
        "--cd",
        "-c",
        "--config",
        "--enable",
        "-p",
        "--profile",
    }
)


def is_codex_comm(comm: str) -> bool:
    """True when the process comm basename is ``codex`` or ``codex.exe``."""
    if not comm:
        return False
    name = Path(str(comm).replace("\\", "/")).name.lower()
    return name in {"codex", "codex.exe"}


def _codex_subcommand(argv: list[str]) -> str | None:
    tokens = [str(t) for t in (argv or ())]
    if tokens and is_codex_comm(tokens[0]):
        tokens = tokens[1:]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            i += 1
            break
        if not tok.startswith("-"):
            break
        i += 1
        name, eq, _ = tok.partition("=")
        if eq:
            continue
        if (
            name in _CODEX_VALUE_OPTIONS
            and i < len(tokens)
            and not tokens[i].startswith("-")
        ):
            i += 1
    if i >= len(tokens):
        return None
    return tokens[i]


def classify_codex_argv(argv: list[str]) -> str:
    """bare / resume / fork → tui; exec / e → exec; app-server; app; else other."""
    cmd = _codex_subcommand(argv)
    if cmd is None or cmd in _TUI_SUBCOMMANDS:
        return "tui"
    if cmd in _EXEC_SUBCOMMANDS:
        return "exec"
    if cmd == "app-server":
        return "app-server"
    if cmd == "app":
        return "app"
    return "other"


def parse_process_table(rows: list[tuple[int, str, list[str]]]) -> list[CodexProcess]:
    """Keep basename-codex rows; drop pid ≤ 1."""
    out: list[CodexProcess] = []
    for row in rows:
        try:
            pid, comm, argv = row
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 1:
            continue
        if not is_codex_comm(str(comm)):
            continue
        argv_list = [str(a) for a in (argv or ())]
        out.append(
            CodexProcess(
                pid=pid,
                argv=argv_list,
                kind=classify_codex_argv(argv_list),
            )
        )
    return out


def _parse_ps_text(text: str) -> list[tuple[int, str, list[str]]]:
    rows: list[tuple[int, str, list[str]]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        comm = parts[1]
        args = parts[2] if len(parts) > 2 else comm
        try:
            argv = shlex.split(args, posix=True)
        except ValueError:
            argv = args.split()
        if not argv:
            argv = [comm]
        rows.append((pid, comm, argv))
    return rows


def _read_process_table() -> list[tuple[int, str, list[str]]]:
    if sys.platform == "win32":
        return []
    completed = subprocess.run(
        ["ps", "-ax", "-o", "pid=,comm=,args="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr or "ps failed")
    return _parse_ps_text(completed.stdout)


def get_running_codex_instances(*, ps=None) -> list[CodexProcess]:
    """SCAN for display: kind == tui only."""
    reader = _read_process_table if ps is None else ps
    rows = reader()
    return [p for p in parse_process_table(rows) if p.kind == "tui"]
