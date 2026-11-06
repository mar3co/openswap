"""Session attention: which of this Mac's sessions need the owner (plan 025).

Claude Code hooks and Codex's ``notify`` program hand *notices* to the worker
over its private socket (``openswap attention``). This module turns them into
*items*, one per occurrence of a session needing the owner, and builds the
optional ``attention`` report the worker sends to the control service.

An item is recorded unpublished and published (reported) only once Claude
Code itself decides the owner seems away: ``permission_prompt`` and
``idle_prompt`` notifications, a question left unanswered for
``QUESTION_FALLBACK_SECONDS``, or a finished Codex turn. Every item gets a
fresh random ID that is never reused. Open items are kept in a private file
under the backup root so they survive a worker restart.

A Claude item also ends with its Claude Code process, whose PID the hook
reports. That needs no ``sessions/{pid}.json``, which a child session started
from another Claude Code session does not write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
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
QUESTION_FALLBACK_SECONDS = 6.0
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


_CLAUDE_FIELDS = {"provider", "event", "session_id", "cwd", "pid", "entrypoint", "tool_name", "tool_key",
                  "description", "questions", "notification_type", "last_message"}
_CODEX_FIELDS = {"provider", "event", "thread_id", "cwd", "last_message"}


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
                or type(item.pid) is not int or not isinstance(item.entrypoint, str)):
            raise ValueError("invalid item")
        return item


def _new_id() -> str:
    return secrets.token_urlsafe(16)


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class AttentionStore:
    """Open items and the closes not yet reported, shared by the IPC and remote threads.

    ``changed`` is set whenever the report may differ, so the remote thread
    can send it at once instead of on its next pass.
    """

    def __init__(self, backup_root: Path, *, clock=time.time, enabled=None):
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
        self._last_message: dict[str, str] = {}  # Claude session ID -> start of the last reply
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
        # The service only ever saw published items; an unpublished one just ends.
        self._closed.extend({"id": item.id, "reason": reason} for item in items if item.published)
        del self._closed[:-MAX_PENDING_CLOSES]
        return True

    def _open(self, kind: str, provider: str, session: str, notice: dict, *, published: bool,
              tool_name: str = "", tool_key: str = "", detail: dict | None = None) -> None:
        self._items.append(Item(_new_id(), kind, provider, session, notice["cwd"], self.clock(), published,
                                tool_name, tool_key, {k: v for k, v in (detail or {}).items() if v},
                                notice.get("pid", 0), notice.get("entrypoint", "")))

    def notice(self, notice: dict) -> None:
        """Apply one validated notice (see ``validate_notice``) to the items.

        Dropped while notices are off."""
        if not self.enabled():
            return
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
        event, session = notice["event"], notice["session_id"]
        mine = [i for i in self._items if i.provider == "claude" and i.session == session]
        prompts = [i for i in mine if i.kind in ("permission", "question")]
        tool_name = notice.get("tool_name", "")
        if event == "PermissionRequest":
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
                waiting = [i for i in prompts if not i.published]
                for item in waiting:
                    item.published = True
                if waiting:
                    return True
                if prompts:
                    return False
                # A prompt whose PermissionRequest never reached us: a notice without detail.
                self._open("permission", "claude", session, notice, published=True)
                return True
            if kind == "idle_prompt":
                if any(i.kind == "idle" for i in mine):
                    return False
                self._open("idle", "claude", session, notice, published=True,
                           detail={"last_message": self._last_message.get(session, "")})
                return True
            return False
        if event == "Stop":
            if notice.get("last_message"):
                self._last_message.pop(session, None)
                self._last_message[session] = notice["last_message"]
                for oldest in list(self._last_message)[:-MAX_LAST_MESSAGES]:
                    del self._last_message[oldest]
            return self._close(prompts, "answered_on_mac")
        if event == "UserPromptSubmit":
            return self._close(mine, "answered_on_mac")
        if event == "SessionEnd":
            self._last_message.pop(session, None)
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

    def tick(self) -> None:
        """Publish questions left unanswered past the fallback, and expire old Codex items."""
        with self._lock:
            now = self.clock()
            changed = False
            for item in self._items:
                if (item.kind == "question" and not item.published
                        and now - item.created_at >= QUESTION_FALLBACK_SECONDS):
                    item.published = changed = True
            expired = [i for i in self._items if i.provider == "codex" and now - i.created_at >= CODEX_ITEM_SECONDS]
            changed = self._close(expired, "expired") or changed
            if changed:
                self._commit()

    def prune(self) -> None:
        """Close the Claude items whose Claude Code process has ended."""
        from openswap.process_detection import is_pid_alive

        with self._lock:
            gone = [i for i in self._items if i.provider == "claude" and i.pid and not is_pid_alive(i.pid)]
            if self._close(gone, "session_ended"):
                self._commit()

    def clear(self) -> None:
        """Notices were turned off: drop every item without reporting closes."""
        with self._lock:
            if self._items or self._closed or self._last_message:
                self._items, self._closed, self._last_message = [], [], {}
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
