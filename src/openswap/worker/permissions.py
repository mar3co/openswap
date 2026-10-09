"""What a remote session may do: the account's own settings, and an optional per-Mac limit.

Owner decision (2026-10-08, plan 017): a remote Claude or Codex session uses
the permission settings of the account it runs on, exactly as running
``claude`` or ``codex`` there would, instead of a tool list OpenSwap picks.
Nobody is at the Mac to answer a prompt, so anything that would ask is
denied, unless the account's own automatic mode decides (Claude's ``auto``
mode, Codex's ``auto_review`` reviewer).

- **Claude.** The account's OpenSwap profile has its own
  ``settings.json``; Claude Code reads its permission mode and allow, deny
  and ask rules from there (``--setting-sources user``: a repo's own
  ``.claude/settings.json`` never applies to a remote task, since ``-p``
  skips the trust dialog that would approve it). ``openswap worker claude
  prepare`` offers to copy those keys from ``~/.claude/settings.json``.
- **Codex.** Each account's isolated home keeps ``permissions.json``
  (approval policy, approvals reviewer, sandbox mode), copied from
  ``~/.codex/config.toml`` when the owner says so. Codex's own sandbox is the
  boundary for its shell commands, so the folder rules, the hidden sign-ins
  and the shell's lack of network stay OpenSwap's; only ``read-only`` narrows
  them.
- **Per-Mac limit.** ``openswap worker permissions follow|no-shell|read-only``
  (default ``follow``) is set only on the Mac, never by the control service.

Whatever the mode, including ``bypassPermissions``, OpenSwap's Seatbelt
profile (Claude) or Codex's permission profile keeps the system boundaries:
writes only in the task's folder or worktree, no other account's sign-in,
no backup root, no journal, no write to the owner's working copy.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path

FOLLOW, NO_SHELL, READ_ONLY = "follow", "no-shell", "read-only"
OVERRIDES = (FOLLOW, NO_SHELL, READ_ONLY)
OVERRIDE_DESCRIPTIONS = {
    FOLLOW: "follow each account's own Claude or Codex settings",
    NO_SHELL: "no shell commands (Claude: no Bash and no hooks; Codex: no shell tool)",
    READ_ONLY: "read only (Claude: read and web tools only; Codex: read-only sandbox)",
}

MAX_SETTINGS_BYTES = 1024 * 1024


class PermissionSettingsError(ValueError):
    """A permission settings file that cannot be trusted to say what the owner chose."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _read_json_object(path: Path) -> dict | None:
    """A JSON object from ``path`` without following a symlink; None when absent."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        # A symlink (ELOOP with O_NOFOLLOW) or unreadable: never treated as absent.
        raise PermissionSettingsError("settings_unsafe") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SETTINGS_BYTES:
            raise PermissionSettingsError("settings_unsafe")
        data = os.read(fd, MAX_SETTINGS_BYTES + 1)
    finally:
        os.close(fd)
    try:
        value = json.loads(data.decode("utf-8")) if data.strip() else {}
    except (UnicodeError, ValueError, RecursionError):
        raise PermissionSettingsError("settings_invalid") from None
    if not isinstance(value, dict):
        raise PermissionSettingsError("settings_invalid")
    return value


# -- Claude ---------------------------------------------------------------------------

CLAUDE_SETTINGS_FILE = "settings.json"
# The values Claude Code accepts for ``permissions.defaultMode``.
CLAUDE_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
# What `claude prepare` copies from ~/.claude/settings.json: the mode and the rules.
COPIED_CLAUDE_KEYS = ("defaultMode", "allow", "deny", "ask", "disableBypassPermissionsMode")
_RULE_KEYS = ("allow", "deny", "ask")
# A permission rule: a tool name (``Bash``, ``mcp__server__tool``), optionally
# with one parenthesized specifier that does not itself end the rule early
# (``Bash(git status:*)``, ``Read(//abs/**)``, ``WebFetch(domain:x.com)``).
_RULE_SHAPE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\((?:[^()\n]|\([^()\n]*\))*\))?")
# The built-in tools that run commands. Unknown names are ignored by Claude
# Code, so the list may name tools a given version does not have.
CLAUDE_SHELL_TOOLS = ("Bash", "PowerShell", "Monitor", "REPL", "BashOutput", "KillShell")
# ``read-only``: reading and web research only.
CLAUDE_READ_ONLY_TOOLS = ("Read", "Grep", "Glob", "WebSearch", "WebFetch")
# Files in the profile that change what a later session may do or is told.
# Jobs may not write them (Seatbelt), so a task can never widen or instruct
# the next one: settings, user memory and rules, agents and their memory,
# commands, skills, hooks, output styles, plugins, and ``projects/`` (each
# project's auto-memory lives there; protecting the whole folder also stops a
# task from renaming a prepared folder into place). Jobs keep no session
# history (``--no-session-persistence``), so they need nothing there.
# Also the cached server policy and its limits, which an ordinary (kickoff or
# interactive) session on this profile would load as managed policy, and the
# files that schedule or start work later.
# ``.claude.json`` too: it keeps per-project approvals (``projects[…].allowedTools``)
# and the account the launch's identity check reads.
CLAUDE_PROFILE_CONFIG = (".claude.json", "settings.json", "settings.local.json", "CLAUDE.md", "CLAUDE.local.md",
                         "remote-settings.json", "remote-settings-consent.json", "policy-limits.json",
                         "scheduled_tasks.json", "monitors.json", "daemon.json", "cowork_settings.json",
                         "keybindings.json")
CLAUDE_PROFILE_CONFIG_DIRS = ("agents", "agent-memory", "commands", "rules", "skills", "hooks", "output-styles",
                              "plugins", "projects")


@dataclass(frozen=True)
class ClaudePermissions:
    """The permission part of a profile's ``settings.json`` (counts only, never the rules)."""

    mode: str | None = None  # None: Claude Code's own default mode
    allow: int = 0
    deny: int = 0
    ask: int = 0

    @property
    def configured(self) -> bool:
        return self.mode is not None or bool(self.allow or self.deny or self.ask)

    def describe(self) -> str:
        mode = self.mode or "Claude Code's default"
        rules = [f"{count} {name}" for name, count in (("allow", self.allow), ("deny", self.deny),
                                                     ("ask", self.ask)) if count]
        return f"mode {mode}" + (f", {', '.join(rules)} rules" if rules else "")

    def to_dict(self) -> dict:
        return {"mode": self.mode, "allow_rules": self.allow, "deny_rules": self.deny, "ask_rules": self.ask}


def _rule_ok(rule: str) -> bool:
    if not _RULE_SHAPE.fullmatch(rule):
        return False
    if rule.startswith("Bash(") and ":*" in rule[5:-1].removesuffix(":*"):
        return False  # the prefix wildcard ``:*`` is only valid at the end
    return True


def _claude_permissions_block(settings: dict) -> dict:
    block = settings.get("permissions", {})
    if not isinstance(block, dict):
        raise PermissionSettingsError("settings_invalid")
    mode = block.get("defaultMode")
    if mode is not None and mode not in CLAUDE_MODES:
        raise PermissionSettingsError("settings_invalid")
    for key in (*_RULE_KEYS, "additionalDirectories"):
        rules = block.get(key, [])
        if (not isinstance(rules, list) or len(rules) > 4096
                or not all(isinstance(rule, str) and 0 < len(rule) <= 4096 for rule in rules)):
            raise PermissionSettingsError("settings_invalid")
        if key in _RULE_KEYS and not all(_rule_ok(rule) for rule in rules):
            # Not `Tool` or `Tool(specifier)`: a Claude Code version that
            # rejects the file over it would drop every deny rule with it.
            raise PermissionSettingsError("settings_invalid")
    # The only value Claude Code accepts; anything else would make it drop the file.
    if block.get("disableBypassPermissionsMode", "disable") != "disable":
        raise PermissionSettingsError("settings_invalid")
    return block


def _summary(block: dict) -> ClaudePermissions:
    return ClaudePermissions(block.get("defaultMode"), len(block.get("allow", [])), len(block.get("deny", [])),
                             len(block.get("ask", [])))


def read_claude_permissions(profile: Path) -> ClaudePermissions:
    """The profile's permission settings; raises when they cannot be trusted.

    Claude Code silently ignores a settings file that fails validation in
    ``-p`` mode, which would drop the owner's deny rules, so a launch refuses
    one instead (an absent file is Claude Code's defaults).
    """
    settings = _read_json_object(Path(profile) / CLAUDE_SETTINGS_FILE)
    return _summary(_claude_permissions_block(settings or {}))


# The permission keys a launch also passes at flag level (``--settings``).
FLAG_PERMISSION_KEYS = (*COPIED_CLAUDE_KEYS, "additionalDirectories")


def profile_permission_flags(profile: Path) -> dict:
    """The profile's validated permission keys, to pass again as flag-level settings.

    Claude Code skips a whole settings file that fails its schema anywhere
    (a malformed hook, an unknown key), and with it the mode and every deny
    rule. Passing the validated permission keys on the command line as well
    keeps them in force even then; they are the same values, so nothing else
    changes. Raises like :func:`read_claude_permissions`.
    """
    settings = _read_json_object(Path(profile) / CLAUDE_SETTINGS_FILE) or {}
    block = _claude_permissions_block(settings)
    return {key: block[key] for key in FLAG_PERMISSION_KEYS if key in block}


def merge_flag_settings(base: dict, overlay: dict | None) -> dict:
    """``overlay`` over ``base`` (flag-level settings): rule lists add up, other keys replace."""
    merged = json.loads(json.dumps(base))
    for key, value in (overlay or {}).items():
        if key == "permissions" and isinstance(value, dict):
            block = merged.setdefault("permissions", {})
            for name, item in value.items():
                if name in _RULE_KEYS and isinstance(item, list):
                    block[name] = [*block.get(name, []), *item]
                else:
                    block[name] = item
        else:
            merged[key] = value
    return merged


def default_claude_permissions(home: Path | None = None) -> dict | None:
    """The permission keys of the owner's ``~/.claude/settings.json`` (None when it sets none)."""
    path = (Path(home) if home is not None else Path.home()) / ".claude" / CLAUDE_SETTINGS_FILE
    try:
        # The owner's own file may be a dotfiles link: follow it here (reading only).
        if path.is_symlink():
            path = Path(os.path.realpath(path))
        settings = _read_json_object(path)
    except (OSError, PermissionSettingsError):
        return None
    if settings is None:
        return None
    try:
        block = _claude_permissions_block(settings)
    except PermissionSettingsError:
        return None
    copied = {key: block[key] for key in COPIED_CLAUDE_KEYS if key in block}
    return copied or None


def write_claude_permissions(profile: Path, *, copied: dict | None = None, mode: str | None = None) -> ClaudePermissions:
    """Set the profile's permission keys, keeping everything else in its ``settings.json``.

    ``copied`` (from :func:`default_claude_permissions`) replaces the mode and
    the rules; ``mode`` then sets the mode alone.
    """
    from openswap.worker.containment import write_private

    if mode is not None and mode not in CLAUDE_MODES:
        raise PermissionSettingsError("mode_invalid")
    path = Path(profile) / CLAUDE_SETTINGS_FILE
    settings = _read_json_object(path) or {}
    block = dict(_claude_permissions_block(settings))
    if copied is not None:
        for key in COPIED_CLAUDE_KEYS:
            if key in copied:
                block[key] = copied[key]
            else:
                block.pop(key, None)
    if mode is not None:
        block["defaultMode"] = mode
    settings["permissions"] = block
    _claude_permissions_block(settings)  # never write what a launch would refuse
    write_private(path, (json.dumps(settings, indent=2) + "\n").encode("utf-8"))
    return _summary(block)


def credential_rules(paths) -> list[str]:
    """Deny rules keeping Claude Code's file tools off ``paths`` (absolute, ``//`` form).

    Claude Code must read its own credentials file, so the Seatbelt profile
    cannot hide it; these rules keep the Read, Edit and search tools off it in
    every mode (deny rules win over allow rules and the mode). A shell command
    is not bound by them, which is why ``no-shell`` is the limit that protects
    the sign-in.
    """
    rules = []
    names = []
    for path in paths:
        literal = _gitignore_literal(str(path))
        rules += [f"Read(/{literal})", f"Edit(/{literal})"]
        name = Path(str(path)).name
        if name not in names:
            names.append(name)
    for name in names:
        # The same file has other spellings (``/System/Volumes/Data/Users/…``,
        # a case variant on a case-insensitive volume, ``..`` segments): match
        # its name anywhere, in any case, as well.
        anywhere = f"//**/{_case_insensitive_glob(name)}"
        rules += [f"Read({anywhere})", f"Edit({anywhere})"]
    return rules


def _case_insensitive_glob(name: str) -> str:
    """A file name as a gitignore pattern matching it in any letter case."""
    out = []
    for ch in _gitignore_literal("/" + name)[1:]:
        out.append(f"[{ch.lower()}{ch.upper()}]" if ch.isascii() and ch.isalpha() else ch)
    return "".join(out)


def _gitignore_literal(path: str) -> str:
    """An absolute path as a gitignore pattern matching exactly that path.

    Read and Edit rules use gitignore syntax, so ``*``, ``?``, ``[``, ``]``
    and ``\\`` in a folder name (legal on macOS) are escaped; a trailing
    space would be dropped, so it is escaped too. Parentheses need no escape.
    """
    if not path.startswith("/") or any(ord(ch) < 0x20 for ch in path):
        raise ValueError("unsupported path for a permission rule")
    text = "".join("\\" + ch if ch in "\\*?[]" else ch for ch in path)
    if text.endswith(" "):
        text = text[:-1] + "\\ "
    return text


def claude_permission_args(override: str, overlay: dict | None = None, protected=()) -> list[str]:
    """Extra ``claude`` arguments: the per-Mac limit and the ``protected`` files' deny rules.

    ``overlay`` is flag-level settings (higher precedence than the profile's),
    used only by the live check to measure specific modes. Flag-level deny
    rules add to the profile's own.
    """
    settings = json.loads(json.dumps(overlay or {}))  # a deep copy
    args: list[str] = []
    if protected:
        block = settings.setdefault("permissions", {})
        block["deny"] = [*block.get("deny", []), *credential_rules(protected)]
    if override == NO_SHELL:
        args += ["--disallowedTools", ",".join(CLAUDE_SHELL_TOOLS)]
        settings["disableAllHooks"] = True
    elif override == READ_ONLY:
        args += ["--tools", ",".join(CLAUDE_READ_ONLY_TOOLS)]
        settings["disableAllHooks"] = True
    elif override != FOLLOW:
        raise ValueError("unknown permission override")
    if settings:
        args += ["--settings", json.dumps(settings, sort_keys=True, separators=(",", ":"))]
    return args


def shell_allowed(override: str) -> bool:
    """Whether the per-Mac limit leaves shell commands to the account's settings."""
    return override == FOLLOW


# -- Codex ----------------------------------------------------------------------------

CODEX_SETTINGS_FILE = "permissions.json"
CODEX_APPROVALS = ("never", "on-request", "on-failure", "untrusted")
CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
CODEX_REVIEWERS = ("user", "auto_review", "guardian_subagent")


@dataclass(frozen=True)
class CodexLaunch:
    """What one Codex launch gets: written into the isolated home's ``config.toml``."""

    approval_policy: str  # "never" or "on-request"
    reviewer: str | None  # None: the owner answers (nobody is there, so asks are refused)
    writable: bool  # False: a read-only sandbox, nothing in the folder may change
    shell: bool


@dataclass(frozen=True)
class CodexPermissions:
    """An account's Codex approval and sandbox settings for remote tasks.

    Not recorded yet means Codex's own defaults (``on-request``,
    ``workspace-write``, the owner as reviewer).
    """

    approval: str = "on-request"
    sandbox: str = "workspace-write"
    reviewer: str = "user"
    recorded: bool = False

    def describe(self) -> str:
        text = f"approval {self.approval}, sandbox {self.sandbox}"
        if self.reviewer != "user":
            text += f", reviewer {self.reviewer}"
        return text + ("" if self.recorded else " (Codex defaults)")

    def to_dict(self) -> dict:
        return {"approval_policy": self.approval, "sandbox_mode": self.sandbox,
                "approvals_reviewer": self.reviewer, "recorded": self.recorded}

    def launch(self, override: str) -> CodexLaunch:
        """The launch settings under the per-Mac limit.

        ``codex exec`` refuses every approval request (nobody can answer), so
        any policy that may ask behaves like ``on-request``: whatever would
        ask is denied, unless the account's reviewer is Codex's automatic one.
        ``untrusted`` asks before every command that is not known safe, so it
        runs with no shell tool at all (never wider than the owner chose).
        ``danger-full-access`` still gets OpenSwap's folder rules: Codex's own
        sandbox is the only boundary its shell commands have.
        """
        if override not in OVERRIDES:
            raise ValueError("unknown permission override")
        return CodexLaunch(
            approval_policy="never" if self.approval == "never" else "on-request",
            reviewer=self.reviewer if self.reviewer != "user" else None,
            writable=self.sandbox != "read-only" and override != READ_ONLY,
            shell=self.approval != "untrusted" and override != NO_SHELL,
        )


def _codex_from(raw: dict, *, recorded: bool) -> CodexPermissions:
    approval = raw.get("approval_policy", "on-request")
    sandbox = raw.get("sandbox_mode", "workspace-write")
    reviewer = raw.get("approvals_reviewer", "user")
    if isinstance(approval, dict):
        # A granular policy: some prompts asked (refused headless), some
        # refused outright. Kept as ``on-request`` with the owner as the
        # reviewer, so every ask is refused: an automatic reviewer could
        # approve what a granular ``false`` would have rejected.
        granular = approval.get("granular")
        if (set(approval) != {"granular"} or not isinstance(granular, dict) or not granular
                or not all(isinstance(key, str) and type(value) is bool for key, value in granular.items())):
            raise PermissionSettingsError("settings_invalid")
        approval, reviewer = "on-request", "user"
    if approval not in CODEX_APPROVALS or sandbox not in CODEX_SANDBOXES or reviewer not in CODEX_REVIEWERS:
        raise PermissionSettingsError("settings_invalid")
    return CodexPermissions(approval, sandbox, reviewer, recorded)


def read_codex_permissions(home: Path) -> CodexPermissions:
    """The account's recorded Codex settings (Codex's defaults when none are recorded)."""
    raw = _read_json_object(Path(home) / CODEX_SETTINGS_FILE)
    if raw is None:
        return CodexPermissions()
    return _codex_from(raw, recorded=True)


def default_codex_permissions(codex_home: Path | None = None) -> CodexPermissions | None:
    """``approval_policy``, ``approvals_reviewer`` and ``sandbox_mode`` from the owner's
    ``~/.codex/config.toml`` (with its selected ``profile``, if any); None when it sets none."""
    root = Path(codex_home) if codex_home is not None else Path.home() / ".codex"
    try:
        text = (root / "config.toml").read_text(encoding="utf-8")
        config = tomllib.loads(text)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    keys = ("approval_policy", "sandbox_mode", "approvals_reviewer")
    chosen = {key: config[key] for key in keys if key in config}
    profile = config.get("profile")
    profiles = config.get("profiles")
    if isinstance(profile, str) and isinstance(profiles, dict) and isinstance(profiles.get(profile), dict):
        chosen.update({key: profiles[profile][key] for key in keys if key in profiles[profile]})
    if not chosen:
        return None
    try:
        return _codex_from(chosen, recorded=True)
    except PermissionSettingsError:
        return None


def write_codex_permissions(home: Path, permissions: CodexPermissions) -> CodexPermissions:
    from openswap.worker.containment import write_private

    value = _codex_from({"approval_policy": permissions.approval, "sandbox_mode": permissions.sandbox,
                         "approvals_reviewer": permissions.reviewer}, recorded=True)
    write_private(Path(home) / CODEX_SETTINGS_FILE, (json.dumps({
        "approval_policy": value.approval, "sandbox_mode": value.sandbox, "approvals_reviewer": value.reviewer,
    }, indent=2) + "\n").encode("utf-8"))
    return value
