"""Session attention: which of this Mac's sessions need the owner (plan 025).

Claude Code's and Codex's hooks hand *notices* to the worker over its private
socket (``openswap attention``). This module turns them into
*items*, one per occurrence of a session needing the owner, and builds the
optional ``attention`` report the worker sends to the control service.

An item is recorded unpublished and published (reported) only once Claude
Code itself decides the owner seems away (its ``permission_prompt`` and
``idle_prompt`` notifications; ``permission_prompt`` fires for questions too).
Codex has no such notification: its approval is published after the same six
seconds, and its finished turn after Claude Code's 60-second idle wait, each
only if nothing on the thread has answered it by then. Every item gets a
fresh random ID that is never reused. Open items are kept in a private file
under the backup root so they survive a worker restart.

A "No" or Esc at the Mac fires no hook, so the worker also reads each
session's ``sessions/{pid}.json`` (``status`` and ``statusUpdatedAt``) in every
managed config home. Hooks are async and arrive out of order: each carries
the time it ran, which orders it against the others and against the session
files. A Claude item also ends with its Claude Code process, whose PID the
hook reports, which covers a child session that writes no session file.

Codex writes no session status. A Codex approval answered at the Mac shows in
Codex's own log database (``logs_*.sqlite``: an ``exec_approval`` or other
answer operation on the thread), and Esc fires its ``Interrupt`` hook. A
Codex item ends with the Codex process that ran its hooks (the shared
app-server, ``codex exec`` or the desktop app's), and after 24 hours at most.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from openswap.worker.protocol import (
    ACK_OUTCOMES, DETAIL_CAPS, MAX_ATTENTION_ACKS, MAX_ATTENTION_CLOSED, MAX_ATTENTION_ITEMS, MAX_BODY,
    MAX_FOLDER_LABEL, MAX_OPTION, MAX_OPTIONS, MAX_QUESTION, MAX_QUESTION_HEADER, MAX_QUESTIONS, MAX_SESSION_LABEL,
    ProtocolError, attention_id, attention_reply, timestamp,
)

logger = logging.getLogger(__name__)

STATE_FILE = Path("worker") / "attention.json"
# What keeps notices or replies from working now, in words, for Settings.
STATUS_FILE = Path("worker") / "attention-status.json"
STATE_UNREADABLE = ("OpenSwap can't read its record of open session notices (worker/attention.json), so sessions "
                    "send nothing until it can.")
STATE_VERSION = 1
QUESTION_TOOL = "AskUserQuestion"
# An answer seen in a session file closes its item this much later, so an exit
# or /clear right after it (Esc, then quit) closes it as session_ended instead.
SESSION_END_GRACE_SECONDS = 1.5
# A hook's own time is used only within this of the worker's clock.
MAX_HOOK_AGE_SECONDS = 3600
# Claude Code sends ``permission_prompt`` this long after the dialog shows,
# and the dialog's PermissionRequest runs as it shows (measured: 6.0 s, also
# for each of several subagent prompts waiting at once). A notice is matched
# to the recorded call whose PermissionRequest ran this long before it.
PROMPT_NOTICE_SECONDS = 6.0
PROMPT_MATCH_SECONDS = 2.0
# Session statuses that mean no dialog is on screen; a missing or unknown
# status is never read as an answer.
NOT_WAITING = frozenset({"busy", "idle"})
CODEX_ITEM_SECONDS = 24 * 3600
# Claude Code's waits before its own notifications, applied to Codex's items.
CODEX_PROMPT_SECONDS = PROMPT_NOTICE_SECONDS
CODEX_FINISHED_SECONDS = 60.0
# The operations Codex logs when a person answers a prompt (any decision).
CODEX_ANSWER_OPERATIONS = ("exec_approval", "patch_approval", "resolve_elicitation", "user_input_answer",
                           "request_permissions_response")
# Closes kept for a report the service has not accepted yet (an offline Mac, or
# a backend without the extension); past this the oldest go unreported.
MAX_PENDING_CLOSES = 200
# Sessions whose last reply is kept for their next idle item.
MAX_LAST_MESSAGES = 200
# Room below MAX_BODY for the request's other fields.
REPORT_BUDGET = MAX_BODY - 4096
# Shown instead of the folder's name while details are off.
GENERIC_FOLDER_LABEL = "a session"

CLAUDE_EVENTS = frozenset({
    "PermissionRequest", "Notification", "Stop", "UserPromptSubmit", "PostToolUse",
    "PostToolUseFailure", "SubagentStop", "SessionEnd",
})
CODEX_EVENTS = frozenset({"PermissionRequest", "PostToolUse", "Stop", "UserPromptSubmit", "Interrupt",
                          "SessionEnd"})
MAX_ID = 200
MAX_PATH = 4096
MAX_PID = 2**31 - 1
# Session surfaces by Claude Code's ``entrypoint``.
SURFACES = {"cli": "Terminal", "claude-vscode": "VS Code", "claude-desktop": "Claude Desktop"}

# Replies (S2). A channel's permission request and its hook's PermissionRequest
# arrive this close together (measured: the request 0.1 s first); an unpaired
# request is dropped after REQUEST_SECONDS.
PAIR_SECONDS = 5.0
REQUEST_SECONDS = 10.0
# A dialog that closes this soon after the watch's verdict reached it was closed
# by that verdict; one that closed later (or earlier) was answered at the Mac.
CONFIRM_SECONDS = 2.0
# The question hook's own timeout is 600 s; its wait ends before that.
QUESTION_WAIT_SECONDS = 590.0
# A handled reply's outcome is acknowledged again until this long past its expiry
# (the service takes a first acknowledgement until 10 minutes past it).
HANDLED_KEEP_SECONDS = 660.0
REPLY_LIFE_SECONDS = 600.0
MAX_HANDLED = 500
MAX_CHANNELS = 64
MAX_WAITERS = 64
MAX_WAITERS_PER_PROCESS = 4
# After the service refused a batch of reply outcomes, each is tried alone this much later.
ACK_RETRY_SECONDS = 60.0
MAX_REQUESTS = 50
MAX_INCOMING = 100


class NoticeError(ValueError):
    """A notice that is not a well-formed hook or Codex report."""


LOG_EVERY_SECONDS = 600.0
_logged: dict[str, float] = {}


def log_limited(code: str, message: str, *args, level: int = logging.WARNING) -> None:
    """Log ``message`` at most once per ``LOG_EVERY_SECONDS`` for each ``code``:
    the attention paths retry every few seconds."""
    now = time.monotonic()
    if now - _logged.get(code, -math.inf) >= LOG_EVERY_SECONDS:
        _logged[code] = now
        logger.log(level, message, *args)


def clean_text(value: object, limit: int, *, multiline: bool = False) -> str:
    """``value`` as display text: control characters become spaces (newline, CR
    and tab survive when ``multiline``), trimmed and cut to ``limit`` code points.
    Empty when nothing is left."""
    if not isinstance(value, str):
        return ""
    keep = "\n\r\t" if multiline else ""
    cleaned = "".join(" " if unicodedata.category(c) == "Cc" and c not in keep else c for c in scrub(value))
    return cleaned.strip()[:limit].strip()


_SURROGATE = re.compile("[\ud800-\udfff]")


def scrub(value):
    """``value`` with every lone surrogate in its text (nested too) replaced by
    U+FFFD: such text cannot be encoded, so it would break a whole report."""
    if isinstance(value, str):
        return _SURROGATE.sub("\ufffd", value)
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {scrub(k): scrub(v) for k, v in value.items()}
    return value


def question_entries(value: object) -> list[tuple[dict, str, list[str]]]:
    """``AskUserQuestion``'s questions as the report shows them (the question,
    its header and its option labels, cut to the report's caps), each with the
    raw question text and raw option labels at the same positions."""
    if not isinstance(value, list):
        return []
    out = []
    for entry in value[:MAX_QUESTIONS]:
        if not isinstance(entry, dict):
            continue
        question = clean_text(entry.get("question"), MAX_QUESTION, multiline=True)
        if not question:
            continue
        options = entry.get("options") if isinstance(entry.get("options"), list) else []
        raw = [o.get("label") if isinstance(o, dict) else o for o in options]
        kept = [(clean_text(r, MAX_OPTION), r) for r in raw]
        kept = [(label, r) for label, r in kept if label][:MAX_OPTIONS]
        cleaned = {"question": question, "options": [label for label, _ in kept]}
        header = clean_text(entry.get("header"), MAX_QUESTION_HEADER)
        if header:
            cleaned["header"] = header
        out.append((cleaned, entry.get("question"), [r for _, r in kept]))
    return out


def questions_whole(value: object) -> bool:
    """Whether the report can show every question and every option of
    ``AskUserQuestion``'s ``questions`` whole: none dropped (past the caps, or a
    label that cleans to nothing) and none cut. Only such a question takes an answer."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_QUESTIONS:
        return False
    for entry in value:
        question = entry.get("question") if isinstance(entry, dict) else None
        options = entry.get("options") if isinstance(entry, dict) else None
        if (not isinstance(question, str) or not clean_text(question, MAX_QUESTION, multiline=True)
                or len(question.strip()) > MAX_QUESTION or not isinstance(options, list)
                or len(options) > MAX_OPTIONS):
            return False
        for option in options:
            label = option.get("label") if isinstance(option, dict) else option
            if not isinstance(label, str) or not clean_text(label, MAX_OPTION) or len(label.strip()) > MAX_OPTION:
                return False
    return True


def clean_questions(value: object) -> list[dict]:
    """``AskUserQuestion``'s questions, cut to the report's caps: the question,
    its header and its option labels only."""
    return [cleaned for cleaned, _, _ in question_entries(value)]


def answer_positions(questions: object, answers: object) -> list[list[int]]:
    """The (question, option) positions, in the report's questions, of the
    answers ``AskUserQuestion`` received; [] when any answer is not one of the
    shown options (typed text, or an option past the caps)."""
    if not isinstance(answers, dict):
        return []
    positions = []
    for index, (_, raw_question, raw_labels) in enumerate(question_entries(questions)):
        answer = answers.get(raw_question) if isinstance(raw_question, str) else None
        if answer not in raw_labels:
            return []
        positions.append([index, raw_labels.index(answer)])
    return positions


def _bounded_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID or any(ord(c) < 32 for c in value):
        raise NoticeError("invalid_notice")
    return value


def is_entrypoint(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 64 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                                                                   for c in value)


def is_agent_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 64 and all(
        c.isascii() and (c.isalnum() or c in "_-") for c in value)


def _optional_text(data: dict, name: str, limit: int, *, multiline: bool = False) -> str:
    value = data.get(name, "")
    if not isinstance(value, str) or len(value) > limit or clean_text(value, limit, multiline=multiline) != value:
        raise NoticeError("invalid_notice")
    return value


_CLAUDE_FIELDS = {"provider", "event", "session_id", "cwd", "at", "pid", "entrypoint", "agent_id", "tool_name",
                  "tool_key", "description", "questions", "notification_type", "last_message", "prompt_kind",
                  "preview_key", "answers", "reply", "answerable"}
_CODEX_FIELDS = {"provider", "event", "session_id", "cwd", "at", "pid", "agent_id", "tool_name", "tool_key",
                 "description", "last_message"}
PROMPT_KINDS = frozenset({"channel", "task", "end"})
# The token a delivered message carries back in its channel turn's tag.
REPLY_TOKEN = re.compile(r"[a-z0-9]{1,32}")


def validate_notice(value: object) -> dict:
    """A closed notice object as ``openswap attention`` sends it, or NoticeError.

    Text must already be cleaned and cut to the report's caps (the hook does
    that), so nothing is cut here.
    """
    if not isinstance(value, dict):
        raise NoticeError("invalid_notice")
    value = scrub(value)
    provider = value.get("provider")
    allowed = _CLAUDE_FIELDS if provider == "claude" else _CODEX_FIELDS if provider == "codex" else None
    if allowed is None or not set(value) <= allowed or not {"event", "cwd"} <= set(value):
        raise NoticeError("invalid_notice")
    if value["event"] not in (CLAUDE_EVENTS if provider == "claude" else CODEX_EVENTS):
        raise NoticeError("invalid_notice")
    _bounded_id(value.get("session_id"))
    cwd = value["cwd"]
    if not isinstance(cwd, str) or len(cwd) > MAX_PATH or any(ord(c) < 32 for c in cwd):
        raise NoticeError("invalid_notice")
    _optional_text(value, "tool_name", DETAIL_CAPS["tool_name"])
    _optional_text(value, "description", DETAIL_CAPS["description"], multiline=True)
    _optional_text(value, "last_message", DETAIL_CAPS["last_message"], multiline=True)
    _optional_text(value, "notification_type", 64)
    for name in ("tool_key", "preview_key"):
        key = value.get(name, "")
        if not isinstance(key, str) or len(key) > 64 or not all(c in "0123456789abcdef" for c in key):
            raise NoticeError("invalid_notice")
    answers = value.get("answers", [])
    if not (isinstance(answers, list) and len(answers) <= MAX_QUESTIONS and all(
            isinstance(a, list) and len(a) == 2 and type(a[0]) is int and type(a[1]) is int
            and 0 <= a[0] < MAX_QUESTIONS and 0 <= a[1] < MAX_OPTIONS for a in answers)):
        raise NoticeError("invalid_notice")
    if "reply" in value and not (isinstance(value["reply"], str) and REPLY_TOKEN.fullmatch(value["reply"])):
        raise NoticeError("invalid_notice")
    if "questions" in value and clean_questions(value["questions"]) != value["questions"]:
        raise NoticeError("invalid_notice")
    if "pid" in value and (type(value["pid"]) is not int or not 1 < value["pid"] <= MAX_PID):
        raise NoticeError("invalid_notice")
    if "entrypoint" in value and not is_entrypoint(value["entrypoint"]):
        raise NoticeError("invalid_notice")
    if "at" in value and (type(value["at"]) not in (int, float) or not math.isfinite(value["at"])):
        raise NoticeError("invalid_notice")
    if "answerable" in value and value["answerable"] is not True:
        raise NoticeError("invalid_notice")
    if "prompt_kind" in value and value["prompt_kind"] not in PROMPT_KINDS:
        raise NoticeError("invalid_notice")
    if "agent_id" in value and not (isinstance(value["agent_id"], str) and 0 < len(value["agent_id"]) <= 64
                                    and all(c.isascii() and (c.isalnum() or c in "_-") for c in value["agent_id"])):
        raise NoticeError("invalid_notice")
    return value


@dataclass
class Item:
    id: str
    kind: str  # permission | question | idle | finished
    provider: str  # claude | codex
    session: str  # Claude session ID or Codex thread ID (a subagent's: its parent's); never leaves the Mac
    cwd: str  # never leaves the Mac
    created_at: float  # epoch seconds
    published: bool = False
    tool_name: str = ""
    tool_key: str = ""
    detail: dict = field(default_factory=dict)
    pid: int = 0  # the Claude Code or Codex process; 0 when the hook could not tell
    entrypoint: str = ""  # Claude Code's own name for its surface (cli, claude-vscode, ...)
    agent: str = ""  # the subagent whose call it is (Codex: its own thread ID); "" for the main thread
    preview_key: str = ""  # digest of the call as a channel would preview it; "" when that is not exact
    started: str = ""  # when ``pid``'s process began (``ps`` lstart): a reused PID differs; "" when unknown
    answerable: bool = False  # a question whose every question and option the report shows whole

    @classmethod
    def from_dict(cls, data: object) -> Item:
        if isinstance(data, dict):
            data = {"preview_key": "", "started": "", "answerable": False, **data}  # saved by an earlier build
        if not isinstance(data, dict) or set(data) != {f for f in cls.__dataclass_fields__}:
            raise ValueError("invalid item")
        item = cls(**data)
        if (not isinstance(item.id, str) or item.kind not in ("permission", "question", "idle", "finished")
                or item.provider not in ("claude", "codex") or not isinstance(item.session, str)
                or not isinstance(item.cwd, str) or type(item.created_at) not in (int, float)
                or type(item.published) is not bool or not isinstance(item.tool_name, str)
                or not isinstance(item.tool_key, str) or not isinstance(item.detail, dict)
                or type(item.pid) is not int or not isinstance(item.entrypoint, str)
                or not isinstance(item.agent, str) or not isinstance(item.preview_key, str)
                or not isinstance(item.started, str) or type(item.answerable) is not bool):
            raise ValueError("invalid item")
        return item


@dataclass(frozen=True)
class SessionRecord:
    """What a running session's ``sessions/{pid}.json`` says: its status
    (``busy``, ``waiting``, ``idle``), when that status began (epoch seconds,
    0 when the record does not say) and, while waiting, what for."""
    session_id: str
    pid: int
    status: str
    status_at: float = 0.0
    waiting_for: str = ""
    proc_start: str = ""  # when the process began (``procStart``, as ``ps`` lstart shows it); "" when not said


def read_records(config_homes) -> tuple[list[SessionRecord], bool]:
    """The records of every running Claude Code session in ``config_homes``
    (one session can run in several processes), and whether every record could
    be read. A record whose PID is not running is left out; whether the PID is
    still the same process is the reader's to check (``proc_start``)."""
    from openswap.process_detection import is_pid_alive

    records, complete = [], True
    for home in config_homes:
        try:
            paths = list((Path(home) / "sessions").glob("*.json"))
        except OSError:
            complete = False
            continue
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                pid, session = data["pid"], data["sessionId"]
                if type(pid) is not int or not isinstance(session, str) or not session:
                    raise ValueError("no pid or session")
                if not is_pid_alive(pid):
                    continue
                records.append(_session_record(data, session, pid))
            except FileNotFoundError:
                continue  # the session ended between listing and reading
            except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
                complete = False
    return records, complete


def _newest_log_database(home: Path) -> Path | None:
    def version(path: Path) -> int:
        suffix = path.stem.rpartition("_")[2]
        return int(suffix) if suffix.isdigit() else -1

    found = [p for p in Path(home).glob("logs_*.sqlite") if version(p) >= 0]
    return max(found, key=version) if found else None


def read_codex_answers(codex_homes, threads: dict[str, float]) -> dict[str, list[tuple[str, float]]]:
    """Each answer a person gave to a prompt on ``threads`` (thread ID -> the
    earliest time of interest), as Codex's own log database records it: by
    thread ID, (the log row's identity, epoch seconds), oldest first; threads
    without an answer left out. Raises OSError or sqlite3.Error when a home's
    log cannot be read."""
    from urllib.parse import quote

    answers: dict[str, list[tuple[str, float]]] = {}
    ids = sorted(threads)
    if not ids:
        return answers
    since = math.floor(min(threads.values())) - 1
    matches = " or ".join("feedback_log_body like ?" for _ in CODEX_ANSWER_OPERATIONS)
    query = (f"select id, thread_id, ts + ts_nanos / 1e9 from logs where thread_id in "
             f"({','.join('?' for _ in ids)}) and ts >= ? and ({matches}) order by ts, ts_nanos, id")
    params = [*ids, since, *(f'%operation="{op}"%' for op in CODEX_ANSWER_OPERATIONS)]
    for home in codex_homes:
        database = _newest_log_database(home)
        if database is None:
            continue
        connection = sqlite3.connect(f"file:{quote(str(database))}?mode=ro", uri=True, timeout=0.2)
        try:
            for row, thread, at in connection.execute(query, params):
                if isinstance(thread, str) and isinstance(at, (int, float)):
                    answers.setdefault(thread, []).append((f"{database}:{row}", float(at)))
        finally:
            connection.close()
    return answers


@dataclass
class Channel:
    """An OpenSwap channel's registration: the Claude Code process it serves (by
    PID and start time) and the channel process itself, held for that Claude
    Code process's life. ``link`` is its live connection, None while it is away."""
    claude_pid: int
    claude_started: str
    pid: int
    started: str
    home: str  # the config home whose sessions/{claude_pid}.json names the session
    link: object = None
    # Another process tried to register for this Claude Code process while the
    # channel ran: which one is real cannot be told, so no replies, for its life.
    contested: bool = False

    def saved(self) -> list:
        return [self.claude_started, self.pid, self.started, self.home, self.contested]

    @classmethod
    def from_saved(cls, claude_pid: int, entry: object) -> Channel:
        if (not isinstance(entry, list) or len(entry) not in (4, 5) or not isinstance(entry[0], str)
                or type(entry[1]) is not int or not isinstance(entry[2], str) or not isinstance(entry[3], str)
                or (len(entry) == 5 and type(entry[4]) is not bool)):
            raise ValueError("invalid channel")
        return cls(claude_pid, entry[0], entry[1], entry[2], entry[3], contested=len(entry) == 5 and entry[4])


@dataclass
class ChannelRequest:
    """A permission prompt as a channel relays it: Claude Code's own words for the
    call (``preview``), and the ``request_id`` a verdict must name."""
    session: str
    claude_pid: int
    request_id: str
    tool_name: str
    description: str
    preview: str
    preview_key: str
    at: float
    channel: tuple = ()  # (pid, start) of the channel process that relayed it


@dataclass
class Waiter:
    """A question hook waiting for the watch's answer."""
    session: str
    tool_key: str
    agent: str
    claude_pid: int
    at: float
    done: threading.Event = field(default_factory=threading.Event)
    answers: list | None = None
    item_id: str = ""  # the question item it waits for, once seen
    reply_id: str = ""  # the reply whose answer it holds


@dataclass
class Sent:
    """A reply delivered to a session, until its item closes."""
    reply_id: str
    action: str
    at: float
    answers: list | None = None
    token: str = ""
    exit_at: float | None = None  # when the session was first seen leaving ``waiting`` after it
    # Other prompts waited in the session when it was sent: the session file
    # cannot tell which of them closed, so the Mac never knows this one won.
    shared: bool = False


def _all_off():
    from openswap.settings import AttentionSettings

    return AttentionSettings(notices=False, details=False, replies=False)


def preview_key(preview: str) -> str:
    return hashlib.sha256(preview.encode("utf-8", "surrogatepass")).hexdigest()[:32]


# ``ps`` lstart in the C locale, as the worker reads it: any other form (another
# locale's names) cannot be compared, so it is taken as not said.
_LSTART = re.compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} \d{1,2} \d\d:\d\d:\d\d \d{4}")


def _session_record(data: dict, session: str, pid: int) -> SessionRecord:
    at, waiting, started = data.get("statusUpdatedAt"), data.get("waitingFor"), data.get("procStart")
    started = " ".join(started.split()) if isinstance(started, str) else ""
    return SessionRecord(session, pid, data.get("status") if isinstance(data.get("status"), str) else "",
                         at / 1000 if type(at) in (int, float) and math.isfinite(at) else 0.0,
                         waiting if isinstance(waiting, str) else "", started if _LSTART.fullmatch(started) else "")


def read_session(home: str, pid: int) -> SessionRecord | None:
    """``home``'s ``sessions/{pid}.json`` for the Claude Code process ``pid``, read now."""
    try:
        data = json.loads((Path(home) / "sessions" / f"{pid}.json").read_text(encoding="utf-8"))
        session = data["sessionId"]
        if data["pid"] != pid or not isinstance(session, str) or not session:
            return None
        return _session_record(data, session, pid)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
        return None


def _new_id() -> str:
    return secrets.token_urlsafe(16)


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class AttentionStore:
    """Open items and the closes not yet reported, shared by the IPC and remote threads.

    ``changed`` is set whenever the report may differ, so the remote thread
    can send it at once instead of on its next pass.
    """

    def __init__(self, backup_root: Path, *, clock=time.time, enabled=None, records=None, codex_answers=None,
                 switches=None, homes=None, install=None):
        self.backup_root = Path(backup_root)
        self.path = self.backup_root / STATE_FILE
        self.clock = clock
        # Whether the owner turned notices on (True, False, or None while the
        # settings cannot be read); read once for every notice, so one
        # arriving after the switch went off is dropped.
        self.enabled = enabled if enabled is not None else (lambda: True)
        self.changed = threading.Event()
        self._lock = threading.Lock()
        self._items: list[Item] = []
        self._closed: list[dict] = []  # {"id", "reason"} not yet acknowledged by the service
        # By (Claude session ID, Claude Code PID): one session can run in several
        # processes. (hook time, start of the last reply), when the owner last
        # submitted a prompt, and when that process's session last left ``waiting``.
        # A Codex thread's prompt time is kept by thread ID.
        self._last_message: dict[tuple, tuple[float, str]] = {}
        self._prompted: dict[object, float] = {}
        self._left_waiting: dict[tuple[str, int], float] = {}
        # The session files: ``records()`` reads them as (records, complete).
        self._read_records = records
        # Codex's log: ``codex_answers(threads)`` as ``read_codex_answers``.
        self._read_codex_answers = codex_answers
        self._codex_seen: dict[str, float] = {}  # Codex log rows already applied, with their times
        self._records: dict[str, list[SessionRecord]] = {}  # by session ID: its live processes' records
        self._known: set[str] = set()  # sessions seen in a record, so a missing one has ended
        self._known_procs: set[tuple[str, int]] = set()  # the same, per process
        self._starts: dict[int, str] = {}  # PID -> when that process began, while it runs
        self._due: dict[str, float] = {}  # item ID -> when its answer at the Mac closes it
        self._due_exit: dict[str, float] = {}  # item ID -> when its session left ``waiting``
        self._refresh_lock = threading.Lock()
        self._secret = b""
        # False while the saved state could not be read (a passing I/O or
        # permission error): nothing is taken or saved until it can.
        self.ready = False
        # Replies (S2). ``switches()`` returns the owner's AttentionSettings;
        # ``homes()`` the managed config homes, where a channel's Claude Code
        # process must have its session file.
        self.switches = switches if switches is not None else _all_off
        self.homes = homes if homes is not None else (lambda: [])
        self._channels: dict[int, Channel] = {}  # by Claude Code PID; the first registration holds it
        self._requests: list[ChannelRequest] = []  # channel permission requests not yet paired
        self._attached: dict[str, ChannelRequest] = {}  # item ID -> the one request it answers
        self._tainted: set[str] = set()  # items whose request could not be told apart: never actionable
        self._waiters: list[Waiter] = []  # question hooks waiting for an answer
        self._sent: dict[str, Sent] = {}  # item ID -> the reply delivered to it, until the item closes
        self._handled: dict[str, list] = {}  # reply ID -> [outcome, keep until]
        self._acks: dict[str, str] = {}  # reply ID -> outcome not yet acknowledged to the service
        self._incoming: list = []  # replies the heartbeat queued, applied off its thread
        # The heartbeat thread's own lock, for ``_acks`` and ``_incoming`` only: it
        # never waits on the store's lock, which is held across disk writes.
        self._ack_lock = threading.Lock()
        self._suspects: list[str] = []  # outcomes in a batch the service refused: tried alone
        self._ack_retry_at = 0.0
        self._offered: dict[str, list] = {}  # item ID -> actions the last accepted report offered
        self._reported = False  # a report was accepted since this start
        self._inflight: dict[str, str] = {}  # reply ID -> item ID, marked sent but not yet handed over
        self._unknown: set[str] = set()  # items with a reply whose hand-over a restart left unknown
        self._held: set[str] = set()  # Claude items to publish once the settings read again
        self._reset_until = 0.0  # replies may predate a set-aside state until then (by ``clock``)
        self._seen: dict[str, dict[int, float]] = {}  # session -> process (0: unknown) -> when last seen
        self._install = install  # puts hooks into config homes created since; returns problems
        self._problems: dict[str, str] = {}
        self._problems_lock = threading.Lock()
        try:
            (self.backup_root / STATUS_FILE).unlink(missing_ok=True)  # a previous run's
        except OSError:
            pass
        self._load()
        self._note_contested()  # a contested session stays without replies across a restart

    # --- status ----------------------------------------------------------------

    def note_problem(self, code: str, text: str | None) -> None:
        """Say (or, with None, stop saying) what keeps notices or replies from
        working, for Settings (``read_problems``)."""
        with self._problems_lock:
            if self._problems.get(code) == text:
                return
            if text is None:
                self._problems.pop(code, None)
            else:
                self._problems[code] = text
            self._write_problems()

    def _write_problems(self) -> None:
        path = self.backup_root / STATUS_FILE
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".attention-status-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"problems": sorted(self._problems.values())}, handle)
            os.replace(tmp, path)
        except OSError as exc:
            log_limited("status_save", "Could not save session notice status: %s", exc)

    def install(self) -> None:
        if self._install is None:
            return
        try:
            for problem in self._install():
                log_limited(f"install:{problem}", "Session notices: %s", problem)
        except Exception as exc:  # noqa: BLE001 - tried again on the next pass
            log_limited("install", "Session notice hooks could not be checked: %s", exc)

    def open_ids(self) -> set[str]:
        with self._lock:
            return {item.id for item in self._items}

    def _ensure_ready(self) -> bool:
        if not self.ready:
            with self._lock:  # one load at a time, never over state built since
                if not self.ready:
                    self._load()
                    if self.ready:
                        self._note_contested()
        return self.ready

    # --- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                raise ValueError("unknown attention state")
            secret = bytes.fromhex(data["secret"])
            items = [Item.from_dict(item) for item in data["items"]]
            closed = data["closed"]
            if len(secret) != 32 or not isinstance(closed, list) or not all(
                    isinstance(c, dict) and set(c) == {"id", "reason"} for c in closed):
                raise ValueError("invalid attention state")
            saved, handled, acks = data.get("channels", {}), data.get("handled", {}), data.get("acks", {})
            inflight, codex_seen = data.get("inflight", {}), data.get("codex_seen", {})
            held = data.get("held", [])
            if (not isinstance(saved, dict) or not isinstance(handled, dict) or not isinstance(acks, dict)
                    or not isinstance(inflight, dict) or not all(isinstance(v, str) for v in inflight.values())
                    or not isinstance(codex_seen, dict)
                    or not isinstance(held, list) or not all(isinstance(i, str) for i in held)
                    or not all(type(v) in (int, float) for v in codex_seen.values())):
                raise ValueError("invalid attention state")
            channels = {int(pid): Channel.from_saved(int(pid), entry) for pid, entry in saved.items()}
            if not all(isinstance(v, list) and len(v) == 2 and v[0] in ACK_OUTCOMES
                       and type(v[1]) in (int, float) for v in handled.values()) or not all(
                    v in ACK_OUTCOMES for v in acks.values()):
                raise ValueError("invalid attention state")
        except FileNotFoundError:
            self._secret, self.ready = secrets.token_bytes(32), True
            self.note_problem("state", None)
            return
        except (ValueError, KeyError, TypeError, AttributeError) as exc:  # UnicodeDecodeError included
            # Only a file that parses wrong is set aside. Starting empty only drops
            # notices; it never reports anything as answered.
            aside = self.path.with_name(f"{self.path.name}.unreadable-{int(time.time())}")
            try:
                os.replace(self.path, aside)
            except OSError as move:
                log_limited("state_unreadable", "Session attention state unreadable (%s) and could not be set "
                            "aside (%s); waiting", exc, move)
                self.note_problem("state", STATE_UNREADABLE)
                return
            logger.warning("Session attention state unreadable (%s); set aside as %s, starting empty", exc, aside)
            # Which replies were handled is lost with it: none issued before now is
            # delivered (a reply lives 10 minutes, and is kept HANDLED_KEEP_SECONDS more).
            self._secret, self.ready = secrets.token_bytes(32), True
            self._reset_until = self.clock() + REPLY_LIFE_SECONDS + HANDLED_KEEP_SECONDS
            self.note_problem("state", None)
            return
        except OSError as exc:
            # A passing read error: the per-Mac key and the open items stay on disk.
            log_limited("state_io", "Session attention state could not be read (%s); retrying", exc)
            self.note_problem("state", STATE_UNREADABLE)
            return
        self._secret, self._items, self._closed = secret, items, closed
        self._channels, self._handled, self._acks = channels, handled, acks
        # Marked sent before a restart, handed over or not: unknown. Never
        # reported refused or closed, and their items take no other reply;
        # once their item is gone, they stay "sent".
        open_ids = {item.id for item in items}
        self._inflight = {r: i for r, i in inflight.items() if i in open_ids}
        self._unknown, self._codex_seen = set(self._inflight.values()), dict(codex_seen)
        self._held = {i for i in held if i in open_ids}
        self.ready = True
        self.note_problem("state", None)

    def _save(self) -> None:
        if not self.ready:
            return  # the file on disk is still the one to keep
        data = {"version": STATE_VERSION, "secret": self._secret.hex(),
                "items": [asdict(item) for item in self._items], "closed": self._closed,
                "channels": {str(pid): c.saved() for pid, c in self._channels.items()},
                "handled": self._handled, "inflight": self._inflight, "codex_seen": self._codex_seen,
                "held": sorted(self._held)}
        with self._ack_lock:
            data["acks"] = dict(self._acks)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".attention-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _commit(self) -> None:
        """After a change: save (a failure only loses restart survival) and wake the reporter."""
        try:
            self._save()
        except OSError as exc:
            log_limited("state_save", "Could not save session attention state: %s", exc)
        self.changed.set()

    # --- lifecycle -----------------------------------------------------------

    def _close(self, items: list[Item], reason: str, *, won: tuple = ()) -> bool:
        """Close ``items`` for ``reason``; those in ``won`` were decided by the
        watch's own reply, which is then ``applied``. Any other reply still in
        flight for a closed item settles as not applied (it stays ``sent``)."""
        if not items:
            return False
        gone = {id(item) for item in items}
        self._items = [item for item in self._items if id(item) not in gone]
        won_ids = {item.id for item in won}
        for item in items:
            self._due.pop(item.id, None)
            self._due_exit.pop(item.id, None)
            self._attached.pop(item.id, None)
            self._tainted.discard(item.id)
            self._held.discard(item.id)
            self._offered.pop(item.id, None)
            if item.id in self._unknown:
                self._unknown.discard(item.id)
                # Its reply stays "sent" for good: whether it landed is not known.
                self._inflight = {r: i for r, i in self._inflight.items() if i != item.id}
            sent = self._sent.pop(item.id, None)
            if sent is not None and item.id in won_ids:
                self._record(sent.reply_id, "applied")
        # The service only ever saw published items; an unpublished one just ends.
        self._closed.extend({"id": item.id, "reason": "answered_from_watch" if item.id in won_ids else reason}
                            for item in items if item.published)
        del self._closed[:-MAX_PENDING_CLOSES]
        return True

    def _open(self, kind: str, provider: str, session: str, notice: dict, *, published: bool,
              tool_name: str = "", tool_key: str = "", detail: dict | None = None) -> None:
        pid = notice.get("pid", 0)
        self._items.append(Item(_new_id(), kind, provider, session, notice["cwd"], self._at(notice), published,
                                tool_name, tool_key, {k: v for k, v in (detail or {}).items() if v},
                                pid, notice.get("entrypoint", ""), notice.get("agent_id", ""),
                                notice.get("preview_key", "") if kind == "permission" else "",
                                self._starts.get(pid, ""), kind == "question" and notice.get("answerable") is True))

    # --- processes ---------------------------------------------------------------

    def _learn_start(self, pid: int) -> None:
        """Remember when ``pid``'s process began, read while it runs (outside the lock)."""
        if pid and pid not in self._starts:
            from openswap.worker import peers

            proc = peers.process(pid)
            if proc is not None:
                self._starts[pid] = proc.started

    def _alive(self, pid: int, started: str = "") -> bool:
        """Whether ``pid`` still runs the process that began at ``started``
        ("" when not known): a reused PID is not that process."""
        from openswap.process_detection import is_pid_alive

        if not is_pid_alive(pid):
            self._starts.pop(pid, None)
            return False
        if not started:
            return True
        known = self._starts.get(pid)  # read by ``refresh`` outside the lock
        return known is None or " ".join(known.split()) == " ".join(started.split())

    def _record_for(self, session: str, pid: int) -> SessionRecord | None:
        """The session file of ``session`` written by Claude Code process ``pid``;
        with ``pid`` 0 (unknown), the one record only when exactly one process
        runs the session."""
        records = self._records.get(session, [])
        if pid:
            return next((r for r in records if r.pid == pid), None)
        return records[0] if len(records) == 1 else None

    def _same_process(self, session: str, a: int, b: int) -> bool:
        """Whether a hook or item of process ``a`` speaks for one of ``b`` in
        ``session``. An unknown process (0) is any process only while no second
        process runs the session."""
        return a == b or ((not a or not b) and len(self._records.get(session, [])) <= 1)

    def _left_since(self, session: str, pid: int) -> float:
        """When ``pid``'s copy of the session last left ``waiting``; for an
        unknown process, the earliest of every live process's (each must agree)."""
        if pid:
            return self._left_waiting.get((session, pid), 0.0)
        pids = [r.pid for r in self._records.get(session, [])]
        return min((self._left_waiting.get((session, p), 0.0) for p in pids), default=0.0)

    def _at(self, notice: dict) -> float:
        """When the hook ran (its own clock reading), else now. Hooks are async and
        can arrive out of order; this is what orders them against each other and
        against a session file's ``statusUpdatedAt``."""
        now = self.clock()
        at = notice.get("at")
        return at if isinstance(at, (int, float)) and abs(at - now) <= MAX_HOOK_AGE_SECONDS else now

    def notice(self, notice: dict) -> None:
        """Apply one validated notice (see ``validate_notice``) to the items.

        Dropped while notices are off. While the settings cannot be read it is
        applied, but nothing new is published until they read again (an item
        replacing one the phone shows is). A ``permission_prompt`` is applied
        against a fresh read of the session files, so only a prompt still
        waiting is published."""
        if not self._ensure_ready():
            return
        on = self.enabled()
        if on is False:
            return
        publishes = on is True
        self._learn_start(notice.get("pid", 0))
        if notice.get("provider") == "claude":
            with self._lock:
                self._seen.setdefault(notice["session_id"], {})[notice.get("pid", 0)] = self._at(notice)
        # A reply in flight is settled against what the session file says now.
        if notice.get("event") == "Notification" or (notice.get("provider") == "claude" and any(
                i.session == notice.get("session_id") for i in self._items if i.id in self._sent)):
            self.refresh(for_notice=True)
        with self._lock:
            if self._apply(notice, publishes):
                self._commit()

    def _shown(self, item: Item) -> bool:
        """Published, or held only until the settings read again."""
        return item.published or item.id in self._held

    def _publish(self, item: Item, publishes: bool) -> None:
        if publishes:
            item.published = True
        else:
            self._held.add(item.id)

    def _apply(self, notice: dict, publishes: bool) -> bool:
        """One notice; without ``publishes``, what it would publish is held
        (``_held``) until the settings read again."""
        if notice["provider"] == "codex":
            return self._apply_codex(notice, publishes)
        event, session, at = notice["event"], notice["session_id"], self._at(notice)
        pid = notice.get("pid", 0)
        key = (session, pid)
        # Only this process's items: the same session may run in another process too.
        mine = [i for i in self._items if i.provider == "claude" and i.session == session
                and self._same_process(session, i.pid, pid)]
        prompts = [i for i in mine if i.kind in ("permission", "question")]
        tool_name = notice.get("tool_name", "")
        record = self._record_for(session, pid)
        if event == "PermissionRequest":
            # A subagent asking again has moved past a prompt the watch denied.
            moved_on = self._agent_moved_on(prompts, notice.get("agent_id", ""), at, notice.get("tool_key", ""))
            if at < self._left_since(session, pid):
                return moved_on  # its prompt was already answered (a decline faster than this hook)
            if tool_name == QUESTION_TOOL:
                self._open("question", "claude", session, notice, published=False, tool_name=tool_name,
                           tool_key=notice.get("tool_key", ""), detail={"questions": notice.get("questions", [])})
            else:
                self._open("permission", "claude", session, notice, published=False, tool_name=tool_name,
                           tool_key=notice.get("tool_key", ""),
                           detail={"tool_name": tool_name, "description": notice.get("description", "")})
                self._pair(session)
            return True
        if event == "Notification":
            kind = notice.get("notification_type")
            if kind == "permission_prompt":
                recorded = [i for i in prompts if not self._shown(i)]
                shown_at = at - PROMPT_NOTICE_SECONDS
                changed = False
                if record is not None and record.status in NOT_WAITING:
                    # The status answers only the calls recorded before it was set. A
                    # status set before this dialog showed is older than the dialog,
                    # which then still waits; one set since closed it.
                    answered = [i for i in recorded if i.created_at <= record.status_at]
                    changed = self._close(answered, "answered_on_mac")
                    recorded = [i for i in recorded if i not in answered]
                    if record.status_at >= shown_at - PROMPT_MATCH_SECONDS and not any(
                            abs(i.created_at - shown_at) <= PROMPT_MATCH_SECONDS for i in recorded):
                        return changed
                # A call whose own notice is overdue ended before it (answered fast).
                ended = [i for i in recorded if i.created_at < shown_at - PROMPT_MATCH_SECONDS]
                changed = self._close(ended, "answered_on_mac") or changed
                match = sorted((i for i in recorded if i not in ended
                                and abs(i.created_at - shown_at) <= PROMPT_MATCH_SECONDS),
                               key=lambda i: abs(i.created_at - shown_at))[:1]
                if match:
                    self._publish(match[0], publishes)
                    return True
                if any(self._shown(i) for i in prompts):
                    # This prompt's notice again: a queued subagent dialog gets a
                    # second one when it finally shows. The owner already knows.
                    return changed
                # A prompt whose PermissionRequest never reached us (or a notice
                # delay other than Claude Code's default): a notice without detail.
                question = record is not None and record.waiting_for == "input needed"
                # Dated when its dialog showed, like a recorded call.
                self._open("question" if question else "permission", "claude", session,
                           {**notice, "at": shown_at}, published=False)
                self._publish(self._items[-1], publishes)
                return True
            if kind == "idle_prompt":
                if at < self._prompted.get(key, 0.0):
                    return False
                said_at, said = self._last_message.get(key, (0.0, ""))
                # A reply from before the latest prompt belongs to an older turn (an
                # Esc-interrupted turn has no Stop of its own).
                last = said if said_at > self._prompted.get(key, 0.0) else ""
                idle = next((i for i in mine if i.kind == "idle"), None)
                if idle is not None:
                    # Still idle: a new item only for a turn that ended since it opened.
                    return said_at > idle.created_at and self._idle_again(idle, notice, last)
                self._open("idle", "claude", session, notice, published=False, detail={"last_message": last})
                self._publish(self._items[-1], publishes)
                return True
            return False
        if event == "Stop":
            changed = False
            if at > self._prompted.get(key, 0.0):
                if notice.get("last_message"):
                    self._last_message.pop(key, None)
                    self._last_message[key] = (at, notice["last_message"])
                    for oldest in list(self._last_message)[:-MAX_LAST_MESSAGES]:
                        del self._last_message[oldest]
                # A turn the owner did not start (a background task's result) ended
                # while the session stayed idle: what the owner saw is out of date.
                idle = next((i for i in mine if i.kind == "idle" and i.created_at < at), None)
                if idle is not None:
                    changed = self._idle_again(idle, {**notice, "at": at}, notice.get("last_message", ""))
            # The main thread's turn ended: its own prompts were answered. A
            # background subagent's prompt may still wait, and a late Stop of an
            # earlier turn says nothing about this turn's prompts.
            ended = self._await_allowed([i for i in prompts if not i.agent and i.created_at <= at])
            # A Stop follows a channel deny (a "No" at the Mac fires none).
            return self._close(ended, "answered_on_mac", won=self._won(ended, at, "deny")) or changed
        if event == "SubagentStop":
            # That subagent ended (a "No" or Esc on its prompt ends it too): its
            # prompts are gone. Another subagent's, or the main thread's, may still wait.
            agent = notice.get("agent_id", "")
            ended = self._await_allowed([i for i in prompts if agent and i.agent == agent and i.created_at <= at])
            return self._close(ended, "answered_on_mac", won=self._won(ended, at, "deny"))
        if event == "UserPromptSubmit":
            prompt = notice.get("prompt_kind")
            if prompt == "end":
                return False  # /clear or /exit: SessionEnd closes the items as session_ended
            self._prompted[key] = max(at, self._prompted.get(key, 0.0))
            for oldest in list(self._prompted)[:-MAX_LAST_MESSAGES]:
                del self._prompted[oldest]
            if prompt == "task":
                # A background task's result starting a turn: the owner did
                # nothing, and a subagent's prompt may still be waiting.
                return False
            if prompt == "channel":
                # OpenSwap's own channel turn: the watch's message reached the idle
                # item it was sent to. It answers no subagent's prompt; a main-thread
                # prompt still open is stale.
                delivered = [i for i in mine if i.kind == "idle" and notice.get("reply")
                             and self._sent.get(i.id) is not None and self._sent[i.id].token == notice["reply"]]
                stale = [i for i in prompts if not i.agent and i.created_at <= at]
                changed = self._close(delivered, "answered_from_watch", won=delivered)
                return self._close(stale, "answered_on_mac") or changed
            return self._close([i for i in mine if i.created_at <= at], "answered_on_mac")
        if event == "SessionEnd":
            for table in (self._last_message, self._prompted, self._left_waiting):
                table.pop(key, None)
            return self._close(mine, "session_ended")
        # PostToolUse / PostToolUseFailure: the prompt for that call was answered.
        agent, call = notice.get("agent_id", ""), notice.get("tool_key", "")
        if tool_name == QUESTION_TOOL:
            # Its own question only: another agent's is still open. Among one
            # agent's, the questions decide (the answers join the input).
            mine = [i for i in prompts if i.kind == "question" and i.agent == agent]
            match = [i for i in mine if call and i.tool_key == call][:1]
            if not match:
                # Not provably this question: it closes, but no answer is claimed for it.
                return self._close(mine if len(mine) == 1 else [], "answered_on_mac")
            return self._close(match, "answered_on_mac", won=self._won(match, at, notice.get("answers") or None))
        same_tool = [i for i in prompts if i.kind == "permission" and i.tool_name == tool_name and i.agent == agent]
        # Another call of the same tool finishing (another agent, a parallel
        # call) says nothing about this prompt: only its own input matches.
        match = [i for i in same_tool if i.tool_key == call][:1] if call else same_tool[:1]
        moved_on = self._agent_moved_on(prompts, agent, at, call)
        # The call ran: an allow from the watch, if it decided the prompt (a
        # failing call was still allowed).
        return self._close(match, "answered_on_mac", won=self._won(match, at, "allow")) or moved_on

    def _await_allowed(self, ended: list[Item]) -> list[Item]:
        """``ended`` (prompts whose turn or subagent ended) without those the
        watch allowed: the call's PostToolUse may still be on its way, so they
        close ``SESSION_END_GRACE_SECONDS`` after this event unless it comes,
        whether or not an answer was already due. The session waiting again
        meanwhile is another dialog: it never keeps them open."""
        waiting = [i for i in ended if self._sent_action(i) == "allow"]
        for item in waiting:
            self._due[item.id] = self.clock() + SESSION_END_GRACE_SECONDS
            self._due_exit[item.id] = math.inf
        return [i for i in ended if i not in waiting]

    def _idle_again(self, idle: Item, notice: dict, last: str) -> bool:
        """The session is idle after another turn: the item the owner saw closes
        as ``superseded`` and a new one (a new ID) stands for this turn, so a
        message sent to the old one can never land after a turn it did not see."""
        held = idle.id in self._held
        self._close([idle], "superseded")
        self._open("idle", "claude", idle.session, {**notice, "cwd": idle.cwd, "pid": idle.pid,
                                                   "entrypoint": idle.entrypoint}, published=idle.published,
                   detail={"last_message": last})
        if held:
            self._held.add(self._items[-1].id)
        return True

    def _apply_codex(self, notice: dict, publishes: bool) -> bool:
        """One Codex hook. ``Stop``, ``UserPromptSubmit`` and ``Interrupt`` run
        only for a thread the owner works in (Codex runs ``SubagentStop`` for a
        subagent and no hook for its own helper threads), so they open and
        close that thread's items only; a subagent's approval is its own."""
        event, thread, at = notice["event"], notice["session_id"], self._at(notice)
        mine = [i for i in self._items if i.provider == "codex" and i.session == thread]
        own = [i for i in mine if i.agent == notice.get("agent_id", "")]
        prompts = [i for i in own if i.kind == "permission"]
        tool_name = notice.get("tool_name", "")
        if event == "PermissionRequest":
            self._open("permission", "codex", thread, notice, published=False, tool_name=tool_name,
                       tool_key=notice.get("tool_key", ""),
                       detail={"tool_name": tool_name, "description": notice.get("description", "")})
            return True
        if event == "PostToolUse":
            key = notice.get("tool_key", "")
            return self._close([i for i in prompts if i.tool_name == tool_name and i.tool_key == key][:1],
                               "answered_on_mac")
        if event == "SessionEnd":
            self._prompted.pop(thread, None)
            return self._close(mine, "session_ended")
        if event == "Stop":
            changed = self._close([i for i in prompts if i.created_at <= at], "answered_on_mac")
            if at < self._prompted.get(thread, 0.0) or any(i.kind == "finished" and i.created_at > at for i in own):
                return changed  # a late Stop of an earlier turn
            finished = [i for i in own if i.kind == "finished"]
            self._close(finished, "superseded")
            # While the settings cannot be read, ``tick`` publishes nothing: a turn
            # replacing one the phone shows goes at once.
            self._open("finished", "codex", thread, notice,
                       published=not publishes and any(i.published for i in finished),
                       detail={"last_message": notice.get("last_message", "")})
            return True
        if event == "UserPromptSubmit":
            self._prompted[thread] = max(at, self._prompted.get(thread, 0.0))
            for oldest in list(self._prompted)[:-MAX_LAST_MESSAGES]:
                del self._prompted[oldest]
            return self._close([i for i in own if i.created_at <= at], "answered_on_mac")
        # Interrupt: Esc or "No" at the prompt ends the turn.
        return self._close([i for i in prompts if i.created_at <= at], "answered_on_mac")

    def refresh(self, *, for_notice: bool = False) -> None:
        """Read the session files and Codex's log (outside the lock) and apply what they say.

        ``for_notice``: a notice the settings already let through needs this
        read, so the settings are not read again."""
        if not self._ensure_ready():
            return
        with self._lock:
            idle = not self._items and not self._channels
        if idle and not for_notice and not self.enabled():
            return  # notices off and nothing open: nothing to read for
        # One read at a time, so an older read is never applied after a newer one.
        with self._refresh_lock:
            if self._read_records is not None:
                try:
                    records, complete = self._read_records()
                except Exception as exc:  # noqa: BLE001 - an unreadable folder must not stop notices
                    logger.debug("Session files unreadable: %s", exc)
                else:
                    records = list(records.values()) if isinstance(records, dict) else list(records)
                    with self._lock:
                        pids = {r.pid for r in records} | {i.pid for i in self._items if i.pid}
                    for pid in pids:
                        self._learn_start(pid)  # never under the lock: ``ps`` can stall
                    self.observe(records, complete)
            self._observe_codex_answers()

    def _observe_codex_answers(self) -> None:
        """Close the Codex approvals a person has answered since they were asked:
        each logged answer closes the oldest approval on its thread asked before
        it, once."""
        if self._read_codex_answers is None:
            return
        with self._lock:
            threads: dict[str, float] = {}
            for item in self._items:
                if item.provider == "codex" and item.kind == "permission":
                    thread = item.agent or item.session
                    threads[thread] = min(item.created_at, threads.get(thread, math.inf))
            if not threads:
                self._codex_seen.clear()
                return
        try:
            answers = self._read_codex_answers(threads)
        except Exception as exc:  # noqa: BLE001 - without Codex's log, the thread's next event closes it
            logger.debug("Codex's log unreadable: %s", exc)
            return
        with self._lock:
            answered: list[Item] = []
            for thread, rows in answers.items():
                for row, at in sorted(rows, key=lambda r: r[1]):
                    if row in self._codex_seen:
                        continue
                    self._codex_seen[row] = at
                    asked = sorted((i for i in self._items if i.provider == "codex" and i.kind == "permission"
                                    and (i.agent or i.session) == thread and i.created_at <= at
                                    and i not in answered), key=lambda i: i.created_at)
                    answered.extend(asked[:1])
            oldest = min(threads.values()) - 1
            self._codex_seen = {row: at for row, at in self._codex_seen.items() if at >= oldest}
            if self._close(answered, "answered_on_mac"):
                self._commit()

    def observe(self, records, complete: bool) -> None:
        """Apply the running sessions' records (a list, or a dict by session ID),
        as read from every managed config home. ``complete`` is False when a
        record could not be read: then no session counts as gone for want of
        its record. One session can run in several Claude Code processes; each
        process's record speaks only for that process's items.

        - A prompt whose session has left ``waiting`` since the prompt was
          recorded was answered at the Mac (Allow, No, Esc). It closes after
          ``SESSION_END_GRACE_SECONDS``, unless the session ends meanwhile (an
          exit or ``/clear`` is never an answer). An idle item is not closed
          here: a turn the owner did not type (OpenSwap's channel, a
          background task) also leaves ``idle``.
        - A session whose record went away, or whose Claude Code process
          ended, closes its items as ``session_ended``.
        """
        records = list(records.values()) if isinstance(records, dict) else list(records)
        with self._lock:
            now = self.clock()
            changed = False
            live: dict[str, list[SessionRecord]] = {}
            for record in records:
                if self._alive(record.pid, record.proc_start):  # a stale file whose PID now runs another process says nothing
                    live.setdefault(record.session_id, []).append(record)
            open_sessions = {i.session for i in self._items if i.provider == "claude"}
            open_procs = {(i.session, i.pid) for i in self._items if i.provider == "claude"}
            # Only running sessions and those with items are kept: nothing grows with every session ever seen.
            self._records = live
            keep = {r.pid for r in records} | {i.pid for i in self._items} | set(self._channels)
            # In place: IPC threads add to it without this lock.
            for pid in [pid for pid in list(self._starts) if pid not in keep]:
                self._starts.pop(pid, None)
            self._seen = {s: seen for s, seen in self._seen.items() if s in open_sessions}
            for record in [r for rs in live.values() for r in rs]:
                if record.session_id in open_sessions:
                    self._seen.setdefault(record.session_id, {})[record.pid] = now
            self._known = {s for s in self._known if s in open_sessions} | set(live)
            self._known_procs = {k for k in self._known_procs if k in open_procs} | {
                (r.session_id, r.pid) for rs in live.values() for r in rs}
            self._left_waiting = {k: t for k, t in self._left_waiting.items()
                                  if k[0] in live or k[0] in open_sessions}
            for record in [r for rs in live.values() for r in rs]:
                if record.status in NOT_WAITING and record.status_at:
                    key = (record.session_id, record.pid)
                    self._left_waiting[key] = max(record.status_at, self._left_waiting.get(key, 0.0))
            # A channel request whose session has stopped waiting since lost its prompt.
            self._requests = [r for r in self._requests if r.at >= self._left_since(r.session, r.claude_pid)]
            for pid in [pid for pid in self._channels if not self._alive(pid)]:
                del self._channels[pid]  # that Claude Code process ended: its registration with it
                changed = True
            if changed:
                self._note_contested()
            won = []
            ended = []
            for item in [i for i in self._items if i.provider == "claude"]:
                mine = live.get(item.session, [])
                if item.pid:
                    record = next((r for r in mine if r.pid == item.pid), None)
                    gone = not self._alive(item.pid, item.started) or (
                        record is None and complete and (item.session, item.pid) in self._known_procs)
                else:
                    record = None
                    gone = not mine and complete and item.session in self._known
                if gone:
                    ended.append(item)
                    continue
                if item.kind not in ("permission", "question"):
                    continue
                if item.pid:
                    answered = record is not None and record.status in NOT_WAITING and (
                        record.status_at > item.created_at)
                    exit_at = record.status_at if answered else 0.0
                else:
                    # No process recorded: every live process of the session must agree.
                    answered = bool(mine) and all(r.status in NOT_WAITING and r.status_at > item.created_at
                                                  for r in mine)
                    exit_at = min((r.status_at for r in mine), default=0.0) if answered else 0.0
                    record = mine[0] if answered and len(mine) == 1 else None
                if not answered:
                    continue
                sent = self._sent.get(item.id)
                if sent is not None and record is not None:
                    if sent.exit_at is None:
                        sent.exit_at = record.status_at
                    # The dialog closed: approved (busy) or declined (idle).
                    if self._won([item], record.status_at, "allow" if record.status == "busy" else "deny"):
                        won.append(item)
                        continue
                if not self._shown(item):
                    changed = self._close([item], "answered_on_mac") or changed
                elif item.id not in self._due:
                    self._due[item.id] = now + SESSION_END_GRACE_SECONDS
                    self._due_exit[item.id] = exit_at
            for item in ended:
                key = (item.session, item.pid)
                for table in (self._last_message, self._prompted, self._left_waiting):
                    table.pop(key, None)
                self._known_procs.discard(key)
                if not live.get(item.session):
                    self._known.discard(item.session)
            changed = self._close(ended, "session_ended") or changed
            changed = self._close(won, "answered_from_watch", won=won) or changed
            if changed:
                self._commit()

    def tick(self) -> None:
        """Close the answers whose grace has passed; end the Codex items whose
        process ended, expire old ones and publish those still open after
        their wait."""
        with self._lock:
            now = self.clock()
            due = [i for i in self._items if self._due.get(i.id, now + 1) <= now]
            for item in list(due):
                # Their own signals (PostToolUse, SubagentStop, Stop) close them.
                if self._waiting_again(item):
                    due.remove(item)
                    del self._due[item.id]
                    self._due_exit.pop(item.id, None)
            changed = self._close(due, "answered_on_mac")
            codex = [i for i in self._items if i.provider == "codex"]
            ended = [i for i in codex if i.pid and not self._alive(i.pid, i.started)]
            changed = self._close(ended, "session_ended") or changed
            expired = [i for i in codex if i not in ended and now - i.created_at >= CODEX_ITEM_SECONDS]
            changed = self._close(expired, "expired") or changed
            waited = [i for i in self._items if i.provider == "codex" and not i.published and now - i.created_at >= (
                CODEX_FINISHED_SECONDS if i.kind == "finished" else CODEX_PROMPT_SECONDS)]
            # A held prompt already answered at the Mac (awaiting its close) never goes.
            held = [i for i in self._items if i.id in self._held and i.id not in self._due]
            # Nothing new goes while the settings cannot be read.
            if (waited or held) and self.enabled() is True:
                for item in waited + held:
                    item.published = True
                    self._held.discard(item.id)
                changed = True
            if changed:
                self._commit()

    def clear(self) -> None:
        """Notices were turned off: drop every item without reporting closes."""
        with self._lock:
            if self._items or self._closed or self._last_message:
                self._items, self._closed, self._last_message, self._due = [], [], {}, {}
                self._due_exit = {}
                self._attached, self._sent, self._requests = {}, {}, []
                # A reply whose hand-over is unknown stays "sent"; one in hand-over settles there.
                self._inflight = {r: i for r, i in self._inflight.items() if i not in self._unknown}
                self._unknown = set()
                self._tainted.clear()
                self._held.clear()
                self._commit()

    # --- replies: channels, prompts and questions ------------------------------

    def _queued_with(self, item: Item, before: float) -> bool:
        """Whether another prompt of the item's process, recorded before
        ``before``, is open: queued dialogs."""
        return any(i.id != item.id and i.provider == "claude" and i.session == item.session
                   and i.kind in ("permission", "question") and self._same_process(i.session, i.pid, item.pid)
                   and i.created_at < before for i in self._items)

    def _waiting_again(self, item: Item) -> bool:
        """Whether the item's session, having left ``waiting`` (its answer is
        due), waits again while another prompt recorded before then is open:
        queued dialogs, and the one answered cannot be told from the one now
        shown."""
        record = self._record_for(item.session, item.pid)
        exit_at = self._due_exit.get(item.id, 0.0)
        return (record is not None and record.status == "waiting" and record.status_at > exit_at
                and self._queued_with(item, exit_at))

    def _unsure(self, item: Item) -> bool:
        """Whether the item is one of several queued prompts of its process
        recorded before its session last left ``waiting``: one of them was
        answered at the Mac, and which one shows only on its own signal."""
        left = self._left_since(item.session, item.pid)
        return item.kind in ("permission", "question") and left > item.created_at and self._queued_with(item, left)

    def _sent_action(self, item: Item) -> str:
        sent = self._sent.get(item.id)
        return sent.action if sent is not None else ""

    def _won(self, items: list[Item], at: float, effect) -> list[Item]:
        """The items whose prompt the watch's reply decided: what happened
        (``effect``: allow, deny or the answer's positions), seen at ``at``, is
        what the watch sent, after it was sent, and the dialog closed within
        ``CONFIRM_SECONDS`` of it when the session file shows when it closed."""
        won = []
        for item in items:
            sent = self._sent.get(item.id)
            if sent is None or sent.shared or at < sent.at:
                continue
            if sent.exit_at is not None and not sent.at <= sent.exit_at <= sent.at + CONFIRM_SECONDS:
                continue
            if (sent.answers if sent.action == "answer" else sent.action) == effect:
                won.append(item)
        return won

    def _agent_moved_on(self, prompts: list[Item], agent: str, at: float, key: str) -> bool:
        """A subagent's next call shows its earlier prompt is gone: one the watch
        denied closes now (a subagent's prompt has no Stop of its own)."""
        if not agent:
            return False
        gone = [i for i in prompts if i.agent == agent and i.tool_key != key and self._sent_action(i) == "deny"
                and i.created_at < at]
        return self._close(gone, "answered_on_mac", won=self._won(gone, at, "deny"))

    def _pair(self, session: str) -> bool:
        """Attach each channel request of ``session`` to the one recorded call it
        previews. Only an exact, unambiguous pairing counts: the same tool, the
        same input (the preview's digest), within PAIR_SECONDS, one request and
        one call. Calls that share a preview with another open call or request
        can never be told apart and stay notices; so does every other call."""
        now = self.clock()
        self._requests = [r for r in self._requests if now - r.at <= REQUEST_SECONDS]
        open_calls = [i for i in self._items if i.session == session and i.kind == "permission"
                      and i.preview_key and i.id not in self._due]
        changed = False
        for key in {(i.tool_name, i.preview_key) for i in open_calls}:
            calls = [i for i in open_calls if (i.tool_name, i.preview_key) == key]
            requests = [r for r in self._requests if r.session == session and (r.tool_name, r.preview_key) == key]
            attached = [i for i in calls if i.id in self._attached]
            if len(calls) > 1 or len(requests) + len(attached) > 1:
                for item in calls:
                    self._tainted.add(item.id)
                    if item.id not in self._sent and self._attached.pop(item.id, None) is not None:
                        changed = True
                continue
            item = calls[0]
            if attached or not requests or item.id in self._tainted:
                continue
            request = requests[0]
            if abs(item.created_at - request.at) > PAIR_SECONDS or (item.pid and item.pid != request.claude_pid):
                continue
            self._requests.remove(request)
            self._attached[item.id] = request
            # Shown as Claude Code relays it, so Allow names exactly what was shown.
            item.detail = {k: v for k, v in (("tool_name", request.tool_name), ("description", request.description),
                                             ("input_preview", request.preview)) if v}
            changed = True
        return changed

    def channel_home(self, claude_pid: int) -> str | None:
        """The managed config home whose session file lists ``claude_pid``, or None."""
        for home in self.homes():
            record = read_session(str(home), claude_pid)
            if record is not None:
                return str(home)
        return None

    def register_channel(self, channel, claude, link) -> bool:
        """Register ``link`` as the OpenSwap channel of Claude Code process
        ``claude`` (both ``peers.Process``). The caller checked that ``channel`` is
        that process's direct child, started as OpenSwap's channel, and that
        Claude Code was started with the channel named.

        The first channel of a Claude Code process holds it. Another candidate
        while the holder runs (another MCP server, a command the session ran,
        whatever its command line says) cannot be told from the real one: the
        session then gets no replies for that process's life, and status says
        why. A new channel takes over only once the holder's process is gone
        (``/mcp`` → Reconnect starts a new one, which Claude Code 2.1.311 still
        relays to and obeys: measured). Only while that process runs from a
        managed config home."""
        from openswap.worker import peers

        if not self._ensure_ready():
            return False
        home = self.channel_home(claude.pid)
        if home is None:
            return False
        with self._lock:
            full = len(self._channels) >= MAX_CHANNELS and claude.pid not in self._channels
            idle = [c for c in self._channels.values() if c.link is None] if full else []
            held = self._channels.get(claude.pid)
        if idle:
            self._evict(idle)
        holder_gone, checked = False, None
        if held is not None and held.claude_started == claude.started and (held.pid, held.started) != (
                channel.pid, channel.started):
            checked = (held.pid, held.started)
            proc = peers.process(held.pid)  # outside the lock: ``ps`` can stall
            holder_gone = (proc is None and not self._alive(held.pid)) or (
                proc is not None and proc.started != held.started)
        with self._lock:
            held = self._channels.get(claude.pid)
            if held is not None and held.claude_started == claude.started:
                if held.contested:
                    return False
                if (held.pid, held.started) != (channel.pid, channel.started):
                    # Gone only as checked: another candidate may have taken it since.
                    if not holder_gone or (held.pid, held.started) != checked:
                        held.contested = True
                        logger.warning("Session replies off for Claude Code process %d: another process tried to "
                                       "register as its OpenSwap channel", claude.pid)
                        self._commit()
                        self._note_contested()
                        if held.link is not None:
                            held.link.close()
                        return False
                    held.pid, held.started = channel.pid, channel.started  # the holder is gone: reconnected
            elif len(self._channels) >= MAX_CHANNELS and held is None:
                logger.warning("Session replies: %d channels are registered; refusing another", MAX_CHANNELS)
                return False
            else:
                held = self._channels[claude.pid] = Channel(claude.pid, claude.started, channel.pid, channel.started,
                                                            home)
                self._note_contested()  # a blocked registration of an ended process may be gone now
            old, held.link = held.link, link
            self._commit()
        if old is not None and old is not link:
            old.close()
        return True

    def _note_contested(self) -> None:
        count = sum(1 for c in self._channels.values() if c.contested)
        self.note_problem("contested", None if not count else (
            f"Replies are off for {count} Claude Code session{'s' if count > 1 else ''}: another process tried to "
            "act as OpenSwap's channel there. Start the session again with openswap claude to answer it."))

    def _evict(self, idle: list[Channel]) -> None:
        """Drop registrations whose Claude Code process is gone, even where its PID
        now names another process (after a restart of the Mac)."""
        from openswap.worker import peers

        from openswap.process_detection import is_pid_alive

        gone = []
        for held in idle:
            proc = peers.process(held.claude_pid)
            # Unreadable is not gone: a live process keeps its registration.
            if (proc is None and not is_pid_alive(held.claude_pid)) or (
                    proc is not None and proc.started != held.claude_started):
                gone.append(held.claude_pid)
        with self._lock:
            for held in [h for h in idle if h.claude_pid in gone]:
                if self._channels.get(held.claude_pid) is held and held.link is None:
                    del self._channels[held.claude_pid]
            if gone:
                self._note_contested()
                self._commit()

    def unregister_channel(self, claude_pid: int, link) -> None:
        """The channel's connection ended; its registration stays for the process's life."""
        with self._lock:
            held = self._channels.get(claude_pid)
            if held is not None and held.link is link:
                held.link = None
                self.changed.set()

    def channel_request(self, claude_pid: int, link, data: object) -> None:
        """A permission prompt relayed by a registered channel."""
        if (not self._ensure_ready() or not isinstance(data, dict)
                or not set(data) <= {"request_id", "tool_name", "description", "input_preview"}):
            return
        request_id, tool_name = data.get("request_id"), data.get("tool_name")
        description, preview = data.get("description", ""), data.get("input_preview", "")
        try:
            attention_id(request_id)
        except ProtocolError:
            return
        if (not isinstance(tool_name, str) or clean_text(tool_name, DETAIL_CAPS["tool_name"]) != tool_name
                or not isinstance(description, str) or not isinstance(preview, str)
                or len(preview) > DETAIL_CAPS["input_preview"]):
            return
        with self._lock:
            held = self._channels.get(claude_pid)
            relayed = held is not None and held.link is link and not held.contested
            home, channel = (held.home, (held.pid, held.started)) if relayed else (None, ())
        record = read_session(home, claude_pid) if home is not None else None
        if record is None:
            return
        at = self.clock()
        with self._lock:
            self._requests.append(ChannelRequest(
                record.session_id, claude_pid, request_id, tool_name,
                clean_text(description, DETAIL_CAPS["description"], multiline=True),
                clean_text(preview, DETAIL_CAPS["input_preview"], multiline=True), preview_key(preview), at,
                channel))
            del self._requests[:-MAX_REQUESTS]
            if self._pair(record.session_id):
                self._commit()

    def _live_channel(self, item: Item) -> Channel | None:
        """The connected, uncontested channel of the process the item belongs to."""
        held = self._channels.get(item.pid) if item.pid else None
        if held is None or held.link is None or held.contested:
            return None
        record = self._record_for(item.session, item.pid)
        return held if record is not None else None

    def _alone(self, item: Item) -> bool:
        """Whether nothing but the item's own Claude Code process has been seen
        for its session since the item opened (a hook of another process, of an
        unknown one, another session file): only then can a reply be sure to
        reach the session the owner saw."""
        if not item.pid:
            return False
        return all(pid == item.pid for pid, at in self._seen.get(item.session, {}).items()
                   if at >= item.created_at)

    def question_waiter(self, claude, session: str, tool_key: str, agent: str = "") -> Waiter | None:
        """A waiter for the question hook of ``session`` running under Claude Code
        process ``claude`` (a ``peers.Process``), or None (the hook then lets the
        dialog be): replies off, no connected channel registered for that process,
        that process's session file names another session, or that process has
        ``MAX_WAITERS_PER_PROCESS`` waiting already.

        Any command the session runs can connect as a question hook too. It
        gains nothing: an answer reaches a waiter only while exactly one waits
        for that question (so an extra one only keeps the question from taking
        an answer), and the answer is what the transcript records anyway. Such
        commands can also take all ``MAX_WAITERS_PER_PROCESS`` places, and that
        process's questions then take no answer until they leave."""
        switches = self.switches()
        if (not self._ensure_ready() or not (switches.notices and switches.replies) or not isinstance(session, str)
                or not isinstance(tool_key, str) or not isinstance(agent, str)):
            return None
        with self._lock:
            held = self._channels.get(claude.pid)
            if held is None or held.claude_started != claude.started or held.link is None or held.contested:
                return None
            home = held.home
        record = read_session(home, claude.pid)
        if record is None or record.session_id != session:
            return None
        with self._lock:
            if (len(self._waiters) >= MAX_WAITERS
                    or sum(w.claude_pid == claude.pid for w in self._waiters) >= MAX_WAITERS_PER_PROCESS):
                return None
            waiter = Waiter(session, tool_key, agent, claude.pid, self.clock())
            self._waiters.append(waiter)
            self.changed.set()
        return waiter

    def waiter_over(self, waiter: Waiter) -> bool:
        """Whether a waiting question hook should stop: its question closed (or,
        never seen, its session stopped waiting), its channel went away, or the
        wait passed QUESTION_WAIT_SECONDS."""
        with self._lock:
            if waiter.done.is_set():
                return True
            if self.clock() - waiter.at >= QUESTION_WAIT_SECONDS:
                return True
            held = self._channels.get(waiter.claude_pid)
            if held is None or held.link is None:
                return True
            item = self._waited(waiter)
            if item is not None:
                waiter.item_id = item.id
                return False
            return bool(waiter.item_id) or self._left_since(waiter.session, waiter.claude_pid) > waiter.at

    def _waited(self, waiter: Waiter) -> Item | None:
        return next((i for i in self._items if self._waits_for(waiter, i)), None)

    @staticmethod
    def _waits_for(waiter: Waiter, item: Item) -> bool:
        """Whether ``waiter`` is the question hook of this very occurrence: the
        same process, session, agent and questions, run with the item's own
        ``PermissionRequest`` (the synchronous and the async hook start together)."""
        return (item.kind == "question" and item.session == waiter.session and item.tool_key == waiter.tool_key
                and item.agent == waiter.agent and item.pid == waiter.claude_pid
                and abs(item.created_at - waiter.at) <= PAIR_SECONDS)

    def end_waiter(self, waiter: Waiter, *, handed: bool = False) -> None:
        """A question hook's connection ended. An answer set for it that never
        reached the hook settles as not delivered, and its item takes a reply again."""
        with self._lock:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
                self.changed.set()
            if waiter.reply_id and not handed:
                self._not_handed_over(waiter.reply_id)

    def _not_handed_over(self, reply_id: str) -> None:
        """A reply known not to have reached its session (under the lock)."""
        item_id = self._inflight.pop(reply_id, None)
        if item_id is None:
            return
        sent = self._sent.get(item_id)
        if sent is not None and sent.reply_id == reply_id:
            del self._sent[item_id]
        still_open = any(i.id == item_id for i in self._items)
        self._record(reply_id, "refused" if still_open else "item_closed")
        self._commit()

    # --- replies: the gate ------------------------------------------------------

    def _actions(self, item: Item, switches) -> list[str]:
        """The replies an item takes now. Only an item a registered channel can
        answer takes any, and only while nothing but its own Claude Code process
        has been seen for its session since it opened: a permission prompt paired
        with the channel's own request (Allow only while details are on, since
        nothing else shows what it would allow), a question shown whole whose one
        synchronous hook waits for it, an idle session."""
        if (not (switches.notices and switches.replies) or item.provider != "claude" or not item.published
                or item.id in self._unknown or not self._alone(item) or self._live_channel(item) is None
                or self._unsure(item)):
            return []
        if item.kind == "permission":
            request = self._attached.get(item.id)
            held = self._channels.get(item.pid)
            # Only the channel that relayed the request can answer it.
            if request is None or item.id in self._tainted or request.channel != (held.pid, held.started):
                return []
            return ["allow", "deny"] if switches.details else ["deny"]
        if item.kind == "question":
            whole = switches.details and item.answerable and item.detail.get("questions")
            return ["answer"] if whole and sum(self._waits_for(w, item) for w in self._waiters) == 1 else []
        return ["message"] if item.kind == "idle" else []

    def queue_replies(self, replies: object, source: object = None) -> None:
        """From the heartbeat thread: keep the replies for the attention thread,
        with the registration (``source``) that received them."""
        if not isinstance(replies, list) or not replies:
            return
        with self._ack_lock:
            self._incoming.extend((source, raw) for raw in replies[:MAX_INCOMING])
            del self._incoming[:-MAX_INCOMING]
        self.changed.set()

    def _record(self, reply_id: str, outcome: str, until: float | None = None, *, queue: bool = True) -> None:
        """A reply's outcome, kept for answering it again and (unless ``queue`` is
        False) queued for the service."""
        entry = self._handled.get(reply_id)
        self._handled[reply_id] = [outcome, until if until is not None else entry[1] if entry else self.clock()]
        if queue:
            with self._ack_lock:
                self._acks[reply_id] = outcome
        for oldest in list(self._handled)[:-MAX_HANDLED]:
            del self._handled[oldest]

    def apply_replies(self, now: float | None = None, source: object = None) -> None:
        """Apply the queued replies ``source`` received, each at most once; ``now``
        is its service's clock (replies expire by it). Replies another
        registration received are dropped: the worker no longer reports through
        it, and a rebuilt registration of the same worker gets its own again,
        since the service resends a reply until it hears the outcome. Nothing is
        applied while the record of handled replies cannot be read (the service
        sends them again)."""
        if not self._ensure_ready() or not self._reported:
            return  # kept: what this start offered is known only once a report was accepted
        with self._ack_lock:
            incoming, self._incoming = self._incoming, []
        for origin, raw in incoming:
            if origin is not source:
                continue
            try:
                self._apply_reply(raw, self.clock() if now is None else now)
            except Exception:  # noqa: BLE001 - one bad reply must not stop the others
                logger.warning("Session reply could not be applied", exc_info=True)
        with self._lock:
            clock = self.clock()
            with self._ack_lock:
                owed = set(self._acks)
            stale = [r for r, (_, until) in self._handled.items()
                     if until < clock and r not in self._inflight and r not in owed]
            for reply_id in stale:
                del self._handled[reply_id]
            if stale:
                self._commit()

    def _again(self, reply_id: str) -> bool:
        """A reply already handled came back (its acknowledgement was lost): the
        same outcome again, never applied twice; nothing for one whose hand-over
        is unknown after a restart (under the lock)."""
        handled = self._handled.get(reply_id)
        if handled is None:
            return False
        if reply_id not in self._inflight:
            with self._ack_lock:
                self._acks[reply_id] = handled[0]
        return True

    def _apply_reply(self, raw: object, now: float) -> None:
        try:
            reply = attention_reply(raw)
            expires = timestamp(reply["expires_at"]).timestamp()
        except ProtocolError:
            reply_id = raw.get("id") if isinstance(raw, dict) else None
            try:
                attention_id(reply_id)
            except ProtocolError:
                return
            with self._lock:
                if not self._again(reply_id):
                    self._record(reply_id, "refused", self.clock() + HANDLED_KEEP_SECONDS)
                    self._commit()
            return
        reply_id, until = reply["id"], self.clock() + max(0.0, expires - now) + HANDLED_KEEP_SECONDS
        switches = self.switches()
        with self._lock:
            if self._again(reply_id):
                return
            outcome, target = self._gate(reply, now, expires, switches)
        if outcome is None:
            outcome = self._deliver(reply, target, until)
        if outcome is not None:
            with self._lock:
                self._record(reply_id, outcome, until)
                self._commit()

    def _gate(self, reply: dict, now: float, expires: float, switches):
        """The outcome of a reply that cannot be delivered, or (None, item) for one
        that can, judged now (under the lock). A reply is taken only for an
        action the last report the service accepted offered for that item."""
        if expires <= now:
            return "expired", None
        if not (switches.notices and switches.replies):
            return "refused", None
        item = next((i for i in self._items if i.id == reply["item_id"]), None)
        if item is None:
            # After the saved state was set aside, a reply may predate this start.
            return ("refused" if self.clock() < self._reset_until else "item_closed"), None
        if item.id in self._due:
            # Answered at the Mac (it closes after its grace), unless queued dialogs hide which.
            return ("refused" if self._waiting_again(item) else "item_closed"), None
        if (reply["action"] not in self._offered.get(item.id, ()) or reply["action"] not in self._actions(item, switches)
                or item.id in self._sent):
            return "refused", None
        if reply["action"] == "answer":
            questions = item.detail.get("questions", [])
            answers = reply["answers"]
            if (len(answers) != len(questions) or any(a["question"] >= len(questions)
                                                      or a["option"] >= len(questions[a["question"]]["options"])
                                                      for a in answers)):
                return "refused", None
        return None, item

    def _deliver(self, reply: dict, item: Item, until: float) -> str | None:
        """Hand a reply to its session. The prompt must still be waiting in that
        very session (read now from its session file), nothing but the item's
        own process may have been seen for it, and the record that the reply
        was handled must be saved first; returns the outcome, or None once
        ``sent`` was recorded here."""
        action = reply["action"]
        with self._lock:
            request = self._attached.get(item.id) if action in ("allow", "deny") else None
            # A verdict goes only to the channel of the process that relayed its request.
            held = self._channels.get(request.claude_pid) if request is not None else self._live_channel(item)
            if held is not None and (held.contested or (request is not None and (
                    request.channel != (held.pid, held.started) or request.claude_pid != item.pid))):
                held = None
            home, pid, link = (held.home, held.claude_pid, held.link) if held is not None else (None, 0, None)
        record = read_session(home, pid) if home is not None else None
        with self._lock:
            if item not in self._items or item.id in self._due:
                return "item_closed"
            if item.id in self._sent or item.id in self._unknown or not self._alone(item):
                return "refused"
            if link is None or (action in ("allow", "deny") and request is None):
                return "refused"  # the channel went away, or the prompt's pairing did
            if record is None or record.session_id != item.session:
                return "item_closed"  # that session ended or was cleared
            if action == "message":
                if record.status != "idle":
                    return "refused"  # typed into, running a turn, or a dialog waits: never typed into
                # A turn ended since (a newer item stands for it). The slack: an item
                # opened at a turn's own Stop may be dated just before its status.
                if record.status_at > item.created_at + SESSION_END_GRACE_SECONDS:
                    return "item_closed"
            elif record.status != "waiting":
                return "item_closed"  # the prompt was answered at the Mac
            elif self._unsure(item):
                return "refused"  # queued dialogs: which was answered is not known yet
            sent = Sent(reply["id"], action, self.clock(), shared=action != "message" and any(
                i is not item and i.session == item.session and i.kind in ("permission", "question")
                for i in self._items))
            waiters = []
            if action == "answer":
                sent.answers = sorted([a["question"], a["option"]] for a in reply["answers"])
                waiters = [w for w in self._waiters if self._waits_for(w, item)]
                if len(waiters) != 1:
                    return "refused"
                message = None
            elif action == "message":
                sent.token = secrets.token_hex(8)
                message = {"message": {"content": reply["text"], "reply": sent.token}}
            else:
                message = {"verdict": {"request_id": request.request_id, "behavior": action}}
            # Recorded and saved before the session can act on it, so the answer's
            # own signal (PostToolUse, Stop, the channel turn) finds it, and so a
            # restart never hands it over twice. The service hears "sent" only
            # once it went out.
            self._sent[item.id] = sent
            self._inflight[reply["id"]] = item.id
            self._record(reply["id"], "sent", until, queue=False)
            try:
                self._save()
            except OSError as exc:
                log_limited("state_save", "Session reply refused: its record could not be saved (%s)", exc)
                del self._sent[item.id]
                del self._inflight[reply["id"]]
                return "refused"
            self.changed.set()
            for waiter in waiters:
                waiter.answers, waiter.reply_id = sent.answers, reply["id"]
                waiter.done.set()
        if message is not None:
            if link.send(message):
                self.handed_over(reply["id"])
            else:
                with self._lock:
                    self._not_handed_over(reply["id"])
        return None

    def handed_over(self, reply_id: str) -> None:
        """A reply reached its session (the channel or a waiting question hook took it)."""
        with self._lock:
            self._inflight.pop(reply_id, None)
            if self._handled.get(reply_id, [""])[0] == "sent":
                with self._ack_lock:
                    if reply_id not in self._acks:  # not applied meanwhile
                        self._acks[reply_id] = "sent"
                self._commit()

    def take_acks(self) -> list[dict]:
        """Up to MAX_ATTENTION_ACKS outcomes for the next heartbeat (its own small
        lock, never the store's). After the service refused a batch, its entries
        go one at a time, after ``ACK_RETRY_SECONDS``, and the others go on."""
        with self._ack_lock:
            suspects = [r for r in self._suspects if r in self._acks]
            self._suspects = suspects
            if suspects and time.monotonic() >= self._ack_retry_at:
                return [{"id": suspects[0], "outcome": self._acks[suspects[0]]}]
            return [{"id": rid, "outcome": outcome} for rid, outcome in self._acks.items()
                    if rid not in suspects][:MAX_ATTENTION_ACKS]

    def acks_sent(self, acks: list[dict]) -> None:
        """The service accepted a heartbeat carrying ``acks``."""
        with self._ack_lock:
            for ack in acks:
                if self._acks.get(ack["id"]) == ack["outcome"]:
                    del self._acks[ack["id"]]
        if acks:
            # Saved with the store's next change: outcomes taken just before a
            # restart may go again, which the service ignores.
            self.changed.set()

    def acks_refused(self, acks: list[dict]) -> None:
        """The service refused a heartbeat for carrying ``acks``. One tried alone
        is given up (logged); a batch is tried again an entry at a time."""
        with self._ack_lock:
            if len(acks) == 1 and acks[0]["id"] in self._suspects:
                logger.warning("Session reply outcome %s refused by the control service; given up", acks[0]["id"])
                self._suspects.remove(acks[0]["id"])
                if self._acks.get(acks[0]["id"]) == acks[0]["outcome"]:
                    del self._acks[acks[0]["id"]]
            else:
                self._suspects.extend(a["id"] for a in acks if a["id"] not in self._suspects)
            self._ack_retry_at = time.monotonic() + ACK_RETRY_SECONDS

    # --- the report ----------------------------------------------------------

    def folder_id(self, cwd: str) -> str:
        return hmac.new(self._secret, b"folder\0" + cwd.encode("utf-8", "surrogatepass"),
                        hashlib.sha256).hexdigest()[:32]

    def session_id(self, item: Item) -> str:
        """An opaque per-Mac ID for the item's session (Claude session ID or Codex thread ID)."""
        raw = f"session\0{item.provider}\0{item.session}".encode("utf-8", "surrogatepass")
        return hmac.new(self._secret, raw, hashlib.sha256).hexdigest()[:32]

    def _session_label(self, item: Item) -> str:
        """The surface and a short tag from the opaque ``session_id``, never from
        the session's own ID."""
        surface = "Codex" if item.provider == "codex" else SURFACES.get(item.entrypoint, "Claude Code")
        return clean_text(f"{surface} · {self.session_id(item)[:4]}", MAX_SESSION_LABEL)

    def report(self, *, notices: bool, details: bool, folder_label, replies: bool = False,
               budget: int = REPORT_BUDGET, plain: frozenset = frozenset(), bare: frozenset = frozenset(),
               closes_only: bool = False) -> tuple[dict, list[str]]:
        """The ``attention`` body (without ``worker_epoch``) and the close IDs it carries.

        ``folder_label(cwd)`` returns an approved Remote tasks folder's label,
        or None. Pass the close IDs to ``acknowledge`` once the service accepts.
        The body fits ``budget`` bytes: the oldest items' detail goes first,
        then the oldest items. Items in ``plain`` go without detail, those in
        ``bare`` with generic labels, and with ``closes_only`` no item goes
        (each a way past a service that refuses the report). An item the
        protocol would refuse is sent without detail or with generic labels,
        or not at all, never holding back the rest or the closes.
        """
        from openswap.settings import AttentionSettings

        switches = AttentionSettings(notices=notices, details=details, replies=replies)
        with self._lock:
            published = sorted((i for i in self._items if i.published), key=lambda i: i.created_at)
            chosen = [replace(item, detail=dict(item.detail)) for item in published[-MAX_ATTENTION_ITEMS:]]
            actions = {item.id: self._actions(item, switches) for item in chosen}
            closed = [dict(c) for c in self._closed[:MAX_ATTENTION_CLOSED]]
        # Labels may touch the file system (a stalled network folder): never under the lock.
        items = []
        for item in chosen if notices and not closes_only else []:
            label = folder_label(item.cwd)
            if label is None:
                name = clean_text(Path(item.cwd).name, MAX_FOLDER_LABEL) if details else ""
                label = name or GENERIC_FOLDER_LABEL
            entry = {"id": item.id, "kind": item.kind, "provider": item.provider,
                     "session_label": self._session_label(item), "session_id": self.session_id(item),
                     "folder_id": self.folder_id(item.cwd),
                     "folder_label": label, "created_at": _stamp(item.created_at), "actions": actions[item.id]}
            if details and item.detail and item.id not in plain:
                entry["detail"] = scrub(item.detail)  # saved by a build that kept lone surrogates
            if item.id in bare:
                self._generic_labels(entry)
            entry = self._valid_entry(entry)
            if entry is not None:
                items.append(entry)
        body = {"notices_enabled": notices, "replies_enabled": notices and switches.replies, "items": items,
                "closed": closed}
        # Escaped non-ASCII text can exceed the protocol's size limit: drop the
        # oldest items' detail, then the oldest items, until the request fits.
        for entry in items:
            if len(json.dumps(body, allow_nan=False).encode()) <= budget:
                break
            self._drop_detail(entry)
        while items and len(json.dumps(body, allow_nan=False).encode()) > budget:
            items.pop(0)
        for entry in items:
            if "detail" not in entry:
                self._drop_detail(entry)  # details off: nothing shows what Allow or an answer would do
        return body, [c["id"] for c in closed]

    @staticmethod
    def _drop_detail(entry: dict) -> None:
        """Without its detail, an item takes no Allow and no answer: the owner
        would approve or answer something not shown."""
        entry.pop("detail", None)
        entry["actions"] = [a for a in entry["actions"] if a not in ("allow", "answer")]

    def _valid_entry(self, entry: dict) -> dict | None:
        """``entry`` as the protocol takes it: without its detail, or with generic
        labels, when that is what the protocol would refuse; None when even
        that is refused (logged)."""
        from openswap.worker.protocol import attention_item

        for repair in (None, self._drop_detail, self._generic_labels):
            if repair is not None:
                repair(entry)
            try:
                attention_item(entry)
                return entry
            except ProtocolError:
                continue
        log_limited("item_refused", "Session notice %s left out of the report: the protocol refuses it", entry["id"])
        return None

    @staticmethod
    def _generic_labels(entry: dict) -> None:
        entry["session_label"], entry["folder_label"] = "Session", GENERIC_FOLDER_LABEL

    def acknowledge(self, closed_ids: list[str], items: list[dict] | None = None) -> None:
        """The service accepted a report carrying these closes (never sent again)
        and these ``items``: a reply is taken only for an action they offered."""
        if items is not None:
            with self._lock:
                self._offered = {entry["id"]: list(entry["actions"]) for entry in items}
                self._reported = True
        if not closed_ids:
            return
        with self._lock:
            sent = set(closed_ids)
            self._closed = [c for c in self._closed if c["id"] not in sent]
            try:
                self._save()
            except OSError as exc:
                log_limited("state_save", "Could not save session attention state: %s", exc)
            if self._closed:
                self.changed.set()  # more closes wait: the next report carries them at once


def read_problems(backup_root: Path) -> list[str]:
    """What the running worker says keeps session notices or replies from working."""
    try:
        data = json.loads((Path(backup_root) / STATUS_FILE).read_text(encoding="utf-8"))
        problems = data.get("problems") if isinstance(data, dict) else None
        return [p for p in problems if isinstance(p, str)] if isinstance(problems, list) else []
    except (OSError, ValueError):
        return []


def approved_folder_label(workspaces, cwd: str) -> str | None:
    """The Remote tasks label of the approved folder ``cwd`` is in, or None.

    A folder of repos is labeled by the repo directly inside it, as its
    Remote tasks entries are; anything else inside it by the folder's own label."""
    from openswap import pathid
    from openswap.settings import valid_account_label

    if not cwd:
        return None
    for workspace in workspaces:
        root = workspace.work_root if workspace.work_root is not None else workspace.output_root
        try:
            if not pathid.inside(cwd, root):
                continue
        except (OSError, ValueError, RuntimeError):
            continue
        if workspace.repos:
            try:
                relative = pathid.canonical(cwd).relative_to(pathid.canonical(root)).parts
            except ValueError:  # inside by identity, not by spelling
                relative = ()
            name = relative[0] if relative else ""
            if name and valid_account_label(name) and (Path(root) / name / ".git").exists():
                return clean_text(name, MAX_FOLDER_LABEL) or None
        return clean_text(workspace.display_label, MAX_FOLDER_LABEL) or None
    return None
