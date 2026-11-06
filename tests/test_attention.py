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
                                                          "tool_name", "tool_key"}


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
    result = subprocess.run([sys.executable, "-c", "import sys; from openswap.attention import main; "
                             "sys.exit(main(['Stop']))"],
                            input=json.dumps({"session_id": SESSION, "cwd": "/x"}).encode(),
                            capture_output=True, env=env, timeout=10)
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


def record(status, status_at, session=SESSION, waiting_for=""):
    return {session: SessionRecord(session, os.getpid(), status, status_at, waiting_for)}


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
    items.notice(post(command="b") | at(clock, 20))
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


def test_another_processs_record_for_the_session_is_ignored(store):
    items, clock = store
    items.notice(permission() | at(clock, 0) | {"pid": os.getpid()})
    stale = {SESSION: SessionRecord(SESSION, os.getppid(), "idle", clock.now + 1)}
    items.observe(stale, True)
    items.notice(notification("permission_prompt") | at(clock, 6) | {"pid": os.getpid()})
    clock.now += 10
    items.tick()
    assert len(published(items)) == 1


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
    waiting = {**record("waiting", t + 0.1), **record("waiting", t + 7.1, session=OTHER)}
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
    reads = [({**record("idle", t + 20), **record("busy", t + 30, session=OTHER)}, True)]
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
    assert _session_records(tmp_path)() == ({}, False)


def test_a_background_task_turn_refreshes_the_idle_items_reply(store):
    items, clock = store
    items.notice(hook("Stop", last_assistant_message="Launched both.") | at(clock, 1))
    items.notice(notification("idle_prompt") | at(clock, 61))
    [first] = published(items)
    items.notice(hook("UserPromptSubmit", prompt="<task-notification>\n<task-id>x</task-id>") | at(clock, 90))
    items.notice(hook("Stop", last_assistant_message="Result is 42.") | at(clock, 95))
    items.notice(notification("idle_prompt") | at(clock, 155))
    [again] = published(items)
    assert again["id"] == first["id"] and again["detail"] == {"last_message": "Result is 42."}


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
        {SESSION: SessionRecord(SESSION, os.getpid(), "waiting", 1_800_000_000.5, "permission prompt")}, True)
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
    items.observe({}, True)
    left = published(items)
    assert [i["session_label"] for i in left] == ["VS Code · 77aa", "Claude Code · nopi"]
    assert [c["reason"] for c in closes(items)] == ["session_ended"]


def test_the_hook_reports_its_claude_code_process_and_surface(monkeypatch):
    monkeypatch.setattr(attention, "claude_pid", lambda: 4242)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    notice = hook("Stop")
    assert (notice["pid"], notice["entrypoint"]) == (4242, "cli")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "Bad Value!")
    assert "entrypoint" not in hook("Stop")


@pytest.mark.parametrize(("ps_out", "expected"), [
    ("  4242 /bin/zsh\n", 4242),      # through a shell: its parent
    ("     1 /bin/sh\n", 0),          # Claude Code already gone
    ("  4242 claude\n", "parent"),    # run directly
    ("", 0),                          # ps could not tell
    (TimeoutError, 0),
])
def test_claude_pid_is_unknown_rather_than_a_shell(monkeypatch, ps_out, expected):
    import subprocess

    monkeypatch.setattr(attention, "claude_pid", REAL_CLAUDE_PID)

    def ps(*_args, **_kwargs):
        if ps_out is TimeoutError:
            raise subprocess.TimeoutExpired("ps", 0.3)
        return subprocess.CompletedProcess([], 0, ps_out, "")

    monkeypatch.setattr(attention.subprocess, "run", ps)
    monkeypatch.delenv("CLAUDE_CODE_SHELL_PREFIX", raising=False)
    assert attention.claude_pid() == (os.getppid() if expected == "parent" else expected)
    monkeypatch.setenv("CLAUDE_CODE_SHELL_PREFIX", "/usr/local/bin/my-wrapper")
    assert attention.claude_pid() == 0


def test_claude_pid_skips_the_shell_claude_code_ran_the_hook_through(monkeypatch):
    import subprocess

    monkeypatch.setattr(attention, "claude_pid", REAL_CLAUDE_PID)
    out = subprocess.run(["/bin/sh", "-c", f"{sys.executable} -c 'from openswap.attention import claude_pid; "
                          "print(claude_pid())'; true"], capture_output=True, text=True)
    assert int(out.stdout) == os.getpid()


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
    assert ours == {"type": "command", "command": f"{EXE} attention PostToolUse", "async": True, "timeout": 10}
    assert data["hooks"]["Notification"][0]["matcher"] == "permission_prompt|idle_prompt"
    assert _ours(home) == {e: f"{EXE} attention {e}" for e in attention.HOOK_EVENTS}
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
    attention.set_hooks(claude, EXE)  # a removal that never finished
    assert attention.reconcile(root) == []
    assert _ours(claude) == {}


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
    assert not attention.is_our_hook_command("openswap attention rm -rf")
    assert not attention.is_our_hook_command("~/bin/notify attention Stop")
    assert not attention.is_our_hook_command("openswap statusline")


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
    changed, problems = attention.apply(root, True)
    assert changed and problems == []
    assert _ours(claude) == _ours(code) == {e: f"{EXE} attention {e}" for e in attention.HOOK_EVENTS}
    # A profile without its own settings.json would lose ~/.claude's once sharing is on.
    assert not (alone / "settings.json").exists()
    assert attention.remove(root) is True
    assert "hooks" not in _settings(claude) and "hooks" not in _settings(code)


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
    """Codex's log as ``read_codex_answers`` reads it: thread -> when a person answered."""

    def __init__(self):
        self.at, self.fail = {}, False

    def __call__(self, threads):
        if self.fail:
            raise sqlite3.OperationalError("no such table: logs")
        return {t: at for t, at in self.at.items() if t in threads}


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
    assert (item["kind"], item["provider"], item["session_label"]) == ("permission", "codex", "Codex · 01a1")
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
    assert answers == {THREAD: pytest.approx(START + 3.075)}


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
    (home / "config.toml").write_text(text.replace("hooks = true", "hooks = false"))
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
    assert set(CODEX_HOOKS_STATUS) == {"trusted", "review", "disabled", "off", "blocked", "missing", "unknown"}
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
                                    "model = \"o3\"\n\n[features]\nmemories = true\n"])
def test_c13_removal_puts_the_hooks_feature_back_exactly(codex_home, before):
    home, root = codex_home
    (home / "config.toml").write_text(before)
    attention.apply(root, True)
    assert tomllib.loads((home / "config.toml").read_text())["features"]["hooks"] is True
    attention.remove(root)
    assert (home / "config.toml").read_text() == before
    assert not (home / "hooks.json").exists() and not attention.codex_launcher(root).exists()


def test_c13_the_feature_stays_on_while_other_hooks_remain(codex_home):
    home, root = codex_home
    theirs = {"hooks": [{"type": "command", "command": "~/bin/log"}]}
    (home / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [theirs]}}))
    attention.apply(root, True)
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
        attention.set_codex_hooks(Path("/etc/codex"), root, True)
    before = {p for p in tmp_path.rglob("*")}
    attention.apply(root, True)
    written = {p for p in tmp_path.rglob("*")} - before
    assert {p for p in written if p.is_file()} <= {home / "hooks.json", home / "config.toml",
                                                   attention.codex_launcher(root), root / attention.CODEX_STATE_FILE}


@pytest.mark.parametrize("text", ['features = "x"\n', "[features\n", 'features.hooks = false\n'])
def test_a_config_this_cannot_rewrite_is_left_alone(codex_home, text):
    home, root = codex_home
    (home / "config.toml").write_text(text)
    _, problems = attention.apply(root, True)
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
    assert load_attention_settings(tmp_path) == AttentionSettings(notices=False, details=False)


def test_settings_rows_show_the_two_switches_on_a_paired_mac():
    from openswap.menubar_display import remote_attention_rows

    assert remote_attention_rows({}, enabled=True) == []
    rows = remote_attention_rows({"attention": {"notices": False, "details": True}}, enabled=True)
    assert [(r["id"], r.get("value"), r.get("disabled")) for r in rows[1:]] == [
        ("remote_attention_notices", False, None), ("remote_attention_details", True, True)]
    rows = remote_attention_rows({"attention": {"notices": True, "details": True}}, enabled=False)
    assert rows[-1]["id"] == "remote_attention_note"


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
                                         "closed": {}}
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
                                         "closed": {}}
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
