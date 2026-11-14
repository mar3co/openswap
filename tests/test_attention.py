"""Session attention (plan 025, phase S1): hooks, items, IPC notices and the report."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import sqlite3
import time
import tomllib
from pathlib import Path

import pytest

from openswap import attention
from openswap.exceptions import ConfigError
from openswap.settings import (
    AttentionSettings, configure_worker_service, load_attention_settings, update_worker_settings,
    write_attention_settings,
)
from openswap.worker import ipc
from openswap.worker.attention import (
    SESSION_END_GRACE_SECONDS, AttentionStore, Item, NoticeError, SessionRecord, approved_folder_label, read_records,
    validate_notice,
)
from openswap.worker.protocol import MAX_BODY, ProtocolError, attention_report
from openswap.worker.refserver import ControlStore

REAL_CLAUDE_PID = attention.claude_pid
pytestmark = pytest.mark.skipif(os.name != "posix", reason="the worker and its hooks are POSIX-only")

SESSION = "3f2a9c1e-0000-4000-8000-000000000001"
OTHER = "77aa0000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _no_real_session(monkeypatch):
    """Hooks here run under pytest, not Claude Code: no process or surface to report."""
    monkeypatch.setattr(attention, "claude_pid", lambda: 0)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SHELL_PREFIX", raising=False)


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


def hook(event, session=SESSION, **fields):
    """A notice as ``openswap attention <event>`` builds it from Claude Code's input."""
    data = {"session_id": session, "cwd": "/Users/o/code/app", "hook_event_name": event, **fields}
    notice = attention._hook_notice(event, json.dumps(data).encode())
    return validate_notice(notice)


def permission(tool="Bash", command="npm test", **extra):
    return hook("PermissionRequest", tool_name=tool,
                tool_input={"command": command, "description": "Run the tests"}, **extra)


def post(tool="Bash", command="npm test", event="PostToolUse", **extra):
    return hook(event, tool_name=tool, tool_input={"command": command, "description": "Run the tests"}, **extra)


def question():
    return hook("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": [
        {"question": "Which database?", "header": "DB", "multiSelect": False,
         "options": [{"label": "Postgres", "description": "x"}, {"label": "SQLite", "description": "y"}]}]})


START = 1_800_000_000.0  # the test clock's start


def notification(kind, session=SESSION):
    """As Claude Code sends it: a ``permission_prompt`` 6 s after its dialog
    (hooks without ``at`` run at START)."""
    notice = hook("Notification", session=session, notification_type=kind, message="Claude needs you")
    return notice | {"at": START + 6} if kind == "permission_prompt" else notice


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    return AttentionStore(tmp_path, clock=clock), clock


def published(store):
    body, _ = store.report(notices=True, details=True, folder_label=lambda cwd: None)
    return body["items"]


def closes(store):
    return store.report(notices=True, details=True, folder_label=lambda cwd: None)[0]["closed"]


# --- the hook entry point -----------------------------------------------------


def test_hook_input_maps_to_its_notice_and_never_carries_raw_tool_input():
    notice = permission(command="curl https://x?token=secret")
    assert notice["event"] == "PermissionRequest" and notice["tool_name"] == "Bash"
    assert notice["description"] == "Run the tests"
    assert "secret" not in json.dumps(notice)
    assert len(notice["tool_key"]) == 32
    assert post(command="curl https://x?token=secret")["tool_key"] == notice["tool_key"]
    assert hook("Notification", notification_type="idle_prompt")["notification_type"] == "idle_prompt"
    stop = hook("Stop", last_assistant_message="Done.\x1b[31m\nAll green " + "x" * 2000)
    assert stop["last_message"].startswith("Done. [31m\nAll green") and len(stop["last_message"]) == 1000
    assert hook("UserPromptSubmit", prompt="<task-notification>\n…")["prompt_kind"] == "task"
    assert hook("UserPromptSubmit", prompt="/clear")["prompt_kind"] == "end"
    assert "prompt_kind" not in hook("UserPromptSubmit", prompt="fix the <task-notification> parser")
    assert "fix the" not in json.dumps(hook("UserPromptSubmit", prompt="fix the bug"))
    assert question()["questions"] == [{"question": "Which database?", "header": "DB",
                                        "options": ["Postgres", "SQLite"]}]
    # Other tools send their name only.
    assert set(permission(tool="Write", command="x")) == {"provider", "event", "session_id", "cwd",
                                                          "tool_name", "tool_key", "preview_key"}


@pytest.mark.parametrize("raw", [b"", b"{not json", b"[]", b'{"cwd": "/x"}', b'{"session_id": 7}'])
def test_malformed_hook_input_sends_nothing(raw):
    assert attention._hook_notice("Stop", raw) is None


def _run_main(monkeypatch, argv, stdin=b""):
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    started = time.monotonic()
    code = attention.main(argv)
    return code, time.monotonic() - started


def test_hook_without_a_worker_exits_quietly_at_once(monkeypatch, capsys):
    stdin = json.dumps({"session_id": SESSION, "cwd": "/x", "last_assistant_message": "hi"}).encode()
    code, took = _run_main(monkeypatch, ["Stop"], stdin)
    assert (code, capsys.readouterr()) == (0, ("", ""))
    assert took < 1


def test_oversized_or_unknown_hook_input_exits_quietly(monkeypatch, capsys):
    monkeypatch.setattr(attention, "MAX_STDIN_BYTES", 64)
    sent = []
    monkeypatch.setattr(attention, "send_notice", sent.append)
    for argv, stdin in ((["Stop"], b"{" + b" " * 100 + b"}"), (["NotAnEvent"], b"{}"), ([], b"")):
        code, took = _run_main(monkeypatch, argv, stdin)
        assert code == 0 and took < 1
    assert sent == [] and capsys.readouterr() == ("", "")


def test_hook_stops_at_its_deadline_whatever_happens(tmp_path):
    """A worker that never answers cannot hold the hook past its second."""
    import subprocess

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    home = tmp_path / "h"
    path = ipc.socket_path(home / "Library" / "Application Support" / "OpenSwap")
    path.parent.mkdir(parents=True, mode=0o700)
    listener.bind(str(path))
    listener.listen(1)  # accepted by the kernel, never read
    env = {**os.environ, "HOME": str(home)}
    started = time.monotonic()
    hook_process = subprocess.Popen([sys.executable, "-c", "import sys; from openswap.attention import main; "
                                     "sys.exit(main(['Stop']))"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env)
    # Its input never ends: only the deadline can end the hook.
    hook_process.stdin.write(json.dumps({"session_id": SESSION, "cwd": "/x"}).encode())
    hook_process.stdin.flush()
    out, err = hook_process.stdout.read(), hook_process.stderr.read()  # until the hook exits
    result = subprocess.CompletedProcess([], hook_process.wait(timeout=10), out, err)
    hook_process.stdin.close()
    listener.close()
    path.unlink()
    if path.parent.parent == Path("/tmp"):
        path.parent.rmdir()
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
    assert time.monotonic() - started < 3  # interpreter start-up plus the 0.9 s deadline


# --- items and their lifecycle ---------------------------------------------------


def test_permission_publishes_only_on_permission_prompt(store):
    items, clock = store
    items.notice(permission())
    assert published(items) == []
    items.notice(notification("permission_prompt"))
    [item] = published(items)
    assert (item["kind"], item["detail"]) == ("permission", {"tool_name": "Bash", "description": "Run the tests"})
    items.notice(notification("permission_prompt"))  # the same prompt again: nothing new
    assert len(published(items)) == 1


def test_two_waiting_permissions_are_two_items_with_their_own_ids(store):
    items, _ = store
    items.notice(permission(command="a"))
    items.notice(notification("permission_prompt"))
    items.notice(permission(command="b"))  # a subagent's prompt, queued behind the first
    items.notice(notification("permission_prompt"))
    first, second = published(items)
    assert first["id"] != second["id"]
    items.notice(post(command="b"))
    assert [i["id"] for i in published(items)] == [first["id"]]
    assert closes(items) == [{"id": second["id"], "reason": "answered_on_mac"}]


def test_item_ids_are_never_reused(store):
    items, _ = store
    seen = set()
    for _ in range(5):
        items.notice(notification("idle_prompt"))
        [item] = published(items)
        assert item["id"] not in seen
        seen.add(item["id"])
        items.notice(hook("UserPromptSubmit"))


def test_question_publishes_on_permission_prompt_only(store):
    items, clock = store
    items.notice(question())
    clock.now += 60
    items.tick()
    assert published(items) == []  # no timer of its own
    items.notice(notification("permission_prompt"))
    [item] = published(items)
    assert item["kind"] == "question" and item["detail"]["questions"][0]["options"] == ["Postgres", "SQLite"]
    items.notice(post(tool="AskUserQuestion", command="answered"))
    assert published(items) == [] and closes(items)[0]["reason"] == "answered_on_mac"


def test_idle_publishes_on_idle_prompt_with_the_last_message(store):
    items, _ = store
    items.notice(hook("Stop", last_assistant_message="All tests pass."))
    assert published(items) == []
    items.notice(notification("idle_prompt"))
    items.notice(notification("idle_prompt"))  # at most one idle item per session
    [item] = published(items)
    assert (item["kind"], item["detail"]) == ("idle", {"last_message": "All tests pass."})


@pytest.mark.parametrize(("signal", "closed_kinds", "reason"), [
    (lambda: hook("UserPromptSubmit"), {"permission", "question", "idle"}, "answered_on_mac"),
    (lambda: hook("Stop"), {"permission", "question"}, "answered_on_mac"),
    (lambda: post(event="PostToolUseFailure"), {"permission"}, "answered_on_mac"),
    (lambda: hook("SessionEnd", reason="clear"), {"permission", "question", "idle"}, "session_ended"),
])
def test_each_close_signal_closes_the_right_items(store, signal, closed_kinds, reason):
    items, _ = store
    items.notice(permission())
    items.notice(notification("permission_prompt"))
    items.notice(question())
    items.notice(notification("permission_prompt"))
    items.notice(notification("idle_prompt"))
    items.notice(permission() | {"session_id": OTHER})  # another session's prompt is never touched
    items.notice(notification("permission_prompt", session=OTHER))
    before = {i["id"]: i["kind"] for i in published(items)}
    items.notice(signal())
    after = {i["id"] for i in published(items)}
    gone = {i: k for i, k in before.items() if i not in after}
    assert sorted(gone.values()) == sorted(closed_kinds)
    assert {c["id"]: c["reason"] for c in closes(items)} == {i: reason for i in gone}


def test_an_answer_before_publishing_reports_nothing(store):
    items, _ = store
    items.notice(permission())
    items.notice(post())
    items.notice(notification("permission_prompt"))  # a late notification for it
    body = items.report(notices=True, details=True, folder_label=lambda cwd: None)[0]
    assert body["closed"] == []
    # Nothing was waiting, so the late notification opens a bare notice.
    assert [i.get("detail") for i in body["items"]] == [None]


# --- Required outcomes rows 1-7 (plan 025, phase 0 results) -----------------------


def at(clock, seconds):
    """A hook's own time, ``seconds`` after the test clock's start."""
    return {"at": clock.now + seconds}


def record(status, status_at, session=SESSION, waiting_for="", pid=None):
    return [SessionRecord(session, os.getpid() if pid is None else pid, status, status_at, waiting_for)]


def test_row1_a_call_that_ends_before_its_prompt_is_never_published(store):
    items, clock = store
    t = clock.now
    # Auto mode decides call A; its PermissionRequest is the only trace.
    items.notice(permission(command="a") | at(clock, 0))
    items.observe(record("busy", t - 30), True)
    # Call B is declined at the Mac faster than the watcher; its hook arrives late.
    items.observe(record("idle", t + 3), True)
    items.notice(permission(command="b") | at(clock, 2))
    # A later prompt (call C, a new turn) must publish only C.
    items.observe(record("waiting", t + 10, waiting_for="permission prompt"), True)
    items.notice(permission(command="c") | at(clock, 9))
    items.notice(notification("permission_prompt") | at(clock, 16))
    assert len(published(items)) == 1
    assert [i.tool_key for i in items._items] == [attention.tool_key({"command": "c", "description": "Run the tests"})]
    assert closes(items) == []  # nothing of A or B was ever shown


def first_is(items, command):
    return [i.tool_key for i in items._items if i.published][:1] == [
        attention.tool_key({"command": command, "description": "Run the tests"})]


def test_a_late_notice_for_an_answered_call_leaves_the_next_call_alone(store):
    items, clock = store
    t = clock.now
    items.notice(permission(command="a") | at(clock, 0))
    items.observe(record("busy", t + 3), True)  # A answered at 3
    items.notice(permission(command="b") | at(clock, 5.9))  # B's dialog shows
    items.notice(notification("permission_prompt") | at(clock, 6))  # A's notice, late
    assert published(items) == []
    items.observe(record("waiting", t + 5.95, waiting_for="permission prompt"), True)
    items.notice(notification("permission_prompt") | at(clock, 11.9))
    assert first_is(items, "b")


def test_parallel_subagent_prompts_each_publish_their_own_call(store):
    """Measured live: both PermissionRequests run at once, each notice 6.0 s after its own."""
    items, clock = store
    t = clock.now
    items.notice(permission(command="b", agent_id="a7c7645e861d1e073") | at(clock, 0))
    # The subagents run in the background: the main turn ends while both prompts wait.
    items.notice(hook("Stop", last_assistant_message="Launched both.") | at(clock, 0.2))
    items.notice(permission(command="a", agent_id="aa66adb8aacae2617") | at(clock, 0.4))
    items.observe(record("waiting", t, waiting_for="permission prompt"), True)
    items.notice(notification("permission_prompt") | at(clock, 6.0))
    assert first_is(items, "b")
    [first] = published(items)
    items.notice(notification("permission_prompt") | at(clock, 6.4))
    assert len(published(items)) == 2
    # Allowing B shows A's queued dialog, and Claude Code notifies for it again.
    items.notice(notification("permission_prompt") | at(clock, 26))
    assert len(published(items)) == 2
    # B allowed: its result starts a turn; A still waits (the file stays waiting).
    items.notice(post(command="b", agent_id="a7c7645e861d1e073") | at(clock, 20))
    items.notice(hook("UserPromptSubmit", prompt="<task-notification>\n<task-id>x</task-id>") | at(clock, 21))
    items.notice(hook("Stop", last_assistant_message="B done.") | at(clock, 23))
    items.observe(record("waiting", t, waiting_for="permission prompt"), True)
    clock.now += 10
    items.tick()
    [left] = published(items)
    assert left["id"] != first["id"]
    assert [i.tool_key for i in items._items] == [attention.tool_key({"command": "a", "description": "Run the tests"})]


def test_a_call_answered_before_its_notice_is_never_published_by_another(store):
    items, clock = store
    t = clock.now
    items.notice(permission(command="declined fast") | at(clock, 0))  # a subagent's, No within 6 s
    items.notice(permission(command="still waiting") | at(clock, 30))
    items.observe(record("waiting", t, waiting_for="permission prompt"), True)
    items.notice(notification("permission_prompt") | at(clock, 36))
    [item] = published(items)
    assert [i.tool_key for i in items._items] == [
        attention.tool_key({"command": "still waiting", "description": "Run the tests"})]


def test_an_unknown_session_status_is_never_read_as_an_answer(store):
    items, clock = store
    items.notice(permission() | at(clock, 0))
    items.observe(record("", clock.now + 1), True)  # a record without status, or a new status
    items.notice(notification("permission_prompt") | at(clock, 6))
    clock.now += 10
    items.tick()
    assert len(published(items)) == 1


@pytest.mark.parametrize("other", ["idle", "busy", "gone"])
def test_another_processs_record_for_the_session_is_ignored(store, other):
    """W10: the same session in a second process (``claude --resume`` elsewhere)."""
    items, clock = store
    mine = SessionRecord(SESSION, os.getpid(), "waiting", clock.now - 1, "permission prompt")
    items.observe([mine], True)
    items.notice(permission() | at(clock, 0) | {"pid": os.getpid()})
    items.notice(notification("permission_prompt") | at(clock, 6) | {"pid": os.getpid()})
    [shown] = published(items)
    theirs = SessionRecord(SESSION, os.getppid(), other if other != "gone" else "idle", clock.now + 8)
    items.observe([mine, theirs], True)  # the other process left waiting after the item opened
    if other == "gone":
        items.observe([mine], True)  # and then ended
    clock.now += 10
    items.tick()
    assert published(items) == [shown] and shown["detail"]["description"] == "Run the tests"
    assert closes(items) == []


def test_row2_a_permission_prompt_publishes_only_the_prompt_waiting_now_once(store):
    items, clock = store
    t = clock.now
    items.notice(permission(command="x") | at(clock, 0))
    # The session left waiting (declined) and is busy on a new turn: nothing waits.
    items.observe(record("busy", t + 2), True)
    items.notice(notification("permission_prompt") | at(clock, 6))
    assert published(items) == [] and items._items == []
    items.observe(record("waiting", t + 10, waiting_for="input needed"), True)
    items.notice(notification("permission_prompt") | at(clock, 16))
    items.notice(notification("permission_prompt") | at(clock, 16.2))  # the same notice again
    [bare] = published(items)  # its PermissionRequest never arrived: one notice, kind from the record
    assert bare["kind"] == "question" and "detail" not in bare


@pytest.mark.parametrize("next_status", ["idle", "busy"])
def test_row3_a_decline_at_the_mac_closes_the_item_as_answered_on_mac(store, next_status):
    items, clock = store
    t = clock.now
    items.notice(permission() | at(clock, 0))
    items.observe(record("waiting", t + 0.05, waiting_for="permission prompt"), True)
    items.notice(notification("permission_prompt") | at(clock, 6))
    # No, Esc, or "No, and tell Claude…" with the next turn already running.
    items.observe(record(next_status, t + 9), True)
    assert len(published(items)) == 1  # inside the grace an exit could still follow
    clock.now += SESSION_END_GRACE_SECONDS
    items.tick()
    assert published(items) == [] and [c["reason"] for c in closes(items)] == ["answered_on_mac"]


def test_row4_one_prompt_ending_closes_only_its_own_item(store):
    items, clock = store
    t = clock.now
    items.notice(permission(command="a") | at(clock, 0))
    items.notice(notification("permission_prompt") | at(clock, 6))
    items.notice(permission(command="b") | at(clock, 7) | {"session_id": OTHER})
    items.notice(notification("permission_prompt", session=OTHER) | at(clock, 13))
    waiting = record("waiting", t + 0.1) + record("waiting", t + 7.1, session=OTHER)
    items.observe(waiting, True)
    items.notice(post(command="b") | {"session_id": OTHER})
    [left] = published(items)
    assert left["session_id"] == items.session_id(next(i for i in items._items if i.session == SESSION))
    clock.now += 10
    items.tick()
    assert len(published(items)) == 1  # still waiting: never closed


def test_row5_after_a_restart_an_item_whose_session_moved_on_closes(tmp_path):
    clock = Clock()
    t = clock.now
    first = AttentionStore(tmp_path, clock=clock)
    first.notice(permission() | at(clock, 0))
    first.notice(notification("permission_prompt") | at(clock, 6))
    first.notice(notification("idle_prompt", session=OTHER) | at(clock, 6))
    reads = [(record("idle", t + 20) + record("busy", t + 30, session=OTHER), True)]
    again = AttentionStore(tmp_path, clock=clock, records=lambda: reads[0])
    clock.now += 60
    again.refresh()
    clock.now += SESSION_END_GRACE_SECONDS
    again.tick()
    # The prompt closed; the idle item waits for a typed prompt (a channel or
    # background turn also leaves idle, so the file alone never closes it).
    assert [i["kind"] for i in published(again)] == ["idle"]
    assert [c["reason"] for c in closes(again)] == ["answered_on_mac"]


@pytest.mark.parametrize("ending", ["exit", "clear", "hook"])
def test_row6_an_exit_or_clear_is_session_ended_never_answered_on_mac(store, ending):
    items, clock = store
    t = clock.now
    items.notice(permission() | at(clock, 0))
    items.observe(record("waiting", t + 0.1), True)
    items.notice(notification("permission_prompt") | at(clock, 6))
    # Esc first (the record leaves waiting), then the session goes away.
    items.observe(record("idle", t + 8), True)
    if ending == "exit":
        items.observe({}, True)
    elif ending == "clear":
        items.notice(hook("UserPromptSubmit", prompt="/clear") | at(clock, 8.5))
        items.observe(record("idle", t + 8.6, session="new-session-after-clear"), True)
    else:
        items.notice(hook("SessionEnd", reason="other") | at(clock, 8.5))
    clock.now += 10
    items.tick()
    assert [c["reason"] for c in closes(items)] == ["session_ended"]


def test_row6_an_unreadable_record_is_not_an_exit(store):
    items, clock = store
    items.notice(notification("idle_prompt"))
    items.observe(record("idle", clock.now - 1), True)
    items.observe({}, False)
    assert len(published(items)) == 1


def test_a_late_stop_of_the_previous_turn_leaves_the_new_prompt_open(store):
    items, clock = store
    items.notice(hook("UserPromptSubmit", prompt="next") | at(clock, 6))
    items.notice(permission() | at(clock, 7))
    items.notice(hook("Stop", last_assistant_message="Turn one done.") | at(clock, 5))
    items.notice(notification("permission_prompt") | at(clock, 13))
    [item] = published(items)
    assert item["detail"]["tool_name"] == "Bash"


def test_a_home_that_could_not_be_listed_hides_no_ended_session(tmp_path, monkeypatch):
    from openswap.worker.runtime import _session_records

    def homes(root, problems):
        problems.append("sessions: permission denied")
        return []

    monkeypatch.setattr(attention, "config_homes", homes)
    assert _session_records(tmp_path)() == ([], False)


def test_q3_a_background_task_turn_opens_a_new_idle_item(store):
    """Plan 025 S2 review Q3: a turn that ends while the session stays idle gets a new item."""
    items, clock = store
    items.notice(hook("Stop", last_assistant_message="Launched both.") | at(clock, 1))
    items.notice(notification("idle_prompt") | at(clock, 61))
    [first] = published(items)
    items.notice(hook("UserPromptSubmit", prompt="<task-notification>\n<task-id>x</task-id>") | at(clock, 90))
    items.notice(hook("Stop", last_assistant_message="Result is 42.") | at(clock, 95))
    [again] = published(items)
    assert again["id"] != first["id"] and again["detail"] == {"last_message": "Result is 42."}
    assert closes(items) == [{"id": first["id"], "reason": "superseded"}]
    items.notice(notification("idle_prompt") | at(clock, 155))  # that turn's own notice: the same item
    assert published(items) == [again]


def test_a_channel_turn_leaves_a_waiting_subagent_prompt_open(store):
    items, clock = store
    items.notice(permission(agent_id="a1") | at(clock, 0))
    items.notice(notification("permission_prompt") | at(clock, 6))
    items.notice(hook("UserPromptSubmit", prompt='<channel source="openswap">Continue</channel>') | at(clock, 9))
    assert len(published(items)) == 1


def test_row7_an_idle_item_after_an_interrupted_turn_has_no_older_message(store):
    items, clock = store
    items.notice(hook("UserPromptSubmit", prompt="first") | at(clock, 0))
    items.notice(hook("Stop", last_assistant_message="Old reply.") | at(clock, 5))
    items.notice(hook("UserPromptSubmit", prompt="second") | at(clock, 10))
    # Esc interrupts the second turn: no Stop. A late Stop of the first turn changes nothing.
    items.notice(hook("Stop", last_assistant_message="Late.") | at(clock, 4))
    items.notice(notification("idle_prompt") | at(clock, 72))
    [item] = published(items)
    assert "detail" not in item


def test_openswaps_own_channel_turn_does_not_close_the_idle_item(store):
    items, clock = store
    items.notice(permission() | at(clock, 0))
    items.notice(notification("permission_prompt") | at(clock, 6))
    items.notice(notification("idle_prompt") | at(clock, 7))
    items.notice(hook("UserPromptSubmit", prompt='<channel source="openswap">Continue</channel>') | at(clock, 9))
    items.observe(record("busy", clock.now + 9.1), True)  # the channel's turn runs
    clock.now += 10
    items.tick()
    assert [i["kind"] for i in published(items)] == ["idle"]
    items.notice(hook("UserPromptSubmit", prompt='<channel source="slack">hi</channel>') | at(clock, 10))
    assert published(items) == []


def test_session_records_are_read_from_every_home(tmp_path):
    home, other = tmp_path / "a", tmp_path / "b"
    (home / "sessions").mkdir(parents=True)
    (other / "sessions").mkdir(parents=True)
    (home / "sessions" / f"{os.getpid()}.json").write_text(json.dumps({
        "pid": os.getpid(), "sessionId": SESSION, "status": "waiting", "statusUpdatedAt": 1_800_000_000_500,
        "waitingFor": "permission prompt"}))
    (other / "sessions" / "999999.json").write_text(json.dumps({"pid": 999999, "sessionId": OTHER}))  # gone
    assert read_records([home, other, tmp_path / "missing"]) == (
        [SessionRecord(SESSION, os.getpid(), "waiting", 1_800_000_000.5, "permission prompt")], True)
    (other / "sessions" / "1.json").write_text("{torn")
    assert read_records([home, other])[1] is False


def test_another_call_of_the_same_tool_finishing_leaves_the_prompt_open(store):
    """A subagent's or a parallel call's PostToolUse carries the session's ID too."""
    items, _ = store
    items.notice(permission(command="needs approval"))
    items.notice(notification("permission_prompt"))
    items.notice(post(command="auto-approved elsewhere"))
    assert len(published(items)) == 1
    items.notice(post(command="needs approval"))
    assert published(items) == []


def test_a_gone_claude_process_closes_its_items(store):
    import subprocess

    items, _ = store
    ended = subprocess.Popen([sys.executable, "-c", "pass"])
    ended.wait()
    items.notice(notification("idle_prompt") | {"pid": ended.pid, "entrypoint": "cli"})
    items.notice(notification("idle_prompt", session=OTHER) | {"pid": os.getpid(), "entrypoint": "claude-vscode"})
    items.notice(notification("idle_prompt", session="no-pid-known"))
    items.observe([], True)
    left = published(items)
    assert [i["session_label"].split(" · ")[0] for i in left] == ["VS Code", "Claude Code"]
    assert [c["reason"] for c in closes(items)] == ["session_ended"]


def test_the_hook_reports_its_claude_code_process_and_surface(monkeypatch):
    monkeypatch.setattr(attention, "claude_pid", lambda: 4242)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    notice = hook("Stop")
    assert (notice["pid"], notice["entrypoint"]) == (4242, "cli")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "Bad Value!")
    assert "entrypoint" not in hook("Stop")


@pytest.mark.parametrize(("ps_out", "expected"), [
    (["  4242 /bin/sh\n", "  4243 /bin/zsh\n", "  1 claude\n"], 4243),  # its wrapper, then Claude Code's shell
    (["  4242 claude\n"], "parent"),  # run directly
    (["     1 /bin/sh\n"], 0),         # Claude Code already gone
    (["  4242 sh\n"] * 3, 0),          # shells all the way: cannot tell
    ([""], 0),                         # ps could not tell
    ([TimeoutError], 0),
])
def test_claude_pid_is_unknown_rather_than_a_shell(monkeypatch, ps_out, expected):
    import subprocess

    monkeypatch.setattr(attention, "claude_pid", REAL_CLAUDE_PID)

    def run(outputs):
        outputs = iter(outputs)

        def ps(*_args, **_kwargs):
            out = next(outputs)
            if out is TimeoutError:
                raise subprocess.TimeoutExpired("ps", 0.3)
            return subprocess.CompletedProcess([], 0, out, "")
        return ps

    monkeypatch.setattr(attention.subprocess, "run", run(ps_out))
    monkeypatch.delenv("CLAUDE_CODE_SHELL_PREFIX", raising=False)
    assert attention.claude_pid() == (os.getppid() if expected == "parent" else expected)
    monkeypatch.setattr(attention.subprocess, "run", run(ps_out))
    monkeypatch.setenv("CLAUDE_CODE_SHELL_PREFIX", "/usr/local/bin/my-wrapper")
    assert attention.claude_pid() == 0


def test_claude_pid_skips_the_shell_claude_code_ran_the_hook_through(monkeypatch):
    import subprocess

    monkeypatch.setattr(attention, "claude_pid", REAL_CLAUDE_PID)
    out = subprocess.run(["/bin/sh", "-c", f"/bin/sh -c \"{sys.executable} -c 'from openswap.attention import "
                          "claude_pid; print(claude_pid())'; true\"; true"], capture_output=True, text=True)
    assert int(out.stdout) == os.getpid()  # through the hook's wrapper and the shell Claude Code ran it in


def test_open_items_survive_a_restart_and_a_lost_file_reports_no_closes(tmp_path):
    clock = Clock()
    first = AttentionStore(tmp_path, clock=clock)
    first.notice(notification("idle_prompt"))
    [item] = published(first)
    again = AttentionStore(tmp_path, clock=clock)
    assert published(again) == [item]
    assert oct((tmp_path / "worker" / "attention.json").stat().st_mode & 0o777) == "0o600"
    (tmp_path / "worker" / "attention.json").write_text("{torn")
    empty = AttentionStore(tmp_path, clock=clock)
    assert published(empty) == [] and closes(empty) == []


def test_notices_are_dropped_while_switched_off(tmp_path):
    on = [False]
    items = AttentionStore(tmp_path, clock=Clock(), enabled=lambda: on[0])
    items.notice(notification("idle_prompt"))
    assert published(items) == []


@pytest.mark.parametrize("notice", [
    None, [], {"provider": "claude", "event": "Stop", "cwd": "/x"},
    {"provider": "claude", "event": "Bogus", "session_id": "s", "cwd": "/x"},
    {"provider": "claude", "event": "Stop", "session_id": "s", "cwd": "/x", "extra": 1},
    {"provider": "claude", "event": "Stop", "session_id": "s", "cwd": "/x", "last_message": "x" * 1001},
    {"provider": "claude", "event": "Stop", "session_id": "s", "cwd": "/x", "last_message": "bad\x00"},
    {"provider": "claude", "event": "PostToolUse", "session_id": "s", "cwd": "/x", "tool_key": "NOTHEX"},
    {"provider": "codex", "event": "Stop", "cwd": "/x"},
    {"provider": "codex", "event": "SubagentStop", "session_id": "t", "cwd": "/x"},
    {"provider": "codex", "event": "Stop", "session_id": "t", "cwd": "/x", "prompt_kind": "task"},
])
def test_malformed_notices_are_refused(notice):
    with pytest.raises(NoticeError):
        validate_notice(notice)


# --- the report body ------------------------------------------------------------


def test_report_follows_the_details_switch_and_folder_rules(store):
    items, _ = store
    items.notice(permission())
    items.notice(notification("permission_prompt"))
    with_details = items.report(notices=True, details=True, folder_label=lambda cwd: None)[0]["items"][0]
    without = items.report(notices=True, details=False, folder_label=lambda cwd: None)[0]["items"][0]
    approved = items.report(notices=True, details=False, folder_label=lambda cwd: "App")[0]["items"][0]
    assert with_details["folder_label"] == "app" and "detail" in with_details
    assert without["folder_label"] == "a session" and "detail" not in without
    assert approved["folder_label"] == "App"
    assert with_details["folder_id"] == without["folder_id"] and "/" not in with_details["folder_id"]
    assert items.report(notices=False, details=True, folder_label=lambda cwd: None)[0]["items"] == []
    attention_report({"worker_epoch": 1, **items.report(notices=True, details=True,
                                                         folder_label=lambda cwd: None)[0]})


def test_sessions_in_one_folder_get_their_own_opaque_ids(store):
    items, _ = store
    items.notice(notification("idle_prompt"))
    items.notice(notification("idle_prompt", session=OTHER))
    body = items.report(notices=True, details=True, folder_label=lambda cwd: None)[0]
    first, second = body["items"]
    assert first["folder_id"] == second["folder_id"]
    assert first["session_id"] != second["session_id"] and first["session_id"] != first["folder_id"]
    wire = json.dumps(body)
    assert SESSION not in wire and OTHER not in wire and "/Users/o/code/app" not in wire
    assert items.report(notices=True, details=True, folder_label=lambda cwd: None)[0]["items"][0]["session_id"] \
        == first["session_id"]


def test_report_keeps_the_newest_fifty_and_fits_the_body_limit(store):
    items, clock = store
    big = "☃" * 1000  # escaped as \\uXXXX: six bytes each
    for n in range(60):
        clock.now += 1
        items._items.append(Item(f"id{n}", "permission", "claude", f"s{n}", "/x", clock.now, True, "Bash", "",
                                 {"tool_name": "Bash", "description": big, "input_preview": big * 4,
                                  "last_message": big}))
    body = items.report(notices=True, details=True, folder_label=lambda cwd: None)[0]
    assert [i["id"] for i in body["items"]] == [f"id{n}" for n in range(10, 60)]
    assert len(json.dumps({"worker_epoch": 1, **body}).encode()) <= MAX_BODY
    assert "detail" not in body["items"][0] and "detail" in body["items"][-1]
    attention_report({"worker_epoch": 1, **body})


def test_unreported_closes_are_capped(store):
    from openswap.worker.attention import MAX_PENDING_CLOSES

    items, _ = store
    for _ in range(MAX_PENDING_CLOSES + 10):
        items.notice(notification("idle_prompt"))
        items.notice(hook("UserPromptSubmit"))
    assert len(items._closed) == MAX_PENDING_CLOSES


def test_acknowledged_closes_are_never_sent_again(store):
    items, _ = store
    items.notice(notification("idle_prompt"))
    items.notice(hook("UserPromptSubmit"))
    body, ids = items.report(notices=True, details=True, folder_label=lambda cwd: None)
    assert len(ids) == 1
    items.acknowledge(ids)
    assert closes(items) == []


def test_approved_remote_folder_label(tmp_path):
    from openswap.settings import WorkerWorkspace

    app, org = tmp_path / "app", tmp_path / "org"
    (app / "src").mkdir(parents=True)
    (org / "repo" / ".git").mkdir(parents=True)
    (org / "repo" / "pkg").mkdir()
    (org / "client-notes").mkdir()
    workspaces = (WorkerWorkspace("app", tmp_path / "out", label="My app", work_root=app),
                  WorkerWorkspace("org", tmp_path / "out2", work_root=org, repos=True))
    assert approved_folder_label(workspaces, str(app / "src")) == "My app"
    assert approved_folder_label(workspaces, str(org / "repo" / "pkg")) == "repo"
    # Not a repo: the approved folder's own label, never the private name inside it.
    assert approved_folder_label(workspaces, str(org / "client-notes")) == "org"
    assert approved_folder_label(workspaces, str(tmp_path)) is None


@pytest.mark.parametrize("mutate", [
    lambda b: b["items"].extend([b["items"][0]] * 50),
    lambda b: b["items"][0].update(session_label="x" * 101),
    lambda b: b["items"][0].update(kind="other"),
    lambda b: b["items"][0].update(folder_id="a/b"),
    lambda b: b["items"][0].update(session_id="x" * 65),
    lambda b: b["items"][0].update(detail={}),
    lambda b: b["items"][0].update(detail={"input_preview": "x" * 4001}),
    lambda b: b["items"][0].update(detail={"questions": [{"question": "q", "options": ["o"] * 5}]}),
    lambda b: b["items"][0].update(detail={"tool_name": "a\nb"}),
    lambda b: b["items"][0].update(actions=["allow", "allow"]),
    lambda b: b["closed"].append({"id": b["items"][0]["id"], "reason": "answered_on_mac"}),
    lambda b: b["closed"].append({"id": "x", "reason": "dismissed"}),
    lambda b: b.update(notices_enabled="yes"),
    lambda b: b["items"][0].update(kind=[]),
    lambda b: b["closed"].append({"id": "x", "reason": {}}),
])
def test_report_caps_and_shapes_are_enforced(mutate):
    body = {"worker_epoch": 1, "notices_enabled": True, "replies_enabled": False, "closed": [], "items": [{
        "id": "abc", "kind": "idle", "provider": "claude", "session_label": "Terminal · 3f2a",
        "folder_id": "f" * 32, "folder_label": "app", "created_at": "2026-11-06T10:00:00Z", "actions": [],
        "detail": {"last_message": "Done\nok"}}]}
    attention_report(json.loads(json.dumps(body)))
    mutate(body)
    with pytest.raises(ProtocolError):
        attention_report(body)


# --- IPC -------------------------------------------------------------------------


class _Control:
    def __init__(self):
        self.calls = []

    def status(self):
        from openswap.worker.models import (ProviderAvailability, RemoteConnectivity, WorkerProcessState,
                                            WorkerSnapshot)

        self.calls.append("status")
        return WorkerSnapshot(enabled=True, paused=False, process_state=WorkerProcessState.RUNNING,
                              remote_connectivity=RemoteConnectivity.DISABLED,
                              provider=ProviderAvailability(False, "x"), active_job=None, queue_depth=0)

    def stop(self, job_id):
        from openswap.worker.models import ControlResult

        self.calls.append("stop")
        return ControlResult(True, job_id=job_id)

    def set_paused(self, paused):
        from openswap.worker.models import ControlResult

        self.calls.append("pause")
        return ControlResult(True)


@pytest.fixture
def served(tmp_path):
    root = tmp_path / "root"
    path = ipc.socket_path(root)
    items = AttentionStore(root, clock=Clock())
    control = _Control()
    stop, ready = threading.Event(), threading.Event()
    thread = threading.Thread(target=ipc.serve, args=(path, control, stop),
                              kwargs={"ready_event": ready, "notices": items}, daemon=True)
    thread.start()
    assert ready.wait(2)
    yield root, path, items, control
    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    if path.parent.parent == Path("/tmp"):  # a long root's short socket folder
        path.parent.rmdir()


def test_a_hook_notice_reaches_the_store_and_gets_only_an_acknowledgement(served):
    root, path, items, _ = served
    attention.send_notice(dict(notification("idle_prompt")), backup_root=root)
    assert len(published(items)) == 1
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
        c.connect(str(path))
        c.sendall(json.dumps({"version": 1, "command": "notice", "notice": notification("idle_prompt")}).encode()
                  + b"\n")
        assert json.loads(c.recv(4096)) == {"version": 1, "ok": True, "result": {}}
        c2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    with c2:
        c2.connect(str(path))
        c2.sendall(b'{"version":1,"command":"notice","notice":{"provider":"x"}}\n')
        assert json.loads(c2.recv(4096))["error"] == "invalid_notice"


def test_held_and_waiting_connections_do_not_stall_status_pause_or_stop(served):
    from openswap.worker.client import WorkerClient

    _, path, _, control = served
    held = []
    for _ in range(5):
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.connect(str(path))
        c.sendall(b'{"version":1,')  # a long-lived connection mid-request
        held.append(c)
    try:
        client = WorkerClient(path)
        started = time.monotonic()
        client.status()
        client.set_paused(True)
        client.stop("job")
        assert time.monotonic() - started < 1
        assert control.calls == ["status", "pause", "stop"]
    finally:
        for c in held:
            c.close()


def test_connections_past_the_cap_are_closed_unread(served, monkeypatch):
    _, path, _, _ = served
    held = []
    try:
        for _ in range(ipc.MAX_CONNECTIONS):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(path))
            held.append(c)
        time.sleep(0.3)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as extra:
            extra.settimeout(2)
            extra.connect(str(path))
            assert extra.recv(10) == b""
    finally:
        for c in held:
            c.close()


# --- Claude Code settings.json --------------------------------------------------


EXE = "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap"


def _settings(home: Path) -> dict:
    return json.loads((home / "settings.json").read_text())


def _ours(home: Path) -> dict:
    """OpenSwap's hook command per event in ``home``."""
    return {event: hook["command"] for event, groups in _settings(home).get("hooks", {}).items()
            for group in groups for hook in group["hooks"] if attention.is_our_hook_command(hook["command"])}


def test_install_adds_only_openswap_hooks_and_removal_leaves_the_rest(tmp_path):
    home = tmp_path / ".claude"
    home.mkdir()
    theirs = {"matcher": "Bash", "hooks": [{"type": "command", "command": "~/bin/audit"}]}
    (home / "settings.json").write_text(json.dumps({
        "model": "opus", "statusLine": {"type": "command", "command": "x"},
        "hooks": {"PostToolUse": [theirs], "PreToolUse": [theirs]}}))
    assert attention.set_hooks(home, EXE) is True
    data = _settings(home)
    assert data["model"] == "opus" and data["hooks"]["PreToolUse"] == [theirs]
    assert data["hooks"]["PostToolUse"][0] == theirs
    ours = data["hooks"]["PostToolUse"][1]["hooks"][0]
    assert ours == {"type": "command", "command": attention.hook_command("PostToolUse", EXE), "async": True, "timeout": 10}
    assert data["hooks"]["Notification"][0]["matcher"] == "permission_prompt|idle_prompt"
    assert _ours(home) == {e: attention.hook_command(e, EXE) for e in attention.HOOK_EVENTS}
    assert attention.set_hooks(home, EXE) is False  # already there
    assert attention.set_hooks(home, "/other/openswap") is True  # repointed, not doubled
    assert len(_settings(home)["hooks"]["Stop"]) == 1
    assert attention.set_hooks(home, None) is True
    assert _settings(home) == {"model": "opus", "statusLine": {"type": "command", "command": "x"},
                               "hooks": {"PostToolUse": [theirs], "PreToolUse": [theirs]}}


def test_removal_keeps_other_hooks_sharing_a_group_with_ours(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    shared = {"hooks": [{"type": "command", "command": "~/bin/log"},
                        {"type": "command", "command": f"{EXE} attention Stop"}]}
    (home / "settings.json").write_text(json.dumps({"hooks": {"Stop": [shared]}}))
    attention.set_hooks(home, None)
    assert _settings(home) == {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "~/bin/log"}]}]}}


@pytest.mark.parametrize("content", ["{torn", "[]", json.dumps({"hooks": []}),
                                     json.dumps({"hooks": {"Stop": {"not": "a list"}}})])
def test_a_settings_file_that_cannot_be_read_is_left_alone(tmp_path, content):
    home = tmp_path / "h"
    home.mkdir()
    (home / "settings.json").write_text(content)
    with pytest.raises(ConfigError):
        attention.set_hooks(home, EXE)
    assert (home / "settings.json").read_text() == content


def test_hooks_write_through_a_linked_settings_file(tmp_path):
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}")
    home = tmp_path / "h"
    home.mkdir()
    (home / "settings.json").symlink_to(real)
    attention.set_hooks(home, EXE)
    assert (home / "settings.json").is_symlink() and "hooks" in json.loads(real.read_text())


def test_removal_never_rewrites_a_file_without_openswap_hooks(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    text = '{"hooks": {}, "model": "opus"}'
    (home / "settings.json").write_text(text)
    assert attention.set_hooks(home, None) is False
    assert (home / "settings.json").read_text() == text


def test_worker_start_takes_out_hooks_left_after_notices_went_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    claude = Path.home() / ".claude"
    claude.mkdir(exist_ok=True)
    attention.set_hooks(claude, EXE, backup_root=root)  # a removal that never finished
    assert attention.reconcile(root) == []
    assert not (claude / "settings.json").exists()  # OpenSwap created it for its hooks alone


def test_worker_start_leaves_the_hooks_when_the_switch_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    root.mkdir()
    (root / "settings.json").write_text("{torn")
    claude = Path.home() / ".claude"
    claude.mkdir(exist_ok=True)
    attention.set_hooks(claude, EXE)
    with pytest.raises(ConfigError):
        attention.reconcile(root)
    assert _ours(claude) != {}


def test_is_our_hook_command():
    assert attention.is_our_hook_command("openswap attention Stop")
    assert attention.is_our_hook_command(f"'{EXE}' attention Notification")
    assert attention.is_our_hook_command(attention.hook_command("Stop", EXE))
    # A newer build's event is OpenSwap's too (a downgrade must still take it out).
    assert attention.is_our_hook_command("openswap attention SomeLaterEvent")
    assert attention.is_our_hook_command(attention.hook_command("SomeLaterEvent", EXE))
    assert not attention.is_our_hook_command("~/bin/notify attention Stop")
    assert not attention.is_our_hook_command("openswap statusline")
    assert not attention.is_our_hook_command("/bin/sh -c 'x' ~/bin/notify attention Stop")


def test_config_homes_and_apply_cover_every_managed_home(tmp_path, monkeypatch):
    from openswap.claude_desktop import DesktopProfiles

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(attention, "program", lambda: EXE)
    root = tmp_path / "backup"
    shared = root / "sessions" / "2-a_x.com"
    alone = root / "sessions" / "3-b_x.com"
    shared.mkdir(parents=True)
    alone.mkdir(parents=True)
    claude = Path.home() / ".claude"
    claude.mkdir(exist_ok=True)
    (shared / "settings.json").symlink_to(claude / "settings.json")
    store = DesktopProfiles()
    profile = store.add("Work")
    code = store.directories(profile, create=True)[1]
    assert attention.config_homes(root) == [claude, shared, alone, code]
    problems: list = []
    assert attention.apply(root, True, problems=problems) and problems == []
    assert _ours(claude) == _ours(code) == _ours(alone) == {
        e: attention.hook_command(e, EXE) for e in attention.HOOK_EVENTS}
    # H8: a profile that does not share ~/.claude's settings gets a file of its
    # own, holding only OpenSwap's hooks, which sharing may replace later.
    assert attention.only_ours(alone / "settings.json") and not attention.only_ours(shared / "settings.json")
    assert attention.remove(root) is True
    # OpenSwap created each for its hooks alone: nothing else is in them.
    assert not any((home / "settings.json").exists() for home in (claude, code, alone))


def test_set_notices_saves_the_switch_and_reports_what_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(attention, "program", lambda: EXE)
    root = tmp_path / "backup"
    claude = Path.home() / ".claude"
    claude.mkdir(exist_ok=True)
    (claude / "settings.json").write_text("{torn")
    problems = attention.set_notices(root, True)
    assert load_attention_settings(root) == AttentionSettings(notices=True, details=True)
    assert len(problems) == 1 and str(claude) in problems[0]


# --- Codex (plan 025 D5: Codex hooks; required outcomes C1-C14) -----------------------

# Shapes as Codex 0.166.0-alpha.6 sent them (live run, 2026-11-06).
THREAD = "01a1b26a-eaa7-7221-8332-da938297142e"
SUBAGENT = "01a1b26b-2ce2-7d81-bfc2-1b055006921a"


def codex(event, thread=THREAD, **fields):
    data = {"session_id": thread, "transcript_path": None, "cwd": "/Users/o/code/app", "hook_event_name": event,
            "model": "gpt-5.1", "permission_mode": "default", **fields}
    notice = attention._codex_notice(event, json.dumps(data).encode())
    return None if notice is None else validate_notice(notice)


def codex_permission(command="touch marker", thread=THREAD, **extra):
    return codex("PermissionRequest", thread, turn_id="t1", tool_name="Bash",
                 tool_input={"command": command, "description": "Create a marker file?"}, **extra)


def codex_post(command="touch marker", thread=THREAD, **extra):
    return codex("PostToolUse", thread, turn_id="t1", tool_name="Bash", tool_use_id="call_4",
                 tool_input={"command": command}, tool_response="", **extra)


def codex_stop(message="Codex mock reply 7", thread=THREAD):
    return codex("Stop", thread, turn_id="t1", stop_hook_active=False, last_assistant_message=message)


class Answers:
    """Codex's log as ``read_codex_answers`` reads it: thread -> each answer a
    person gave, (row, time). ``at[thread] = t`` logs one more answer."""

    def __init__(self):
        self.rows, self.fail = {}, False
        self.at = _Log(self.rows)

    def __call__(self, threads):
        if self.fail:
            raise sqlite3.OperationalError("no such table: logs")
        return {t: list(rows) for t, rows in self.rows.items() if t in threads}


class _Log:
    def __init__(self, rows):
        self.rows = rows

    def __setitem__(self, thread, at):
        rows = self.rows.setdefault(thread, [])
        rows.append((f"logs_2.sqlite:{sum(map(len, self.rows.values()))}", at))


@pytest.fixture
def codex_store(tmp_path):
    clock, answers = Clock(), Answers()
    return AttentionStore(tmp_path, clock=clock, codex_answers=answers), clock, answers


def wait(items, clock, seconds):
    clock.now += seconds
    items.refresh()
    items.tick()


def test_c1_an_approval_still_open_after_six_seconds_is_published(codex_store):
    items, clock, _ = codex_store
    items.notice(codex_permission())
    wait(items, clock, 5.9)
    assert published(items) == []
    wait(items, clock, 0.1)
    [item] = published(items)
    assert (item["kind"], item["provider"], item["session_label"]) == (
        "permission", "codex", f"Codex · {items.session_id(items._items[0])[:4]}")
    assert item["detail"] == {"tool_name": "Bash", "description": "Create a marker file?"}


@pytest.mark.parametrize("signal", ["interrupt", "log"])
def test_c2_a_decline_within_six_seconds_never_pushes(codex_store, signal):
    items, clock, answers = codex_store
    items.notice(codex_permission())
    if signal == "interrupt":  # Esc, or "No", at Codex's prompt
        items.notice(codex("Interrupt", turn_id="t1") | {"at": START + 2})
    else:
        answers.at[THREAD] = START + 2  # operation="exec_approval" (decision: Cancel) in Codex's log
    wait(items, clock, 10)
    assert published(items) == [] and closes(items) == []


def test_c2_an_answer_after_the_push_closes_it_as_answered_on_the_mac(codex_store):
    items, clock, answers = codex_store
    items.notice(codex_permission())
    wait(items, clock, 7)
    [item] = published(items)
    answers.at[THREAD] = START + 9
    wait(items, clock, 3)
    assert published(items) == [] and closes(items) == [{"id": item["id"], "reason": "answered_on_mac"}]


def test_c2_an_answer_to_an_earlier_prompt_does_not_close_a_later_one(codex_store):
    items, clock, answers = codex_store
    answers.at[THREAD] = START - 1
    items.notice(codex_permission())
    wait(items, clock, 7)
    assert len(published(items)) == 1


def test_c2_without_codex_log_the_item_closes_at_the_threads_next_event(codex_store):
    items, clock, answers = codex_store
    answers.fail = True
    items.notice(codex_permission())
    wait(items, clock, 7)
    [item] = published(items)
    items.notice(codex_stop() | {"at": START + 20})
    assert closes(items) == [{"id": item["id"], "reason": "answered_on_mac"}]


def test_codex_log_answers_are_read_by_thread_from_codex_log_database(tmp_path):
    from openswap.worker.attention import read_codex_answers

    home = tmp_path / "codex"
    home.mkdir()
    db = sqlite3.connect(home / "logs_2.sqlite")
    db.execute("create table logs (id integer primary key, ts integer, ts_nanos integer, level text, target text,"
               " feedback_log_body text, thread_id text)")
    body = ('session_loop{{thread_id={t}}}: Submission thread_id={t} submission_id=x operation="{op}" '
            'op=ExecApproval {{ id: "call_4" }}')
    db.executemany("insert into logs (ts, ts_nanos, level, target, feedback_log_body, thread_id) values (?,?,?,?,?,?)", [
        (int(START) + 3, 75_000_000, "DEBUG", "codex_core::session::handlers", body.format(t=THREAD, op="exec_approval"),
         THREAD),
        (int(START) + 4, 0, "DEBUG", "codex_core::session::handlers", body.format(t=OTHER, op="turn_input"), OTHER),
        (int(START) - 600, 0, "DEBUG", "codex_core::session::handlers", body.format(t=OTHER, op="patch_approval"),
         OTHER),
    ])
    db.commit()
    db.close()
    answers = read_codex_answers([home, tmp_path / "no-codex"], {THREAD: START, OTHER: START})
    assert answers == {THREAD: [(f"{home / 'logs_2.sqlite'}:1", pytest.approx(START + 3.075))]}


@pytest.mark.parametrize("schema", ["create table logs (id integer primary key, ts integer)",
                                    "create table other (x text)", None])
def test_c2_an_unexpected_codex_log_is_no_signal(tmp_path, schema):
    from openswap.worker.attention import read_codex_answers

    home = tmp_path / "codex"
    home.mkdir()
    if schema is None:
        (home / "logs_2.sqlite").write_bytes(b"not a database")
    else:
        db = sqlite3.connect(home / "logs_2.sqlite")
        db.execute(schema)
        db.commit()
        db.close()
    before = (home / "logs_2.sqlite").read_bytes()
    clock = Clock()
    items = AttentionStore(tmp_path / "root", clock=clock,
                           codex_answers=lambda threads: read_codex_answers([home], threads))
    items.notice(codex_permission())
    wait(items, clock, 7)
    assert len(published(items)) == 1 and closes(items) == []  # published normally, nothing guessed
    assert (home / "logs_2.sqlite").read_bytes() == before  # read-only
    items.notice(codex_stop() | {"at": START + 8})
    assert [c["reason"] for c in closes(items)] == ["answered_on_mac"]


def _transcript(tmp_path, reviewer, turn="t1"):
    path = tmp_path / "rollout.jsonl"
    lines = [{"type": "session_meta", "payload": {"thread_source": "user"}},
             {"type": "turn_context", "payload": {"turn_id": "t0", "approvals_reviewer": "user"}},
             {"type": "turn_context", "payload": {"turn_id": turn, "approvals_reviewer": reviewer}}]
    path.write_text("".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines))
    return str(path)


def test_c3_approvals_no_person_is_asked_for_open_nothing(tmp_path, codex_store):
    assert codex_permission(permission_mode="bypassPermissions") is None  # approval_policy = "never"
    assert codex_permission(transcript_path=_transcript(tmp_path, "auto_review")) is None
    assert codex_permission(transcript_path=_transcript(tmp_path, "user")) is not None
    assert codex_permission(transcript_path=str(tmp_path / "gone.jsonl")) is not None  # cannot tell: a person
    # The owner's own PermissionRequest hook allowing it: the call runs at once.
    items, clock, _ = codex_store
    items.notice(codex_permission())
    items.notice(codex_post() | {"at": START + 0.2})
    wait(items, clock, 10)
    assert published(items) == [] and closes(items) == []


@pytest.mark.parametrize("answer", ["post", "stop", "prompt"])
def test_c4_the_threads_next_event_closes_its_approval(codex_store, answer):
    items, clock, _ = codex_store
    items.notice(codex_permission())
    wait(items, clock, 7)
    [item] = published(items)
    event = {"post": codex_post(), "stop": codex_stop(),
             "prompt": codex("UserPromptSubmit", turn_id="t2", prompt="next")}[answer]
    items.notice(event | {"at": START + 8})
    assert closes(items) == [{"id": item["id"], "reason": "answered_on_mac"}]


def test_c4_events_from_another_thread_or_call_never_close_it(codex_store):
    items, clock, _ = codex_store
    items.notice(codex_permission())
    wait(items, clock, 7)
    for event in (codex_post(thread=OTHER), codex_stop(thread=OTHER), codex("Interrupt", OTHER, turn_id="x"),
                  codex_post(command="touch other")):
        items.notice(event | {"at": START + 8})
    assert len(published(items)) == 1 and closes(items) == []


def test_c5_a_finished_turn_is_pushed_only_after_sixty_quiet_seconds(codex_store):
    items, clock, _ = codex_store
    items.notice(codex_stop())
    wait(items, clock, 59)
    assert published(items) == []
    wait(items, clock, 1)
    [item] = published(items)
    assert (item["kind"], item["detail"]) == ("finished", {"last_message": "Codex mock reply 7"})


def test_c5_a_prompt_inside_the_window_closes_it_without_a_push(codex_store):
    items, clock, _ = codex_store
    items.notice(codex_stop())
    items.notice(codex("UserPromptSubmit", turn_id="t2", prompt="more") | {"at": START + 30})
    wait(items, clock, 120)
    assert published(items) == [] and closes(items) == []


def test_c6_typing_again_closes_the_finished_item(codex_store):
    items, clock, _ = codex_store
    items.notice(codex_stop())
    wait(items, clock, 61)
    [item] = published(items)
    items.notice(codex("UserPromptSubmit", turn_id="t2", prompt="more") | {"at": START + 70})
    assert closes(items) == [{"id": item["id"], "reason": "answered_on_mac"}]


def test_c7_items_end_with_the_codex_process_never_as_answered(codex_store):
    import subprocess

    items, clock, _ = codex_store
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    items.notice(codex_permission() | {"pid": gone.pid})
    items.notice(codex_stop(thread=OTHER) | {"pid": os.getpid()})
    wait(items, clock, 61)
    [alive] = published(items)
    assert alive["kind"] == "finished"
    assert closes(items) == []  # the ended process's approval was never pushed
    items.notice(codex("SessionEnd", OTHER, reason="other"))
    assert closes(items) == [{"id": alive["id"], "reason": "session_ended"}]


def test_c7_every_codex_item_expires_after_a_day(codex_store):
    items, clock, _ = codex_store
    items.notice(codex_permission())
    wait(items, clock, 7)
    [item] = published(items)
    wait(items, clock, 24 * 3600)
    assert closes(items) == [{"id": item["id"], "reason": "expired"}]


def test_c8_a_subagents_approval_is_its_own(codex_store):
    items, clock, answers = codex_store
    items.notice(codex_permission(agent_id=SUBAGENT, agent_type="default"))
    items.notice(codex_stop() | {"at": START + 1})  # the owner's thread ends its turn
    wait(items, clock, 61)
    kinds = sorted(i["kind"] for i in published(items))
    assert kinds == ["finished", "permission"]
    answers.at[SUBAGENT] = START + 62  # answered on the subagent's own thread
    wait(items, clock, 1)
    assert [i["kind"] for i in published(items)] == ["finished"]


def test_c9_a_finished_turn_needs_no_recorded_prompt(tmp_path):
    clock = Clock()
    first = AttentionStore(tmp_path, clock=clock)
    first.notice(codex("UserPromptSubmit", turn_id="t1", prompt="go"))
    restarted = AttentionStore(tmp_path, clock=clock)  # the worker restarted mid-turn, or `codex resume`
    restarted.notice(codex_stop() | {"at": START + 5})
    clock.now += 70
    restarted.tick()
    assert [i["kind"] for i in published(restarted)] == ["finished"]


def test_codex_hook_entry_point_sends_the_notice(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(attention, "send_notice", sent.append)
    stdin = json.dumps({"session_id": THREAD, "cwd": "/r", "hook_event_name": "Stop", "turn_id": "t",
                        "last_assistant_message": "Done"}).encode()
    code, took = _run_main(monkeypatch, ["codex", "Stop"], stdin)
    assert (code, capsys.readouterr()) == (0, ("", "")) and took < 1
    assert [{k: v for k, v in n.items() if k != "at"} for n in sent] == [
        {"provider": "codex", "event": "Stop", "session_id": THREAD, "cwd": "/r", "last_message": "Done"}]


def test_codex_hook_hash_is_the_one_codex_records():
    command = ("SPIKE_DIR=/private/tmp/claude-501/-Users-yohan-GitHub-opensoft-openswap--claude-worktrees-p025-s1-"
               "attention-notices-b693e7/cc37aaa0-a1a7-4a50-8b25-82920f08b375/scratchpad/cx/run /usr/bin/python3 "
               "/private/tmp/claude-501/-Users-yohan-GitHub-opensoft-openswap--claude-worktrees-p025-s1-attention-"
               "notices-b693e7/cc37aaa0-a1a7-4a50-8b25-82920f08b375/scratchpad/cx/hooklog.py")
    entry = {"type": "command", "command": command, "timeout": 5, "async": False}
    assert attention.codex_hook_hash("PermissionRequest", entry) == (
        "sha256:c538785d39f38f0963b9ce062a82a0e3f6b1c6b1deb69a3fb526c14c495e1c7f")
    assert attention.codex_hook_hash("SessionEnd", entry | {"timeout": 3}) == (
        "sha256:01c58ee1bca1e201c5359aa65d532556cbbb0b55bcf71ca126e109fb507116d4")


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(attention, "CODEX_MANAGED_FILES", (tmp_path / "etc" / "requirements.toml",))
    monkeypatch.setattr(attention, "CODEX_MANAGED_PLISTS", (tmp_path / "etc" / "codex.plist",))
    monkeypatch.setattr(attention, "program", lambda: EXE)
    return home, tmp_path / "backup"


def _trust(home, root):
    """What Codex writes once the owner chooses "Trust all and continue"."""
    data = json.loads((home / "hooks.json").read_text())
    lines = []
    for event in attention.CODEX_HOOK_EVENTS:
        entry = attention.codex_hook_entry(event, attention.codex_launcher(root))
        for g, group in enumerate(data["hooks"][event]):
            if group["hooks"] == [entry]:
                label = {"PermissionRequest": "permission_request", "PostToolUse": "post_tool_use", "Stop": "stop",
                         "UserPromptSubmit": "user_prompt_submit", "Interrupt": "interrupt",
                         "SessionEnd": "session_end"}[event]
                lines.append(f'[hooks.state."{home / "hooks.json"}:{label}:{g}:0"]\n'
                             f'trusted_hash = "{attention.codex_hook_hash(event, entry)}"\n')
    with open(home / "config.toml", "a") as handle:
        handle.write("\n" + "\n".join(lines))


def test_c10_settings_shows_codex_trust_state_from_codex_files(codex_home, tmp_path):
    home, root = codex_home
    assert attention.codex_hooks_status(home, root) == "missing"
    attention.apply(root, True)
    assert attention.codex_hooks_status(home, root) == "review"
    _trust(home, root)
    assert attention.codex_hooks_status(home, root) == "trusted"
    text = (home / "config.toml").read_text()
    (home / "config.toml").write_text(text.replace('"\n', '"\nenabled = false\n', 1))
    assert attention.codex_hooks_status(home, root) == "disabled"
    (home / "config.toml").write_text("[features]\nhooks = false\n" + text)
    assert attention.codex_hooks_status(home, root) == "off"
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "requirements.toml").write_text("allow_managed_hooks_only = true\n")
    assert attention.codex_hooks_status(home, root) == "blocked"
    (tmp_path / "etc" / "requirements.toml").write_text("[torn\n")
    assert attention.codex_hooks_status(home, root) == "unknown"
    assert attention.codex_hooks_status(tmp_path / "no-codex", root) is None


def test_c10_settings_rows_say_what_codex_will_do():
    from openswap.menubar_display import CODEX_HOOKS_STATUS, remote_attention_rows

    rows = remote_attention_rows({"attention": {"notices": True, "details": True, "codex": "review"}}, enabled=True)
    assert rows[-1]["id"] == "remote_attention_codex" and "Hooks need review" in rows[-1]["label"]
    assert set(CODEX_HOOKS_STATUS) == {"trusted", "review", "review_others", "disabled", "off", "blocked", "missing",
                                       "unknown"}
    rows = remote_attention_rows({"attention": {"notices": False, "details": True, "codex": "review"}}, enabled=True)
    assert all(r["id"] != "remote_attention_codex" for r in rows)


def test_c11_entries_stay_byte_identical_across_updates_moves_and_builds(codex_home, monkeypatch):
    home, root = codex_home
    attention.apply(root, True)
    before = (home / "hooks.json").read_bytes()
    for moved in ("/Users/o/Downloads/OpenSwap.app/Contents/MacOS/OpenSwap", "/Users/o/dev/.venv/bin/openswap"):
        monkeypatch.setattr(attention, "program", lambda moved=moved: moved)
        attention.apply(root, True)
        assert (home / "hooks.json").read_bytes() == before
        assert moved in attention.codex_launcher(root).read_text()


def test_the_launcher_runs_the_current_program_with_codex_input(codex_home, tmp_path, monkeypatch):
    import subprocess

    home, root = codex_home
    record = tmp_path / "ran.json"
    fake = tmp_path / "openswap"
    fake.write_text(f"#!{sys.executable}\nimport json, sys\n"
                    f"open({str(record)!r}, 'w').write(json.dumps([sys.argv[1:], sys.stdin.read()]))\n")
    fake.chmod(0o755)
    monkeypatch.setattr(attention, "program", lambda: str(fake))
    attention.apply(root, True)
    command = json.loads((home / "hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
    subprocess.run(["/bin/sh", "-c", command], input="{}", text=True, check=True)
    assert json.loads(record.read_text()) == [["attention", "codex", "Stop"], "{}"]
    fake.unlink()  # a moved app: the hook ends quietly
    assert subprocess.run(["/bin/sh", "-c", command], input="{}", text=True, check=False).returncode == 0


def test_c12_installing_keeps_every_trusted_user_hook_in_its_place(codex_home):
    home, root = codex_home
    theirs = {"hooks": [{"type": "command", "command": "python3 ~/.codex/hooks/audit.py", "timeout": 30}]}
    original = {"description": "mine", "hooks": {"PermissionRequest": [{"matcher": "Bash", **theirs}],
                                                 "Stop": [theirs, theirs]}}
    (home / "hooks.json").write_text(json.dumps(original))
    state = (f'[hooks.state."{home / "hooks.json"}:stop:1:0"]\n'
             'trusted_hash = "sha256:theirs"\n')
    (home / "config.toml").write_text(state)
    attention.apply(root, True)
    data = json.loads((home / "hooks.json").read_text())
    assert data["description"] == "mine"
    assert data["hooks"]["PermissionRequest"][0] == {"matcher": "Bash", **theirs}
    assert data["hooks"]["Stop"][:2] == [theirs, theirs]
    assert state in (home / "config.toml").read_text()


@pytest.mark.parametrize("before", ["", "[features]\nhooks = true\n", "[features]\nhooks = false\nplugins = true\n",
                                    "model = \"o3\"\n\n[features]\nmemories = true\n",
                                    "[features]\ncodex_hooks = false\n", "[features]\nhooks = false # mine\n"])
def test_c13_removal_puts_the_hooks_feature_back_exactly(codex_home, before):
    home, root = codex_home
    if before:
        (home / "config.toml").write_text(before)
    attention.apply(root, True, owner=True)
    assert attention.codex_hooks_on(tomllib.loads((home / "config.toml").read_text() if before else ""))
    attention.remove(root)
    assert ((home / "config.toml").read_text() if before else not (home / "config.toml").exists()) in (before, True)
    assert not (home / "hooks.json").exists() and not attention.codex_launcher(root).exists()


def test_c13_the_feature_stays_on_while_hooks_added_since_remain(codex_home):
    home, root = codex_home
    (home / "config.toml").write_text("[features]\nhooks = false\n")
    attention.apply(root, True, owner=True)
    theirs = {"hooks": [{"type": "command", "command": "~/bin/log"}]}
    data = json.loads((home / "hooks.json").read_text())
    data["hooks"]["Stop"].append(theirs)  # the owner adds a hook of their own since
    (home / "hooks.json").write_text(json.dumps(data))
    attention.remove(root)
    assert json.loads((home / "hooks.json").read_text()) == {"hooks": {"Stop": [theirs]}}
    assert tomllib.loads((home / "config.toml").read_text())["features"]["hooks"] is True


def test_c13_turning_notices_back_on_shows_whether_codex_asks_again(codex_home):
    home, root = codex_home
    attention.apply(root, True)
    _trust(home, root)
    attention.remove(root)
    attention.apply(root, True)
    assert attention.codex_hooks_status(home, root) == "trusted"  # Codex kept its record: no screen
    text = (home / "config.toml").read_text()
    (home / "config.toml").write_text(text[:text.index("[hooks.state")])
    assert attention.codex_hooks_status(home, root) == "review"  # Settings says the screen comes back


def test_c14_managed_codex_files_are_never_written(codex_home, tmp_path):
    home, root = codex_home
    with pytest.raises(ConfigError):
        attention.install_codex_hooks(Path("/etc/codex"), root, owner=True)
    (home / "config.toml").write_text("[features]\nhooks = false\n")
    before = {p for p in tmp_path.rglob("*")}
    attention.apply(root, True, owner=True)
    written = {p for p in tmp_path.rglob("*")} - before
    assert {p for p in written if p.is_file()} <= {home / "hooks.json", home / "config.toml",
                                                   attention.codex_launcher(root), root / attention.CODEX_STATE_FILE,
                                                   root / attention.HOOKS_RECORD}


@pytest.mark.parametrize("text", ["[features\n", 'features.hooks = false\n'])
def test_a_config_this_cannot_rewrite_is_left_alone(codex_home, text):
    home, root = codex_home
    (home / "config.toml").write_text(text)
    problems: list = []
    attention.apply(root, True, owner=True, problems=problems)
    assert len(problems) == 1 and problems[0].startswith("Codex:")
    assert (home / "config.toml").read_text() == text


# --- settings -------------------------------------------------------------------------


def test_attention_switches_default_off_and_fail_closed(tmp_path):
    assert load_attention_settings(tmp_path) == AttentionSettings(notices=False, details=True)
    write_attention_settings(tmp_path, notices=True)
    write_attention_settings(tmp_path, details=False)
    assert load_attention_settings(tmp_path) == AttentionSettings(notices=True, details=False)
    raw = json.loads((tmp_path / "settings.json").read_text())
    raw["worker"]["attention"] = {"notices": "yes"}
    (tmp_path / "settings.json").write_text(json.dumps(raw))
    assert load_attention_settings(tmp_path) == AttentionSettings(notices=False, details=False, readable=False)


def test_settings_rows_show_the_switches_on_a_paired_mac():
    from openswap.menubar_display import remote_attention_rows

    assert remote_attention_rows({}, enabled=True) == []
    rows = remote_attention_rows({"attention": {"notices": False, "details": True}}, enabled=True)
    assert [(r["id"], r.get("value"), r.get("disabled")) for r in rows[1:]] == [
        ("remote_attention_notices", False, None), ("remote_attention_details", True, True),
        ("remote_attention_replies", False, True)]
    rows = remote_attention_rows({"attention": {"notices": True, "details": True}}, enabled=False)
    assert rows[-1]["id"] == "remote_attention_note"
    rows = remote_attention_rows({"attention": {"notices": True, "details": True, "replies": True,
                                                "launcher": "/Apps/O S.app/Contents/MacOS/OpenSwap claude"}},
                                 enabled=True)
    assert rows[-1]["id"] == "remote_attention_replies_note"
    assert "start Claude with /Apps/O S.app/Contents/MacOS/OpenSwap claude instead" in rows[-1]["label"]


@pytest.mark.parametrize(("on_path", "launcher"), [
    (["bin"], "openswap claude"),
    ([], "full"),
    (["bin", "old"], "full"),
    (["old", "bin"], "full"),
])
def test_settings_name_the_short_command_only_when_every_openswap_runs_this_copy(
        tmp_path, monkeypatch, on_path, launcher):
    exe = tmp_path / "O S.app" / "Contents" / "MacOS" / "OpenSwap"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "openswap").symlink_to(exe)
    (tmp_path / "old").mkdir()
    (tmp_path / "old" / "openswap").write_text("#!/bin/sh\n")
    (tmp_path / "old" / "openswap").chmod(0o755)
    for name in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "UV_INSTALL_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join([str(exe.parent), *(str(tmp_path / d) for d in on_path)]))
    monkeypatch.setattr(attention.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(attention, "program", lambda: str(exe))
    expected = "openswap claude" if launcher != "full" else f"'{exe}' claude"
    assert attention.claude_launcher() == expected


def test_settings_name_the_short_command_for_an_install_on_path(monkeypatch):
    monkeypatch.setattr(attention, "program", lambda: "openswap")
    assert attention.claude_launcher() == "openswap claude"


@pytest.mark.parametrize(("paired", "notices", "shown"), [(True, False, True), (False, False, False),
                                                           (False, True, True)])
def test_the_switch_stays_reachable_on_an_unpaired_mac_while_on(paired, notices, shown):
    from openswap.menubar_display import MenuBarSettings, settings_page_rows

    rows = settings_page_rows(MenuBarSettings(), strategy="x", threshold=90, worker_paired=paired,
                              worker_status={"attention": {"notices": notices, "details": True}})
    assert any(r["id"] == "remote_attention_notices" for r in rows) is shown


# --- the worker report and the reference service ----------------------------------------


class _Transport:
    def __init__(self, service, key):
        self.service, self.key, self.calls = service, key, []

    def request(self, operation, data):
        self.calls.append(operation)
        return self.service.request(operation, data, self.key)


@pytest.fixture
def reporting(tmp_path):
    from openswap.worker.leases import stable_account_identity
    from openswap.worker.remote import RemoteClient
    from openswap.worker.runtime import WorkerRuntime

    service = ControlStore(tmp_path / "service" / "db")
    paired = service.request("pair", {"code": service.issue_code()})
    root = tmp_path / "local"
    update_worker_settings(root, enabled=True)
    configure_worker_service(root, "http://127.0.0.1:8765")
    write_attention_settings(root, notices=True)
    runtime = WorkerRuntime(root, account_identity=stable_account_identity("codex", "synthetic"))
    runtime.attention = AttentionStore(root, clock=Clock())
    transport = _Transport(service, paired["device_key"])
    client = RemoteClient(runtime, "http://127.0.0.1:8765", paired["device_key"], transport=transport)
    client.tick()
    return client, runtime.attention, service, paired["worker_id"], transport, root


def test_the_worker_reports_the_whole_set_and_each_close_once(reporting):
    client, items, service, worker, transport, _ = reporting
    assert "attention" in service.capabilities(worker)
    client.sync_attention()
    assert service.attention(worker) == {"notices_enabled": True, "replies_enabled": False, "items": [],
                                         "closed": {}, "replies": []}
    items.notice(permission())
    items.notice(notification("permission_prompt"))
    client.sync_attention()
    [item] = service.attention(worker)["items"]
    calls = len(transport.calls)
    client.sync_attention()
    assert len(transport.calls) == calls  # nothing changed: nothing sent
    items.notice(post())
    client.sync_attention()
    assert service.attention(worker)["items"] == []
    assert service.attention(worker)["closed"] == {item["id"]: "answered_on_mac"}
    assert closes(items) == []


def test_turning_notices_off_reports_an_empty_set_and_drops_the_items(reporting):
    client, items, service, worker, _, root = reporting
    items.notice(notification("idle_prompt"))
    client.sync_attention()
    write_attention_settings(root, notices=False)
    client.sync_attention()
    assert service.attention(worker) == {"notices_enabled": False, "replies_enabled": False, "items": [],
                                         "closed": {}, "replies": []}
    write_attention_settings(root, notices=True)
    client.sync_attention()
    assert service.attention(worker)["items"] == []


def test_a_new_registration_keeps_the_report_and_the_worker_sends_it_again(reporting):
    client, items, service, worker, transport, _ = reporting
    items.notice(notification("idle_prompt"))
    client.sync_attention()
    [item] = service.attention(worker)["items"]
    old_epoch = client.worker_epoch
    client.worker_epoch = None
    client.tick()
    assert client.worker_epoch == old_epoch + 1
    assert service.attention(worker)["items"] == [item]  # a restart is not a new item
    sent = transport.calls.count("attention")
    client.sync_attention()
    assert transport.calls.count("attention") == sent + 1
    assert service.attention(worker)["items"] == [item]
    with pytest.raises(ProtocolError) as stale:
        service.request("attention", {"worker_epoch": old_epoch, "notices_enabled": True, "replies_enabled": False,
                                      "items": [], "closed": []}, client.transport.key)
    assert stale.value.code == "stale_epoch"


def test_a_backend_without_the_extension_is_not_asked_again(reporting):
    client, items, _, _, transport, _ = reporting

    class NoAttention:
        def __init__(self, inner):
            self.inner, self.calls = inner, []

        def request(self, operation, data):
            self.calls.append(operation)
            if operation == "attention":
                raise ProtocolError("unsupported_version", 404)
            return self.inner.request(operation, data)

    client.transport = NoAttention(transport)
    items.notice(notification("idle_prompt"))
    client.sync_attention()
    items.notice(hook("UserPromptSubmit"))
    client.sync_attention()
    assert client.transport.calls == ["attention"]
    assert len(closes(items)) == 1  # kept until a service accepts it


def test_the_reference_service_refuses_a_worker_that_did_not_advertise_it(tmp_path):
    service = ControlStore(tmp_path / "db")
    paired = service.request("pair", {"code": service.issue_code()})
    key = paired["device_key"]
    epoch = service.request("register", {}, key)["worker_epoch"]
    body = {"worker_epoch": epoch, "notices_enabled": True, "replies_enabled": False, "items": [], "closed": []}
    with pytest.raises(ProtocolError) as refused:
        service.request("attention", body, key)
    assert refused.value.code == "invalid_request"
    service.request("capabilities", {"worker_epoch": epoch, "features": ["attention"]}, key)
    assert service.request("attention", body, key) == {"item_count": 0}
    service.revoke(paired["worker_id"])
    assert service.attention(paired["worker_id"]) is None


def test_the_reference_service_lists_reports(tmp_path, capsys):
    from openswap.worker.refserver.cli import main as refserver_main

    database = tmp_path / "private" / "db"
    service = ControlStore(database)
    paired = service.request("pair", {"code": service.issue_code()})
    key = paired["device_key"]
    epoch = service.request("register", {}, key)["worker_epoch"]
    service.request("capabilities", {"worker_epoch": epoch, "features": ["attention"]}, key)
    item = {"id": "abc", "kind": "idle", "provider": "claude", "session_label": "Terminal · 3f2a",
            "folder_id": "f" * 32, "folder_label": "app", "created_at": "2026-11-06T10:00:00Z", "actions": []}
    service.request("attention", {"worker_epoch": epoch, "notices_enabled": True, "replies_enabled": False,
                                  "items": [item], "closed": []}, key)
    assert refserver_main(["attention", "--database", str(database)]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[paired["worker_id"]]["items"] == [item]


def test_the_attention_loop_drops_items_once_notices_are_off_even_offline(reporting):
    client, items, _, _, _, root = reporting
    items.notice(notification("idle_prompt"))
    client.state = "offline"
    write_attention_settings(root, notices=False)
    client.stop_event = threading.Event()
    thread = threading.Thread(target=client._attention_loop, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and items._items:
            time.sleep(0.05)
        assert items._items == []
    finally:
        client.stop_event.set()
        thread.join(timeout=3)


def test_the_attention_loop_reports_a_notice_within_a_second(reporting, monkeypatch):
    client, items, service, worker, _, _ = reporting
    client.stop_event = threading.Event()
    thread = threading.Thread(target=client._attention_loop, daemon=True)
    thread.start()
    try:
        time.sleep(0.2)
        items.notice(notification("idle_prompt"))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (service.attention(worker) or {}).get("items"):
            time.sleep(0.05)
        assert len(service.attention(worker)["items"]) == 1
    finally:
        client.stop_event.set()
        thread.join(timeout=3)


def test_a_notice_waits_for_the_client_to_come_online_then_goes_at_once(reporting):
    client, items, service, worker, _, _ = reporting
    client.state = "offline"
    client.stop_event = threading.Event()
    thread = threading.Thread(target=client._attention_loop, daemon=True)
    thread.start()
    try:
        items.notice(notification("idle_prompt"))
        time.sleep(1)
        assert not (service.attention(worker) or {}).get("items")
        client.state = "online"
        deadline = time.monotonic() + 1.5  # well before the next 5-second pass
        while time.monotonic() < deadline and not (service.attention(worker) or {}).get("items"):
            time.sleep(0.05)
        assert len(service.attention(worker)["items"]) == 1
    finally:
        client.stop_event.set()
        thread.join(timeout=3)


def test_a_configured_worker_reports_notices(reporting):
    from datetime import datetime, timedelta, timezone

    from openswap.worker.pairing import Enrollment
    from openswap.worker.remote_state import ConfiguredRemote

    client, items, service, worker, transport, _ = reporting
    enrollment = Enrollment(worker, transport.key, datetime.now(timezone.utc) + timedelta(days=30))
    configured = ConfiguredRemote(client.runtime, client_factory=lambda *_a, **_k: client,
                                  enrollment_loader=lambda _url: enrollment)
    stop = threading.Event()
    thread = threading.Thread(target=configured.run, args=(stop,), daemon=True)
    items.notice(notification("idle_prompt"))  # waiting before the worker has a client
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not (service.attention(worker) or {}).get("items"):
            time.sleep(0.05)
        assert len(service.attention(worker)["items"]) == 1
    finally:
        stop.set()
        thread.join(timeout=10)


# --- S1 review round (plan 025, 025-review-s1.md) ------------------------------------


def _recorded(root):
    return json.loads((root / attention.HOOKS_RECORD).read_text())


def test_h1_removal_covers_every_recorded_file_found_or_not(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(attention, "program", lambda: EXE)
    root = tmp_path / "backup"
    gone_home = tmp_path / "dropped-profile"  # a home no longer discovered (registry unreadable, profile dropped)
    gone_home.mkdir()
    (gone_home / "settings.json").write_text('{"model": "opus"}')
    attention.set_hooks(gone_home, EXE, backup_root=root)
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    attention.set_hooks(unreadable, EXE, backup_root=root)
    monkeypatch.setattr(attention, "config_homes", lambda *_a, **_k: [])
    (unreadable / "settings.json").chmod(0)
    with pytest.raises(ConfigError, match="unreadable"):  # named; uninstall does not report the step done
        attention.remove(root)
    assert _settings(gone_home) == {"model": "opus"}
    assert list(_recorded(root)) == [str(unreadable / "settings.json")]  # kept for the next try
    (unreadable / "settings.json").chmod(0o600)
    assert attention.remove(root) is True
    assert not (root / attention.HOOKS_RECORD).exists()


def test_h1_an_unreadable_record_is_named_never_read_as_nothing_to_do(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    (root / "worker").mkdir(parents=True)
    (root / attention.HOOKS_RECORD).write_text("{torn")
    with pytest.raises(ConfigError, match="record"):
        attention.remove(root)


def test_h2_removal_never_touches_a_file_openswap_never_wrote(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    root = tmp_path / "backup"
    claude = Path.home() / ".claude"
    claude.mkdir(exist_ok=True)
    torn = '{"hooks": {"Stop": [{"hooks": [{"command": "openswap attention Stop"}]}]'  # torn, not ours to judge
    (claude / "settings.json").write_text(torn)
    (tmp_path / "codex" / "hooks.json").write_text("[not codex's form]")
    assert attention.remove(root) is False
    assert (claude / "settings.json").read_text() == torn
    assert (tmp_path / "codex" / "hooks.json").read_text() == "[not codex's form]"


def test_h2_a_downgrade_takes_out_a_newer_builds_hooks_too(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    home = tmp_path / "h"
    home.mkdir()
    attention.set_hooks(home, EXE, backup_root=root)
    data = _settings(home)
    data["hooks"]["SomeLaterEvent"] = [{"hooks": [{"type": "command",
                                                   "command": attention.hook_command("SomeLaterEvent", EXE)}]}]
    data["model"] = "opus"
    (home / "settings.json").write_text(json.dumps(data))
    attention.remove(root)
    assert _settings(home) == {"model": "opus"}


def test_h2_the_codex_feature_is_put_back_with_none_of_openswaps_entries_left(codex_home):
    home, root = codex_home
    (home / "config.toml").write_text("[features]\nhooks = false\n")
    attention.apply(root, True, owner=True)
    (home / "hooks.json").unlink()  # the owner deleted hooks.json; OpenSwap's record of the feature remains
    attention.remove(root)
    assert (home / "config.toml").read_text() == "[features]\nhooks = false\n"


def test_h3_removal_writes_through_a_linked_hooks_file_and_never_unlinks_it(codex_home, tmp_path):
    home, root = codex_home
    real = tmp_path / "dotfiles" / "hooks.json"
    real.parent.mkdir()
    real.write_text("{}")
    (home / "hooks.json").symlink_to(real)
    attention.apply(root, True)
    assert "Stop" in json.loads(real.read_text())["hooks"]
    attention.remove(root)
    assert (home / "hooks.json").is_symlink() and json.loads(real.read_text()) == {"hooks": {}}


def test_h3_a_file_the_owner_replaced_since_is_never_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    home = tmp_path / "h"
    home.mkdir()
    attention.set_hooks(home, EXE, backup_root=root)  # created by OpenSwap
    text = (home / "settings.json").read_text()
    (home / "settings.json").unlink()
    (home / "settings.json").write_text(text)  # the owner's own file now, with the same content
    attention.remove(root)
    assert (home / "settings.json").exists() and _settings(home) == {}


def test_h3_settings_reads_trust_under_the_resolved_codex_home(codex_home, tmp_path, monkeypatch):
    home, root = codex_home
    linked = tmp_path / "codex-link"
    linked.symlink_to(home)
    monkeypatch.setenv("CODEX_HOME", str(linked))
    attention.apply(root, True)
    _trust(home, root)  # Codex keys trust by the resolved home (measured: hooks/list)
    assert attention.codex_hooks_status(linked, root) == "trusted"
    text = (home / "config.toml").read_text()
    (home / "config.toml").write_text(text.replace(str(home), str(linked)))  # an unresolved key is not Codex's
    assert attention.codex_hooks_status(linked, root) == "review"


def _shifted(home, root, state_after):
    """hooks.json with OpenSwap's Stop entry, then the owner's own Stop hook
    after it, which taking OpenSwap's out moves from group 1 to group 0."""
    attention.apply(root, True)
    data = json.loads((home / "hooks.json").read_text())
    theirs = {"type": "command", "command": "~/bin/log", "timeout": 10, "async": False}
    data["hooks"]["Stop"].append({"hooks": [theirs]})
    (home / "hooks.json").write_text(json.dumps(data))
    source = os.path.join(os.path.realpath(home), "hooks.json")
    trusted = attention.codex_hook_hash("Stop", theirs)
    lines = [f'[hooks.state."{source}:stop:1:0"]\ntrusted_hash = "{trusted}"\n']
    if state_after:
        lines.append(f'[hooks.state."{source}:stop:0:0"]\n{state_after}\n')
    (home / "config.toml").write_text("\n".join(lines))
    return theirs


def test_h4_the_worker_never_moves_the_owners_codex_hooks(codex_home):
    home, root = codex_home
    theirs = _shifted(home, root, "")
    write_attention_settings(root, notices=False)
    problems = attention.reconcile(root)
    assert any("left in hooks.json" in p for p in problems)
    assert json.loads((home / "hooks.json").read_text())["hooks"]["Stop"][1] == {"hooks": [theirs]}
    assert attention.codex_launcher(root).exists()  # an entry left in place still has something to run


def test_h4_the_owner_is_told_which_codex_hooks_codex_asks_about_again(codex_home):
    home, root = codex_home
    theirs = _shifted(home, root, "")
    notes = attention.set_notices(root, False)
    assert notes == ["Codex will ask you to review your hook again (Stop: ~/bin/log)"]
    assert json.loads((home / "hooks.json").read_text())["hooks"]["Stop"] == [{"hooks": [theirs]}]


def test_h4_no_removal_lets_a_codex_hook_gain_trust(codex_home):
    home, root = codex_home
    hooked = {"type": "command", "command": "~/bin/log", "timeout": 10, "async": False}
    # The record at the position it would move to trusts this very hook: never moved onto it.
    theirs = _shifted(home, root, f'trusted_hash = "{attention.codex_hook_hash("Stop", hooked)}"\nenabled = true')
    problems = attention.set_notices(root, False)
    assert any("left in hooks.json" in p for p in problems)
    assert json.loads((home / "hooks.json").read_text())["hooks"]["Stop"][1] == {"hooks": [theirs]}


@pytest.mark.parametrize(("record_after", "note"), [
    ('trusted_hash = "sha256:openswaps-own"', "ask you to review your hook again"),  # OpenSwap's trusted record
    ("enabled = false", "turn your hook off"),  # OpenSwap's hook turned off in /hooks
])
def test_h4_the_owner_is_told_how_codex_treats_a_moved_hook(codex_home, record_after, note):
    home, root = codex_home
    theirs = _shifted(home, root, record_after)
    notes = attention.set_notices(root, False)
    assert notes == [f"Codex will {note} (Stop: ~/bin/log)"]
    assert json.loads((home / "hooks.json").read_text())["hooks"]["Stop"] == [{"hooks": [theirs]}]


def test_h5_only_the_owners_ok_turns_codex_hooks_on_when_other_hooks_would_start(codex_home):
    home, root = codex_home
    (home / "config.toml").write_text("[features]\nhooks = false\n")
    theirs = {"hooks": [{"type": "command", "command": "~/bin/audit"}]}
    (home / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [theirs]}}))
    attention.set_notices(root, True)
    assert not attention.codex_hooks_on(tomllib.loads((home / "config.toml").read_text()))
    assert attention.codex_consent_needed(root) == ["Stop: ~/bin/audit"]
    assert attention.enable_codex_hooks(root) == []
    assert attention.codex_hooks_on(tomllib.loads((home / "config.toml").read_text()))
    assert attention.codex_consent_needed(root) == []
    attention.set_notices(root, False)  # only OpenSwap's added since: put back
    assert (home / "config.toml").read_text() == "[features]\nhooks = false\n"


def test_h5_the_worker_never_turns_codex_hooks_on(codex_home):
    home, root = codex_home
    (home / "config.toml").write_text("[features]\nhooks = false\n")
    write_attention_settings(root, notices=True)
    assert attention.reconcile(root) == []
    assert (home / "config.toml").read_text() == "[features]\nhooks = false\n"
    assert (home / "hooks.json").exists()  # the entries go in; the feature is the owner's to turn on


def test_h5_settings_never_advises_trusting_all_while_other_hooks_wait(codex_home):
    from openswap.menubar_display import CODEX_HOOKS_STATUS

    home, root = codex_home
    theirs = {"hooks": [{"type": "command", "command": "~/bin/audit"}]}
    (home / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [theirs]}}))
    attention.apply(root, True)
    assert attention.codex_hooks_status(home, root) == "review_others"
    assert "don't choose" in CODEX_HOOKS_STATUS["review_others"]


@pytest.mark.parametrize(("config", "on"), [
    ({}, True), ({"features": {"hooks": False}}, False), ({"features": {"codex_hooks": False}}, False),
    ({"features": {"codex_hooks": False, "hooks": True}}, True), ({"features": {"codex_hooks": True, "hooks": False}},
                                                                   False)])
def test_h6_codex_hooks_follow_hooks_then_the_legacy_key_then_codexs_default(config, on):
    assert attention.codex_hooks_on(config) is on  # measured: Codex 0.166 `codex features list`


def test_h6_settings_shows_the_legacy_key_turning_hooks_off(codex_home):
    home, root = codex_home
    attention.apply(root, True)
    _trust(home, root)
    text = (home / "config.toml").read_text()
    (home / "config.toml").write_text("[features]\ncodex_hooks = false\n" + text)
    assert attention.codex_hooks_status(home, root) == "off"


def test_h7_a_hook_whose_program_is_missing_or_fails_exits_quietly(tmp_path):
    import subprocess

    failing = tmp_path / "old-openswap"
    failing.write_text("#!/bin/sh\necho 'usage: openswap ...' >&2\nexit 2\n")
    failing.chmod(0o755)
    for program in (str(tmp_path / "gone" / "openswap"), str(failing), "openswap-not-on-path"):
        for event in ("PermissionRequest", "question"):
            ran = subprocess.run(["/bin/sh", "-c", attention.hook_command(event, program)], input="{}",
                                 capture_output=True, text=True)
            assert (ran.returncode, ran.stdout, ran.stderr) == (0, "", "")


def test_h7_settings_names_hooks_that_point_at_a_missing_program(tmp_path, monkeypatch):
    from openswap.menubar_display import remote_attention_rows

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    home = tmp_path / "h"
    home.mkdir()
    attention.set_hooks(home, "/Applications/Gone.app/Contents/MacOS/OpenSwap", backup_root=root)
    assert attention.recorded_programs(root) == ["/Applications/Gone.app/Contents/MacOS/OpenSwap"]
    rows = remote_attention_rows({"attention": {"notices": True, "details": True,
                                                "missing": attention.recorded_programs(root)}}, enabled=True)
    assert any(r["id"] == "remote_attention_missing" for r in rows)


def test_h8_a_profile_added_while_the_worker_runs_gets_hooks_and_none_after_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(attention, "program", lambda: EXE)
    root = tmp_path / "backup"
    write_attention_settings(root, notices=True)
    profile = root / "sessions" / "4-c_x.com"
    profile.mkdir(parents=True)
    assert attention.reconcile(root, installs_only=True) == []  # the worker's pass every 30 s
    assert attention.only_ours(profile / "settings.json")
    write_attention_settings(root, notices=False)
    late = root / "sessions" / "5-d_x.com"
    late.mkdir()
    assert attention.reconcile(root, installs_only=True) == []
    assert not (late / "settings.json").exists()  # no home gets hooks after the switch went off


def test_h8_sharing_replaces_a_file_of_only_openswaps_hooks_and_unsharing_keeps_them(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from openswap.models import Platform
    from openswap.session import SessionManager

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(attention, "program", lambda: EXE)
    root = tmp_path / "backup"
    write_attention_settings(root, notices=True)
    claude = Path.home() / ".claude"
    claude.mkdir(exist_ok=True)
    attention.set_hooks(claude, EXE, backup_root=root)
    profile = root / "sessions" / "2-a_x.com"
    profile.mkdir(parents=True)
    attention.set_hooks(profile, EXE, backup_root=root)
    manager = SessionManager.__new__(SessionManager)
    manager.switcher = SimpleNamespace(platform=Platform.MACOS, backup_dir=root)
    manager._logger = SimpleNamespace(warning=lambda *_: None)
    monkeypatch.setattr(SessionManager, "_sync_mcp_servers", lambda *_a: None)
    manager._sync_sharing(profile, True)
    assert (profile / "settings.json").is_symlink() and _ours(profile) == _ours(claude) != {}
    manager._sync_sharing(profile, False)
    assert not (profile / "settings.json").is_symlink() and _ours(profile) == _ours(claude)


class _Refusing:
    """A control service that refuses ``attention`` reports ``when(data)`` says
    (every one by default) with ``status`` and ``code``."""

    def __init__(self, transport, status, code, when=None):
        self.transport, self.status, self.code, self.sent = transport, status, code, []
        self.when = when or (lambda data: True)

    def request(self, operation, data):
        if operation == "attention":
            self.sent.append(data)
            if self.status is not None and self.when(data):
                raise ProtocolError(self.code, self.status)
        return self.transport.request(operation, data)


def test_w1_a_forbidden_report_waits_long_says_why_and_resumes(reporting, monkeypatch):
    from openswap.worker import remote
    from openswap.worker.attention import read_problems

    client, items, service, worker, transport, root = reporting
    refusing = client.transport = _Refusing(transport, 403, "forbidden")
    items.notice(notification("idle_prompt"))
    client.sync_attention()
    client.sync_attention()  # within the backoff: not asked again
    assert len(refusing.sent) == 1
    assert "refuses session notices" in read_problems(root)[0]
    refusing.status = None
    client._attention_shape.retry_at = 0.0  # the 15 minutes have passed
    client.sync_attention()
    assert len(service.attention(worker)["items"]) == 1 and read_problems(root) == []
    assert remote.ATTENTION_FORBIDDEN_SECONDS >= 600


def test_w1_a_report_refused_as_too_large_shrinks(reporting):
    client, items, service, worker, transport, _ = reporting
    refusing = client.transport = _Refusing(transport, 413, "body_too_large")
    for n in range(20):
        items.notice(hook("Stop", session=f"s{n}", last_assistant_message="é" * 900) | {"at": START - 1})
        items.notice(notification("idle_prompt", session=f"s{n}"))
    client.sync_attention()
    first = len(json.dumps(refusing.sent[-1]))
    refusing.when = lambda data: len(json.dumps(data)) > first // 4
    for _ in range(4):
        client.sync_attention()
    report = service.attention(worker)
    assert report is not None and len(report["items"]) == 20  # details went; every item still did


def test_w1_one_items_refused_detail_is_repaired_and_the_rest_and_closes_go(reporting):
    client, items, service, worker, transport, _ = reporting
    items.notice(hook("Stop", session=OTHER, last_assistant_message="fine") | {"at": START - 1})
    items.notice(notification("idle_prompt", session=OTHER))
    items.notice(notification("idle_prompt"))
    client.sync_attention()
    items.notice(hook("Stop", last_assistant_message="poison") | {"at": START + 1})
    items.notice(notification("idle_prompt") | {"at": START + 2})  # the same idle item, with that reply
    items.notice(hook("UserPromptSubmit", session=OTHER, prompt="typed") | {"at": START + 3})  # a close waits
    refusing = client.transport = _Refusing(transport, 400, "invalid_request", lambda data: any(
        e.get("detail", {}).get("last_message") == "poison" for e in data["items"]))
    for _ in range(6):
        client.sync_attention()
    report = service.attention(worker)
    assert len(report["items"]) == 1 and "detail" not in report["items"][0]  # repaired, not dropped
    assert sorted(report["closed"].values()) == ["answered_on_mac", "superseded"]
    assert len(refusing.sent) < 6


def test_w1_closes_get_through_a_service_that_refuses_every_item(reporting):
    client, items, service, worker, transport, _ = reporting
    items.notice(notification("idle_prompt"))
    client.sync_attention()
    [shown] = service.attention(worker)["items"]
    items.notice(hook("UserPromptSubmit", prompt="typed") | {"at": START + 1})
    items.notice(notification("idle_prompt", session=OTHER) | {"at": START + 2})
    client.transport = _Refusing(transport, 400, "invalid_request", lambda data: bool(data["items"]))
    for _ in range(4):
        client.sync_attention()
    assert service.attention(worker)["closed"] == {shown["id"]: "answered_on_mac"}


def test_w2_one_session_in_two_processes_keeps_each_ones_items(store):
    items, clock = store
    a, b = os.getpid(), os.getppid()
    items.observe([SessionRecord(SESSION, a, "idle", clock.now - 5), SessionRecord(SESSION, b, "idle", clock.now - 5)],
                  True)
    items.notice(notification("idle_prompt") | {"pid": a})
    items.notice(hook("UserPromptSubmit", prompt="typed in the other terminal") | {"pid": b, "at": clock.now + 1})
    assert len(published(items)) == 1  # b's prompt never answers a's item
    items.notice(hook("UserPromptSubmit", prompt="typed") | {"pid": 0, "at": clock.now + 2})
    assert len(published(items)) == 1  # an unknown process speaks for neither while both run


def test_w2_an_item_of_no_known_process_closes_only_when_every_copy_agrees(store):
    items, clock = store
    a, b = os.getpid(), os.getppid()
    both = [SessionRecord(SESSION, a, "waiting", clock.now - 1), SessionRecord(SESSION, b, "waiting", clock.now - 1)]
    items.observe(both, True)
    items.notice(permission() | at(clock, 0))
    items.notice(notification("permission_prompt") | at(clock, 6))
    items.observe([SessionRecord(SESSION, a, "busy", clock.now + 8), both[1]], True)
    clock.now += 10
    items.tick()
    assert len(published(items)) == 1
    items.observe([SessionRecord(SESSION, a, "busy", clock.now), SessionRecord(SESSION, b, "idle", clock.now)], True)
    clock.now += 10
    items.tick()
    assert published(items) == []


def test_w2_a_reused_pid_is_not_the_items_process(store, monkeypatch):
    items, clock = store
    items._starts[os.getpid()] = "Thu Nov  5 09:00:00 2026"  # what the process that ran the hook began at
    items.notice(notification("idle_prompt") | {"pid": os.getpid()})
    assert len(published(items)) == 1
    items._starts[os.getpid()] = "Fri Nov  6 10:00:00 2026"  # the PID now runs another process
    items.observe([], True)
    assert published(items) == [] and [c["reason"] for c in closes(items)] == ["session_ended"]
    stale = SessionRecord(OTHER, os.getpid(), "waiting", clock.now, proc_start="Thu Nov  5 09:00:00 2026")
    items.observe([stale], True)
    assert items._records == {}  # a session file whose PID now runs another process says nothing


@pytest.mark.parametrize(("status_at", "shown"), [(-30, True), (8, False)])
def test_w3_a_status_counts_as_the_answer_only_when_set_after_the_call(store, status_at, shown):
    items, clock = store
    t = clock.now
    items.notice(permission() | at(clock, 0))
    items.observe(record("idle", t + status_at), True)  # before the dialog (a lagging watcher), or after it
    items.notice(notification("permission_prompt") | at(clock, 6))
    assert len(published(items)) == (1 if shown else 0)


def test_w4_a_subagent_prompt_closes_when_that_subagent_ends(store):
    items, clock = store
    sub = "a101aadecfe40f562"
    items.observe(record("waiting", clock.now - 1, waiting_for="permission prompt"), True)
    items.notice(permission(agent_id=sub) | at(clock, 0))
    items.notice(permission(command="b", agent_id="a77bdeec2513e90cd") | at(clock, 0.01))
    items.notice(notification("permission_prompt") | at(clock, 6))
    items.notice(hook("Stop") | at(clock, 0.1))  # the main thread's turn: says nothing about them
    items.notice(hook("SubagentStop", agent_id="aba2869f8fa98be6f") | at(clock, 0.2))  # a helper agent
    assert len(published(items)) == 1
    items.notice(hook("SubagentStop", agent_id=sub) | at(clock, 20))  # "No" on its prompt ended it
    assert published(items) == [] and [c["reason"] for c in closes(items)] == ["answered_on_mac"]


def test_w4_a_brief_busy_between_queued_dialogs_closes_none_while_one_waits(store):
    items, clock = store
    t = clock.now
    items.notice(permission(command="a", agent_id="sub1") | at(clock, 0))
    items.notice(permission(command="b", agent_id="sub2") | at(clock, 0.1))
    items.notice(notification("permission_prompt") | at(clock, 6))
    items.notice(notification("permission_prompt") | at(clock, 6.1))
    items.observe(record("busy", t + 20), True)
    items.observe(record("waiting", t + 20.5, waiting_for="permission prompt"), True)  # the queued one shows
    clock.now += 25
    items.tick()
    assert len(published(items)) == 2
    items.observe(record("idle", t + 40), True)  # nothing waits when the grace ends: both are gone
    clock.now += 25
    items.tick()
    assert published(items) == []


def test_w5_each_logged_codex_answer_closes_one_approval_once(codex_store):
    items, clock, answers = codex_store
    items.notice(codex_permission(command="a") | {"at": START})
    items.notice(codex_permission(command="b") | {"at": START + 0.1})
    wait(items, clock, 7)
    assert len(published(items)) == 2
    answers.at[THREAD] = START + 8  # one answer
    wait(items, clock, 0.5)
    wait(items, clock, 0.5)  # read again: the same row never closes the other
    assert len(published(items)) == 1
    answers.at[THREAD] = START + 9
    wait(items, clock, 0.5)
    assert published(items) == []


def test_w6_closes_past_one_report_all_reach_the_service_with_their_reasons(reporting):
    client, items, service, worker, _, _ = reporting
    for n in range(60):
        items.notice(notification("idle_prompt", session=f"s{n}"))
    client.sync_attention()
    shown = [i["id"] for i in service.attention(worker)["items"]]
    assert len(shown) == 50
    for n in range(60):
        items.notice(hook("SessionEnd", session=f"s{n}"))
    assert items.changed.is_set()
    for _ in range(3):
        client.sync_attention()
    assert service.attention(worker)["items"] == []
    assert set(service.attention(worker)["closed"]) == set(shown)  # kept though they left the list a report earlier


def test_w7_an_unknown_switch_is_ignored_and_an_unreadable_file_wipes_nothing(reporting):
    client, items, service, worker, _, root = reporting
    raw = json.loads((root / "settings.json").read_text())
    raw["worker"]["attention"]["later_switch"] = True  # written by a newer build
    (root / "settings.json").write_text(json.dumps(raw))
    assert load_attention_settings(root).notices is True
    write_attention_settings(root, details=False)
    assert json.loads((root / "settings.json").read_text())["worker"]["attention"]["later_switch"] is True
    items.notice(notification("idle_prompt"))
    items.notice(notification("idle_prompt", session=OTHER))
    client.sync_attention()
    (root / "settings.json").write_text("{torn")
    items.notice(hook("SessionEnd", session=OTHER))
    client.sync_attention()
    assert len(items._items) == 1  # never wiped for that reason
    report = service.attention(worker)
    assert len(report["items"]) == 1 and report["items"][0]["actions"] == [] and "detail" not in report["items"][0]
    assert list(report["closed"].values()) == ["session_ended"]  # the waiting close still went
    from openswap.worker.attention import read_problems

    assert "can't read its settings" in read_problems(root)[0]


def test_w8_state_is_synced_and_only_a_file_that_parses_wrong_is_set_aside(tmp_path, monkeypatch):
    from openswap.worker import attention as worker_attention

    first = AttentionStore(tmp_path, clock=Clock())
    first.notice(notification("idle_prompt"))
    saved = (tmp_path / worker_attention.STATE_FILE).read_text()
    real_read = Path.read_text

    def flaky(self, *args, **kwargs):
        if self.name == "attention.json":
            raise PermissionError("passing")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky)
    again = AttentionStore(tmp_path, clock=Clock())
    again.notice(notification("idle_prompt", session=OTHER))  # not taken while the file cannot be read
    assert not again.ready
    monkeypatch.setattr(Path, "read_text", real_read)
    assert (tmp_path / worker_attention.STATE_FILE).read_text() == saved
    again.refresh()
    assert again.ready and len(published(again)) == 1 and again._secret == first._secret
    (tmp_path / worker_attention.STATE_FILE).write_text("{torn")
    fresh = AttentionStore(tmp_path, clock=Clock())
    assert fresh.ready and list(tmp_path.glob("worker/attention.json.unreadable-*"))


def test_w8_the_state_file_is_synced_before_it_replaces_the_old_one(tmp_path, monkeypatch):
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd)))
    AttentionStore(tmp_path, clock=Clock()).notice(notification("idle_prompt"))
    assert synced


def test_w9_text_with_a_lone_surrogate_is_replaced_never_dropped(store, tmp_path):
    items, _ = store
    raw = ('{"session_id": "%s", "cwd": "/Users/o/\\ud800app", "hook_event_name": "Stop", '
           '"last_assistant_message": "done \\udc00 here"}' % SESSION).encode()
    items.notice(validate_notice(attention._hook_notice("Stop", raw)))
    items.notice(validate_notice({**notification("idle_prompt"), "cwd": "/Users/o/\ud800app"}))
    [item] = published(items)
    assert item["detail"]["last_message"] == "done � here"
    json.dumps(item).encode("utf-8")
    items._items[0].detail = {"last_message": "saved \udc00 by an older build"}
    [item] = published(items)
    assert item["detail"]["last_message"] == "saved � by an older build"


def test_minor_the_session_label_never_shows_the_session_id(store):
    items, _ = store
    items.notice(notification("idle_prompt"))
    [item] = published(items)
    assert SESSION[:4] not in item["session_label"] and item["session_label"].endswith(item["session_id"][:4])


def test_minor_nothing_is_read_while_notices_are_off_and_nothing_is_open(tmp_path):
    reads = []
    items = AttentionStore(tmp_path, clock=Clock(), enabled=lambda: False,
                           records=lambda: (reads.append(1), ([], True))[1])
    items.refresh()
    assert reads == []


def test_minor_a_rewrite_keeps_the_files_mode_and_a_trailing_comment(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[features]\nhooks = false # mine\n")
    path.chmod(0o640)
    attention._write_text(path, attention._feature_text(path.read_text(), True))
    assert path.read_text() == "[features]\nhooks = true # mine\n" and path.stat().st_mode & 0o777 == 0o640


def test_minor_status_pause_and_stop_keep_their_slots(served):
    from openswap.worker.client import WorkerClient

    _, path, _, control = served
    held = []
    try:
        for _ in range(ipc.MAX_CONNECTIONS - ipc.CONTROL_RESERVED):
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.connect(str(path))
            held.append(c)
        time.sleep(0.3)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
            c.connect(str(path))
            c.sendall(json.dumps({"version": 1, "command": "notice", "notice": notification("idle_prompt")}).encode()
                      + b"\n")
            assert json.loads(c.recv(4096))["error"] == "busy"  # a reserved slot serves only control calls
        WorkerClient(path).status()
        assert control.calls == ["status"]
    finally:
        for c in held:
            c.close()


def test_w2_a_process_start_in_another_locale_is_not_compared(tmp_path):
    home = tmp_path / "h"
    (home / "sessions").mkdir(parents=True)
    for started, kept in (("Fri Nov  6 17:31:25 2026", "Fri Nov 6 17:31:25 2026"), ("ven  6 nov 17:31:25 2026", "")):
        (home / "sessions" / f"{os.getpid()}.json").write_text(json.dumps(
            {"pid": os.getpid(), "sessionId": SESSION, "status": "idle", "procStart": started}))
        [found], _ = read_records([home])
        assert found.proc_start == kept


# --- review of the fix round (Opus seats) ----------------------------------------


def test_review_a_proxys_error_page_never_reshapes_the_report(reporting, monkeypatch):
    from openswap.worker import remote

    client, items, _, _, _, _ = reporting
    items.notice(notification("idle_prompt"))
    client._attention_refused(items, ProtocolError("service_unavailable", 400), {"items": [{"id": "x", "detail": {}}]})
    assert client._attention_shape.plain == set() and not client._attention_shape.closes_only

    from io import BytesIO
    from urllib.error import HTTPError

    page = HTTPError("https://x", 502, "Bad Gateway", {}, BytesIO(b"<html>Bad Gateway</html>"))
    transport = remote.Transport("https://control.example")
    monkeypatch.setattr(transport.opener, "open", lambda *a, **k: (_ for _ in ()).throw(page))
    with pytest.raises(ProtocolError) as raised:
        transport.request("attention", {})
    assert (raised.value.status, raised.value.code) == (502, "service_unavailable")  # not a 400 refusal


def test_review_a_refusal_while_one_item_is_tried_blames_it_only_if_it_was_its_detail(reporting):
    client, items, service, worker, transport, _ = reporting
    items.notice(hook("Stop", session=OTHER, last_assistant_message="fine") | {"at": START - 1})
    items.notice(notification("idle_prompt", session=OTHER))
    items.notice(hook("Stop", last_assistant_message="also fine") | {"at": START - 1})
    items.notice(notification("idle_prompt"))
    # The service refuses labels it cannot take, never any detail.
    refusing = client.transport = _Refusing(transport, 400, "invalid_request", lambda data: any(
        e["session_label"] != "Session" for e in data["items"]))
    for _ in range(12):
        client.sync_attention()
    refusing.status = None
    client._attention_shape.retry_at = 0.0
    client.sync_attention()
    report = service.attention(worker)
    assert all("detail" in e for e in report["items"])  # no item lost its detail over a label refusal


def test_review_an_unreadable_state_file_says_so_in_settings(tmp_path, monkeypatch):
    from openswap.worker import attention as worker_attention

    AttentionStore(tmp_path, clock=Clock()).notice(notification("idle_prompt"))
    real_read = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError("root-owned"))
                        if self.name == "attention.json" else real_read(self, *a, **k))
    store = AttentionStore(tmp_path, clock=Clock())
    assert not store.ready and "can't read its record" in worker_attention.read_problems(tmp_path)[0]
    assert not store.register_channel(None, None, None)  # nothing taken that a later load would overwrite


def test_review_recorded_programs_survives_an_owners_odd_hook_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    home = tmp_path / "h"
    home.mkdir()
    gone = str(tmp_path / "Gone.app" / "openswap")
    attention.set_hooks(home, gone, backup_root=root)
    data = _settings(home)
    data["hooks"]["Odd"] = [{"hooks": ["echo hi"]}, {"hooks": None}, "x"]
    (home / "settings.json").write_text(json.dumps(data))
    assert attention.recorded_programs(root) == [gone]


def test_review_an_empty_profile_settings_file_is_the_owners(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{}")
    assert not attention.only_ours(path)


def test_review_a_failed_write_keeps_a_file_openswap_created_deletable(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    home = tmp_path / "h"
    home.mkdir()
    attention.set_hooks(home, EXE, backup_root=root)
    real = attention._write_text

    def full(path, text, **kwargs):
        if path.name == "settings.json":
            raise OSError(28, "No space left on device")
        return real(path, text, **kwargs)

    monkeypatch.setattr(attention, "_write_text", full)
    with pytest.raises(OSError):
        attention.set_hooks(home, "/elsewhere/openswap", backup_root=root)
    monkeypatch.setattr(attention, "_write_text", real)
    attention.remove(root)
    assert not (home / "settings.json").exists()


def test_review_turning_codex_hooks_on_again_records_what_the_owner_set_since(codex_home):
    home, root = codex_home
    (home / "config.toml").write_text("[features]\nhooks = false\n")
    (home / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command",
                                                                                  "command": "~/bin/a"}]}]}}))
    write_attention_settings(root, notices=True)
    attention.enable_codex_hooks(root)
    attention.set_notices(root, False)  # put back: hooks = false
    (home / "config.toml").write_text("")  # the owner removes the line (Codex's default: on)...
    attention._save_codex_state(root, {os.path.realpath(home): {"hooks": True, "others": []}})  # a stale record
    (home / "config.toml").write_text("[features]\nhooks = false\n")  # ...then sets it off again
    attention.set_notices(root, True)
    attention.enable_codex_hooks(root)
    attention.set_notices(root, False)
    assert (home / "config.toml").read_text() == "[features]\nhooks = false\n"


def test_review_uninstall_tells_the_owner_which_codex_hooks_codex_asks_about(codex_home):
    home, root = codex_home
    _shifted(home, root, 'trusted_hash = "sha256:openswaps-own"')
    notes: list = []
    attention.remove(root, notes=notes)
    assert notes == ["Codex will ask you to review your hook again (Stop: ~/bin/log)"]


def test_review_the_question_hook_stops_waiting_once_claude_code_gave_up(tmp_path, monkeypatch):
    root = tmp_path / "w"
    path = ipc.socket_path(root)
    path.parent.mkdir(parents=True, mode=0o700)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    parents = iter([4242] + [1] * 100)  # its wrapper was killed: reparented
    monkeypatch.setattr(attention.os, "getppid", lambda: next(parents))
    started = time.monotonic()
    try:
        assert attention._wait_for_answer(SESSION, "f" * 32, "", root) is None
    finally:
        server.close()
    assert time.monotonic() - started < 3


def test_review_codex_answers_already_applied_stay_applied_across_a_restart(tmp_path):
    answers = Answers()
    clock = Clock()
    first = AttentionStore(tmp_path, clock=clock, codex_answers=answers)
    first.notice(codex_permission(command="a") | {"at": START})
    first.notice(codex_permission(command="b") | {"at": START + 0.1})
    wait(first, clock, 7)
    answers.at[THREAD] = START + 8
    wait(first, clock, 0.5)
    again = AttentionStore(tmp_path, clock=clock, codex_answers=answers)
    wait(again, clock, 0.5)
    assert len(published(again)) == 1  # the same log row never closes the other approval


def test_review_an_item_without_detail_never_stalls_the_items_tried_after_a_refusal(reporting):
    client, items, service, worker, transport, _ = reporting
    items.notice(notification("idle_prompt", session=OTHER))  # no last message: never any detail
    items.notice(notification("idle_prompt", session="third"))
    items.notice(hook("Stop", last_assistant_message="poison") | {"at": START - 1})
    items.notice(notification("idle_prompt"))
    refusing = client.transport = _Refusing(transport, 400, "invalid_request", lambda data: any(
        e.get("detail", {}).get("last_message") == "poison" for e in data["items"]))
    for _ in range(4):
        client.sync_attention()
    items.notice(hook("Stop", session="fourth", last_assistant_message="fine") | {"at": START - 1})
    items.notice(notification("idle_prompt", session="fourth"))
    for _ in range(6):
        client.sync_attention()
    shown = {e["detail"]["last_message"] for e in service.attention(worker)["items"] if "detail" in e}
    assert shown == {"fine"} and refusing.sent  # the new item's detail went; only the refused one is kept out


# --- follow-ups from the S fix round's final review (mar3co/openswap#182) ----------------


def test_a1_the_replies_switch_never_turns_codex_hooks_on(codex_home):
    home, root = codex_home
    attention.set_notices(root, True)
    (home / "config.toml").write_text("[features]\nhooks = false\n")  # the owner turns Codex's hooks off since
    for replies in (True, False):
        attention.set_replies(root, replies)
        assert (home / "config.toml").read_text() == "[features]\nhooks = false\n"


def _installed_program(tmp_path, monkeypatch) -> str:
    exe = tmp_path / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    exe.chmod(0o755)
    monkeypatch.setattr(attention, "program", lambda: str(exe))
    return str(exe)


def test_a2_an_owners_own_bin_codex_hook_is_never_taken_for_openswaps(codex_home, tmp_path, monkeypatch):
    home, root = codex_home
    _installed_program(tmp_path, monkeypatch)
    attention.apply(root, True)
    theirs = {"hooks": [{"type": "command", "command": "/Users/o/bin/codex-hook Stop"}]}
    data = json.loads((home / "hooks.json").read_text())
    data["hooks"]["Stop"].append(theirs)
    (home / "hooks.json").write_text(json.dumps(data))
    assert attention.recorded_programs(root) == [] and attention.other_launchers(root) == []
    attention.remove(root)
    assert json.loads((home / "hooks.json").read_text()) == {"hooks": {"Stop": [theirs]}}
    assert not (root / attention.HOOKS_RECORD).exists() and not attention.codex_launcher(root).exists()


def test_a2_another_openswaps_launcher_is_named_only_while_it_is_there(codex_home, tmp_path, monkeypatch):
    home, root = codex_home
    exe = _installed_program(tmp_path, monkeypatch)
    attention.apply(root, True)
    other = tmp_path / "other-backup"
    attention._write_launcher(other, exe)
    data = json.loads((home / "hooks.json").read_text())
    data["hooks"]["Stop"].append({"hooks": [attention.codex_hook_entry("Stop", attention.codex_launcher(other))]})
    (home / "hooks.json").write_text(json.dumps(data))
    assert attention.other_launchers(root) == [str(attention.codex_launcher(other))]
    attention.codex_launcher(other).write_text("#!/bin/sh\nexec ~/bin/mine\n")
    assert attention.other_launchers(root) == [] and attention.recorded_programs(root) == []
    attention.codex_launcher(other).unlink()
    assert attention.other_launchers(root) == [] and attention.recorded_programs(root) == []


def test_a2_settings_names_another_openswaps_launcher():
    from openswap.menubar_display import remote_attention_rows

    rows = remote_attention_rows({"attention": {"notices": True, "details": True,
                                                "others": ["/b/bin/codex-hook"]}}, enabled=True)
    assert any(r["id"] == "remote_attention_others" and "/b/bin/codex-hook" in r["label"] for r in rows)


def test_f6_a_recorded_file_that_cannot_be_checked_is_named_and_kept_and_the_rest_go(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(attention, "program", lambda: EXE)
    root = tmp_path / "backup"
    homes = [tmp_path / "a", tmp_path / "b"]
    for home in homes:
        home.mkdir()
        attention.set_hooks(home, EXE, backup_root=root)
    write_attention_settings(root, notices=True)
    blocked = homes[0] / "settings.json"
    real = Path.is_symlink

    def is_symlink(self):
        if self == blocked:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    problems = attention.set_notices(root, False)
    assert len(problems) == 1 and str(blocked) in problems[0]
    assert list(_recorded(root)) == [str(blocked)] and not (homes[1] / "settings.json").exists()
    assert any(str(blocked) in p for p in attention.reconcile(root))
    with pytest.raises(ConfigError, match="Permission denied"):
        attention.remove(root)
    monkeypatch.setattr(Path, "is_symlink", real)
    assert attention.reconcile(root) == [] and not blocked.exists()


def _after_an_empty_group(home, root, state_before, state_after):
    """Stop: an empty group, then OpenSwap's entry, then the owner's hook,
    which taking OpenSwap's out moves from 2:0 to 1:0."""
    attention.apply(root, True)
    data = json.loads((home / "hooks.json").read_text())
    theirs = {"type": "command", "command": "~/bin/log", "timeout": 10, "async": False}
    data["hooks"]["Stop"] = [{"hooks": []}, *data["hooks"]["Stop"], {"hooks": [theirs]}]
    (home / "hooks.json").write_text(json.dumps(data))
    source = os.path.join(os.path.realpath(home), "hooks.json")
    lines = [f'[hooks.state."{source}:stop:{at}"]\n{state}\n'
             for at, state in (("2:0", state_before), ("1:0", state_after)) if state]
    (home / "config.toml").write_text("\n".join(lines))
    return theirs


def test_a3_no_move_past_an_empty_group_lets_a_codex_hook_gain_trust(codex_home):
    home, root = codex_home
    hooked = {"type": "command", "command": "~/bin/log", "timeout": 10, "async": False}
    theirs = _after_an_empty_group(home, root, "", f'trusted_hash = "{attention.codex_hook_hash("Stop", hooked)}"')
    problems = attention.set_notices(root, False)
    assert any("left in hooks.json" in p for p in problems)
    assert json.loads((home / "hooks.json").read_text())["hooks"]["Stop"][2] == {"hooks": [theirs]}


def test_a3_the_note_past_an_empty_group_names_the_hook_that_moves(codex_home):
    home, root = codex_home
    theirs = _after_an_empty_group(home, root, "", "enabled = false")
    assert attention.set_notices(root, False) == ["Codex will turn your hook off (Stop: ~/bin/log)"]
    assert json.loads((home / "hooks.json").read_text())["hooks"]["Stop"] == [{"hooks": []}, {"hooks": [theirs]}]


def test_minor_a_file_recorded_under_two_config_home_paths_is_deleted_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    root = tmp_path / "backup"
    home = tmp_path / "home"
    home.mkdir()
    linked = tmp_path / "a-link"  # sorts first: changed before the path OpenSwap created the file through
    linked.symlink_to(home)
    attention.set_hooks(home, EXE, backup_root=root)
    attention.set_hooks(linked, EXE, backup_root=root)
    assert len(_recorded(root)) == 2
    attention.remove(root)
    assert not (home / "settings.json").exists() and not (root / attention.HOOKS_RECORD).exists()


# --- follow-ups from the S fix round's final review (mar3co/openswap#183) ----------------


class _Switch:
    """The owner's switches as the worker reads them; ``readable`` False: the file cannot be read."""
    readable = True

    def enabled(self):
        return True if self.readable else None

    def switches(self):
        return AttentionSettings(notices=self.readable, readable=self.readable)


def test_b3_unreadable_settings_still_close_and_update_what_the_phone_shows(tmp_path):
    switch, clock = _Switch(), Clock()
    items = AttentionStore(tmp_path, clock=clock, enabled=switch.enabled, switches=switch.switches)
    items.notice(permission(command="a") | at(clock, 0))
    items.notice(notification("permission_prompt"))
    items.notice(hook("Stop", session=OTHER, last_assistant_message="one") | at(clock, 1))
    items.notice(notification("idle_prompt", session=OTHER) | at(clock, 61))
    items.notice(permission(command="b") | at(clock, 70))
    assert len(published(items)) == 2
    switch.readable = False
    items.notice(permission(command="c") | at(clock, 71))
    items.notice(notification("idle_prompt", session="third") | at(clock, 72))
    items.notice(hook("Stop", session=OTHER, last_assistant_message="two") | at(clock, 73))  # replaces the idle item
    items.notice(post(command="a", event="PostToolUseFailure") | at(clock, 74))
    items.notice(notification("permission_prompt") | at(clock, 76))  # b's: not published now
    assert [e["detail"]["last_message"] for e in published(items)] == ["two"]
    assert sorted(c["reason"] for c in closes(items)) == ["answered_on_mac", "superseded"]
    assert len(items._items) == 4  # b, c and the third session's idle item wait, unpublished
    items.notice(hook("UserPromptSubmit", session=OTHER, prompt="typed") | at(clock, 80))
    assert published(items) == []


def test_b3_codex_hooks_with_unreadable_settings_publish_only_a_replacement(tmp_path):
    switch, clock = _Switch(), Clock()
    items = AttentionStore(tmp_path, clock=clock, enabled=switch.enabled, switches=switch.switches)
    items.notice(codex_stop("first") | {"at": START})
    wait(items, clock, 61)
    items.notice(codex_permission() | {"at": START + 62})
    switch.readable = False
    items.notice(codex_permission(command="touch other") | {"at": START + 62.5})
    items.notice(codex_post() | {"at": START + 63})
    items.notice(codex_stop("second") | {"at": START + 64})
    items.notice(codex_stop("elsewhere", thread=SUBAGENT) | {"at": START + 64})
    assert [e["detail"]["last_message"] for e in published(items)] == ["second"]
    assert [c["reason"] for c in closes(items)] == ["superseded"] and len(items._items) == 2


def test_b4_the_report_budget_comes_back_after_a_run_of_taken_reports(reporting):
    from openswap.worker import remote
    from openswap.worker.attention import REPORT_BUDGET

    client, items, service, worker, transport, _ = reporting
    shape = client._attention_shape
    refusing = client.transport = _Refusing(transport, 413, "body_too_large")
    items.notice(notification("idle_prompt"))
    client.sync_attention()
    assert shape.budget < REPORT_BUDGET
    refusing.status = None
    for _ in range(remote.ATTENTION_BUDGET_RECOVERY + 2):
        client.sync_attention()  # one report taken; the same report again is never sent
    assert shape.budget < REPORT_BUDGET
    for n in range(remote.ATTENTION_BUDGET_RECOVERY - 1):
        items.notice(notification("idle_prompt", session=f"s{n}"))
        client.sync_attention()
    assert shape.budget == REPORT_BUDGET
    refusing.status = 413  # straight after it came back: shrunk for good
    items.notice(notification("idle_prompt", session="again"))
    client.sync_attention()
    refusing.status = None
    for n in range(remote.ATTENTION_BUDGET_RECOVERY * 2):
        items.notice(notification("idle_prompt", session=f"later{n}"))
        client.sync_attention()
    assert shape.budget < REPORT_BUDGET


# --- fix round 1 -----------------------------------------------------------------------


def test_f3_one_read_of_the_settings_decides_a_notice(tmp_path, monkeypatch):
    from openswap import settings
    from openswap.worker import runtime

    monkeypatch.setattr(attention, "reconcile", lambda *a, **k: [])
    reads, racing = [], []

    def load(root, **kwargs):
        reads.append(1)
        if racing and len(reads) % 2:
            return AttentionSettings(notices=False, details=False, readable=False)
        return AttentionSettings(notices=True)  # readable again by the next read

    monkeypatch.setattr(settings, "load_attention_settings", load)
    items = runtime._attention_store(tmp_path)
    items.notice(notification("idle_prompt"))
    assert len(published(items)) == 1
    racing.append(True)
    reads.clear()
    items.notice(hook("UserPromptSubmit", prompt="typed"))
    assert published(items) == [] and len(reads) == 1


def test_f4_prompts_waiting_while_the_settings_are_unreadable_publish_once_they_read_again(tmp_path):
    switch, clock = _Switch(), Clock()
    items = AttentionStore(tmp_path, clock=clock, enabled=switch.enabled, switches=switch.switches,
                           codex_answers=Answers())
    switch.readable = False
    items.notice(permission() | at(clock, 0))
    items.notice(notification("permission_prompt"))
    items.notice(codex_permission() | at(clock, 0))
    wait(items, clock, 10)
    assert published(items) == []
    switch.readable = True
    items.tick()
    assert sorted(e["provider"] for e in published(items)) == ["claude", "codex"]


def test_f4_held_queued_prompts_survive_one_answered_at_the_mac(tmp_path):
    switch, clock = _Switch(), Clock()
    items = AttentionStore(tmp_path, clock=clock, enabled=switch.enabled, switches=switch.switches,
                           codex_answers=Answers())
    t = clock.now
    switch.readable = False
    items.notice(permission(command="a", agent_id="sub1") | at(clock, 0))
    items.notice(permission(command="b", agent_id="sub2") | at(clock, 0.1))
    items.notice(notification("permission_prompt") | at(clock, 6))
    items.notice(notification("permission_prompt") | at(clock, 6.1))
    items.observe(record("busy", t + 20), True)
    items.observe(record("waiting", t + 20.5, waiting_for="permission prompt"), True)  # the queued one shows
    clock.now += 25
    items.tick()
    switch.readable = True
    items.tick()
    assert len(published(items)) == 2


def test_f4_a_held_prompt_answered_at_the_mac_is_never_published(tmp_path):
    switch, clock = _Switch(), Clock()
    items = AttentionStore(tmp_path, clock=clock, enabled=switch.enabled, switches=switch.switches,
                           codex_answers=Answers())
    t = clock.now
    switch.readable = False
    items.notice(permission() | at(clock, 0))
    items.notice(notification("permission_prompt") | at(clock, 6))
    clock.now = t + 20
    items.observe(record("busy", t + 20), True)  # answered at the Mac
    clock.now = t + 20.5
    switch.readable = True
    items.tick()  # inside its grace
    assert published(items) == []


def test_f3_a_notice_reads_the_settings_once_even_when_the_store_is_idle(tmp_path):
    reads = []

    def enabled():
        reads.append(1)
        return True if len(reads) == 1 else None  # a second read would race the file

    looked = []
    items = AttentionStore(tmp_path, clock=Clock(), enabled=enabled,
                           records=lambda: (looked.append(1) or [], True))
    items.notice(notification("permission_prompt"))
    assert len(reads) == 1 and looked


def test_f5_a_413_any_time_after_the_budget_came_back_shrinks_it_for_good(reporting):
    from openswap.worker import remote
    from openswap.worker.attention import REPORT_BUDGET

    client, items, service, worker, transport, _ = reporting
    shape = client._attention_shape
    refusing = client.transport = _Refusing(transport, None, "body_too_large")
    sessions = iter(range(1000))

    def report(status=None):
        refusing.status = status
        items.notice(notification("idle_prompt", session=f"s{next(sessions)}"))
        client.sync_attention()

    report(413)
    for _ in range(remote.ATTENTION_BUDGET_RECOVERY):
        report()
    assert shape.budget == REPORT_BUDGET
    for _ in range(3):
        report()  # taken: the proxy's limit lies in between
    report(413)
    for _ in range(remote.ATTENTION_BUDGET_RECOVERY * 3):
        report()
    assert shape.budget < REPORT_BUDGET
