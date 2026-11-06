"""Session attention on this Mac: the hook entry point and its installation (plan 025).

``openswap attention <event>`` is the command Claude Code's hooks run, and
``openswap attention codex-notify -- <previous program…>`` the program
Codex's ``notify`` names. Both hand a *notice* to the local worker and exit
within a second whatever happens, printing nothing: a hook's output could be
read as a decision. Without a running worker the notice is dropped.

The hooks go into the ``settings.json`` of every config home OpenSwap manages
(``~/.claude``, account session profiles and Claude Desktop profiles' Code
folders) with ``async: true``. Codex's ``notify`` is chained: the program
that was there stays in OpenSwap's own argv and still runs with the same
arguments.
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
CODEX_NOTIFY = "codex-notify"
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


def main(argv: list[str]) -> int:
    """``openswap attention <event>``: never raises, never prints, exits 0."""
    import signal

    previous = None
    try:
        previous = signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.setitimer(signal.ITIMER_REAL, HOOK_SECONDS)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        if argv[:1] == [CODEX_NOTIFY]:
            notice = _codex_notice(argv[1:])
        elif len(argv) == 1 and argv[0] in HOOK_EVENTS:
            notice = _hook_notice(argv[0], _read_stdin())
        else:
            notice = None
        if notice is not None:
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


def _codex_notice(argv: list[str]) -> dict | None:
    """Run the program ``notify`` named before OpenSwap with the same arguments,
    then the notice for its ``agent-turn-complete`` payload (the last argument).

    The previous program starts before anything is imported: a slow start
    must not let the hook's deadline skip it."""
    if argv[:1] != ["--"] or len(argv) < 2:
        return None
    previous, payload = argv[1:-1], argv[-1]
    if previous:
        try:
            subprocess.Popen([*previous, payload], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except (OSError, ValueError):
            pass
    from openswap.worker.attention import CODEX_EVENT, clean_text
    from openswap.worker.protocol import DETAIL_CAPS

    try:
        data = json.loads(payload)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("type") != CODEX_EVENT:
        return None
    thread = data.get("thread-id", data.get("thread_id"))
    if not isinstance(thread, str) or not thread:
        return None
    notice = {"provider": "codex", "event": CODEX_EVENT, "thread_id": thread[:200],
              "cwd": data["cwd"] if isinstance(data.get("cwd"), str) else ""}
    message = clean_text(data.get("last-assistant-message"), DETAIL_CAPS["last_message"], multiline=True)
    if message:
        notice["last_message"] = message
    return notice


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


def is_our_notify(notify: object) -> bool:
    return (isinstance(notify, list) and len(notify) >= 4 and all(isinstance(w, str) for w in notify)
            and notify[1:4] == ["attention", CODEX_NOTIFY, "--"] and _is_openswap(notify[0]))


# --- Claude Code settings.json ----------------------------------------------


def config_homes(backup_root: Path) -> list[Path]:
    """Every Claude Code config home OpenSwap manages: ``~/.claude`` (and
    ``CLAUDE_CONFIG_DIR`` when set), each account session profile, and each
    Claude Desktop profile's Code folder. Folders that do not exist are left out."""
    from openswap import paths

    homes = [paths.get_default_claude_config_home(), paths.get_claude_config_home()]
    sessions = Path(backup_root) / "sessions"
    try:
        homes += sorted(p for p in sessions.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        pass
    try:
        from openswap.claude_desktop import DesktopProfiles

        store = DesktopProfiles()
        # Archived too: hooks put in before archiving must come out again.
        for profile in store.list(include_archived=True):
            code = store.paths(profile)[1]
            if code is not None:
                homes.append(code)
    except Exception as exc:  # noqa: BLE001 - an unreadable registry leaves its profiles out
        logger.warning("Claude Desktop profiles left out of session notices: %s", exc)
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


# --- Codex notify -------------------------------------------------------------


_NOTIFY_LINE = re.compile(r"\s*notify\s*=")


def _toml_string(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError("Codex's notify program has a name this cannot write safely")
    return json.dumps(value, ensure_ascii=False)


def _set_notify_text(text: str, notify: list[str] | None) -> str:
    """``text`` (a config.toml) with the top-level ``notify`` set to ``notify``
    (None: removed), every other line kept as written. Raises ConfigError
    unless the result parses to the same document with only ``notify`` changed."""
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Codex's config.toml does not parse ({exc}); refusing to rewrite it") from exc
    line = None if notify is None else "notify = [" + ", ".join(_toml_string(w) for w in notify) + "]\n"
    expected = {k: v for k, v in before.items() if k != "notify"}
    if notify is not None:
        expected["notify"] = notify
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    candidates = []
    if "notify" in before:
        # The value may span lines: each start is tried with each end, and only
        # a rewrite that leaves every other key unchanged is accepted.
        for start, current in enumerate(lines):
            if _NOTIFY_LINE.match(current):
                candidates += [(start, end) for end in range(start + 1, len(lines) + 1)]
    else:
        candidates.append((0, 0))
    for start, end in candidates:
        rewritten = "".join(lines[:start] + ([line] if line else []) + lines[end:])
        try:
            if tomllib.loads(rewritten) == expected:
                return rewritten
        except tomllib.TOMLDecodeError:
            continue
    raise ConfigError("Codex's notify setting is in a form this cannot rewrite safely")


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


def chain_codex_notify(codex_home: Path, executable: str) -> bool:
    """Point Codex's ``notify`` at OpenSwap, keeping the program that was there
    (as the tail of OpenSwap's argv, so it still runs with the same arguments).

    Already chained: only a different OpenSwap path is repointed. A program
    that replaced OpenSwap's entry since becomes the kept one. Returns whether
    config.toml changed. Codex not installed (no home): nothing to do.
    """
    if not Path(codex_home).is_dir():
        return False
    path, text = _codex_config(codex_home)
    try:
        current = tomllib.loads(text).get("notify")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Codex's config.toml does not parse ({exc}); refusing to rewrite it") from exc
    if is_our_notify(current):
        kept = current[4:]
    elif current is None:
        kept = []
    elif isinstance(current, list) and all(isinstance(w, str) for w in current):
        kept = current
    else:
        raise ConfigError("Codex's notify setting is not a list of strings; refusing to rewrite it")
    wanted = [executable, "attention", CODEX_NOTIFY, "--", *kept]
    if current == wanted:
        return False
    _write_text(path, _set_notify_text(text, wanted))
    return True


def unchain_codex_notify(codex_home: Path) -> bool:
    """Put back the program ``notify`` named before OpenSwap, only while
    ``notify`` still names OpenSwap. Returns whether config.toml changed."""
    path, text = _codex_config(codex_home)
    if not text:
        return False
    try:
        current = tomllib.loads(text).get("notify")
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Codex's config.toml does not parse ({exc}); its notify was not checked") from exc
    if not is_our_notify(current):
        return False
    _write_text(path, _set_notify_text(text, current[4:] or None))
    return True


# --- turning it on and off -----------------------------------------------------


def apply(backup_root: Path, enabled: bool) -> tuple[bool, list[str]]:
    """Install (or remove) the hooks in every managed config home and chain (or
    unchain) Codex's ``notify``. Every place is tried; returns whether anything
    changed, and what could not be changed, in words, for the owner."""
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
            changed = chain_codex_notify(codex_home(), executable) or changed
        else:
            changed = unchain_codex_notify(codex_home()) or changed
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
    """Uninstall: take OpenSwap's hooks out of every managed config home and put
    Codex's ``notify`` back. Raises ConfigError naming what could not be changed."""
    changed, problems = apply(backup_root, False)
    if problems:
        raise ConfigError("; ".join(problems))
    return changed


def reconcile(backup_root: Path) -> list[str]:
    """At worker start: while notices are on, install the hooks in config homes
    created since and re-chain a ``notify`` another program rewrote; while
    they are off, take out hooks a failed or interrupted removal left. An
    unreadable switch changes nothing (ConfigError): it is not an "off"."""
    from openswap.settings import load_attention_settings

    with _switch_lock(backup_root):
        return apply(backup_root, load_attention_settings(backup_root, strict=True).notices)[1]
