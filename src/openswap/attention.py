"""Session attention on this Mac: the hook entry point and its installation (plan 025).

``openswap attention <event>`` is the command Claude Code's hooks run, and
``openswap attention codex <event>`` the one Codex's hooks run (through a
launcher script at a fixed path). Both hand a *notice* to the local worker
and exit within a second whatever happens, printing nothing: a hook's output
could be read as a decision. Without a running worker the notice is dropped.

The Claude Code hooks go into the ``settings.json`` of every config home
OpenSwap manages (``~/.claude``, account session profiles and Claude Desktop
profiles' Code folders) with ``async: true``. The Codex hooks go into the
Codex home's ``hooks.json``, with Codex's ``hooks`` feature turned on; its
``notify`` is left alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

from openswap.exceptions import ConfigError

logger = logging.getLogger(__name__)

HOOK_SECONDS = 0.9  # the hook process ends by then whatever happens
SEND_TIMEOUT_SECONDS = 0.5
MAX_STDIN_BYTES = 32 * 1024 * 1024
CODEX = "codex"
# Hook events and their matchers ("" matches everything).
HOOK_EVENTS = {
    "PermissionRequest": "",
    "Notification": "permission_prompt|idle_prompt",
    "Stop": "",
    "UserPromptSubmit": "",
    "PostToolUse": "",
    "PostToolUseFailure": "",
    "SessionEnd": "",
}
HOOK_TIMEOUT_SECONDS = 10


# --- the entry point ---------------------------------------------------------


CHANNEL_TAG = '<channel source="openswap"'
# How Claude Code starts a turn with a background task's (a subagent's) result.
TASK_TAG = "<task-notification>"
SESSION_COMMANDS = frozenset({"/clear", "/exit", "/quit"})


def main(argv: list[str]) -> int:
    """``openswap attention <event>``: never raises, never prints, exits 0."""
    import signal
    import time

    started = time.time()

    previous = None
    try:
        previous = signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.setitimer(signal.ITIMER_REAL, HOOK_SECONDS)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        if len(argv) == 2 and argv[0] == CODEX and argv[1] in CODEX_HOOK_EVENTS:
            notice = _codex_notice(argv[1], _read_stdin())
        elif len(argv) == 1 and argv[0] in HOOK_EVENTS:
            notice = _hook_notice(argv[0], _read_stdin())
        else:
            notice = None
        if notice is not None:
            notice["at"] = started
            send_notice(notice)
    except BaseException:  # noqa: BLE001 - a hook must never fail loudly
        pass
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    return 0


def _read_stdin() -> bytes:
    data = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    return b"" if len(data) > MAX_STDIN_BYTES else data


def tool_key(tool_input: object) -> str:
    """A digest that matches a ``PermissionRequest`` to its ``PostToolUse``; the
    tool's input itself never leaves the hook."""
    encoded = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _hook_notice(event: str, raw: bytes) -> dict | None:
    """The notice for one Claude Code hook input, or None when it is not one."""
    from openswap.worker.attention import clean_questions, clean_text
    from openswap.worker.protocol import DETAIL_CAPS

    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not data["session_id"]:
        return None
    from openswap.worker.attention import is_entrypoint

    notice = {"provider": "claude", "event": event, "session_id": data["session_id"][:200],
              "cwd": data["cwd"] if isinstance(data.get("cwd"), str) else ""}
    pid = claude_pid()
    if pid:
        notice["pid"] = pid
    entrypoint = os.environ.get("CLAUDE_CODE_ENTRYPOINT")
    if is_entrypoint(entrypoint):
        notice["entrypoint"] = entrypoint
    tool_name = clean_text(data.get("tool_name"), DETAIL_CAPS["tool_name"])
    tool_input = data.get("tool_input")
    if event in ("PermissionRequest", "PostToolUse", "PostToolUseFailure") and tool_name:
        agent = data.get("agent_id")
        if isinstance(agent, str) and 0 < len(agent) <= 64 and all(
                c.isascii() and (c.isalnum() or c in "_-") for c in agent):
            notice["agent_id"] = agent
        notice["tool_name"] = tool_name
        notice["tool_key"] = tool_key(tool_input)
        if event == "PermissionRequest" and isinstance(tool_input, dict):
            if tool_name == "AskUserQuestion":
                notice["questions"] = clean_questions(tool_input.get("questions"))
            elif tool_name == "Bash":
                # The model's own words for the command, never the raw command.
                description = clean_text(tool_input.get("description"), DETAIL_CAPS["description"],
                                         multiline=True)
                if description:
                    notice["description"] = description
    elif event == "Notification":
        notice["notification_type"] = clean_text(data.get("notification_type"), 64)
    elif event == "UserPromptSubmit":
        # What kind of prompt only; the prompt itself never leaves the hook.
        prompt = data.get("prompt") if isinstance(data.get("prompt"), str) else ""
        if prompt.startswith(CHANNEL_TAG):
            notice["prompt_kind"] = "channel"
        elif prompt.startswith(TASK_TAG):
            notice["prompt_kind"] = "task"
        elif prompt.strip().split(" ", 1)[0] in SESSION_COMMANDS:
            notice["prompt_kind"] = "end"
    elif event == "Stop":
        message = clean_text(data.get("last_assistant_message"), DETAIL_CAPS["last_message"], multiline=True)
        if message:
            notice["last_message"] = message
    return notice


_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "mksh", "tcsh", "csh"})


def claude_pid() -> int:
    """The Claude Code process running this hook: the parent, or the
    grandparent when Claude Code ran the hook through a shell.

    0 when it cannot be told: a shell's PID taken for Claude Code's would end
    the session's items as soon as the hook exits."""
    parent = os.getppid()
    if os.environ.get("CLAUDE_CODE_SHELL_PREFIX"):
        return 0  # a wrapper of the owner's own may sit between the hook and Claude Code
    try:
        out = subprocess.run(["/bin/ps", "-o", "ppid=,comm=", "-p", str(parent)], capture_output=True,
                             text=True, timeout=0.3).stdout.split(None, 1)
        if len(out) != 2:
            return 0
        pid = int(out[0]) if Path(out[1].strip()).name.lstrip("-") in _SHELLS else parent
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0
    return pid if pid > 1 else 0


def _codex_notice(event: str, raw: bytes) -> dict | None:
    """The notice for one Codex hook input, or None when it is not one or is
    an approval no person is asked to give (``approval_policy = "never"``, or
    Codex's automatic reviewer deciding)."""
    from openswap.worker.attention import clean_text, is_agent_id
    from openswap.worker.protocol import DETAIL_CAPS

    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not data["session_id"]:
        return None
    if event == "PermissionRequest" and (
            data.get("permission_mode") == "bypassPermissions"
            or approvals_reviewer(data.get("transcript_path"), data.get("turn_id")) not in (None, "user")):
        return None
    notice = {"provider": "codex", "event": event, "session_id": data["session_id"][:200],
              "cwd": data["cwd"] if isinstance(data.get("cwd"), str) else ""}
    pid = claude_pid()  # the same rule finds the Codex process that ran the hook
    if pid:
        notice["pid"] = pid
    if is_agent_id(data.get("agent_id")):
        notice["agent_id"] = data["agent_id"]
    tool_name = clean_text(data.get("tool_name"), DETAIL_CAPS["tool_name"])
    tool_input = data.get("tool_input")
    if event in ("PermissionRequest", "PostToolUse") and tool_name:
        notice["tool_name"] = tool_name
        # A PermissionRequest's input carries Codex's reason for asking, which
        # its PostToolUse does not: the key is of the call itself.
        if isinstance(tool_input, dict):
            notice["tool_key"] = tool_key({k: v for k, v in tool_input.items() if k != "description"})
            if event == "PermissionRequest":
                description = clean_text(tool_input.get("description"), DETAIL_CAPS["description"],
                                         multiline=True)
                if description:
                    notice["description"] = description
        else:
            notice["tool_key"] = tool_key(tool_input)
    elif event == "Stop":
        message = clean_text(data.get("last_assistant_message"), DETAIL_CAPS["last_message"], multiline=True)
        if message:
            notice["last_message"] = message
    return notice


TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024


def approvals_reviewer(transcript: object, turn_id: object) -> str | None:
    """Who answers the thread's approvals (Codex's ``approvals_reviewer``:
    ``user`` for a person), as its session record's ``turn_context`` for that
    turn says (else its latest one). None when the record cannot tell."""
    if not isinstance(transcript, str) or not transcript:
        return None
    try:
        with open(transcript, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - TRANSCRIPT_TAIL_BYTES))
            lines = handle.read(TRANSCRIPT_TAIL_BYTES).splitlines()
    except (OSError, ValueError):
        return None
    latest = None
    for line in reversed(lines):
        if b'"turn_context"' not in line:
            continue
        try:
            record = json.loads(line)
            payload = record["payload"] if record.get("type") == "turn_context" else None
        except (ValueError, RecursionError, KeyError, TypeError, AttributeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("approvals_reviewer"), str):
            continue
        if payload.get("turn_id") == turn_id:
            return payload["approvals_reviewer"]
        latest = latest or payload["approvals_reviewer"]
    return latest


def send_notice(notice: dict, *, backup_root: Path | None = None) -> None:
    """Hand one notice to the worker's socket; any failure drops it."""
    from openswap import paths
    from openswap.worker.ipc import IPC_VERSION, MAX_REQUEST_BYTES, socket_path

    if len(notice.get("cwd", "")) > 4096 or any(ord(c) < 32 for c in notice.get("cwd", "")):
        notice["cwd"] = ""
    request = json.dumps({"version": IPC_VERSION, "command": "notice", "notice": notice},
                         separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    if len(request) > MAX_REQUEST_BYTES:
        return
    path = socket_path(backup_root or paths.get_backup_root())
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(SEND_TIMEOUT_SECONDS)
        try:
            client.connect(str(path))
            client.sendall(request)
            client.recv(256)
        except OSError:
            return


# --- which program the hooks run -------------------------------------------


def program() -> str:
    """The openswap the hooks run, as the status line pins it (the app's own
    executable, else this install's ``openswap``)."""
    from openswap.statusline import paint_command

    words = shlex.split(paint_command())
    return words[0] if len(words) == 2 else "openswap"


def hook_command(event: str, executable: str) -> str:
    return f"{shlex.quote(executable)} attention {event}"


def _is_openswap(word: str) -> bool:
    from openswap.statusline import _is_app_executable

    return Path(word).name == "openswap" or _is_app_executable(word)


def is_our_hook_command(command: object) -> bool:
    """Whether ``command`` is ``<openswap> attention <event>`` for any openswap."""
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return len(words) == 3 and words[1] == "attention" and words[2] in HOOK_EVENTS and _is_openswap(words[0])


# --- Claude Code settings.json ----------------------------------------------


def config_homes(backup_root: Path, problems: list | None = None) -> list[Path]:
    """Every Claude Code config home OpenSwap manages: ``~/.claude`` (and
    ``CLAUDE_CONFIG_DIR`` when set), each account session profile, and each
    Claude Desktop profile's Code folder. Folders that do not exist are left out.
    A place that could not be listed is appended to ``problems``."""
    from openswap import paths

    homes = [paths.get_default_claude_config_home(), paths.get_claude_config_home()]
    sessions = Path(backup_root) / "sessions"
    try:
        homes += sorted(p for p in sessions.iterdir() if p.is_dir() and not p.is_symlink())
    except FileNotFoundError:
        pass
    except OSError as exc:
        if problems is not None:
            problems.append(f"{sessions}: {exc}")
    try:
        from openswap.claude_desktop import DesktopProfiles

        store = DesktopProfiles()
        # Archived too: hooks put in before archiving must come out again.
        for profile in store.list(include_archived=True):
            code = store.paths(profile)[1]
            if code is not None:
                homes.append(code)
    except Exception as exc:  # noqa: BLE001 - an unreadable registry leaves its profiles out
        # Read every few seconds by the worker: never a warning per read.
        logger.debug("Claude Desktop profiles left out of session notices: %s", exc)
        if problems is not None:
            problems.append(f"Claude Desktop profiles: {exc}")
    out = []
    for home in homes:
        if home not in out and home.is_dir():
            out.append(home)
    return out


def _without_ours(groups: list) -> list:
    """``groups`` with OpenSwap's attention hooks taken out; a group left empty goes."""
    out = []
    for group in groups:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(hooks, list):
            out.append(group)
            continue
        kept = [h for h in hooks if not (isinstance(h, dict) and is_our_hook_command(h.get("command")))]
        if len(kept) == len(hooks):
            out.append(group)
        elif kept:
            out.append({**group, "hooks": kept})
    return out


def _with_hooks(settings: dict, executable: str | None) -> dict:
    """``settings`` with OpenSwap's hooks for ``executable`` (None: removed).
    Every other hook is kept as written. Raises ConfigError for a ``hooks``
    value this would have to overwrite."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(hooks.get(e, []), list) for e in HOOK_EVENTS):
        raise ConfigError("Claude Code's hooks setting is not in the expected form; refusing to rewrite it")
    hooks = dict(hooks)
    for event, matcher in HOOK_EVENTS.items():
        groups = _without_ours(hooks.get(event, []))
        if executable is not None:
            groups.append({"matcher": matcher, "hooks": [{
                "type": "command", "command": hook_command(event, executable),
                "async": True, "timeout": HOOK_TIMEOUT_SECONDS,
            }]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    result = dict(settings)
    if hooks:
        result["hooks"] = hooks
    else:
        result.pop("hooks", None)
    return result


def _has_ours(settings: dict) -> bool:
    hooks = settings.get("hooks")
    return isinstance(hooks, dict) and any(
        isinstance(h, dict) and is_our_hook_command(h.get("command"))
        for groups in hooks.values() if isinstance(groups, list)
        for group in groups if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for h in group["hooks"])


def set_hooks(config_home: Path, executable: str | None) -> bool:
    """Install OpenSwap's hooks for ``executable`` in one config home, or remove
    them (None). Reads before writing: a settings file that cannot be read is
    left alone (ConfigError). A file without OpenSwap's hooks is never
    rewritten to remove them. Returns whether the file changed."""
    from openswap.statusline import _read_json_for_write, _write_json

    path = Path(config_home) / "settings.json"
    if executable is None and not os.path.lexists(path):
        return False
    settings = _read_json_for_write(path)
    if executable is None and not _has_ours(settings):
        return False
    updated = _with_hooks(settings, executable)
    if updated == settings:
        return False
    _write_json(path, updated)
    return True


# --- Codex hooks.json ----------------------------------------------------------

# Codex hook event -> (timeout, async). Codex runs SessionEnd synchronously
# whatever ``async`` says, and warns at every start when it says true;
# Interrupt allows at most 3 s.
CODEX_HOOK_EVENTS = {
    "PermissionRequest": (10, True), "PostToolUse": (10, True), "Stop": (10, True),
    "UserPromptSubmit": (10, True), "Interrupt": (3, True), "SessionEnd": (3, False),
}
CODEX_LAUNCHER = Path("bin") / "codex-hook"
# Per Codex home: what ``features.hooks`` was before OpenSwap turned it on.
CODEX_STATE_FILE = Path("worker") / "attention-codex.json"
# Codex's own managed configuration: never written; read for whether it blocks hooks.
CODEX_MANAGED_FILES = (Path("/etc/codex/requirements.toml"), Path("/etc/codex/managed_config.toml"))
CODEX_MANAGED_PLISTS = (Path("/Library/Managed Preferences/com.openai.codex.plist"),
                        Path("/Library/Managed Preferences") / Path.home().name / "com.openai.codex.plist")
_MISSING = object()


def codex_launcher(backup_root: Path) -> Path:
    """The script Codex's hook entries run. Its path never changes, so the
    entries, and the trust Codex records against them, stay the same across
    app updates, moves and DEV builds; only the program it starts changes."""
    return Path(backup_root) / CODEX_LAUNCHER


def codex_hook_entry(event: str, launcher: Path) -> dict:
    timeout, run_async = CODEX_HOOK_EVENTS[event]
    return {"type": "command", "command": f"{shlex.quote(str(launcher))} {event}",
            "timeout": timeout, "async": run_async}


def codex_hook_hash(event: str, entry: dict) -> str:
    """The hash Codex records as ``trusted_hash`` once the owner trusts ``entry``
    (no matcher), as Codex 0.166 computes it."""
    label = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
    identity = json.dumps({"event_name": label, "hooks": [entry]}, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    return "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _write_launcher(backup_root: Path, executable: str) -> None:
    path = codex_launcher(backup_root)
    quoted = shlex.quote(executable)
    # A moved app leaves Codex nothing to run until the worker repoints this: say nothing.
    text = (f"#!/bin/sh\ncommand -v {quoted} >/dev/null 2>&1 || exit 0\n"
            f"exec {quoted} attention {CODEX} \"$@\"\n")
    try:
        if path.read_text(encoding="utf-8") == text and os.access(path, os.X_OK):
            return
    except (OSError, UnicodeDecodeError):
        pass
    _write_text(path, text)
    os.chmod(path, 0o755)


def _refuse_managed(codex_home: Path) -> None:
    resolved = os.path.realpath(codex_home)
    if any(resolved == root or resolved.startswith(root + "/") for root in ("/etc", "/private/etc")):
        raise ConfigError(f"{codex_home} holds managed Codex configuration; OpenSwap never writes it")


def _read_hooks_file(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}; refusing to overwrite it unread") from exc
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ConfigError(f"{path} does not parse ({exc}); refusing to rewrite it") from exc
    hooks = data.get("hooks", {}) if isinstance(data, dict) else None
    if not isinstance(hooks, dict) or any(not isinstance(groups, list) for groups in hooks.values()):
        raise ConfigError(f"{path} is not in the form Codex reads; refusing to rewrite it")
    return data


def _is_ours_in(handler: object, launcher: Path) -> bool:
    if not isinstance(handler, dict) or not isinstance(handler.get("command"), str):
        return False
    try:
        words = shlex.split(handler["command"])
    except ValueError:
        return False
    return len(words) == 2 and words[0] == str(launcher) and words[1] in CODEX_HOOK_EVENTS


def _codex_groups(groups: list, event: str, launcher: Path | None, ours_at: Path) -> list:
    """``groups`` with OpenSwap's entry for ``event`` (None: without it).

    An entry already there keeps its place, and another hook's group is never
    moved: Codex records trust by position, so moving one would take back the
    owner's trust in it. A new entry goes last."""
    wanted = None if launcher is None else {"hooks": [codex_hook_entry(event, launcher)]}
    out, placed = [], False
    for group in groups:
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list) or not any(_is_ours_in(h, ours_at) for h in handlers):
            out.append(group)
            continue
        kept = [h for h in handlers if not _is_ours_in(h, ours_at)]
        if wanted is not None and not placed and not kept:
            out.append(wanted)
            placed = True
        elif kept:
            out.append({**group, "hooks": kept})
    if wanted is not None and not placed:
        out.append(wanted)
    return out


def _feature_text(text: str, value: bool | None, *, drop_table: bool = False) -> str:
    """``text`` (a config.toml) with ``[features] hooks`` set to ``value`` (None:
    removed, and with ``drop_table`` the ``[features]`` table too once nothing
    else is in it), every other line kept as written. Raises ConfigError unless
    the result parses to the same document with only that changed."""
    import copy

    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Codex's config.toml does not parse ({exc}); refusing to rewrite it") from exc
    expected = copy.deepcopy(before)
    features = expected.setdefault("features", {})
    if not isinstance(features, dict):
        raise ConfigError("Codex's features setting is in a form this cannot rewrite safely")
    if value is None:
        if "hooks" not in features:
            return text
        del features["hooks"]
    else:
        features["hooks"] = value
    without_table = {k: v for k, v in expected.items() if k != "features"}
    line = None if value is None else f"hooks = {'true' if value else 'false'}\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    header = next((n for n, current in enumerate(lines)
                   if re.fullmatch(r"\s*\[\s*features\s*\]\s*(#.*)?\s*", current)), None)
    candidates = []  # (text, what it must parse to)
    if header is None:
        if line is not None:
            candidates.append((lines + ["\n" if lines else "", "[features]\n", line], expected))
    else:
        table_end = next((n for n in range(header + 1, len(lines)) if lines[n].lstrip().startswith("[")),
                         len(lines))
        found = [n for n in range(header + 1, table_end) if re.match(r"\s*hooks\s*=", lines[n])]
        for n in found:
            if drop_table and not features:
                # The table as OpenSwap added it: a blank line, the header, the key.
                first = header - 1 if header and not lines[header - 1].strip() else header
                candidates.append((lines[:first] + lines[header + 1:n] + lines[n + 1:], without_table))
            candidates.append((lines[:n] + ([line] if line else []) + lines[n + 1:], expected))
        if not found and line is not None:
            candidates.append((lines[:header + 1] + [line] + lines[header + 1:], expected))
    for candidate, wanted in candidates:
        rewritten = "".join(candidate)
        try:
            if tomllib.loads(rewritten) == wanted:
                return rewritten
        except tomllib.TOMLDecodeError:
            continue
    raise ConfigError("Codex's hooks feature setting is in a form this cannot rewrite safely")


def _codex_config(codex_home: Path) -> tuple[Path, str]:
    path = Path(codex_home) / "config.toml"
    try:
        return path, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return path, ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}; refusing to overwrite it unread") from exc


def _write_text(path: Path, text: str) -> None:
    """Atomic write through a symlink, like the status line's settings writes."""
    from openswap.fsutil import replace_with_retry

    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, text.encode("utf-8"))
        os.close(fd)
        fd = -1
        replace_with_retry(tmp, str(target))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _codex_state(backup_root: Path) -> dict:
    try:
        data = json.loads((Path(backup_root) / CODEX_STATE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ConfigError(f"OpenSwap's record of Codex's hooks setting is unreadable ({exc})") from exc
    if not isinstance(data, dict):
        raise ConfigError("OpenSwap's record of Codex's hooks setting is unreadable")
    return data


def _save_codex_state(backup_root: Path, state: dict) -> None:
    path = Path(backup_root) / CODEX_STATE_FILE
    if not state:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_text(path, json.dumps(state, indent=1) + "\n")


def set_codex_hooks(codex_home: Path, backup_root: Path, enabled: bool) -> bool:
    """Install OpenSwap's hooks in one Codex home's ``hooks.json`` and turn on
    Codex's ``hooks`` feature, or take them out and put the feature back as
    it was (left on while ``hooks.json`` still holds other hooks). Codex not
    installed (no home): nothing to do. Returns whether anything changed."""
    home = Path(codex_home)
    _refuse_managed(home)
    if not home.is_dir():
        return False
    launcher = codex_launcher(backup_root)
    path = home / "hooks.json"
    data = _read_hooks_file(path)
    hooks = dict(data.get("hooks", {}))
    for event in CODEX_HOOK_EVENTS:
        groups = _codex_groups(hooks.get(event, []), event, launcher if enabled else None, launcher)
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    updated = {**data, "hooks": hooks}
    changed = False
    if updated != data and not (not enabled and not data):
        if not hooks and set(updated) == {"hooks"}:
            path.unlink()
        else:
            _write_text(path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
        changed = True
    others = any(not _is_ours_in(h, launcher) for groups in hooks.values() for g in groups
                 if isinstance(g, dict) and isinstance(g.get("hooks"), list) for h in g["hooks"])
    return _set_codex_feature(home, backup_root, enabled, keep_on=others) or changed


def _set_codex_feature(home: Path, backup_root: Path, enabled: bool, *, keep_on: bool) -> bool:
    config, text = _codex_config(home)
    try:
        features = tomllib.loads(text).get("features", {})
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Codex's config.toml does not parse ({exc}); refusing to rewrite it") from exc
    current = features.get("hooks", _MISSING) if isinstance(features, dict) else _MISSING
    state = _codex_state(backup_root)
    key = os.path.realpath(home)
    if enabled:
        if current is True:
            return False
        state.setdefault(key, {"hooks": None if current is _MISSING else current,
                               "table": "features" not in tomllib.loads(text)})
        _save_codex_state(backup_root, state)
        _write_text(config, _feature_text(text, True))
        return True
    record = state.pop(key, None)
    if record is None:
        return False
    before = record.get("hooks") if isinstance(record, dict) else True
    # Turned off by the owner since, or still needed by other hooks: left as it is.
    restore = current is True and not keep_on and before in (None, False)
    if restore:
        _write_text(config, _feature_text(text, before, drop_table=record.get("table") is True))
    _save_codex_state(backup_root, state)
    return restore


def _managed_blocks_hooks() -> bool | None:
    """Whether Codex's managed configuration turns hooks off or allows only its
    own (True), lets them run (False), or cannot be read (None)."""
    import base64
    import plistlib

    documents = []
    for path in CODEX_MANAGED_FILES:
        try:
            documents.append(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError):
            return None
    for path in CODEX_MANAGED_PLISTS:
        try:
            with open(path, "rb") as handle:
                prefs = plistlib.load(handle)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None
        for name in ("requirements_toml_base64", "config_toml_base64"):
            if isinstance(prefs, dict) and isinstance(prefs.get(name), str):
                try:
                    documents.append(base64.b64decode(prefs[name]).decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    return None
    for text in documents:
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        features = data.get("features") if isinstance(data.get("features"), dict) else {}
        if data.get("allow_managed_hooks_only") is True or features.get("hooks") is False:
            return True
    return False


def codex_hooks_status(codex_home: Path, backup_root: Path) -> str | None:
    """Codex's actual state for OpenSwap's hooks in one Codex home, from Codex's
    own files: ``trusted``, ``review`` (Codex shows "Hooks need review" at its
    next start), ``disabled`` (turned off in Codex's ``/hooks``), ``off``
    (Codex's hooks feature is off), ``blocked`` (managed configuration),
    ``missing`` (not in ``hooks.json``) or ``unknown``. None without Codex."""
    home = Path(codex_home)
    if not home.is_dir():
        return None
    blocked = _managed_blocks_hooks()
    if blocked is None:
        return "unknown"
    if blocked:
        return "blocked"
    try:
        _, text = _codex_config(home)
        config = tomllib.loads(text)
        data = _read_hooks_file(home / "hooks.json")
    except (ConfigError, tomllib.TOMLDecodeError):
        return "unknown"
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    if features.get("hooks", features.get("codex_hooks")) is False:
        return "off"
    launcher = codex_launcher(backup_root)
    hooks_state = config.get("hooks", {}).get("state") if isinstance(config.get("hooks"), dict) else None
    hooks_state = hooks_state if isinstance(hooks_state, dict) else {}
    sources = {str(home / "hooks.json"), os.path.join(os.path.realpath(home), "hooks.json")}
    statuses = set()
    for event in CODEX_HOOK_EVENTS:
        entry = codex_hook_entry(event, launcher)
        label = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
        place = next(((g, h) for g, group in enumerate(data.get("hooks", {}).get(event, []))
                      if isinstance(group, dict) and isinstance(group.get("hooks"), list)
                      for h, handler in enumerate(group["hooks"]) if handler == entry), None)
        if place is None:
            return "missing"
        state = next((hooks_state[k] for k in (f"{s}:{label}:{place[0]}:{place[1]}" for s in sources)
                      if isinstance(hooks_state.get(k), dict)), {})
        if state.get("enabled") is False:
            statuses.add("disabled")
        elif state.get("trusted_hash") != codex_hook_hash(event, entry):
            statuses.add("review")
    return "disabled" if "disabled" in statuses else "review" if statuses else "trusted"


# --- turning it on and off -----------------------------------------------------


def apply(backup_root: Path, enabled: bool) -> tuple[bool, list[str]]:
    """Install (or remove) the hooks in every managed config home and in the
    Codex home. Every place is tried; returns whether anything changed, and
    what could not be changed, in words, for the owner."""
    from openswap.codex.auth import codex_home

    executable = program() if enabled else None
    changed = False
    problems = []
    for home in config_homes(backup_root):
        is_profile = Path(backup_root) / "sessions" in home.parents
        # An account profile without its own settings.json shares ~/.claude's
        # once sharing is on; creating one would block that link.
        if is_profile and not os.path.lexists(home / "settings.json"):
            continue
        try:
            changed = set_hooks(home, executable) or changed
        except (ConfigError, OSError) as exc:
            problems.append(f"{home}: {exc}")
    try:
        if enabled:
            _write_launcher(backup_root, executable)
        changed = set_codex_hooks(codex_home(), backup_root, enabled) or changed
        if not enabled:
            codex_launcher(backup_root).unlink(missing_ok=True)
    except (ConfigError, OSError) as exc:
        problems.append(f"Codex: {exc}")
    return changed, problems


@contextmanager
def _switch_lock(backup_root: Path):
    """Held while the notices switch is read or written together with the hooks
    it implies, so a worker starting while the owner flips it cannot undo the flip."""
    from openswap.locking import FileLock

    path = Path(backup_root) / "worker" / "attention.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = FileLock(path, timeout=10)
    if not lock.acquire():
        raise ConfigError("another change to session notices is still running; try again")
    try:
        yield
    finally:
        lock.release()


def set_notices(backup_root: Path, enabled: bool) -> list[str]:
    """Settings › Remote › "Tell me when a session needs me": the switch, then the hooks.

    Turning it on from a translocated or mounted copy of the app raises
    ClaudeSwitchError (the hooks would name a path that disappears).
    """
    from openswap.settings import write_attention_settings

    if enabled:
        from openswap import bundle
        from openswap.exceptions import ClaudeSwitchError

        if bundle.runs_from_temporary_location():
            from openswap.launch_agent import MOVE_TO_APPLICATIONS

            raise ClaudeSwitchError(MOVE_TO_APPLICATIONS)
    with _switch_lock(backup_root):
        write_attention_settings(backup_root, notices=enabled)
        return apply(backup_root, enabled)[1]


def remove(backup_root: Path) -> bool:
    """Uninstall: take OpenSwap's hooks out of every managed config home and the
    Codex home. Raises ConfigError naming what could not be changed."""
    changed, problems = apply(backup_root, False)
    if problems:
        raise ConfigError("; ".join(problems))
    return changed


def reconcile(backup_root: Path) -> list[str]:
    """At worker start: while notices are on, install the hooks in config homes
    created since and repoint the Codex launcher at this app; while they are
    off, take out hooks a failed or interrupted removal left. An
    unreadable switch changes nothing (ConfigError): it is not an "off"."""
    from openswap.settings import load_attention_settings

    with _switch_lock(backup_root):
        return apply(backup_root, load_attention_settings(backup_root, strict=True).notices)[1]
