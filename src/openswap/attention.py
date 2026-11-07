"""Session attention on this Mac: the hook entry point and its installation (plan 025).

``openswap attention <event>`` is the command Claude Code's hooks run, and
``openswap attention codex <event>`` the one Codex's hooks run (through a
launcher script at a fixed path). Both hand a *notice* to the local worker
and exit within a second whatever happens, printing nothing: a hook's output
could be read as a decision. Without a running worker the notice is dropped.

The Claude Code hooks go into the ``settings.json`` of every config home
OpenSwap manages (``~/.claude``, account session profiles and Claude Desktop
profiles' Code folders) with ``async: true``. The Codex hooks go into the
Codex home's ``hooks.json``; Codex's ``hooks`` feature is turned on only by
the owner's own action, and its ``notify`` is left alone.
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
    "SubagentStop": "",
    "SessionEnd": "",
}
HOOK_TIMEOUT_SECONDS = 10
# The synchronous question hook (installed only while replies are on) waits up to
# this long for an answer from the phone or watch; the worker ends its wait sooner.
QUESTION_EVENT = "question"
QUESTION_TIMEOUT_SECONDS = 600
CHANNEL_COMMAND = "channel"


# --- the entry point ---------------------------------------------------------


CHANNEL_TAG = '<channel source="openswap"'
# A message from the phone or watch carries its reply's token back in this attribute.
_REPLY_ATTRIBUTE = re.compile(r'<channel source="openswap" reply="([a-z0-9]{1,32})"')
# How Claude Code starts a turn with a background task's (a subagent's) result.
TASK_TAG = "<task-notification>"
SESSION_COMMANDS = frozenset({"/clear", "/exit", "/quit"})


def main(argv: list[str]) -> int:
    """``openswap attention <event>``: never raises, never prints, exits 0.

    ``openswap attention channel`` is OpenSwap's channel (see ``openswap.channel``)
    and ``openswap attention question`` the synchronous question hook, which
    prints a decision when it has one."""
    if argv == [CHANNEL_COMMAND]:
        from openswap.channel import main as channel_main

        return channel_main()
    if argv == [QUESTION_EVENT]:
        return question_main()
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


# Claude Code 2.1.311 previews a call for a channel as ``{ <field>, <field> }``,
# each field ``JSON.stringify(key): JSON.stringify(value)``, with control and
# some look-alike characters replaced, runs of whitespace collapsed, long
# fields cut and credentials masked. Only a preview none of that changed can
# prove which call it shows.
PREVIEW_FIELD_LIMIT = 3500
PREVIEW_TOTAL_LIMIT = 15000


def channel_preview(tool_input: object) -> str | None:
    """The ``input_preview`` Claude Code relays to a channel for this input, or
    None when its preview would not show the input exactly (then no channel
    request can be proven to be this call)."""
    if not isinstance(tool_input, dict):
        return None
    fields = []
    for key, value in tool_input.items():
        try:
            parts = [json.dumps(part, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                     for part in (key, value)]
        except (TypeError, ValueError, RecursionError):
            return None
        for part in parts:
            if (len(part) > PREVIEW_FIELD_LIMIT or "  " in part or "[REDACTED]" in part
                    or not all(" " <= c <= "~" for c in part)):
                return None
        fields.append(f"{parts[0]}: {parts[1]}")
    preview = "{ " + ", ".join(fields) + " }"
    return preview if len(preview) <= PREVIEW_TOTAL_LIMIT else None


def tool_key(tool_input: object) -> str:
    """A digest that matches a ``PermissionRequest`` to its ``PostToolUse``; the
    tool's input itself never leaves the hook."""
    encoded = json.dumps(tool_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def question_key(tool_input: object) -> str:
    """``tool_key`` for ``AskUserQuestion``: of its questions only, since the
    answers join its input by ``PostToolUse``."""
    return tool_key(tool_input.get("questions") if isinstance(tool_input, dict) else None)


def _agent(data: dict) -> str:
    agent = data.get("agent_id")
    return agent if isinstance(agent, str) and 0 < len(agent) <= 64 and all(
        c.isascii() and (c.isalnum() or c in "_-") for c in agent) else ""


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
    if event == "SubagentStop" and _agent(data):
        notice["agent_id"] = _agent(data)
    if event in ("PermissionRequest", "PostToolUse", "PostToolUseFailure") and tool_name:
        agent = _agent(data)
        if agent:
            notice["agent_id"] = agent
        notice["tool_name"] = tool_name
        notice["tool_key"] = question_key(tool_input) if tool_name == "AskUserQuestion" else tool_key(tool_input)
        if event == "PermissionRequest" and tool_name != "AskUserQuestion":
            from openswap.worker.attention import preview_key

            preview = channel_preview(tool_input)
            if preview is not None:
                notice["preview_key"] = preview_key(preview)
        if event != "PermissionRequest" and tool_name == "AskUserQuestion" and isinstance(tool_input, dict):
            from openswap.worker.attention import answer_positions

            response = data.get("tool_response") if isinstance(data.get("tool_response"), dict) else {}
            answers = tool_input.get("answers", response.get("answers"))
            positions = answer_positions(tool_input.get("questions"), answers)
            if positions:
                notice["answers"] = positions
        if event == "PermissionRequest" and isinstance(tool_input, dict):
            if tool_name == "AskUserQuestion":
                from openswap.worker.attention import questions_whole

                raw, questions = tool_input.get("questions"), clean_questions(tool_input.get("questions"))
                # Shown only with every question; answerable only with every option whole too.
                if isinstance(raw, list) and len(questions) == len(raw):
                    notice["questions"] = questions
                    if questions_whole(raw):
                        notice["answerable"] = True
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
            token = _REPLY_ATTRIBUTE.match(prompt)
            if token:
                notice["reply"] = token.group(1)
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
    """The Claude Code process running this hook: the first ancestor that is
    not a shell (the hook's own wrapper, and the shell Claude Code may run
    hooks through, sit in between).

    0 when it cannot be told: a shell's PID taken for Claude Code's would end
    the session's items as soon as the hook exits."""
    if os.environ.get("CLAUDE_CODE_SHELL_PREFIX"):
        return 0  # a wrapper of the owner's own may sit between the hook and Claude Code
    pid = os.getppid()
    try:
        for _ in range(3):
            out = subprocess.run(["/bin/ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True,
                                 timeout=0.3, env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout.split(None, 1)
            if len(out) != 2:
                return 0
            if Path(out[1].strip()).name.lstrip("-") not in _SHELLS:
                return pid if pid > 1 else 0
            pid = int(out[0])
            if pid <= 1:
                return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0
    return 0


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


def question_main(*, backup_root: Path | None = None) -> int:
    """``openswap attention question``: the synchronous ``PermissionRequest`` hook
    on ``AskUserQuestion``, installed only while replies are on.

    In a session with OpenSwap's channel it waits for the owner's answer from
    the phone or watch and gives it to Claude Code; it prints nothing (leaving
    the dialog to the owner) when the session has no channel, the question
    closes, the wait passes 10 minutes or the worker is gone. Never raises."""
    try:
        data = json.loads(_read_stdin())
        if not isinstance(data, dict) or data.get("tool_name") != "AskUserQuestion":
            return 0
        session, tool_input = data.get("session_id"), data.get("tool_input")
        if not isinstance(session, str) or not isinstance(tool_input, dict):
            return 0
        positions = _wait_for_answer(session, question_key(tool_input), _agent(data), backup_root)
        answers = _answers(tool_input.get("questions"), positions)
        if answers:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
                "behavior": "allow", "updatedInput": {**tool_input, "answers": answers}}}}))
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001 - a hook must never fail loudly
        pass
    return 0


def _wait_for_answer(session: str, key: str, agent: str, backup_root: Path | None) -> object:
    from openswap import paths
    from openswap.worker.ipc import IPC_VERSION, socket_path

    import time

    path = socket_path(backup_root or paths.get_backup_root())
    parent, deadline = os.getppid(), time.monotonic() + QUESTION_TIMEOUT_SECONDS - 5
    line = b""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1.0)
        try:
            client.connect(str(path))
            request = {"version": IPC_VERSION, "command": QUESTION_EVENT, "session_id": session, "tool_key": key}
            if agent:
                request["agent_id"] = agent
            client.sendall(json.dumps(request).encode() + b"\n")
            while b"\n" not in line and len(line) <= 65536:
                # Claude Code gave up on the hook (its wrapper was killed): stop waiting
                # rather than take an answer nobody will read.
                if os.getppid() != parent or time.monotonic() > deadline:
                    return None
                try:
                    chunk = client.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    break
                line += chunk
        except OSError:
            return None
    try:
        answer = json.loads(line)
    except (ValueError, RecursionError):
        return None
    return answer.get("answers") if isinstance(answer, dict) else None


def _answers(questions: object, positions: object) -> dict:
    """``AskUserQuestion``'s ``answers`` (question text to option label) for the
    (question, option) positions the owner chose, in the questions as the report
    showed them; {} unless every shown question gets one shown option."""
    from openswap.worker.attention import question_entries

    entries = question_entries(questions)
    # Every question must have been shown: one the report left out would go unanswered.
    if (not isinstance(questions, list) or len(entries) != len(questions) or not isinstance(positions, list)
            or len(positions) != len(entries)):
        return {}
    answers = {}
    for position in positions:
        if not (isinstance(position, list) and len(position) == 2 and all(type(n) is int for n in position)):
            return {}
        question, option = position
        if not 0 <= question < len(entries) or not 0 <= option < len(entries[question][2]):
            return {}
        _, raw_question, raw_labels = entries[question]
        if not isinstance(raw_question, str) or not isinstance(raw_labels[option], str) or raw_question in answers:
            return {}
        answers[raw_question] = raw_labels[option]
    return answers


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


# Every hook entry runs through this: a program that is missing, or present but
# unable to run the hook (an older build, an import failure), exits 0 and prints
# nothing, so no Claude Code or Codex event ever fails or is blocked over it.
HOOK_SHELL = "/bin/sh"
HOOK_SCRIPT = 'command -v "$0" >/dev/null 2>&1 || exit 0; "$0" "$@" 2>/dev/null || exit 0'


def _wrapped(*words: str) -> str:
    return " ".join([HOOK_SHELL, "-c", shlex.quote(HOOK_SCRIPT), *(shlex.quote(w) for w in words)])


def hook_command(event: str, executable: str) -> str:
    return _wrapped(executable, "attention", event)


def _hook_words(command: object) -> list[str]:
    """The command a hook entry runs, without the wrapper (if any)."""
    if not isinstance(command, str):
        return []
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    return words[3:] if len(words) >= 4 and words[:2] == [HOOK_SHELL, "-c"] else words


def _is_openswap(word: str) -> bool:
    from openswap.statusline import _is_app_executable

    return Path(word).name == "openswap" or _is_app_executable(word)


def is_our_hook_command(command: object) -> bool:
    """Whether ``command`` runs ``<openswap> attention …`` for any openswap and
    any event (a newer build's included), wrapped or not."""
    words = _hook_words(command)
    return len(words) >= 3 and words[1] == "attention" and _is_openswap(words[0])


# --- where OpenSwap put hooks ------------------------------------------------------

# Every file OpenSwap ever wrote hooks into, by the path it wrote: removal covers
# these whether or not their home is still found, and no other file.
HOOKS_RECORD = Path("worker") / "attention-hooks.json"


def _identity(path: Path) -> list[int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [info.st_dev, info.st_ino]


def _record(backup_root: Path) -> dict[str, dict]:
    """``{path: {"created": bool, "identity": [dev, inode]}}``: whether OpenSwap
    created the file at its latest install, and the file as OpenSwap last
    wrote it. Raises ConfigError when it cannot be read."""
    path = Path(backup_root) / HOOKS_RECORD
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ConfigError(f"OpenSwap's record of where it put session hooks ({path}) is unreadable ({exc})") from exc
    if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
        raise ConfigError(f"OpenSwap's record of where it put session hooks ({path}) is unreadable")
    return data


def _save_record(backup_root: Path, record: dict) -> None:
    path = Path(backup_root) / HOOKS_RECORD
    if not record:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_text(path, json.dumps(record, indent=1, sort_keys=True) + "\n")


def _write_hooks_file(backup_root: Path | None, path: Path, document: dict) -> None:
    """Write ``document`` to ``path`` (through a link), recorded before the write
    so a removal finds it whatever happens next."""
    if backup_root is None:
        _write_text(path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        return
    record = _record(backup_root)
    key = str(path)
    entry = record.get(key, {})
    # Still the file OpenSwap created (nobody replaced it since), or created now.
    created = not os.path.lexists(path) or (entry.get("created") is True
                                            and entry.get("identity") == _identity(path))
    # Until the write lands, the file is as OpenSwap last left it (or absent).
    record[key] = {"created": created, "identity": entry.get("identity") if created else None}
    _save_record(backup_root, record)
    _write_text(path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    identity, target = _identity(path), os.path.realpath(path)
    for other, entry in record.items():  # the same file reached through a link: OpenSwap's own write
        if other == key or os.path.realpath(other) == target:
            entry["identity"] = identity
    _save_record(backup_root, record)


def _read_document(path: Path) -> dict | None:
    """A hooks file's JSON object; None when it does not exist. ConfigError when
    it cannot be read or is not an object."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"could not read {path}: {exc}; refusing to overwrite it unread") from exc
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ConfigError(f"{path} does not parse ({exc}); refusing to rewrite it") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} is not a JSON object; refusing to rewrite it")
    return data


def _strip_file(path: Path, strip, entry: dict | None) -> bool:
    """Take OpenSwap's hooks out of one file with ``strip(document)``. The file
    goes only when OpenSwap created it at its latest install, nobody replaced it
    since, it is not a link and nothing else is left in it; otherwise what is
    left is written through any link. Returns whether it changed; raises
    ConfigError when it cannot be read or rewritten as it is."""
    document = _read_document(path)
    if document is None:
        return False
    updated = strip(document)
    if updated == document:
        return False
    if (not updated or updated == {"hooks": {}}) and entry is not None and entry.get("created") is True \
            and entry.get("identity") == _identity(path) and not path.is_symlink():
        path.unlink()
    else:
        _write_text(path, json.dumps(updated, indent=2, ensure_ascii=False) + "\n")
    return True


def _remove_recorded(backup_root: Path, strip_for, problems: list) -> bool:
    """Take OpenSwap's hooks out of every file it recorded. ``strip_for(path)``
    returns the function that strips one file, or None to leave it for now. A
    file leaves the record once none of OpenSwap's hooks are left in it; one
    that cannot be changed is named in ``problems``."""
    try:
        record = _record(backup_root)
    except ConfigError as exc:
        problems.append(str(exc))
        return False
    changed = False
    for key, entry in sorted(record.items()):
        path = Path(key)
        strip = strip_for(path)
        if strip is None:
            continue
        try:
            changed = _strip_file(path, strip, entry) or changed
            if path.exists() and _has_ours_in(path):
                continue  # left in place on purpose; ``strip`` said why
        except (ConfigError, OSError) as exc:
            problems.append(f"{path}: {exc}")
            continue
        del record[key]
    try:
        _save_record(backup_root, record)
    except OSError as exc:
        problems.append(f"could not update OpenSwap's record of its session hooks: {exc}")
    return changed


def _has_ours_in(path: Path) -> bool:
    document = _read_document(path) or {}
    hooks = document.get("hooks")
    return isinstance(hooks, dict) and any(
        is_our_hook_command(h.get("command")) or _codex_launcher_word(h.get("command"))
        for groups in hooks.values() if isinstance(groups, list)
        for group in groups if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for h in group["hooks"] if isinstance(h, dict))


def recorded_programs(backup_root: Path) -> list[str]:
    """The programs OpenSwap's recorded hooks run that no longer exist, for Settings."""
    import shutil

    missing = []
    try:
        record = _record(backup_root)
    except ConfigError:
        return []
    for key in record:
        try:
            document = _read_document(Path(key)) or {}
        except ConfigError:
            continue
        hooks = document.get("hooks") if isinstance(document.get("hooks"), dict) else {}
        for groups in hooks.values():
            for group in groups if isinstance(groups, list) else []:
                handlers = group.get("hooks") if isinstance(group, dict) else None
                for handler in handlers if isinstance(handlers, list) else []:
                    if not isinstance(handler, dict):
                        continue
                    words = _hook_words(handler.get("command"))
                    ours = is_our_hook_command(handler.get("command")) or _codex_launcher_word(
                        handler.get("command"))
                    if ours and words and shutil.which(words[0]) is None and words[0] not in missing:
                        missing.append(words[0])
    return missing


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


def _settings_without_ours(settings: dict) -> dict:
    """``settings`` with OpenSwap's hooks taken out of every event, a newer
    build's events too. Raises ConfigError for a ``hooks`` value that is not
    an object."""
    hooks = settings.get("hooks")
    if hooks is None:
        return settings
    if not isinstance(hooks, dict):
        raise ConfigError("Claude Code's hooks setting is not in the expected form; refusing to rewrite it")
    out = {}
    for event, groups in hooks.items():
        kept = _without_ours(groups) if isinstance(groups, list) else groups
        if kept or not groups:
            out[event] = kept
    if out == hooks:
        return settings
    result = dict(settings)
    if out:
        result["hooks"] = out
    else:
        result.pop("hooks")
    return result


def _with_hooks(settings: dict, executable: str, *, question: bool = False) -> dict:
    """``settings`` with OpenSwap's hooks for ``executable``, and with
    ``question`` the synchronous question hook too. Every other hook is kept
    as written. Raises ConfigError for a ``hooks`` value this would have to overwrite."""
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(hooks.get(e, []), list) for e in HOOK_EVENTS):
        raise ConfigError("Claude Code's hooks setting is not in the expected form; refusing to rewrite it")
    hooks = dict(_settings_without_ours(settings).get("hooks", {}))
    for event, matcher in HOOK_EVENTS.items():
        groups = list(hooks.get(event, []))
        groups.append({"matcher": matcher, "hooks": [{
            "type": "command", "command": hook_command(event, executable),
            "async": True, "timeout": HOOK_TIMEOUT_SECONDS,
        }]})
        if question and event == "PermissionRequest":
            groups.append({"matcher": "AskUserQuestion", "hooks": [{
                "type": "command", "command": hook_command(QUESTION_EVENT, executable),
                "timeout": QUESTION_TIMEOUT_SECONDS,
            }]})
        hooks[event] = groups
    return {**settings, "hooks": hooks}


def set_hooks(config_home: Path, executable: str | None, *, question: bool = False,
              backup_root: Path | None = None) -> bool:
    """Install OpenSwap's hooks for ``executable`` in one config home (with
    ``question``, the question hook too), recorded under ``backup_root``; or,
    with None, take them out of it (never deleting the file). Reads before
    writing: a settings file that cannot be read is left alone (ConfigError).
    Returns whether the file changed."""
    path = Path(config_home) / "settings.json"
    if executable is None:
        return _strip_file(path, _settings_without_ours, None)
    settings = _read_document(path) or {}
    updated = _with_hooks(settings, executable, question=question)
    if updated == settings and (backup_root is None or str(path) in _record(backup_root)):
        return False
    _write_hooks_file(backup_root, path, updated)
    return True


def only_ours(path: Path) -> bool:
    """Whether a settings file holds nothing but OpenSwap's hooks, so sharing
    may put ``~/.claude``'s in its place (it has OpenSwap's hooks too)."""
    try:
        document = _read_document(path)
        if document is None or path.is_symlink():
            return False
        rest = _settings_without_ours(document)
        return rest != document and not rest  # OpenSwap's hooks, and nothing else
    except ConfigError:
        return False


def profile_hooks(session_dir: Path, backup_root: Path) -> None:
    """After an account profile's sharing changed: while notices are on, its
    sessions get OpenSwap's hooks (through ``~/.claude``'s settings when shared,
    else in a settings file of its own). Never raises; the worker's next pass
    catches up on anything left."""
    if os.name != "posix":
        return
    try:
        from openswap.settings import load_attention_settings

        root = Path(backup_root)
        if not load_attention_settings(root).notices:
            return
        with _switch_lock(root, timeout=1):
            switches = load_attention_settings(root, strict=True)
            if switches.notices:
                set_hooks(session_dir, program(), question=switches.replies, backup_root=root)
    except Exception as exc:  # noqa: BLE001 - never in the way of starting a session
        logger.debug("Session hooks for %s left to the worker: %s", session_dir, exc)


# --- Codex hooks.json ----------------------------------------------------------

# Codex hook event -> (timeout, async). Codex runs SessionEnd synchronously
# whatever ``async`` says, and warns at every start when it says true;
# Interrupt allows at most 3 s.
CODEX_HOOK_EVENTS = {
    "PermissionRequest": (10, True), "PostToolUse": (10, True), "Stop": (10, True),
    "UserPromptSubmit": (10, True), "Interrupt": (3, True), "SessionEnd": (3, False),
}
CODEX_LAUNCHER = Path("bin") / "codex-hook"
# Per Codex home: what ``features.hooks`` was before OpenSwap turned it on, and
# the other hooks there were then.
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
    return {"type": "command", "command": _wrapped(str(launcher), event), "timeout": timeout, "async": run_async}


def _codex_label(event: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()


def codex_hook_hash(event: str, entry: dict) -> str:
    """The hash Codex records as ``trusted_hash`` once the owner trusts ``entry``
    (no matcher), as Codex 0.166 computes it (``async`` defaults to false)."""
    identity = json.dumps({"event_name": _codex_label(event), "hooks": [{"async": False, **entry}]},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def codex_hooks_on(config: dict) -> bool:
    """Whether Codex runs hooks under ``config`` (a parsed config.toml):
    ``features.hooks``, else the legacy ``features.codex_hooks``, else Codex's
    default, which is on (measured: Codex 0.166, where ``hooks`` wins over
    ``codex_hooks`` whatever their order)."""
    features = config.get("features") if isinstance(config.get("features"), dict) else {}
    for key in ("hooks", "codex_hooks"):
        if isinstance(features.get(key), bool):
            return features[key]
    return True


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
    _write_text(path, text, mode=0o755)


def _refuse_managed(codex_home: Path) -> None:
    resolved = os.path.realpath(codex_home)
    if any(resolved == root or resolved.startswith(root + "/") for root in ("/etc", "/private/etc")):
        raise ConfigError(f"{codex_home} holds managed Codex configuration; OpenSwap never writes it")


def _read_hooks_file(path: Path) -> dict:
    data = _read_document(path) or {}
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(groups, list) for groups in hooks.values()):
        raise ConfigError(f"{path} is not in the form Codex reads; refusing to rewrite it")
    return data


def _codex_launcher_word(command: object) -> bool:
    words = _hook_words(command)
    return len(words) >= 2 and words[0].endswith("/" + str(CODEX_LAUNCHER))


def _is_ours_in(handler: object, launcher: Path) -> bool:
    """Whether a Codex hook entry runs OpenSwap's launcher, for any event."""
    if not isinstance(handler, dict):
        return False
    words = _hook_words(handler.get("command"))
    return len(words) >= 2 and words[0] == str(launcher)


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


def _codex_others(data: dict, launcher: Path) -> list[str]:
    """Every hook in a ``hooks.json`` that is not OpenSwap's, as comparable strings."""
    out = []
    for event, groups in data.get("hooks", {}).items():
        for group in groups if isinstance(groups, list) else []:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            matcher = group.get("matcher") if isinstance(group, dict) else None
            for handler in handlers if isinstance(handlers, list) else [group]:
                if not _is_ours_in(handler, launcher):
                    out.append(json.dumps([event, matcher, handler], sort_keys=True, ensure_ascii=False))
    return sorted(out)


def _describe(other: str) -> str:
    event, _, handler = json.loads(other)
    command = handler.get("command") if isinstance(handler, dict) else None
    return f"{event}: {command}" if isinstance(command, str) else event


def _trust_state(config: dict) -> dict:
    hooks = config.get("hooks") if isinstance(config.get("hooks"), dict) else {}
    state = hooks.get("state")
    return state if isinstance(state, dict) else {}


def _trust_source(home: Path) -> str:
    """The path Codex names ``hooks.json`` by in its trust records: the Codex
    home resolved, the file itself not (measured: Codex 0.166 ``hooks/list``)."""
    return os.path.join(os.path.realpath(home), "hooks.json")


def _moves(groups: list, launcher: Path) -> list[tuple[tuple[int, int], tuple[int, int], object, object]]:
    """Each other hook whose (group, entry) position changes once OpenSwap's
    entries are taken out of ``groups``: (old, new, entry, its group)."""
    moves, new_group = [], 0
    for g, group in enumerate(groups):
        handlers = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(handlers, list):
            if g != new_group:
                moves.append(((g, 0), (new_group, 0), group, group))
            new_group += 1
            continue
        kept = [h for h in handlers if not _is_ours_in(h, launcher)]
        if not kept:
            continue
        index = 0
        for h, handler in enumerate(handlers):
            if _is_ours_in(handler, launcher):
                continue
            if (g, h) != (new_group, index):
                moves.append(((g, h), (new_group, index), handler, group))
            index += 1
        new_group += 1
    return moves


def _codex_without_ours(data: dict, launcher: Path, config: dict, home: Path, *, owner: bool,
                        notes: list, problems: list) -> dict:
    """``data`` (a ``hooks.json``) without OpenSwap's entries. Codex keys its
    trust in a hook by position, so taking an entry out can move the owner's
    hooks after it onto another hook's record. An event where that would let
    a hook gain trust (a record trusting its very hash, or one OpenSwap cannot
    tell, under a matcher) keeps OpenSwap's entry (named in ``problems``); one
    where it changes how Codex treats a hook (asks again, turned off, or no
    longer turned off) is changed only for the owner, who is told which hooks
    and how (``notes``); the worker, with no one to tell, leaves it."""
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or any(not isinstance(groups, list) for groups in hooks.values()):
        raise ConfigError("hooks.json is not in the form Codex reads; refusing to rewrite it")
    state, source = _trust_state(config), _trust_source(home)
    out = {}
    for event, groups in hooks.items():
        kept = _codex_groups(groups, event, None, launcher)
        if kept == groups:
            out[event] = groups
            continue
        label, changed, gains, how = _codex_label(event), [], False, []
        for (og, oh), (ng, nh), handler, group in _moves(groups, launcher):
            before, after = state.get(f"{source}:{label}:{og}:{oh}"), state.get(f"{source}:{label}:{ng}:{nh}")
            if before == after:
                continue
            after = after if isinstance(after, dict) else {}
            unsure = isinstance(group, dict) and group.get("matcher") is not None
            if after.get("trusted_hash") and after.get("enabled") is not False and (
                    unsure or not isinstance(handler, dict)
                    or after["trusted_hash"] == codex_hook_hash(event, handler)):
                gains = True
            command = handler.get("command") if isinstance(handler, dict) else None
            changed.append(f"{event}: {command}" if isinstance(command, str) else event)
            how.append("turn your hook off" if after.get("enabled") is False else "ask you to review your hook again")
        if gains or (changed and not owner):
            problems.append(f"Codex: OpenSwap's {event} hook was left in hooks.json: taking it out would change "
                            f"how Codex treats your own hooks after it ({'; '.join(changed)})")
            out[event] = groups
            continue
        notes.extend(f"Codex will {verb} ({c})" for verb, c in zip(how, changed))
        if kept:
            out[event] = kept
    return {**data, "hooks": out}


def _feature_text(text: str, value: bool | None, *, drop_table: bool = False) -> str:
    """``text`` (a config.toml) with ``[features] hooks`` set to ``value`` (None:
    removed, and with ``drop_table`` the ``[features]`` table too once nothing
    else is in it), every other line kept as written (a comment after the
    ``hooks`` line's value too). Raises ConfigError unless the result parses to
    the same document with only that changed."""
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
    line = None if value is None else f"hooks = {'true' if value else 'false'}"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    header = next((n for n, current in enumerate(lines)
                   if re.fullmatch(r"\s*\[\s*features\s*\]\s*(#.*)?\s*", current)), None)
    candidates = []  # (text, what it must parse to)
    if header is None:
        if line is not None:
            candidates.append((lines + ["\n" if lines else "", "[features]\n", line + "\n"], expected))
    else:
        table_end = next((n for n in range(header + 1, len(lines)) if lines[n].lstrip().startswith("[")),
                         len(lines))
        found = [n for n in range(header + 1, table_end) if re.match(r"\s*hooks\s*=", lines[n])]
        for n in found:
            if drop_table and not features:
                # The table as OpenSwap added it: a blank line, the header, the key.
                first = header - 1 if header and not lines[header - 1].strip() else header
                candidates.append((lines[:first] + lines[header + 1:n] + lines[n + 1:], without_table))
            comment = re.search(r"\s#[^\"']*$", lines[n].rstrip("\n"))
            replaced = [line + (comment.group(0) if comment else "") + "\n"] if line else []
            candidates.append((lines[:n] + replaced + lines[n + 1:], expected))
            if comment:
                candidates.append((lines[:n] + ([line + "\n"] if line else []) + lines[n + 1:], expected))
        if not found and line is not None:
            candidates.append((lines[:header + 1] + [line + "\n"] + lines[header + 1:], expected))
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


def _parse_config(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Codex's config.toml does not parse ({exc}); refusing to rewrite it") from exc


def _write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Atomic, durable write through a symlink, like the status line's settings
    writes, keeping the file's permissions (``mode`` for a new file, else 0600)."""
    from openswap.fsutil import replace_with_retry

    target = Path(os.path.realpath(path)) if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        keep = os.stat(target).st_mode & 0o7777
    except FileNotFoundError:
        keep = 0o600 if mode is None else mode
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        os.write(fd, text.encode("utf-8"))
        os.fchmod(fd, keep if mode is None else mode)
        os.fsync(fd)
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
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_text(path, json.dumps(state, indent=1) + "\n")


def install_codex_hooks(codex_home: Path, backup_root: Path, *, owner: bool = False,
                        consent: bool = False) -> bool:
    """Install OpenSwap's hooks in one Codex home's ``hooks.json``. Codex's
    ``hooks`` feature is turned on only by the owner's own action (``owner``),
    and, while other hooks there would start running with it, only with the
    owner's OK (``consent``; see ``codex_consent_needed``). Codex not installed
    (no home): nothing to do. Returns whether anything changed."""
    home = Path(codex_home)
    _refuse_managed(home)
    if not home.is_dir():
        return False
    launcher = codex_launcher(backup_root)
    path = home / "hooks.json"
    data = _read_hooks_file(path)
    hooks = dict(data.get("hooks", {}))
    for event in CODEX_HOOK_EVENTS:
        hooks[event] = _codex_groups(hooks.get(event, []), event, launcher, launcher)
    updated = {**data, "hooks": {e: g for e, g in hooks.items() if g}}
    changed = False
    if updated != data or str(path) not in _record(backup_root):
        _write_hooks_file(backup_root, path, updated)
        changed = True
    if owner:
        changed = _enable_codex_feature(home, backup_root, _codex_others(updated, launcher), consent) or changed
    return changed


def _enable_codex_feature(home: Path, backup_root: Path, others: list[str], consent: bool) -> bool:
    config, text = _codex_config(home)
    parsed = _parse_config(text)
    if codex_hooks_on(parsed) or (others and not consent):
        return False
    features = parsed.get("features") if isinstance(parsed.get("features"), dict) else {}
    state = _codex_state(backup_root)
    # Off now, so whatever was recorded before no longer says what to put back.
    state[os.path.realpath(home)] = {"hooks": features.get("hooks"), "table": "features" not in parsed,
                                     "file": config.exists(), "others": others}
    _save_codex_state(backup_root, state)
    _write_text(config, _feature_text(text, True))
    return True


def _restore_codex_feature(backup_root: Path, problems: list) -> bool:
    """In every Codex home where OpenSwap turned Codex's ``hooks`` feature on,
    put it back as it was then, unless the owner changed it since or hooks
    other than those there then were added (it stays on, and the record stays
    for a later removal)."""
    try:
        homes = list(_codex_state(backup_root))
    except ConfigError as exc:
        problems.append(f"Codex: {exc}")
        return False
    changed = False
    for key in homes:
        changed = _restore_one_codex_feature(Path(key), backup_root, problems) or changed
    return changed


def _restore_one_codex_feature(home: Path, backup_root: Path, problems: list) -> bool:
    try:
        state = _codex_state(backup_root)
        key = str(home)
        saved = state.get(key)
        if saved is None:
            return False
        if not home.is_dir():
            del state[key]  # that Codex home is gone, and its setting with it
            _save_codex_state(backup_root, state)
            return False
        config, text = _codex_config(home)
        parsed = _parse_config(text)
        features = parsed.get("features") if isinstance(parsed.get("features"), dict) else {}
        if not isinstance(saved, dict) or features.get("hooks") is not True:
            del state[key]  # changed by the owner since: theirs now
            _save_codex_state(backup_root, state)
            return False
        try:
            others = _codex_others(_read_hooks_file(home / "hooks.json"), codex_launcher(backup_root))
        except ConfigError as exc:
            logger.info("Codex's hooks feature left on: %s", exc)
            return False  # cannot tell whether other hooks were added
        if not set(others) <= set(saved.get("others") or []):
            return False
        before = saved.get("hooks")
        rewritten = _feature_text(text, before if isinstance(before, bool) else None,
                                  drop_table=saved.get("table") is True)
        if not rewritten.strip() and saved.get("file") is False:
            config.unlink()
        else:
            _write_text(config, rewritten)
        del state[key]
        _save_codex_state(backup_root, state)
        return True
    except (ConfigError, OSError) as exc:
        problems.append(f"Codex: {exc}")
        return False


def codex_consent_needed(backup_root: Path) -> list[str]:
    """The owner's other Codex hooks that would start running if OpenSwap
    turned on Codex's ``hooks`` feature now; [] when nothing needs asking."""
    from openswap.codex.auth import codex_home

    home = Path(codex_home())
    try:
        if not home.is_dir():
            return []
        if codex_hooks_on(_parse_config(_codex_config(home)[1])):
            return []
        return [_describe(o) for o in _codex_others(_read_hooks_file(home / "hooks.json"),
                                                     codex_launcher(backup_root))]
    except (ConfigError, OSError):
        return []


def enable_codex_hooks(backup_root: Path) -> list[str]:
    """The owner agreed: turn on Codex's ``hooks`` feature (while notices are
    on), knowing their other hooks start running too."""
    from openswap.codex.auth import codex_home
    from openswap.settings import load_attention_settings

    with _switch_lock(backup_root):
        if not load_attention_settings(backup_root, strict=True).notices:
            return []
        try:
            install_codex_hooks(codex_home(), backup_root, owner=True, consent=True)
        except (ConfigError, OSError) as exc:
            return [f"Codex: {exc}"]
    return []


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
        if data.get("allow_managed_hooks_only") is True or not codex_hooks_on(data):
            return True
    return False


def codex_hooks_status(codex_home: Path, backup_root: Path) -> str | None:
    """Codex's actual state for OpenSwap's hooks in one Codex home, from Codex's
    own files: ``trusted``, ``review`` (Codex shows "Hooks need review" at its
    next start), ``review_others`` (the same, with other hooks there also
    waiting for review), ``disabled`` (turned off in Codex's ``/hooks``),
    ``off`` (Codex's hooks feature is off, the legacy ``codex_hooks`` key
    included), ``blocked`` (managed configuration), ``missing`` (not in
    ``hooks.json``, or the launcher it runs is gone) or ``unknown``. None
    without Codex."""
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
    if not codex_hooks_on(config):
        return "off"
    launcher = codex_launcher(backup_root)
    if not os.access(launcher, os.X_OK):
        return "missing"
    hooks_state, source = _trust_state(config), _trust_source(home)
    statuses, others_untrusted = set(), False
    for event, groups in data.get("hooks", {}).items():
        for g, group in enumerate(groups):
            handlers = group.get("hooks") if isinstance(group, dict) else None
            for h, handler in enumerate(handlers if isinstance(handlers, list) else []):
                if _is_ours_in(handler, launcher) or not isinstance(handler, dict):
                    continue
                state = hooks_state.get(f"{source}:{_codex_label(event)}:{g}:{h}")
                state = state if isinstance(state, dict) else {}
                if state.get("enabled") is not False and (
                        isinstance(group, dict) and group.get("matcher") is not None
                        or state.get("trusted_hash") != codex_hook_hash(event, handler)):
                    others_untrusted = True  # or OpenSwap cannot tell: never advise trusting all
    for event in CODEX_HOOK_EVENTS:
        entry = codex_hook_entry(event, launcher)
        place = next(((g, h) for g, group in enumerate(data.get("hooks", {}).get(event, []))
                      if isinstance(group, dict) and isinstance(group.get("hooks"), list)
                      for h, handler in enumerate(group["hooks"]) if handler == entry), None)
        if place is None:
            return "missing"
        state = hooks_state.get(f"{source}:{_codex_label(event)}:{place[0]}:{place[1]}")
        state = state if isinstance(state, dict) else {}
        if state.get("enabled") is False:
            statuses.add("disabled")
        elif state.get("trusted_hash") != codex_hook_hash(event, entry):
            statuses.add("review")
    if "disabled" in statuses:
        return "disabled"
    if statuses:
        return "review_others" if others_untrusted else "review"
    return "trusted"


# --- turning it on and off -----------------------------------------------------


def apply(backup_root: Path, enabled: bool, replies: bool = False, *, owner: bool = False,
          problems: list | None = None, notes: list | None = None) -> bool:
    """Install the hooks in every managed config home (the question hook only
    while ``replies`` too) and in the Codex home, or take them out of every
    file OpenSwap ever put them in. Every place is tried; returns whether
    anything changed. What could not be changed goes into ``problems``, and
    what the owner should know into ``notes``, in words. ``owner`` is True for
    the owner's own action: only then is Codex's hooks feature turned on, or a
    Codex removal that changes how Codex treats other hooks made."""
    from openswap.codex.auth import codex_home

    problems = [] if problems is None else problems
    notes = [] if notes is None else notes
    changed = False
    if enabled:
        executable = program()
        homes_problems: list = []
        for home in config_homes(backup_root, homes_problems):
            try:
                changed = set_hooks(home, executable, question=replies, backup_root=backup_root) or changed
            except (ConfigError, OSError) as exc:
                problems.append(f"{home}: {exc}")
        problems.extend(homes_problems)
        try:
            _write_launcher(backup_root, executable)
            changed = install_codex_hooks(codex_home(), backup_root, owner=owner) or changed
        except (ConfigError, OSError) as exc:
            problems.append(f"Codex: {exc}")
        return changed
    launcher = codex_launcher(backup_root)

    def strip_for(path: Path):
        if path.name != "hooks.json":
            return _settings_without_ours
        try:
            config = _parse_config(_codex_config(path.parent)[1])
        except (ConfigError, OSError) as exc:
            problems.append(f"Codex: {exc}")
            return None
        return lambda data: _codex_without_ours(data, launcher, config, path.parent, owner=owner,
                                                notes=notes, problems=problems)

    changed = _remove_recorded(backup_root, strip_for, problems)
    changed = _restore_codex_feature(backup_root, problems) or changed
    try:
        if not any(Path(p).name == "hooks.json" for p in _record(backup_root)):
            launcher.unlink(missing_ok=True)  # nothing left that runs it
    except (ConfigError, OSError):
        pass
    return changed


@contextmanager
def _switch_lock(backup_root: Path, *, timeout: float = 10):
    """Held while the notices switch is read or written together with the hooks
    it implies, so a worker starting while the owner flips it cannot undo the flip."""
    from openswap.locking import FileLock

    path = Path(backup_root) / "worker" / "attention.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = FileLock(path, timeout=timeout)
    if not lock.acquire():
        raise ConfigError("another change to session notices is still running; try again")
    try:
        yield
    finally:
        lock.release()


def set_notices(backup_root: Path, enabled: bool) -> list[str]:
    """Settings › Remote › "Tell me when a session needs me": the switch, then
    the hooks. Returns what the owner should hear: what could not be changed,
    and which of their own Codex hooks Codex will ask about again.

    Turning it on from a translocated or mounted copy of the app raises
    ClaudeSwitchError (the hooks would name a path that disappears).
    """
    from openswap.settings import write_attention_settings

    if enabled:
        _refuse_temporary_copy()
    with _switch_lock(backup_root):
        switches = write_attention_settings(backup_root, notices=enabled)
        problems: list = []
        notes: list = []
        apply(backup_root, enabled, switches.replies, owner=True, problems=problems, notes=notes)
        return problems + notes


def set_replies(backup_root: Path, enabled: bool) -> list[str]:
    """Settings › Remote › "Allow replies from my phone and watch" (this Mac
    only): the switch, then the question hook in every managed config home."""
    from openswap import bundle
    from openswap.settings import load_attention_settings, write_attention_settings

    with _switch_lock(backup_root):
        if load_attention_settings(backup_root).notices and bundle.runs_from_temporary_location():
            # Rewriting the hooks would name a path that disappears. Turning
            # replies off needs no hook change: the worker refuses them, and the
            # question hook then exits at once until the next worker start.
            if enabled:
                from openswap.exceptions import ClaudeSwitchError
                from openswap.launch_agent import MOVE_TO_APPLICATIONS

                raise ClaudeSwitchError(MOVE_TO_APPLICATIONS)
            write_attention_settings(backup_root, replies=False)
            return []
        switches = write_attention_settings(backup_root, replies=enabled)
        problems: list = []
        notes: list = []
        apply(backup_root, switches.notices, switches.replies, owner=True, problems=problems, notes=notes)
        return problems + notes


def _refuse_temporary_copy() -> None:
    from openswap import bundle
    from openswap.exceptions import ClaudeSwitchError

    if bundle.runs_from_temporary_location():
        from openswap.launch_agent import MOVE_TO_APPLICATIONS

        raise ClaudeSwitchError(MOVE_TO_APPLICATIONS)


def remove(backup_root: Path, notes: list | None = None) -> bool:
    """Uninstall: take OpenSwap's hooks out of every file it ever put them in.
    Raises ConfigError naming what could not be changed (hooks may remain);
    what the owner should know goes into ``notes``."""
    problems: list = []
    changed = apply(backup_root, False, owner=True, problems=problems, notes=notes)
    if problems:
        raise ConfigError("; ".join(problems))
    return changed


def reconcile(backup_root: Path, *, installs_only: bool = False) -> list[str]:
    """For the worker: while notices are on, install the hooks in config homes
    created since and repoint the Codex launcher at this app; while they are
    off (unless ``installs_only``), take out hooks a failed or interrupted
    removal left. Never turns Codex's hooks feature on. An unreadable switch
    changes nothing (ConfigError): it is not an "off"."""
    from openswap.settings import load_attention_settings

    with _switch_lock(backup_root):
        switches = load_attention_settings(backup_root, strict=True)
        if installs_only and not switches.notices:
            return []
        problems: list = []
        apply(backup_root, switches.notices, switches.replies, problems=problems)
        return problems


# --- openswap claude ------------------------------------------------------------

CHANNEL_CONFIG = Path("worker") / "channel-mcp.json"
CHANNEL_SERVER = "server:openswap"
MANAGED_SETTINGS = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
CHANNELS_OFF = ("openswap claude: your organization has turned Claude Code channels off, so replies from your "
                "phone and watch cannot reach this session (notices still work). An Owner can turn them on in "
                "claude.ai › Organization settings › Claude Code › Channels.")


def channel_config(backup_root: Path) -> Path:
    """The ``--mcp-config`` file naming OpenSwap's channel, written for this
    copy of OpenSwap. Nothing is installed into any config home: the channel
    exists only in sessions ``openswap claude`` starts."""
    from openswap.statusline import _write_json

    path = Path(backup_root) / CHANNEL_CONFIG
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    wanted = {"mcpServers": {"openswap": {"command": program(), "args": ["attention", CHANNEL_COMMAND]}}}
    try:
        if json.loads(path.read_text(encoding="utf-8")) == wanted:
            return path
    except (OSError, ValueError):
        pass
    _write_json(path, wanted)
    return path


def channels_blocked() -> bool:
    """Whether managed settings on this Mac turn channels off."""
    try:
        return json.loads(MANAGED_SETTINGS.read_text(encoding="utf-8")).get("channelsEnabled") is False
    except (OSError, ValueError, AttributeError):
        return False


def launch_claude(args: list[str], *, backup_root: Path | None = None, which=None, execv=os.execv) -> int:
    """``openswap claude [args]``: Claude Code with OpenSwap's channel, so the
    session can be answered from the phone and watch. While channels are a
    research preview the channel is named on the development flag, and Claude
    Code asks to confirm it at each start."""
    import shutil

    from openswap import bundle, paths

    claude = (which or shutil.which)("claude")
    if claude is None:
        print("openswap claude: Claude Code (claude) is not on your PATH.", file=sys.stderr)
        return 127
    if bundle.runs_from_temporary_location():
        from openswap.launch_agent import MOVE_TO_APPLICATIONS

        print(f"openswap claude: {MOVE_TO_APPLICATIONS}", file=sys.stderr)
        return 1
    try:
        config = channel_config(backup_root or paths.get_backup_root())
    except (OSError, ConfigError) as exc:
        print(f"openswap claude: could not write the channel's settings: {exc}", file=sys.stderr)
        return 1
    if channels_blocked():
        print(CHANNELS_OFF, file=sys.stderr)
    # One value each (the "=" form): both options take several values, and a
    # prompt or subcommand after them would otherwise be read as another one.
    try:
        execv(claude, [claude, f"--mcp-config={config}",
                       f"--dangerously-load-development-channels={CHANNEL_SERVER}", *args])
    except OSError as exc:
        print(f"openswap claude: could not start {claude}: {exc}", file=sys.stderr)
        return 126
    return 0
