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
  account, its sign-in is in the profile's own credentials file (Claude
  Code's file store: jobs cannot reach the Keychain, below) and no
  interactive Claude session is using it. The owner's default ``~/.claude``
  login is never used or changed; the CLI owns its refresh, and OpenSwap never
  reads, copies, logs or uploads the credentials.
- **Tools.** The account's own permission settings (owner decision
  2026-10-08, :mod:`openswap.worker.permissions`): Claude Code reads the mode
  and the allow, deny and ask rules from the profile's ``settings.json``
  (``--setting-sources user``; a repo's own settings never apply), and
  ``--permission-prompts none`` denies whatever would ask, since nobody is at
  the Mac (``auto`` mode decides by itself, as it does locally). The per-Mac
  limit (``openswap worker permissions``) can take the shell away
  (``no-shell``) or leave only read and web tools (``read-only``). No MCP
  (``--strict-mcp-config``), no skills, no session persistence. Approved
  read-only sources are added with ``--add-dir``.
- **Sandbox.** The CLI runs under ``sandbox-exec`` with a Seatbelt profile that
  holds whatever the mode, ``bypassPermissions`` included: writes only to the
  job folder (or the task's worktree), its own profile (never the files there
  that configure later sessions), its temporary directory and caches; no
  reads of the default Claude and Codex logins or of OpenSwap's backup root
  other than this profile and the job folder; and no Keychain at all (no
  ``security`` command, no Keychain service), so a shell command cannot read
  another account's sign-in, the default login or the worker's own keys. Claude
  Code then keeps this account's sign-in in the profile's credentials file.
  Deny rules keep Claude's file tools off that file in every mode, but a
  shell command in the task can read it (macOS refuses to start Claude Code's
  own bash sandbox inside this one); ``no-shell`` prevents that.
- **Result.** The ``result`` message's text becomes ``result.md``, published
  only after the stop is proven, like Codex's final message.
"""

from __future__ import annotations

import json
import os
import stat
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
from openswap.worker.permissions import (
    CLAUDE_PROFILE_CONFIG,
    CLAUDE_PROFILE_CONFIG_DIRS,
    CLAUDE_READ_ONLY_TOOLS,
    PermissionSettingsError,
    claude_permission_args,
    read_claude_permissions,
)

# The tools the ``read-only`` limit leaves (reading and web research).
RESEARCH_TOOLS = CLAUDE_READ_ONLY_TOOLS
SANDBOX_PROFILE_FILE = "claude.sb"
# Claude Code's file store for the sign-in (used when the Keychain is unreachable).
CREDENTIALS_FILE = ".credentials.json"
# The Keychain's services: a job reaches none of them. ``trustd`` (certificate
# trust) is not among them, so TLS still works.
KEYCHAIN_SERVICES = (
    "com.apple.SecurityServer", "com.apple.securityd", "com.apple.securityd.xpc", "com.apple.secd",
    "com.apple.security.agent", "com.apple.security.agent.login", "com.apple.security.authhost",
    "com.apple.security.keychain-circle", "com.apple.securityd.systemkeychain",
)
SECURITY_TOOL = "/usr/bin/security"
LAUNCHCTL = "/bin/launchctl"
MANAGED_CLAUDE_PATHS = (
    "/Library/Application Support/ClaudeCode/managed-settings.json",
    "/Library/Application Support/ClaudeCode/managed-mcp.json",
    "/Library/Application Support/ClaudeCode/CLAUDE.md",
    "/Library/Managed Preferences/com.anthropic.claudecode.plist",
)


MANAGED_SETTINGS_DIR = "/Library/Application Support/ClaudeCode/managed-settings.d"
REMOTE_SETTINGS_FILE = "remote-settings.json"  # server-managed policy, cached in the config dir


def managed_claude_config(profile: Path, *, run=None, user: str | None = None) -> list[str]:
    """Managed Claude Code policy that would apply to a job (empty when none).

    Managed settings apply over everything a job passes and the account's
    own settings: they can add hooks, environment values, permission rules or
    an API key helper the owner did not choose for the account and the live
    check never measured. So any source refuses a launch: the system files,
    any fragment in
    ``managed-settings.d``, the machine or per-user managed preferences, and
    the server-managed policy Claude Code caches in the profile
    (``remote-settings.json``) unless that cache is an empty object.
    """
    found = [path for path in MANAGED_CLAUDE_PATHS if os.path.lexists(path)]
    try:
        if any(True for _ in os.scandir(MANAGED_SETTINGS_DIR)):
            found.append(MANAGED_SETTINGS_DIR)
    except FileNotFoundError:
        pass
    except OSError:
        found.append(MANAGED_SETTINGS_DIR)  # unreadable: never treated as empty
    if user is None:
        try:
            import pwd

            user = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError, AttributeError):
            user = None
    if user:
        per_user = f"/Library/Managed Preferences/{user}/com.anthropic.claudecode.plist"
        if os.path.lexists(per_user):
            found.append(per_user)
    remote = Path(profile) / REMOTE_SETTINGS_FILE
    if os.path.lexists(remote):
        try:
            if remote.is_symlink() or remote.stat().st_size > 2 * 1024 * 1024:
                raise ValueError
            cached = json.loads(remote.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cached = None
        if cached != {}:
            found.append(str(remote))
    return found


def profile_for(backup_root: Path, identity: str) -> Path | None:
    """The OpenSwap session profile of the roster slot carrying ``identity``."""
    from openswap.session import session_dir_for
    from openswap.worker.accounts import claude_accounts

    for entry in claude_accounts(Path(backup_root)) or ():
        if entry.account_ref == identity:
            return session_dir_for(Path(backup_root), entry.number, entry.email)
    return None


def profile_symlinked(backup_root: Path, profile: Path) -> bool:
    """Whether the profile, or any ancestor inside the backup root, is a symlink.

    Jobs refuse such a layout: the Seatbelt allowance for the profile would
    then apply to wherever the link points.
    """
    profile = Path(profile)
    backup_root = Path(backup_root)
    return profile.is_symlink() or any(parent.is_symlink() for parent in profile.parents
                                       if parent.is_relative_to(backup_root))


def profile_shared(profile: Path, home: Path | None = None) -> bool:
    """Whether the profile mirrors anything from the owner's default ``~/.claude``.

    Scheduled kickoff prepares profiles with ``share=True``, which links
    settings, CLAUDE.md, skills, commands and agents from the default
    profile and records them in OpenSwap's share manifest. A remote research
    job must not inherit any of that.
    """
    from openswap.session import SHARE_MANIFEST

    profile = Path(profile)
    if os.path.lexists(profile / SHARE_MANIFEST):
        return True
    from openswap import pathid

    default = pathid.canonical((Path(home) if home is not None else Path.home()) / ".claude")
    try:
        entries = list(profile.iterdir())
    except OSError:
        return True  # unreadable: never treated as clean
    for entry in entries:
        if entry.is_symlink():
            try:
                target = Path(os.path.realpath(entry))
            except OSError:
                return True
            if pathid.inside(target, default):
                return True
    return False


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


def _sb_regex(path: Path | str) -> str:
    """``path`` as a literal inside an SBPL ``#"..."`` regular expression."""
    text = str(path)
    if any(ord(ch) < 0x20 or ch in '"' for ch in text):
        raise ValueError("unsupported character in a sandbox path")
    return "".join("\\" + ch if ch in ".^$*+?()[]{}|\\" else ch for ch in text)


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
                     readonly_sources: tuple[Path, ...] = (), user_dirs: list[Path] | None = None,
                     write_paths: tuple[Path, ...] = (), read_paths: tuple[Path, ...] = ()) -> str:
    """The Seatbelt profile ``claude`` and everything it starts run under (later rules win in SBPL).

    It holds whatever Claude Code's own permission mode allows:

    - Writes: only the output folder, this account's profile, the run's own
      temporary folder, and this user's cache/temporary folders (system
      frameworks need them). Never the profile files that configure later
      sessions (settings, memory, agents, commands, skills, hooks, plugins),
      so one task cannot widen the next.
    - The other accounts, OpenSwap's own state and the owner's default
      Claude/Codex logins are neither readable nor writable.
    - No Keychain: the ``security`` tool cannot start and the Keychain's
      services cannot be reached, so neither Claude Code nor a shell command
      it runs can read any Keychain item (other accounts' sign-ins, the
      default login, the worker's device key). Claude Code falls back to the
      profile's own credentials file.
    - No way out: LaunchServices, Apple Events, launchd job creation, local
      Unix sockets (other than name resolution's), loopback connections and
      ssh (port 22, to any address) are denied, so nothing the session starts
      runs outside this sandbox or the coalition.
    """
    # ``output_root`` is the session's working directory; ``write_paths`` add
    # what git needs for a work folder's worktree.
    own = [output_root, *write_paths, profile, run_tmp]
    support = [home / "Library" / "Caches", *(darwin_user_dirs() if user_dirs is None else user_dirs)]
    hidden = [home / ".claude", home / ".codex", backup_root]
    readable = [profile, output_root, *readonly_sources, *write_paths, *read_paths]
    configuration = [f"(literal {_sb_string(profile / name)})" for name in CLAUDE_PROFILE_CONFIG]
    configuration += [f"(subpath {_sb_string(profile / name)})" for name in CLAUDE_PROFILE_CONFIG_DIRS]
    # Each project's auto-memory, which later sessions in that folder load.
    configuration.append(f'(regex #"^{_sb_regex(profile / "projects")}/[^/]+/memory(/|$)")')
    services = " ".join(f"(global-name {_sb_string(name)})" for name in KEYCHAIN_SERVICES)

    def subpaths(paths):
        return " ".join(f"(subpath {_sb_string(p)})" for p in paths)

    lines = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f'(allow file-write* {subpaths(support)} (subpath "/dev") (literal "/dev/null"))',
        f"(deny file-write* {subpaths(hidden)} (literal {_sb_string(home / '.claude.json')}))",
        f"(allow file-write* {subpaths(own)})",
        f"(deny file-write* {' '.join(configuration)})",
        f"(deny file-read* {subpaths(hidden)} (literal {_sb_string(home / '.claude.json')}))",
        f"(allow file-read* {subpaths(readable)})",
        f"(deny process-exec (literal {_sb_string(SECURITY_TOOL)}))",
        f"(deny mach-lookup {services})",
        f"(deny file-read* file-write* (subpath {_sb_string(home / 'Library' / 'Keychains')}))",
        # Nothing the session starts may leave this sandbox or the job's
        # coalition: no app or document opened through LaunchServices (an
        # opened app runs unsandboxed), no Apple Events to other apps, no
        # launchd job, and no local Unix socket (a daemon such as Docker's
        # would act outside the sandbox on the task's behalf).
        "(deny lsopen)",
        "(deny appleevent-send)",
        "(deny job-creation)",
        f"(deny process-exec (literal {_sb_string(LAUNCHCTL)}))",
        "(deny network-outbound (remote unix-socket))",
        # Name resolution goes through mDNSResponder's socket.
        '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
        # No service on this Mac (sshd would run a command outside the
        # sandbox and the coalition), nor ssh to this Mac by another address.
        '(deny network-outbound (remote ip "localhost:*"))',
        '(deny network-outbound (remote tcp "*:22"))',
        "",
    ]
    return "\n".join(lines)


def keychain_free_profile() -> str:
    """A Seatbelt profile that only takes the Keychain away (``openswap worker claude prepare``).

    Claude Code's own sign-in then stores the account's credentials in the
    profile's credentials file, where jobs (which cannot reach the Keychain)
    find them. OpenSwap itself never touches them.
    """
    services = " ".join(f"(global-name {_sb_string(name)})" for name in KEYCHAIN_SERVICES)
    return "\n".join([
        "(version 1)",
        "(allow default)",
        f"(deny process-exec (literal {_sb_string(SECURITY_TOOL)}))",
        f"(deny mach-lookup {services})",
        "",
    ])


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
        # No auto-memory: one remote task never leaves notes a later one loads
        # (and its default folders in the profile are unwritable as well).
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    }


def claude_argv(binary: Path, sandbox_profile: Path, readonly_sources: tuple[Path, ...] = (),
                permission_args: tuple[str, ...] | list[str] = (), setting_sources: str = "user",
                ) -> list[str]:
    """Launcher arguments; the task arrives on stdin. Nothing comes from the remote caller.

    No permission mode or tool list is passed: Claude Code takes them from the
    profile's ``settings.json`` (``--setting-sources user``), exactly as
    ``claude`` run there would. ``--permission-prompts none`` turns every
    prompt into a denial. ``permission_args`` is the per-Mac limit
    (:func:`openswap.worker.permissions.claude_permission_args`). Only the
    live check passes ``setting_sources=""`` (its own settings alone); a
    repo's project or local settings never apply.
    """
    if setting_sources not in ("user", ""):
        raise ValueError("only the account's own settings may apply")
    argv = [
        "/usr/bin/sandbox-exec", "-f", str(sandbox_profile), str(binary),
        "-p", "--output-format", "stream-json", "--verbose",
        "--setting-sources", setting_sources, "--permission-prompts", "none",
        "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
        *permission_args,
    ]
    for source in readonly_sources:
        argv += ["--add-dir", str(source)]
    return argv


def credentials_in_file(profile: Path) -> bool:
    """Whether the profile's sign-in is in Claude Code's credentials file (existence only, never read).

    Jobs cannot reach the Keychain, so Claude Code reads the sign-in from
    there; `openswap worker claude prepare` puts it there with Claude Code's
    own login.
    """
    try:
        info = (Path(profile) / CREDENTIALS_FILE).lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size > 0


class ClaudeCodeAdapter(CodexExecAdapter):
    """Live Claude Code adapter; runs nothing unless the Claude opt-in is recorded."""

    provider = "claude"
    _cli_errors = (claude_cli.ClaudeCliError,)

    def __init__(self, backup_root: Path, *, home: Path | None = None, live_sessions=None,
                 settings_for_check: dict | None = None, profile_settings: bool = True, **kwargs):
        root = Path(backup_root)
        kwargs.setdefault("verify", lambda **kw: claude_cli.verify(root, **kw))
        kwargs.setdefault("mode", lambda: execution_mode(root, "claude"))
        kwargs.setdefault("managed", managed_claude_config)
        super().__init__(root, **kwargs)
        self._home = Path(home) if home is not None else Path.home()
        self._live_sessions = live_sessions or _live_sessions
        # The live check runs some jobs with given settings (flag-level, over
        # the profile's) to measure a specific mode; jobs never do.
        self._settings_for_check = dict(settings_for_check) if settings_for_check else None
        # The live check's mode probes leave the profile's own settings out
        # (``--setting-sources ""``), so the owner's rules cannot decide them.
        self._profile_settings = profile_settings or self._settings_for_check is None

    def _grant_allowed(self, path: Path) -> bool:
        """Also never grant a path overlapping the folders the Seatbelt profile hides.

        A granted root becomes an ``--add-dir`` and a later ``allow file-read*``
        rule, which would override the hide rules for the default Claude and
        Codex logins and the backup root (other accounts' profiles). The only
        exceptions are OpenSwap's own job folders (below).
        """
        from openswap import pathid

        if not super()._grant_allowed(path):
            return False
        # On-disk spelling and identity: a case variant is the same folder.
        path = pathid.canonical(path)
        home = pathid.canonical(self._home)
        backup = pathid.canonical(self.backup_root)
        # OpenSwap's own job folders inside the backup root stay grantable:
        # the built-in research area and the live check's folders.
        for own in (backup / "worker" / "research", backup / "live-check"):
            if pathid.inside(path, own):
                return True
        for hidden in (home / ".claude", home / ".codex", backup):
            if pathid.overlap(path, hidden):
                return False
        return not pathid.inside(home / ".claude.json", path)

    def _prepare_account(self, identity: str, workspace: ResolvedWorkspace) -> Path:
        profile = profile_for(self.backup_root, identity)
        if profile is None or not profile.is_dir() or profile_symlinked(self.backup_root, profile):
            # A symlinked profile (or ancestor) would turn the Seatbelt
            # allowance for it into one for wherever the link points.
            raise ProviderLaunchRefused("provider_auth_unavailable")
        if profile_identity(profile) != identity or not credentials_in_file(profile):
            # Not prepared, logged in as another account, or signed in only in
            # the Keychain, which jobs cannot reach: never run on it.
            raise ProviderLaunchRefused("provider_auth_unavailable")
        if self._live_sessions(profile):
            # An interactive Claude session owns this profile's refresh now.
            raise ProviderLaunchRefused("provider_unavailable")
        if profile_shared(profile, self._home):
            # Customizations mirrored from the default profile (kickoff's
            # share=True): `openswap worker claude prepare` removes them.
            raise ProviderLaunchRefused("provider_unavailable")
        if self._managed(profile):
            raise ProviderLaunchRefused("provider_unavailable")
        try:
            # Claude Code would silently ignore a settings file it cannot
            # validate, dropping the owner's deny rules: refuse instead.
            permissions = read_claude_permissions(profile)
        except PermissionSettingsError:
            raise ProviderLaunchRefused("provider_unavailable") from None
        override = self.override()
        # The credentials file stays readable to Claude Code itself (the
        # Seatbelt profile cannot tell it from its tools): deny rules keep the
        # file tools off it, as spelled and as resolved.
        credentials = {str(profile / CREDENTIALS_FILE), str(profile.resolve() / CREDENTIALS_FILE)}
        try:
            self._permission_args = claude_permission_args(override, self._settings_for_check,
                                                           tuple(sorted(credentials)))
        except ValueError:
            raise ProviderLaunchRefused("provider_unavailable") from None
        self._launch_summary = {"permissions": {**permissions.to_dict(), "override": override,
                                                "for_check": self._settings_for_check is not None,
                                                "profile_settings": self._profile_settings}}
        return profile

    def _command(self, pinned, profile: Path, output_root: Path, run_dir: Path,
                 workspace: ResolvedWorkspace) -> tuple[list[str], dict[str, str]]:
        sources = tuple(Path(p).resolve() for p in workspace.readonly_sources)
        try:
            text = seatbelt_profile(
                output_root=output_root.resolve(), profile=profile.resolve(), run_tmp=(run_dir / "tmp").resolve(),
                home=self._home.resolve(), backup_root=self.backup_root.resolve(), readonly_sources=sources,
                write_paths=tuple(Path(p).resolve() for p in workspace.write_paths),
                read_paths=tuple(Path(p).resolve() for p in workspace.read_paths),
            )
            sb = run_dir / SANDBOX_PROFILE_FILE
            write_private(sb, text.encode("utf-8"))
        except (OSError, ValueError):
            raise ProviderLaunchRefused("provider_unavailable") from None
        env = {**claude_env(self._home, profile, run_dir), **dict(workspace.env)}
        return claude_argv(pinned.binary, sb, sources, self._permission_args,
                           "user" if self._profile_settings else ""), env

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
            mode = record.get("permissionMode")
            state.extra["permission_mode"] = mode if isinstance(mode, str) and len(mode) <= 40 else None
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
