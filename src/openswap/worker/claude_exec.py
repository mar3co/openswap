"""The live Claude Code research adapter: the owner's own ``claude -p`` under containment.

Owner decision (decision-claude-auth.md, 2026-10-07): remote tasks may run the
unmodified Claude Code CLI with the owner's native login, on the owner's own
paired Macs, for tasks the owner starts. Everything else follows the Codex
adapter (:mod:`openswap.worker.codex_exec`), whose launch, events, stop proof,
recovery and retention this class reuses:

- **Binary.** The owner's installed ``claude``, pinned by SHA-256 with
  ``openswap worker claude pin`` (:mod:`openswap.worker.claude_cli`) and
  re-hashed before every launch; the auto-updater is off for jobs.
- **Account.** ``CLAUDE_CONFIG_DIR`` is the pinned account's OpenSwap-managed
  session profile (``<backup>/sessions/<slot>-<email>``), the profile scheduled
  kickoff already uses, prepared once with ``openswap worker claude prepare``.
  The job runs only when the profile's recorded login is exactly the leased
  account and no interactive Claude session is using it. The owner's default
  ``~/.claude`` login is never used or changed; the CLI owns its refresh, and
  OpenSwap never reads, copies, logs or uploads the credentials.
- **Tools.** ``--restricted`` with only Read, Grep, Glob, WebSearch and WebFetch:
  no shell, no code execution, no write or edit tool, no MCP
  (``--strict-mcp-config``), no skills, no session persistence; anything that
  would prompt is denied. Approved read-only sources are added with
  ``--add-dir``.
- **Sandbox.** The CLI runs under ``sandbox-exec`` with a Seatbelt profile that
  allows writes only to the job folder, its own profile, its temporary
  directory and caches, and denies reads of the default Claude and Codex
  logins and of OpenSwap's backup root other than this profile and the job
  folder.
- **Result.** The ``result`` message's text becomes ``result.md``, published
  only after the stop is proven, like Codex's final message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from openswap.worker import claude_cli
from openswap.worker.adapter import ProviderLaunchRefused
from openswap.worker.codex_exec import (
    LAST_MESSAGE_FILE,
    MAX_RESULT_BYTES,
    CodexExecAdapter,
    classify_failure,
)
from openswap.worker.containment import write_private
from openswap.worker.leases import LeaseStateError, stable_account_identity
from openswap.worker.live import execution_mode
from openswap.worker.models import ResolvedWorkspace, SafeEventKind

RESEARCH_TOOLS = ("Read", "Grep", "Glob", "WebSearch", "WebFetch")
SANDBOX_PROFILE_FILE = "claude.sb"
MANAGED_CLAUDE_PATHS = (
    "/Library/Application Support/ClaudeCode/managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-mcp.json",
    "/Library/Application Support/ClaudeCode/CLAUDE.md",
    "/Library/Managed Preferences/com.anthropic.claudecode.plist",
)


def managed_claude_config(profile: Path, *, run=None) -> list[str]:
    """Managed/enterprise Claude Code configuration that would override ours (existence only)."""
    return [path for path in MANAGED_CLAUDE_PATHS if os.path.lexists(path)]


def profile_for(backup_root: Path, identity: str) -> Path | None:
    """The OpenSwap session profile of the roster slot carrying ``identity``."""
    from openswap.session import session_dir_for
    from openswap.worker.accounts import claude_accounts

    for entry in claude_accounts(Path(backup_root)) or ():
        if entry.account_ref == identity:
            return session_dir_for(Path(backup_root), entry.number, entry.email)
    return None


def profile_identity(profile: Path) -> str | None:
    """The stable identity of the account the profile is logged in as (public metadata only)."""
    from openswap.session import read_session_identity

    found = read_session_identity(Path(profile))
    if found is None:
        return None
    email, organization = found
    try:
        return stable_account_identity("claude", email, organization)
    except LeaseStateError:
        return None


def _sb_string(path: Path | str) -> str:
    text = str(path)
    if any(ord(ch) < 0x20 or ch in '"\\' for ch in text):
        raise ValueError("unsupported character in a sandbox path")
    return f'"{text}"'


def darwin_user_dirs() -> list[Path]:
    """This user's own temporary and cache directories (``/var/folders/../T`` and ``../C``)."""
    found = []
    for name in (65537, 65538):  # _CS_DARWIN_USER_TEMP_DIR, _CS_DARWIN_USER_CACHE_DIR
        try:
            value = os.confstr(name)
        except (ValueError, OSError):
            value = None
        if value:
            found.append(Path(os.path.realpath(value)))
    return found


def seatbelt_profile(*, output_root: Path, profile: Path, run_tmp: Path, home: Path, backup_root: Path,
                     readonly_sources: tuple[Path, ...] = (), user_dirs: list[Path] | None = None) -> str:
    """The Seatbelt profile ``claude`` runs under (later rules win in SBPL).

    Writes: only the output folder, this account's profile, the run's own
    temporary folder, and this user's cache/temporary folders (system
    frameworks need them). The other accounts, OpenSwap's own state and the
    owner's default Claude/Codex logins are neither readable nor writable.
    """
    own = [output_root, profile, run_tmp]
    support = [home / "Library" / "Caches", *(darwin_user_dirs() if user_dirs is None else user_dirs)]
    hidden = [home / ".claude", home / ".codex", backup_root]
    readable = [profile, output_root, *readonly_sources]

    def subpaths(paths):
        return " ".join(f"(subpath {_sb_string(p)})" for p in paths)

    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* {subpaths(support)} (subpath "/dev") (literal "/dev/null"))',
        f"(deny file-write* {subpaths(hidden)} (literal {_sb_string(home / '.claude.json')}))",
        f"(allow file-write* {subpaths(own)})",
        f"(deny file-read* {subpaths(hidden)} (literal {_sb_string(home / '.claude.json')}))",
        f"(allow file-read* {subpaths(readable)})",
        "",
    ]
    return "\n".join(lines)


def claude_env(home: Path, profile: Path, run_dir: Path) -> dict[str, str]:
    """The job's whole environment: an allowlist, never the worker's own."""
    try:
        import pwd

        user = pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        user = "user"
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        # The real home: Claude Code finds the profile's Keychain item through it.
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(profile),
        "TMPDIR": str(run_dir / "tmp"),
        "LANG": "en_US.UTF-8",
        "USER": user,
        "LOGNAME": user,
        "SHELL": "/bin/zsh",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }


def claude_argv(binary: Path, sandbox_profile: Path, readonly_sources: tuple[Path, ...] = ()) -> list[str]:
    """Fixed launcher arguments; the task arrives on stdin. Nothing comes from the caller."""
    tools = ",".join(RESEARCH_TOOLS)
    argv = [
        "/usr/bin/sandbox-exec", "-f", str(sandbox_profile), str(binary),
        "-p", "--output-format", "stream-json", "--verbose",
        "--restricted", "--tools", tools, "--allowedTools", tools,
        "--permission-mode", "dontAsk", "--permission-prompts", "none",
        "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
    ]
    for source in readonly_sources:
        argv += ["--add-dir", str(source)]
    return argv


class ClaudeCodeAdapter(CodexExecAdapter):
    """Live Claude Code adapter; runs nothing unless the Claude opt-in is recorded."""

    provider = "claude"
    _cli_errors = (claude_cli.ClaudeCliError,)

    def __init__(self, backup_root: Path, *, home: Path | None = None, live_sessions=None, **kwargs):
        root = Path(backup_root)
        kwargs.setdefault("verify", lambda **kw: claude_cli.verify(root, **kw))
        kwargs.setdefault("mode", lambda: execution_mode(root, "claude"))
        kwargs.setdefault("managed", managed_claude_config)
        super().__init__(root, **kwargs)
        self._home = Path(home) if home is not None else Path.home()
        self._live_sessions = live_sessions or _live_sessions

    def _prepare_account(self, identity: str, workspace: ResolvedWorkspace) -> Path:
        profile = profile_for(self.backup_root, identity)
        if profile is None or not profile.is_dir() or profile.is_symlink():
            raise ProviderLaunchRefused("provider_auth_unavailable")
        if profile_identity(profile) != identity:
            # Not prepared, or logged in as another account: never run on it.
            raise ProviderLaunchRefused("provider_auth_unavailable")
        if self._live_sessions(profile):
            # An interactive Claude session owns this profile's refresh now.
            raise ProviderLaunchRefused("provider_unavailable")
        if self._managed(profile):
            raise ProviderLaunchRefused("provider_unavailable")
        return profile

    def _command(self, pinned, profile: Path, output_root: Path, run_dir: Path,
                 workspace: ResolvedWorkspace) -> tuple[list[str], dict[str, str]]:
        sources = tuple(Path(p).resolve() for p in workspace.readonly_sources)
        try:
            text = seatbelt_profile(
                output_root=output_root.resolve(), profile=profile.resolve(), run_tmp=(run_dir / "tmp").resolve(),
                home=self._home.resolve(), backup_root=self.backup_root.resolve(), readonly_sources=sources,
            )
            sb = run_dir / SANDBOX_PROFILE_FILE
            write_private(sb, text.encode("utf-8"))
        except (OSError, ValueError):
            raise ProviderLaunchRefused("provider_unavailable") from None
        return claude_argv(pinned.binary, sb, sources), claude_env(self._home, profile, run_dir)

    def _line(self, state, line: bytes):
        if not line.strip():
            return []
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            state.unparsed_lines += 1
            return []
        if not isinstance(record, dict):
            state.unparsed_lines += 1
            return []
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "init":
            tools = record.get("tools")
            servers = record.get("mcp_servers")
            state.extra["tools"] = sorted(t for t in tools if isinstance(t, str))[:64] if isinstance(tools, list) else None
            state.extra["mcp_servers"] = len(servers) if isinstance(servers, list) else None
            source = record.get("apiKeySource")
            state.extra["api_key_source"] = source if isinstance(source, str) and len(source) <= 40 else None
            if not state.started:
                state.started = True
                return [self._event(state, SafeEventKind.PROVIDER_STARTED)]
            return []
        if kind == "assistant":
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = block.get("name")
                    if isinstance(name, str) and len(name) <= 64:
                        state.item_counts[name] = state.item_counts.get(name, 0) + 1
            return []
        if kind == "result":
            text = record.get("result")
            usage = record.get("usage")
            if isinstance(usage, dict):
                state.usage = {k: v for k, v in usage.items() if isinstance(k, str) and type(v) is int}
            if record.get("subtype") == "success" and record.get("is_error") is not True and isinstance(text, str):
                data = text.encode("utf-8")[:MAX_RESULT_BYTES]
                try:
                    write_private(state.run_dir / LAST_MESSAGE_FILE, data)
                    state.turn_completed = True
                except OSError:
                    state.failure = "provider_unavailable"
            else:
                state.failure = classify_failure(text if isinstance(text, str) else str(record.get("subtype", "")))
            return []
        return []


def _live_sessions(profile: Path) -> bool:
    """Whether an interactive Claude Code session is (or may be) using the profile."""
    from openswap.session import scan_live_sessions

    sessions, unreadable = scan_live_sessions(Path(profile))
    return bool(sessions) or bool(unreadable)
