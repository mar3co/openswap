"""Session attention: which of this Mac's sessions need the owner (plan 025).

Claude Code hooks and Codex's ``notify`` program hand *notices* to the worker
over its private socket (``openswap attention``). This module turns them into
*items*, one per occurrence of a session needing the owner, and builds the
optional ``attention`` report the worker sends to the control service.

An item is recorded unpublished and published (reported) only once Claude
Code itself decides the owner seems away (its ``permission_prompt`` and
``idle_prompt`` notifications; ``permission_prompt`` fires for questions too),
or for a finished Codex turn. Every item gets a
fresh random ID that is never reused. Open items are kept in a private file
under the backup root so they survive a worker restart.

A "No" or Esc at the Mac fires no hook, so the worker also reads each
session's ``sessions/{pid}.json`` (``status`` and ``statusUpdatedAt``) in every
managed config home. Hooks are async and arrive out of order: each carries
the time it ran, which orders it against the others and against the session
files. A Claude item also ends with its Claude Code process, whose PID the
hook reports, which covers a child session that writes no session file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import tempfile
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from openswap.worker.protocol import (
    DETAIL_CAPS, MAX_ATTENTION_CLOSED, MAX_ATTENTION_ITEMS, MAX_BODY, MAX_FOLDER_LABEL, MAX_OPTION,
    MAX_OPTIONS, MAX_QUESTION, MAX_QUESTION_HEADER, MAX_QUESTIONS, MAX_SESSION_LABEL,
)

logger = logging.getLogger(__name__)

STATE_FILE = Path("worker") / "attention.json"
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
    "PostToolUseFailure", "SessionEnd",
})
CODEX_EVENT = "agent-turn-complete"
MAX_ID = 200
MAX_PATH = 4096
MAX_PID = 2**31 - 1
# Session surfaces by Claude Code's ``entrypoint``.
SURFACES = {"cli": "Terminal", "claude-vscode": "VS Code", "claude-desktop": "Claude Desktop"}


class NoticeError(ValueError):
    """A notice that is not a well-formed hook or Codex report."""


def clean_text(value: object, limit: int, *, multiline: bool = False) -> str:
    """``value`` as display text: control characters become spaces (newline, CR
    and tab survive when ``multiline``), trimmed and cut to ``limit`` code points.
    Empty when nothing is left."""
    if not isinstance(value, str):
        return ""
    keep = "\n\r\t" if multiline else ""
    cleaned = "".join(c if unicodedata.category(c) != "Cc" or c in keep else " " for c in value)
    return cleaned.strip()[:limit].strip()


def clean_questions(value: object) -> list[dict]:
    """``AskUserQuestion``'s questions, cut to the report's caps: the question,
    its header and its option labels only."""
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
        labels = [clean_text(o.get("label") if isinstance(o, dict) else o, MAX_OPTION) for o in options]
        cleaned = {"question": question, "options": [label for label in labels if label][:MAX_OPTIONS]}
        header = clean_text(entry.get("header"), MAX_QUESTION_HEADER)
        if header:
            cleaned["header"] = header
        out.append(cleaned)
    return out


def _bounded_id(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID or any(ord(c) < 32 for c in value):
        raise NoticeError("invalid_notice")
    return value


def is_entrypoint(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 64 and all(c in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                                                                   for c in value)


def _optional_text(data: dict, name: str, limit: int, *, multiline: bool = False) -> str:
    value = data.get(name, "")
    if not isinstance(value, str) or len(value) > limit or clean_text(value, limit, multiline=multiline) != value:
        raise NoticeError("invalid_notice")
    return value


_CLAUDE_FIELDS = {"provider", "event", "session_id", "cwd", "at", "pid", "entrypoint", "agent_id", "tool_name",
                  "tool_key", "description", "questions", "notification_type", "last_message", "prompt_kind"}
_CODEX_FIELDS = {"provider", "event", "thread_id", "cwd", "at", "last_message"}
PROMPT_KINDS = frozenset({"channel", "task", "end"})


def validate_notice(value: object) -> dict:
    """A closed notice object as ``openswap attention`` sends it, or NoticeError.

    Text must already be cleaned and cut to the report's caps (the hook does
    that), so nothing is cut here.
    """
    if not isinstance(value, dict):
        raise NoticeError("invalid_notice")
    provider = value.get("provider")
    allowed = _CLAUDE_FIELDS if provider == "claude" else _CODEX_FIELDS if provider == "codex" else None
    if allowed is None or not set(value) <= allowed or not {"event", "cwd"} <= set(value):
        raise NoticeError("invalid_notice")
    event = value["event"]
    if provider == "claude":
        if event not in CLAUDE_EVENTS:
            raise NoticeError("invalid_notice")
        _bounded_id(value.get("session_id"))
    elif event != CODEX_EVENT:
        raise NoticeError("invalid_notice")
    else:
        _bounded_id(value.get("thread_id"))
    cwd = value["cwd"]
    if not isinstance(cwd, str) or len(cwd) > MAX_PATH or any(ord(c) < 32 for c in cwd):
        raise NoticeError("invalid_notice")
    try:
        cwd.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate: no real path has one
        raise NoticeError("invalid_notice") from None
    _optional_text(value, "tool_name", DETAIL_CAPS["tool_name"])
    _optional_text(value, "description", DETAIL_CAPS["description"], multiline=True)
    _optional_text(value, "last_message", DETAIL_CAPS["last_message"], multiline=True)
    _optional_text(value, "notification_type", 64)
    tool_key = value.get("tool_key", "")
    if not isinstance(tool_key, str) or len(tool_key) > 64 or not all(c in "0123456789abcdef" for c in tool_key):
        raise NoticeError("invalid_notice")
    if "questions" in value and clean_questions(value["questions"]) != value["questions"]:
        raise NoticeError("invalid_notice")
    if "pid" in value and (type(value["pid"]) is not int or not 1 < value["pid"] <= MAX_PID):
        raise NoticeError("invalid_notice")
    if "entrypoint" in value and not is_entrypoint(value["entrypoint"]):
        raise NoticeError("invalid_notice")
    if "at" in value and (type(value["at"]) not in (int, float) or not math.isfinite(value["at"])):
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
    session: str  # Claude session ID or Codex thread ID; never leaves the Mac
    cwd: str  # never leaves the Mac
    created_at: float  # epoch seconds
    published: bool = False
    tool_name: str = ""
    tool_key: str = ""
    detail: dict = field(default_factory=dict)
    pid: int = 0  # the Claude Code process; 0 when the hook could not tell
    entrypoint: str = ""  # Claude Code's own name for its surface (cli, claude-vscode, ...)
    agent: str = ""  # the subagent whose call it is; "" for the main thread

    @classmethod
    def from_dict(cls, data: object) -> Item:
        if not isinstance(data, dict) or set(data) != {f for f in cls.__dataclass_fields__}:
            raise ValueError("invalid item")
        item = cls(**data)
        if (not isinstance(item.id, str) or item.kind not in ("permission", "question", "idle", "finished")
                or item.provider not in ("claude", "codex") or not isinstance(item.session, str)
                or not isinstance(item.cwd, str) or type(item.created_at) not in (int, float)
                or type(item.published) is not bool or not isinstance(item.tool_name, str)
                or not isinstance(item.tool_key, str) or not isinstance(item.detail, dict)
                or type(item.pid) is not int or not isinstance(item.entrypoint, str)
                or not isinstance(item.agent, str)):
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


def read_records(config_homes) -> tuple[dict[str, SessionRecord], bool]:
    """The records of every running Claude Code session in ``config_homes``, by
    session ID, and whether every record could be read."""
    from openswap.process_detection import is_pid_alive

    records, complete = {}, True
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
                at = data.get("statusUpdatedAt")
                waiting = data.get("waitingFor")
                records[session] = SessionRecord(
                    session, pid, data.get("status") if isinstance(data.get("status"), str) else "",
                    at / 1000 if type(at) in (int, float) and math.isfinite(at) else 0.0,
                    waiting if isinstance(waiting, str) else "")
            except FileNotFoundError:
                continue  # the session ended between listing and reading
            except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
                complete = False
    return records, complete


def _new_id() -> str:
    return secrets.token_urlsafe(16)


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class AttentionStore:
    """Open items and the closes not yet reported, shared by the IPC and remote threads.

    ``changed`` is set whenever the report may differ, so the remote thread
    can send it at once instead of on its next pass.
    """

    def __init__(self, backup_root: Path, *, clock=time.time, enabled=None, records=None):
        self.backup_root = Path(backup_root)
        self.path = self.backup_root / STATE_FILE
        self.clock = clock
        # Whether the owner turned notices on; read for every notice, so one
        # arriving after the switch went off is dropped.
        self.enabled = enabled if enabled is not None else (lambda: True)
        self.changed = threading.Event()
        self._lock = threading.Lock()
        self._items: list[Item] = []
        self._closed: list[dict] = []  # {"id", "reason"} not yet acknowledged by the service
        # By Claude session ID: (hook time, start of the last reply), when the
        # owner last submitted a prompt, and when the session last left ``waiting``.
        self._last_message: dict[str, tuple[float, str]] = {}
        self._prompted: dict[str, float] = {}
        self._left_waiting: dict[str, float] = {}
        # The session files: ``records()`` reads them as (records, complete).
        self._read_records = records
        self._records: dict[str, SessionRecord] = {}
        self._known: set[str] = set()  # sessions seen in a record, so a missing one has ended
        self._due: dict[str, float] = {}  # item ID -> when its answer at the Mac closes it
        self._refresh_lock = threading.Lock()
        self._secret = b""
        self._load()

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
        except FileNotFoundError:
            self._secret = secrets.token_bytes(32)
            return
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            # Starting empty only drops notices; it never reports anything as answered.
            logger.warning("Session attention state unreadable (%s); starting empty", exc)
            self._secret = secrets.token_bytes(32)
            return
        self._secret, self._items, self._closed = secret, items, closed

    def _save(self) -> None:
        data = {"version": STATE_VERSION, "secret": self._secret.hex(),
                "items": [asdict(item) for item in self._items], "closed": self._closed}
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".attention-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
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
            logger.warning("Could not save session attention state: %s", exc)
        self.changed.set()

    # --- lifecycle -----------------------------------------------------------

    def _close(self, items: list[Item], reason: str) -> bool:
        if not items:
            return False
        gone = {id(item) for item in items}
        self._items = [item for item in self._items if id(item) not in gone]
        for item in items:
            self._due.pop(item.id, None)
        # The service only ever saw published items; an unpublished one just ends.
        self._closed.extend({"id": item.id, "reason": reason} for item in items if item.published)
        del self._closed[:-MAX_PENDING_CLOSES]
        return True

    def _open(self, kind: str, provider: str, session: str, notice: dict, *, published: bool,
              tool_name: str = "", tool_key: str = "", detail: dict | None = None) -> None:
        self._items.append(Item(_new_id(), kind, provider, session, notice["cwd"], self._at(notice), published,
                                tool_name, tool_key, {k: v for k, v in (detail or {}).items() if v},
                                notice.get("pid", 0), notice.get("entrypoint", ""), notice.get("agent_id", "")))

    def _at(self, notice: dict) -> float:
        """When the hook ran (its own clock reading), else now. Hooks are async and
        can arrive out of order; this is what orders them against each other and
        against a session file's ``statusUpdatedAt``."""
        now = self.clock()
        at = notice.get("at")
        return at if isinstance(at, (int, float)) and abs(at - now) <= MAX_HOOK_AGE_SECONDS else now

    def notice(self, notice: dict) -> None:
        """Apply one validated notice (see ``validate_notice``) to the items.

        Dropped while notices are off. A ``permission_prompt`` is applied
        against a fresh read of the session files, so only a prompt still
        waiting is published."""
        if not self.enabled():
            return
        if notice.get("event") == "Notification":
            self.refresh()
        with self._lock:
            if self._apply(notice):
                self._commit()

    def _apply(self, notice: dict) -> bool:
        if notice["provider"] == "codex":
            thread = notice["thread_id"]
            self._close([i for i in self._items if i.provider == "codex" and i.session == thread], "superseded")
            self._open("finished", "codex", thread, notice, published=True,
                       detail={"last_message": notice.get("last_message", "")})
            return True
        event, session, at = notice["event"], notice["session_id"], self._at(notice)
        mine = [i for i in self._items if i.provider == "claude" and i.session == session]
        prompts = [i for i in mine if i.kind in ("permission", "question")]
        tool_name = notice.get("tool_name", "")
        record = self._records.get(session)
        if record is not None and notice.get("pid") and record.pid != notice["pid"]:
            record = None  # another process's record (a reused ID): it says nothing here
        if event == "PermissionRequest":
            if at < self._left_waiting.get(session, 0.0):
                return False  # its prompt was already answered (a decline faster than this hook)
            if tool_name == QUESTION_TOOL:
                self._open("question", "claude", session, notice, published=False, tool_name=tool_name,
                           detail={"questions": notice.get("questions", [])})
            else:
                self._open("permission", "claude", session, notice, published=False, tool_name=tool_name,
                           tool_key=notice.get("tool_key", ""),
                           detail={"tool_name": tool_name, "description": notice.get("description", "")})
            return True
        if event == "Notification":
            kind = notice.get("notification_type")
            if kind == "permission_prompt":
                recorded = [i for i in prompts if not i.published]
                if record is not None and record.status in NOT_WAITING:
                    # Nothing waits now: every call recorded before that ended without a prompt to show.
                    return self._close([i for i in recorded if i.created_at <= record.status_at], "answered_on_mac")
                shown_at = at - PROMPT_NOTICE_SECONDS
                # A call whose own notice is overdue ended before it (answered fast).
                ended = [i for i in recorded if i.created_at < shown_at - PROMPT_MATCH_SECONDS]
                changed = self._close(ended, "answered_on_mac")
                match = sorted((i for i in recorded if i not in ended
                                and abs(i.created_at - shown_at) <= PROMPT_MATCH_SECONDS),
                               key=lambda i: abs(i.created_at - shown_at))[:1]
                if match:
                    match[0].published = True
                    return True
                if any(i.published for i in prompts):
                    # This prompt's notice again: a queued subagent dialog gets a
                    # second one when it finally shows. The owner already knows.
                    return changed
                # A prompt whose PermissionRequest never reached us (or a notice
                # delay other than Claude Code's default): a notice without detail.
                question = record is not None and record.waiting_for == "input needed"
                # Dated when its dialog showed, like a recorded call.
                self._open("question" if question else "permission", "claude", session,
                           {**notice, "at": shown_at}, published=True)
                return True
            if kind == "idle_prompt":
                if at < self._prompted.get(session, 0.0):
                    return False
                said_at, said = self._last_message.get(session, (0.0, ""))
                # A reply from before the latest prompt belongs to an older turn (an
                # Esc-interrupted turn has no Stop of its own).
                last = said if said_at > self._prompted.get(session, 0.0) else ""
                idle = next((i for i in mine if i.kind == "idle"), None)
                if idle is not None:
                    # Still idle after a turn the owner did not start (a background
                    # task's result, OpenSwap's channel): the same item, with that
                    # turn's reply.
                    if idle.detail.get("last_message", "") == last:
                        return False
                    idle.detail = {"last_message": last} if last else {}
                    return True
                self._open("idle", "claude", session, notice, published=True, detail={"last_message": last})
                return True
            return False
        if event == "Stop":
            if notice.get("last_message") and at > self._prompted.get(session, 0.0):
                self._last_message.pop(session, None)
                self._last_message[session] = (at, notice["last_message"])
                for oldest in list(self._last_message)[:-MAX_LAST_MESSAGES]:
                    del self._last_message[oldest]
            # The main thread's turn ended: its own prompts were answered. A
            # background subagent's prompt may still wait, and a late Stop of an
            # earlier turn says nothing about this turn's prompts.
            return self._close([i for i in prompts if not i.agent and i.created_at <= at], "answered_on_mac")
        if event == "UserPromptSubmit":
            prompt = notice.get("prompt_kind")
            if prompt == "end":
                return False  # /clear or /exit: SessionEnd closes the items as session_ended
            self._prompted[session] = max(at, self._prompted.get(session, 0.0))
            for oldest in list(self._prompted)[:-MAX_LAST_MESSAGES]:
                del self._prompted[oldest]
            if prompt == "task":
                # A background task's result starting a turn: the owner did
                # nothing, and a subagent's prompt may still be waiting.
                return False
            # OpenSwap's own channel turn answers no prompt of a subagent and not
            # the idle item (S2 settles that); a main-thread prompt left open is stale.
            answered = [i for i in prompts if not i.agent] if prompt == "channel" else mine
            return self._close([i for i in answered if i.created_at <= at], "answered_on_mac")
        if event == "SessionEnd":
            for table in (self._last_message, self._prompted, self._left_waiting):
                table.pop(session, None)
            return self._close(mine, "session_ended")
        # PostToolUse / PostToolUseFailure: the prompt for that call was answered.
        if tool_name == QUESTION_TOOL:
            match = [i for i in prompts if i.kind == "question"][:1]
        else:
            same_tool = [i for i in prompts if i.kind == "permission" and i.tool_name == tool_name]
            key = notice.get("tool_key", "")
            # Another call of the same tool finishing (a subagent, a parallel
            # call) says nothing about this prompt: only its own input matches.
            match = [i for i in same_tool if i.tool_key == key][:1] if key else same_tool[:1]
        return self._close(match, "answered_on_mac")

    def refresh(self) -> None:
        """Read the session files (outside the lock) and apply what they say."""
        if self._read_records is None:
            return
        # One read at a time, so an older read is never applied after a newer one.
        with self._refresh_lock:
            try:
                records, complete = self._read_records()
            except Exception as exc:  # noqa: BLE001 - an unreadable folder must not stop notices
                logger.debug("Session files unreadable: %s", exc)
                return
            self.observe(records, complete)

    def observe(self, records: dict[str, SessionRecord], complete: bool) -> None:
        """Apply the running sessions' records (by session ID), as read from every
        managed config home. ``complete`` is False when a record could not be
        read: then no session counts as gone for want of its record.

        - A prompt whose session has left ``waiting`` since the prompt was
          recorded was answered at the Mac (Allow, No, Esc). It closes after
          ``SESSION_END_GRACE_SECONDS``, unless the session ends meanwhile (an
          exit or ``/clear`` is never an answer). An idle item is not closed
          here: a turn the owner did not type (OpenSwap's channel, a
          background task) also leaves ``idle``.
        - A session whose record went away, or whose Claude Code process
          ended, closes its items as ``session_ended``.
        """
        from openswap.process_detection import is_pid_alive

        with self._lock:
            now = self.clock()
            changed = False
            open_sessions = {i.session for i in self._items if i.provider == "claude"}
            # Only running sessions and those with items are kept: nothing grows with every session ever seen.
            self._records = dict(records)
            self._known = {s for s in self._known if s in open_sessions} | set(records)
            self._left_waiting = {s: t for s, t in self._left_waiting.items() if s in records or s in open_sessions}
            for session, record in records.items():
                if record.status in NOT_WAITING and record.status_at:
                    self._left_waiting[session] = max(record.status_at, self._left_waiting.get(session, 0.0))
            ended = []
            for item in [i for i in self._items if i.provider == "claude"]:
                record = records.get(item.session)
                if record is not None and item.pid and record.pid != item.pid:
                    record = None  # another process's record (a reused ID): it says nothing here
                if (item.pid and not is_pid_alive(item.pid)) or (
                        record is None and complete and item.session in self._known):
                    ended.append(item)
                    continue
                if (item.kind not in ("permission", "question") or record is None or not record.status_at
                        or record.status_at <= item.created_at or record.status not in NOT_WAITING):
                    continue
                if not item.published:
                    changed = self._close([item], "answered_on_mac") or changed
                elif item.id not in self._due:
                    self._due[item.id] = now + SESSION_END_GRACE_SECONDS
            for session in {i.session for i in ended}:
                for table in (self._records, self._last_message, self._prompted, self._left_waiting):
                    table.pop(session, None)
                self._known.discard(session)
            changed = self._close(ended, "session_ended") or changed
            if changed:
                self._commit()

    def tick(self) -> None:
        """Close the answers whose grace has passed, and expire old Codex items."""
        with self._lock:
            now = self.clock()
            due = [i for i in self._items if self._due.get(i.id, now + 1) <= now]
            changed = self._close(due, "answered_on_mac")
            expired = [i for i in self._items if i.provider == "codex" and now - i.created_at >= CODEX_ITEM_SECONDS]
            changed = self._close(expired, "expired") or changed
            if changed:
                self._commit()

    def clear(self) -> None:
        """Notices were turned off: drop every item without reporting closes."""
        with self._lock:
            if self._items or self._closed or self._last_message:
                self._items, self._closed, self._last_message, self._due = [], [], {}, {}
                self._commit()

    # --- the report ----------------------------------------------------------

    def folder_id(self, cwd: str) -> str:
        return hmac.new(self._secret, b"folder\0" + os.fsencode(cwd), hashlib.sha256).hexdigest()[:32]

    def session_id(self, item: Item) -> str:
        """An opaque per-Mac ID for the item's session (Claude session ID or Codex thread ID)."""
        raw = f"session\0{item.provider}\0{item.session}".encode("utf-8", "surrogatepass")
        return hmac.new(self._secret, raw, hashlib.sha256).hexdigest()[:32]

    @staticmethod
    def _session_label(item: Item) -> str:
        surface = "Codex" if item.provider == "codex" else SURFACES.get(item.entrypoint, "Claude Code")
        short = "".join(c for c in item.session if c.isalnum())[:4]
        return clean_text(f"{surface} · {short}" if short else surface, MAX_SESSION_LABEL)

    def report(self, *, notices: bool, details: bool, folder_label) -> tuple[dict, list[str]]:
        """The ``attention`` body (without ``worker_epoch``) and the close IDs it carries.

        ``folder_label(cwd)`` returns an approved Remote tasks folder's label,
        or None. Pass the close IDs to ``acknowledge`` once the service accepts.
        """
        with self._lock:
            published = sorted((i for i in self._items if i.published), key=lambda i: i.created_at)
            chosen = [replace(item, detail=dict(item.detail)) for item in published[-MAX_ATTENTION_ITEMS:]]
            closed = [dict(c) for c in self._closed[:MAX_ATTENTION_CLOSED]]
        # Labels may touch the file system (a stalled network folder): never under the lock.
        items = []
        for item in chosen if notices else []:
            label = folder_label(item.cwd)
            if label is None:
                name = clean_text(Path(item.cwd).name, MAX_FOLDER_LABEL) if details else ""
                label = name or GENERIC_FOLDER_LABEL
            entry = {"id": item.id, "kind": item.kind, "provider": item.provider,
                     "session_label": self._session_label(item), "session_id": self.session_id(item),
                     "folder_id": self.folder_id(item.cwd),
                     "folder_label": label, "created_at": _stamp(item.created_at), "actions": []}
            if details and item.detail:
                entry["detail"] = item.detail
            items.append(entry)
        body = {"notices_enabled": notices, "replies_enabled": False, "items": items, "closed": closed}
        # Escaped non-ASCII text can exceed the protocol's size limit: drop the
        # oldest items' detail until the request fits.
        for entry in items:
            if len(json.dumps(body, allow_nan=False).encode()) <= REPORT_BUDGET:
                break
            entry.pop("detail", None)
        return body, [c["id"] for c in closed]

    def acknowledge(self, closed_ids: list[str]) -> None:
        """The service accepted a report carrying these closes: never send them again."""
        if not closed_ids:
            return
        with self._lock:
            sent = set(closed_ids)
            self._closed = [c for c in self._closed if c["id"] not in sent]
            try:
                self._save()
            except OSError as exc:
                logger.warning("Could not save session attention state: %s", exc)


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
