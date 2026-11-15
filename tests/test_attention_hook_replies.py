"""Replies without a channel (plan 028, Task 1): a session's own waiting hook
takes Allow, Deny or a question's answer. Each "row N" test pins plan 028's
Required outcome N."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from openswap import attention
from openswap.settings import AttentionSettings
from openswap.worker import attention as store_module
from openswap.worker import channel_link, ipc, peers
from openswap.worker.attention import AttentionStore, SessionRecord, shown_questions, validate_notice
from openswap.worker.peers import Process
from tests.test_attention_replies import (  # noqa: F401 (fixtures)
    CHANNEL, CLAUDE, PID, QUESTIONS, SESSION, Env, FakeLink, _hooks_run_under_the_test, _running, bash, env, hook,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the worker and its hooks are POSIX-only")


class Desktop(Env):
    """A session without OpenSwap's channel, on the surface ``entrypoint``."""

    def __init__(self, tmp_path, entrypoint="claude-desktop"):
        super().__init__(tmp_path)
        self.entrypoint = entrypoint
        self.status("busy", self.clock.now - 10)

    def status(self, status, at, session=SESSION):
        (self.home / "sessions" / f"{PID}.json").write_text(json.dumps(
            {"pid": PID, "sessionId": session, "status": status, "statusUpdatedAt": at * 1000,
             "entrypoint": self.entrypoint}))
        self.store.observe({session: SessionRecord(session, PID, status, at, entrypoint=self.entrypoint)}, True)

    def permission(self, item, command="npm test", *, preview=None, exact=None, agent="", tool_name="Bash",
                   late=0.0, claude=CLAUDE):
        """The prompt's synchronous permission hook, connected when its PermissionRequest ran."""
        now, self.clock.now = self.clock.now, item.created_at + late
        try:
            tool_input = bash(command)
            shown, whole = attention.input_preview(tool_input)
            return self.store.waiter("permission", claude, SESSION, attention.tool_key(tool_input), agent,
                                     tool_name=tool_name, shown=shown if preview is None else preview,
                                     whole=whole if exact is None else exact)
        finally:
            self.clock.now = now


@pytest.fixture
def desktop(tmp_path):
    return Desktop(tmp_path)


# --- rows 1, 2, 5 and 14: what an item takes --------------------------------------------


def test_row1_a_channel_session_takes_no_permission_hook(env):
    item = env.prompt()
    assert Desktop.permission(env, item) is None  # the hook exits at once
    assert env.actions(item) == ["allow", "deny"]  # the channel answers it, as before


@pytest.mark.parametrize("entrypoint", ["claude-desktop", "cli"])
def test_row2_one_waiting_hook_takes_allow_and_deny_and_shows_its_own_call(tmp_path, entrypoint):
    desktop = Desktop(tmp_path, entrypoint)
    item = desktop.prompt(relay=False)
    assert desktop.actions(item) == []  # no hook waiting: nothing would take a reply
    waiter = desktop.permission(item)
    assert item.detail["input_preview"] == attention.input_preview(bash("npm test"))[0]
    assert desktop.actions(item) == ["allow", "deny"]
    desktop.reply(item.id, "allow")
    assert waiter.done.is_set() and waiter.result == "allow"
    assert desktop.outcomes() == {}  # "sent" only once the hook took it
    desktop.store.handed_over(waiter.reply_id)
    assert desktop.outcomes() == {"r1": "sent"}
    desktop.status("busy", desktop.clock.now + 0.5)  # the dialog closed: the verdict decided it
    assert desktop.closes() == {item.id: "answered_from_watch"} and desktop.outcomes() == {"r1": "applied"}


def test_row2_a_hook_may_connect_before_its_prompt_is_recorded(desktop):
    tool_input = bash("npm test")
    desktop.clock.now += 0.1
    shown, whole = attention.input_preview(tool_input)
    waiter = desktop.store.waiter("permission", CLAUDE, SESSION, attention.tool_key(tool_input), tool_name="Bash",
                                  shown=shown, whole=whole)
    desktop.clock.now -= 0.1
    item = desktop.prompt(relay=False)
    assert item.detail["input_preview"] == waiter.shown and desktop.actions(item) == ["allow", "deny"]


@pytest.mark.parametrize(("hooks", "details", "actions"), [
    (0, True, []), (1, True, ["allow", "deny"]), (1, False, ["deny"]), (2, True, []), (2, False, [])])
def test_row5_a_permission_takes_replies_only_from_its_one_waiting_hook(desktop, hooks, details, actions):
    item = desktop.prompt(relay=False)
    for _ in range(hooks):
        desktop.permission(item)
    desktop.switches = AttentionSettings(notices=True, details=details, replies=True)
    assert desktop.actions(item) == actions


@pytest.mark.parametrize("forged_first", [True, False])
def test_row5_a_hook_with_another_preview_takes_every_reply_away_for_good(desktop, forged_first):
    item = desktop.prompt(relay=False)
    forged = '{ "command": "true", "description": "Run the tests" }'
    connect = [lambda: desktop.permission(item, preview=forged), lambda: desktop.permission(item)]
    hooks = [hook() for hook in (connect if forged_first else connect[::-1])]
    assert item.detail["input_preview"] == (forged if forged_first else attention.input_preview(bash("npm test"))[0])
    desktop.store.end_waiter(hooks[0])  # the first to connect leaves
    assert desktop.actions(item) == []
    desktop.reply(item.id, "allow")
    assert desktop.outcomes() == {"r1": "refused"} and not hooks[1].done.is_set()


def test_row5_a_hook_with_another_preview_than_the_one_shown_takes_replies_away_for_good(desktop):
    item = desktop.prompt(relay=False)
    desktop.store.end_waiter(desktop.permission(item))  # its preview stays shown
    desktop.permission(item, preview='{ "command": "true" }')
    assert desktop.actions(item) == []
    desktop.permission(item)
    assert desktop.actions(item) == []


def test_allow_needs_the_item_to_show_the_hooks_own_preview(desktop):
    item = desktop.prompt(relay=False)
    desktop.permission(item)
    item.detail["input_preview"] = '{ "command": "true" }'
    assert item.id not in desktop.store._tainted and desktop.actions(item) == ["deny"]


@pytest.mark.parametrize(("tool_name", "key"), [("", "f" * 32), ("Bash", "")])
def test_a_permission_hook_needs_a_tool_name_and_a_tool_key(desktop, tool_name, key):
    assert desktop.store.waiter("permission", CLAUDE, SESSION, key, tool_name=tool_name) is None


# --- F1: Allow only for a preview that shows the call exactly ------------------------------


@pytest.mark.parametrize(("command", "actions"), [
    ("npm test", ["allow", "deny"]), ("cat <<EOF\nline\nEOF", ["allow", "deny"]),
    ("deploy --token sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx", ["deny"]), ("deploy --password hunter2", ["deny"]),
    ("x" * 5000, ["deny"]), ("echo \u202eevil", ["deny"]), ("echo a\u200bb", ["deny"]),
    ("echo ok" + " " * 40 + "&& rm -rf ~", ["deny"]), ("echo \u05e9\u05dc\u05d5\u05dd", ["deny"]),
], ids=["plain", "multi-line", "token shape", "secret name", "cut", "bidi", "zero-width", "spaces", "right to left"])
def test_f1_allow_only_for_a_preview_that_shows_the_call_exactly(desktop, command, actions):
    item = desktop.prompt(command, relay=False)
    waiter = desktop.permission(item, command)
    assert desktop.actions(item) == actions
    desktop.reply(item.id, "allow")
    assert waiter.done.is_set() == ("allow" in actions)


def test_f1_a_change_by_the_workers_own_cleaning_makes_a_preview_deny_only(desktop):
    item = desktop.prompt(relay=False)
    desktop.permission(item, preview='{ "command": "echo \u202eevil\x7f" }', exact=True)
    assert desktop.actions(item) == ["deny"]
    assert item.detail["input_preview"] == '{ "command": "echo \\u202eevil\\u007f" }'


def test_f1_the_exact_claim_is_the_one_of_the_hook_whose_preview_is_shown(desktop):
    item = desktop.prompt(relay=False)
    inexact, exact = desktop.permission(item, exact=False), desktop.permission(item, exact=True)
    assert desktop.actions(item) == []
    desktop.store.end_waiter(inexact)
    assert desktop.actions(item) == ["allow", "deny"]
    desktop.store.end_waiter(exact)
    waiter = desktop.permission(item, exact=False)
    assert desktop.actions(item) == ["deny"]
    reply = {"id": "r1", "item_id": item.id, "action": "allow"}
    assert desktop.store._deliver(reply, item, desktop.clock.now + 600) == "refused" and not waiter.done.is_set()


@pytest.mark.parametrize("change", ["agent", "tool_key", "tool_name", "late"])
def test_a_hook_of_another_call_never_pairs(desktop, change):
    item = desktop.prompt(relay=False)
    desktop.permission(item, **{"agent": {"agent": "a1"}, "tool_key": {"command": "rm -rf build"},
                                "tool_name": {"tool_name": "Edit"}, "late": {"late": 6.0}}[change])
    assert desktop.actions(item) == [] and "input_preview" not in item.detail


def test_a_hook_of_another_session_or_process_is_not_served(desktop):
    key = attention.tool_key(bash("npm test"))
    assert desktop.store.waiter("permission", CLAUDE, "another", key, tool_name="Bash") is None
    stranger = Process(PID + 5, 1, "Fri Nov  6 13:00:00 2026", ("claude",))
    assert desktop.store.waiter("permission", stranger, SESSION, key, tool_name="Bash") is None
    restarted = Process(PID, 1, "Sat Nov  7 09:00:00 2026", ("claude",))
    (desktop.home / "sessions" / f"{PID}.json").write_text(json.dumps(
        {"pid": PID, "sessionId": SESSION, "status": "waiting", "entrypoint": "claude-desktop",
         "procStart": "Fri Nov  6 14:00:00 2026"}))
    assert desktop.store.waiter("permission", restarted, SESSION, key, tool_name="Bash") is None  # a reused PID
    assert desktop.store.waiter("permission", CLAUDE, SESSION, key, tool_name="Bash") is not None


def test_an_idle_session_without_a_channel_takes_no_message_yet(desktop):
    assert desktop.actions(desktop.idle()) == []


@pytest.mark.parametrize("entrypoint", ["claude-vscode", "sdk-ts", ""])
def test_row14_only_terminal_and_desktop_sessions_take_hook_replies(tmp_path, entrypoint):
    other = Desktop(tmp_path, entrypoint)
    item = other.prompt(relay=False)
    assert other.permission(item) is None and other.actions(item) == []
    question = other.question()
    assert other.waiter() is None and other.actions(question) == []


def test_row14_a_channel_session_in_vs_code_keeps_its_replies(tmp_path):
    vscode = Desktop(tmp_path, "claude-vscode")
    vscode.register()
    assert vscode.actions(vscode.prompt()) == ["allow", "deny"]


def test_row15_hooks_past_the_cap_leave_their_prompt_to_the_mac(desktop, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_WAITERS", 1)
    item = desktop.prompt(relay=False)
    assert desktop.permission(item) is not None
    assert desktop.permission(item) is None


# --- rows 3, 4 and 13: who answered ---------------------------------------------------------


def test_row3_an_answer_at_the_mac_releases_the_hook_and_closes_on_the_mac(desktop):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    assert not desktop.store.waiter_over(waiter)
    desktop.status("busy", desktop.clock.now)  # Allow at the Mac
    assert desktop.store.waiter_over(waiter) and waiter.result is None
    desktop.clock.now += 1.5
    desktop.store.tick()
    assert desktop.closes() == {item.id: "answered_on_mac"}


def test_row3_a_verdict_the_mac_overtook_is_never_applied(desktop):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    desktop.reply(item.id, "deny")
    desktop.store.handed_over(waiter.reply_id)
    assert desktop.outcomes() == {"r1": "sent"}
    desktop.status("busy", desktop.clock.now + 3)  # long after the verdict: the Mac's own answer
    desktop.clock.now += 5
    desktop.store.tick()
    assert desktop.closes() == {item.id: "answered_on_mac"} and desktop.outcomes() == {}


@pytest.mark.parametrize(("status", "won"), [("busy", True), ("idle", False)])
def test_row4_an_allow_wins_only_when_the_dialog_closed_its_way(desktop, status, won):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    desktop.reply(item.id, "allow")
    assert waiter.result == "allow"
    desktop.store.handed_over(waiter.reply_id)
    desktop.outcomes()
    desktop.status(status, desktop.clock.now + 0.5)
    desktop.clock.now += 2
    desktop.store.tick()
    assert desktop.closes() == {item.id: "answered_from_watch" if won else "answered_on_mac"}
    assert desktop.outcomes() == ({"r1": "applied"} if won else {})


# --- G1: a hook's Deny settles at the end of its grace -----------------------------------------


def _denied(desktop, agent="", closed=0.5):
    """A prompt whose own hook took the watch's Deny; the dialog then closed ``closed`` seconds later."""
    item = desktop.prompt(relay=False, agent=agent or None)
    waiter = desktop.permission(item, agent=agent)
    desktop.reply(item.id, "deny")
    desktop.store.handed_over(waiter.reply_id)
    assert desktop.outcomes() == {"r1": "sent"}
    if closed is not None:
        desktop.status("busy", desktop.clock.now + closed)  # a hook's deny lets Claude go on
    return item


def _after_grace(desktop):
    desktop.clock.now += store_module.SESSION_END_GRACE_SECONDS
    desktop.store.tick()


@pytest.mark.parametrize("stop", [False, True], ids=["no Stop", "Stop"])
@pytest.mark.parametrize("status", ["busy", "idle"])
def test_g1_a_hook_deny_is_applied_once_its_grace_passes_without_the_call(desktop, status, stop):
    item = _denied(desktop, closed=None)
    desktop.status(status, desktop.clock.now + 0.5)
    if stop:
        desktop.store.notice(hook("Stop") | desktop.at(0.8))  # the turn ended: still not before the grace
    desktop.store.tick()
    assert desktop.closes() == {} and desktop.outcomes() == {}  # the call's own signal may still come
    _after_grace(desktop)
    assert desktop.closes() == {item.id: "answered_from_watch"} and desktop.outcomes() == {"r1": "applied"}


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
@pytest.mark.parametrize("stop_first", [False, True])
def test_g1_the_calls_own_signal_within_the_grace_means_the_mac_said_yes(desktop, event, stop_first):
    item = _denied(desktop)
    signals = [hook(event, tool_name="Bash", tool_input=bash("npm test")) | desktop.at(0.6),
               hook("Stop") | desktop.at(0.9)]
    for notice in signals[::-1] if stop_first else signals:
        desktop.store.notice(notice)
    _after_grace(desktop)
    assert desktop.closes() == {item.id: "answered_on_mac"} and desktop.outcomes() == {}


def test_g1_the_session_ending_first_credits_nothing(desktop):
    item = _denied(desktop)
    desktop.store.notice(hook("SessionEnd") | desktop.at(1))
    _after_grace(desktop)
    assert desktop.closes() == {item.id: "session_ended"} and desktop.outcomes() == {}


def test_g1_a_session_still_waiting_when_the_grace_from_the_hand_over_ends_closes_on_the_mac(desktop):
    item = _denied(desktop, closed=None)
    desktop.status("waiting", desktop.clock.now + 0.3)  # the next queued dialog showed at once
    _after_grace(desktop)
    assert desktop.closes() == {item.id: "answered_on_mac"} and desktop.outcomes() == {}


def test_g1_a_sibling_calls_signal_never_decides(desktop):
    item = _denied(desktop)
    desktop.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("ls")) | desktop.at(0.6))
    desktop.store.notice(hook("SubagentStop", agent_id="a9") | desktop.at(0.7))
    _after_grace(desktop)
    assert desktop.closes() == {item.id: "answered_from_watch"} and desktop.outcomes() == {"r1": "applied"}


@pytest.mark.parametrize("signal", ["SubagentStop", "PermissionRequest"])
def test_g1_a_subagents_denied_prompt_settles_at_its_grace_whatever_its_agent_does_next(desktop, signal):
    item = _denied(desktop, agent="a1")
    if signal == "SubagentStop":
        desktop.store.notice(hook("SubagentStop", agent_id="a1") | desktop.at(0.8))
    else:
        desktop.store.notice(hook("PermissionRequest", tool_name="Bash", tool_input=bash("ls"), agent_id="a1")
                             | desktop.at(0.8))
    assert desktop.closes() == {}
    _after_grace(desktop)
    assert desktop.closes() == {item.id: "answered_from_watch"} and desktop.outcomes() == {"r1": "applied"}


def test_g1_a_denied_prompt_is_not_queued_beside_a_prompt_recorded_after_its_exit(desktop):
    _denied(desktop)
    t = desktop.clock.now
    desktop.store.notice(hook("PermissionRequest", tool_name="Bash", tool_input=bash("npm run lint")) | {"at": t + 1})
    desktop.status("waiting", t + 0.9)
    retry = next(i for i in desktop.store._items if i.created_at == t + 1)
    waiter = desktop.permission(retry, "npm run lint")
    desktop.status("busy", t + 1.2)  # answered at the Mac while the denied one is in its grace
    assert desktop.store.waiter_over(waiter)


def test_g1_a_prompt_queued_beside_the_denied_one_keeps_its_hook_and_takes_a_reply_after(desktop):
    first = desktop.prompt(relay=False)
    second = desktop.prompt("npm run lint", relay=False, agent="a1")
    hooks = [desktop.permission(first), desktop.permission(second, "npm run lint", agent="a1")]
    desktop.reply(first.id, "deny")
    desktop.store.handed_over(hooks[0].reply_id)
    desktop.status("busy", desktop.clock.now + 0.5)
    assert not desktop.store.waiter_over(hooks[1])
    desktop.status("waiting", desktop.clock.now + 0.8)  # the queued dialog shows
    _after_grace(desktop)
    assert desktop.closes() == {first.id: "answered_on_mac"}  # sent while another prompt waited: never credited
    assert not desktop.store.waiter_over(hooks[1]) and desktop.actions(second) == ["allow", "deny"]


def test_a_hook_that_sent_no_preview_takes_deny_only(desktop):
    item = desktop.prompt(relay=False)
    desktop.permission(item, preview="")
    assert desktop.actions(item) == ["deny"]


def test_allow_reaches_a_hook_only_while_the_item_shows_its_call(desktop):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    desktop.store.end_waiter(desktop.permission(item, preview='{ "command": "true" }'))  # joined and left
    reply = {"id": "r1", "item_id": item.id, "action": "allow"}
    assert desktop.store._deliver(reply, item, desktop.clock.now + 600) == "refused"
    assert not waiter.done.is_set()


def test_a_verdict_is_refused_once_its_hook_left(desktop):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    desktop.accepted()
    desktop.store.end_waiter(waiter)
    desktop.reply(item.id, "deny", offered=False)
    assert desktop.outcomes() == {"r1": "refused"}


# --- rows 6, 7 and 8: the hook going away ---------------------------------------------------


def test_row6_the_session_ending_closes_the_item_and_releases_the_hook(desktop):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    desktop.store.notice(hook("SessionEnd") | desktop.at(1))
    assert desktop.closes() == {item.id: "session_ended"}
    assert desktop.store.waiter_over(waiter) and waiter.result is None


def test_row6_a_hook_whose_claude_code_process_ended_is_released(desktop, monkeypatch):
    waiter = desktop.permission(desktop.prompt(relay=False))
    monkeypatch.setattr("openswap.process_detection.is_pid_alive", lambda pid: False)
    assert desktop.store.waiter_over(waiter)


def _restart(desktop, tmp_path):
    desktop.store = AttentionStore(tmp_path / "root", clock=desktop.clock, switches=lambda: desktop.switches,
                                   homes=lambda: [desktop.home])
    desktop.status("waiting", desktop.clock.now - 5)


def test_row7_after_a_worker_restart_the_prompt_waits_for_a_new_hook(desktop, tmp_path):
    item = desktop.prompt(relay=False)
    desktop.permission(item)
    _restart(desktop, tmp_path)
    item = next(i for i in desktop.store._items if i.id == item.id)
    assert desktop.actions(item) == []
    desktop.permission(item)
    assert desktop.actions(item) == ["allow", "deny"]


def test_row7_a_verdict_sent_before_a_restart_is_never_handed_over_again(desktop, tmp_path):
    item = desktop.prompt(relay=False)
    desktop.permission(item)
    desktop.reply(item.id, "allow")
    _restart(desktop, tmp_path)
    item = next(i for i in desktop.store._items if i.id == item.id)
    again = desktop.permission(item)
    assert desktop.actions(item) == []
    desktop.reply(item.id, "allow")
    assert not again.done.is_set() and desktop.outcomes() == {}


def test_row8_turning_replies_off_releases_every_waiting_hook(desktop):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    desktop.store.tick()
    assert not waiter.done.is_set()
    desktop.switches = AttentionSettings(notices=True, details=True, replies=False)
    desktop.store.tick()
    assert desktop.store.waiter_over(waiter) and waiter.result is None
    desktop.switches = AttentionSettings(notices=True, details=True, replies=True)
    assert desktop.actions(item) == []  # a released hook takes nothing, even before it leaves


# --- F5: a queued prompt's hook is not released early ---------------------------------------


def test_f5_a_queued_prompts_hook_waits_through_a_due_mark_and_takes_a_reply_again(desktop):
    first = desktop.prompt(relay=False)
    second = desktop.prompt("npm run lint", relay=False, agent="a1")
    hooks = [desktop.permission(first), desktop.permission(second, "npm run lint", agent="a1")]
    desktop.status("busy", desktop.clock.now)  # one of the two was answered at the Mac
    assert first.id in desktop.store._due and second.id in desktop.store._due
    assert not any(desktop.store.waiter_over(w) for w in hooks)
    desktop.status("waiting", desktop.clock.now + 0.5)  # the other dialog shows
    _after_grace(desktop)
    assert first.id not in desktop.store._due and not any(desktop.store.waiter_over(w) for w in hooks)
    desktop.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("npm test")) | desktop.at(1))
    assert desktop.store.waiter_over(hooks[0])  # its own prompt closed
    assert desktop.actions(second) == ["allow", "deny"]
    desktop.reply(second.id, "deny")
    assert hooks[1].result == "deny"


def test_f5_a_hook_whose_prompt_is_not_recorded_yet_waits_until_its_own_bound(desktop):
    tool_input = bash("npm test")
    waiter = desktop.store.waiter("permission", CLAUDE, SESSION, attention.tool_key(tool_input), tool_name="Bash")
    desktop.status("busy", desktop.clock.now + 1)  # an earlier dialog was answered
    assert not desktop.store.waiter_over(waiter)
    desktop.clock.now += store_module.HOOK_WAIT_SECONDS
    assert desktop.store.waiter_over(waiter)


def test_a_hook_whose_prompt_was_never_recorded_is_released_once_the_session_is_idle_after_it(desktop):
    tool_input = bash("npm test")
    waiter = desktop.store.waiter("permission", CLAUDE, SESSION, attention.tool_key(tool_input), tool_name="Bash")
    desktop.status("idle", desktop.clock.now + 1)  # its notice could still arrive, dated within PAIR_SECONDS
    assert not desktop.store.waiter_over(waiter)
    desktop.clock.now += store_module.PAIR_SECONDS + 1
    assert desktop.store.waiter_over(waiter)


# --- F6 and F7: unreadable settings, a stale channel registration ---------------------------


def test_f6_unreadable_settings_keep_waiting_hooks_and_refuse_replies(desktop):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    desktop.accepted()
    desktop.switches = AttentionSettings(notices=False, details=False, replies=False, readable=False)
    desktop.store.tick()
    assert not desktop.store.waiter_over(waiter)
    desktop.reply(item.id, "deny", offered=False)
    assert desktop.outcomes() == {"r1": "refused"} and not waiter.done.is_set()
    desktop.store.end_waiter(waiter)
    assert desktop.permission(item) is not None  # a hook starting now registers and waits too


@pytest.mark.parametrize(("notices", "replies"), [(False, True), (False, False), (True, False)])
def test_f6_readable_settings_with_notices_or_replies_off_release_every_hook(desktop, notices, replies):
    waiter = desktop.permission(desktop.prompt(relay=False))
    desktop.switches = AttentionSettings(notices=notices, details=True, replies=replies)
    desktop.store.tick()
    assert desktop.store.waiter_over(waiter) and waiter.result is None
    assert desktop.permission(desktop.prompt("ls", relay=False), "ls") is None


def test_f7_a_registration_for_a_pid_now_held_by_another_claude_code_process_counts_as_absent(desktop):
    previous = Process(PID, 1, "Thu Nov  5 09:00:00 2026", CLAUDE.args)
    link = FakeLink()
    assert desktop.store.register_channel(CHANNEL, previous, link)
    item = desktop.prompt(relay=False)
    assert desktop.permission(item) is not None
    assert PID not in desktop.store._channels and link.closed
    assert desktop.actions(item) == ["allow", "deny"]


# --- questions without a channel ------------------------------------------------------------


def test_a_desktop_question_takes_an_answer_through_its_waiting_hook(desktop):
    item = desktop.question()
    assert desktop.actions(item) == []
    waiter = desktop.waiter()
    assert desktop.actions(item) == ["answer"]
    desktop.reply(item.id, "answer", answers=[{"question": 0, "option": 1}])
    assert waiter.result == [[0, 1]]
    desktop.store.handed_over(waiter.reply_id)
    answered = {"questions": QUESTIONS, "answers": {"Which database?": "SQLite"}}
    desktop.store.notice(hook("PostToolUse", tool_name="AskUserQuestion", tool_input=answered) | desktop.at(0.2))
    assert desktop.closes() == {item.id: "answered_from_watch"}
    assert desktop.outcomes() == {"r1": "applied"}


# --- F4: a question shows its own hook's questions ------------------------------------------


# The real question's options in another order: an answer by position would pick the other one.
RELABELED = [{**QUESTIONS[0], "options": [{"label": "SQLite"}, {"label": "Postgres"}]}]


def _ask(env, shown=None):
    """The question's dialog; ``shown``: questions a forged notice with its tool key says it has."""
    t = env.clock.now
    env.asked = t + 0.1
    notice = hook("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": QUESTIONS})
    env.store.notice(notice | ({"questions": shown} if shown is not None else {}) | {"at": t + 0.1})
    env.status("waiting", t + 0.05)
    env.store.notice(hook("Notification", notification_type="permission_prompt") | {"at": t + 6.1})
    env.clock.now = t + 7
    return next(i for i in env.store._items if i.kind == "question")


def _forged_hook(env):
    """A hook with the real question's key whose questions say otherwise."""
    now, env.clock.now = env.clock.now, env.asked
    try:
        return env.store.waiter("question", CLAUDE, SESSION, attention.question_key({"questions": QUESTIONS}),
                                shown=shown_questions(RELABELED), whole=True)
    finally:
        env.clock.now = now


@pytest.fixture(params=["desktop", "env"], ids=["no channel", "channel"])
def session(request):
    return request.getfixturevalue(request.param)


def test_f4_a_question_takes_an_answer_only_while_it_shows_its_hooks_own_questions(session):
    item = _ask(session)
    waiter = session.waiter()
    assert item.detail["questions"] == waiter.shown == shown_questions(QUESTIONS)
    assert session.actions(item) == ["answer"]


@pytest.mark.parametrize("forged_first", [True, False])
def test_f4_a_notice_whose_questions_differ_from_the_hooks_takes_no_answer_for_good(session, forged_first):
    if forged_first:
        item = _ask(session, shown_questions(RELABELED))
        waiter = session.waiter()
    else:
        session.asked = session.clock.now + 0.1
        waiter = session.waiter()
        item = _ask(session, shown_questions(RELABELED))
    assert session.actions(item) == []
    session.store.end_waiter(waiter)
    session.waiter()
    assert session.actions(item) == []
    session.reply(item.id, "answer", answers=[{"question": 0, "option": 1}])
    assert session.outcomes() == {"r1": "refused"}


def test_f4_two_hooks_with_different_questions_take_no_answer_for_good(session):
    item = _ask(session)
    real, forged = session.waiter(), _forged_hook(session)
    assert session.actions(item) == []
    session.store.end_waiter(forged)
    assert session.actions(item) == [] and not real.done.is_set()


def test_f4_only_the_hook_says_whether_a_question_can_be_answered_whole(session):
    item = _ask(session)
    session.store.waiter("question", CLAUDE, SESSION, attention.question_key({"questions": QUESTIONS}),
                         shown=shown_questions(QUESTIONS), whole=False)
    assert item.detail["questions"] == shown_questions(QUESTIONS) and session.actions(item) == []


def test_f4_a_question_with_hidden_characters_is_shown_escaped_and_takes_no_answer(desktop):
    hidden = [{"question": "Which database?", "options": [{"label": "Postgres"}, {"label": "SQL\u202eetiL"}]}]
    notice = hook("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": hidden})
    assert notice["questions"][0]["options"][1] == "SQL\\u202eetiL"
    assert not store_module.questions_whole(hidden)
    desktop.asked = desktop.clock.now + 0.1
    waiter = desktop.waiter(hidden)
    desktop.store.notice(notice | {"at": desktop.asked})
    desktop.status("waiting", desktop.clock.now + 0.05)
    desktop.store.notice(hook("Notification", notification_type="permission_prompt") | desktop.at(6.1))
    desktop.clock.now += 7
    [item] = desktop.store._items
    assert item.detail["questions"] == waiter.shown == notice["questions"] and desktop.actions(item) == []


# --- the hook process ---------------------------------------------------------------------


def _worker(tmp_path, answer, sizes=None):
    """A worker socket that answers one waiting hook with ``answer`` (None: hangs up),
    keeping the request in ``seen`` (and its size in bytes in ``sizes``)."""
    root = tmp_path / "w"
    path = ipc.socket_path(root)
    path.parent.mkdir(parents=True, mode=0o700)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    seen = []

    def serve():
        connection, _ = server.accept()
        with connection:
            line = connection.makefile("rb").readline()
            seen.append(json.loads(line))
            if sizes is not None:
                sizes.append(len(line.rstrip(b"\n")))
            if answer is not None:
                connection.sendall(json.dumps(answer).encode() + b"\n")
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return root, seen


def _run_hook(monkeypatch, stdin: bytes, root) -> str:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert attention.permission_main(backup_root=root) == 0
    return out.getvalue()


@pytest.mark.parametrize(("answer", "decision"), [
    ({"behavior": "allow"}, {"behavior": "allow"}),
    ({"behavior": "deny"}, {"behavior": "deny", "message": "The owner denied this from their phone or watch."}),
    ({"behavior": None}, None), ({"behavior": "ask"}, None), ({}, None), (None, None)])
def test_row13_the_permission_hook_prints_only_the_watchs_verdict(tmp_path, monkeypatch, answer, decision):
    root, seen = _worker(tmp_path, answer)
    tool_input = {"command": "curl -H 'Authorization: Bearer abc123' https://x.test", "description": "Fetch"}
    stdin = json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_input": tool_input}).encode()
    out = _run_hook(monkeypatch, stdin, root)
    assert seen[0] == {"version": 1, "command": "permission", "session_id": SESSION,
                       "tool_key": attention.tool_key(tool_input), "tool_name": "Bash"}  # withheld: no preview
    if decision is None:
        assert out == ""
    else:
        assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}


@pytest.mark.parametrize("stdin", [b"", b"not json", b"[]", json.dumps({"session_id": SESSION}).encode(),
                                   json.dumps({"session_id": SESSION, "tool_name": "AskUserQuestion",
                                               "tool_input": {"questions": QUESTIONS}}).encode()])
def test_the_permission_hook_leaves_anything_else_to_the_mac_without_asking_the_worker(tmp_path, monkeypatch,
                                                                                        stdin):
    asked = []
    monkeypatch.setattr(attention, "_wait_for_worker", lambda *a: asked.append(a))
    assert _run_hook(monkeypatch, stdin, tmp_path) == "" and asked == []


def test_the_permission_hook_says_when_its_preview_shows_the_call_exactly(tmp_path, monkeypatch):
    root, seen = _worker(tmp_path, {"behavior": None})
    stdin = json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_input": bash("npm test")}).encode()
    assert _run_hook(monkeypatch, stdin, root) == ""
    assert seen[0]["preview"] == attention.input_preview(bash("npm test"))[0] and seen[0]["exact"] is True


@pytest.mark.parametrize("kind", ["permission", "question", "older question"])
def test_what_a_hook_sends_of_its_prompt_reaches_its_waiter(desktop, monkeypatch, kind):
    older, kind = kind == "older question", kind.split()[-1]
    process = Process(PID + 9, PID, "Fri Nov  6 14:00:03 2026", ("openswap", "attention", kind))
    monkeypatch.setattr(peers, "peer_pid", lambda _c: process.pid)
    monkeypatch.setattr(peers, "hook_claude", lambda pid: CLAUDE)
    _running(monkeypatch, process)
    if kind == "permission":
        shown = attention.input_preview(bash("npm test"))[0]
        request = {"tool_key": attention.tool_key(bash("npm test")), "tool_name": "Bash", "preview": shown,
                   "exact": True}
    else:
        shown = shown_questions(QUESTIONS)
        # An older question hook said "answerable", by older rules: only the new hook's "whole" counts.
        request = {"tool_key": attention.question_key({"questions": QUESTIONS}), "questions": shown,
                   "answerable" if older else "whole": True}
    stop = threading.Event()
    a, b = socket.socketpair()
    with a, b:
        serving = threading.Thread(target=channel_link.serve, args=(
            desktop.store, a, {"version": 1, "command": kind, "session_id": SESSION, **request}, stop, lambda: None))
        serving.start()
        deadline = time.monotonic() + 5
        while not desktop.store._waiters and time.monotonic() < deadline:
            time.sleep(0.01)
        [waiter] = desktop.store._waiters
        assert waiter.shown == shown and waiter.whole is not older
        stop.set()
        serving.join(5)


# The largest questions the report shows, in characters JSON escapes as two surrogates each.
WORST = [{"question": "\u00e9" * 1000, "header": "\u00e9" * 100, "options": [{"label": "\u00e9" * 100}] * 4}] * 4


def _ask_hook(monkeypatch, root, questions) -> str:
    stdin = json.dumps({"session_id": SESSION, "tool_name": "AskUserQuestion",
                        "tool_input": {"questions": questions}}).encode()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert attention.question_main(backup_root=root) == 0
    return out.getvalue()


def test_f4_a_question_hooks_request_with_the_largest_questions_fits_what_the_worker_reads(tmp_path, monkeypatch):
    sizes = []
    root, seen = _worker(tmp_path, {"answers": None}, sizes)
    assert _ask_hook(monkeypatch, root, WORST) == ""
    assert seen[0]["questions"] == shown_questions(WORST) and seen[0]["whole"] is True
    assert sizes[0] <= ipc.MAX_REQUEST_BYTES


def test_f4_a_request_past_what_the_worker_reads_is_never_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(ipc, "MAX_REQUEST_BYTES", 200)
    connected = []
    monkeypatch.setattr(attention.socket, "socket", lambda *a: connected.append(a))
    assert _ask_hook(monkeypatch, tmp_path, QUESTIONS) == "" and connected == []


def test_token_shapes_and_header_values_in_claudes_own_words_are_masked(monkeypatch):
    stop = hook("Stop", last_assistant_message="Use Bearer authentication with Authorization: Basic Zq9 here.")
    assert stop["last_message"] == "Use Bearer authentication with Authorization: [REDACTED]"
    stop = hook("Stop", last_assistant_message="Set " + "x" * 975 + f" ghp_{SECRET} as the token.")
    assert len(stop["last_message"]) == 1000 and not _leaks(stop["last_message"], SECRET)
    for event, data in (("PermissionRequest", {"tool_name": "Bash", "tool_input": {
            "command": "x", "description": f"Uses sk-ant-api03-{SECRET}"}}),
            ("Stop", {"last_assistant_message": f"Done: sk-ant-api03-{SECRET}"})):
        codex = attention._codex_notice(event, json.dumps({"session_id": SESSION, "cwd": "/x", **data}).encode())
        assert not _leaks(json.dumps(codex), SECRET) and "[REDACTED]" in json.dumps(codex)


def test_the_permission_hook_without_a_worker_prints_nothing(tmp_path, monkeypatch):
    stdin = json.dumps({"session_id": SESSION, "tool_name": "Bash", "tool_input": bash("ls")}).encode()
    assert _run_hook(monkeypatch, stdin, tmp_path / "none") == ""


def test_only_openswaps_permission_command_is_served(desktop, monkeypatch):
    stranger = Process(PID + 7, PID, "Fri Nov  6 14:00:02 2026", ("python3", "fake.py"))
    monkeypatch.setattr(peers, "peer_pid", lambda _c: stranger.pid)
    _running(monkeypatch, stranger)
    request = {"version": 1, "command": "permission", "session_id": SESSION, "tool_key": "f" * 32,
               "tool_name": "Bash"}
    for sent in (request, {k: v for k, v in request.items() if k != "tool_name"}):
        a, b = socket.socketpair()
        with a, b:
            channel_link.serve(desktop.store, a, sent, threading.Event(), lambda: None)
            assert json.loads(b.recv(4096)) in ({"behavior": None}, {"version": 1, "ok": False,
                                                                     "error": "unsupported_request"})
    assert desktop.store._waiters == []


# --- what the watch shows before Allow (R1a; F1, F3; G2, G3) -----------------------------------


def test_the_preview_shows_the_call_in_claude_codes_channel_form():
    tool_input = {"command": "ls -la", "description": "List files", "timeout": 5}
    assert attention.input_preview(tool_input) == (
        '{ "command": "ls -la", "description": "List files", "timeout": 5 }', True)
    assert attention.input_preview("ls") == ("", False) and attention.input_preview({}) == ("", False)


@pytest.mark.parametrize("tool_input", [
    bash("npm test"), bash("cat <<EOF\nline one\n\tline two\nEOF"), {"command": 'echo "quoted \\ back"'},
    {"file_path": "/repo/app.py", "content": "def f():\n\treturn 1\n"}, {"command": "echo café ✓ 漢字"},
    {"content": "def f():\n    if x:\n        return 1\n"}, {"command": "echo a" + " " * 16 + "b"},
    {"command": "printf 'a\x01b'"},
])
def test_a_preview_with_only_jsons_own_escapes_is_exact(tool_input):
    preview, exact = attention.input_preview(tool_input)
    assert exact and json.loads(preview.replace("{ ", "{").replace(", \"", ",\"")) == tool_input


@pytest.mark.parametrize(("tool_input", "shown"), [
    ({"command": "a" * 5000}, "the middle left out"),
    ({f"f{i}": i for i in range(200)}, "fields left out"),
    ({"command": "echo ok" + " " * 40 + "&& rm -rf ~"}, "40 spaces"),
    ({"content": "def f():\n" + " " * 17 + "return 1\n"}, "17 spaces"),
    ({"value": float("nan")}, "unserializable"),
])
def test_a_preview_that_changes_anything_is_not_exact_and_shows_the_change(tool_input, shown):
    preview, exact = attention.input_preview(tool_input)
    assert not exact and shown in preview and " " * 17 not in preview


def test_a_field_cut_to_the_smallest_share_still_shows_its_marker():
    preview, exact = attention.input_preview({f"f{i}": "v" * 200 for i in range(64)})
    assert not exact and len(preview) <= 4000 and preview.count("the middle left out") == 64
    assert all(f'"f{i}": "v' in preview for i in range(64))
    preview, _ = attention.input_preview({f"f{i}": "v" * 200 for i in range(65)})
    assert "⋯ 2 fields left out ⋯" in preview and len(preview) <= 4000


def test_a_long_call_is_cut_in_the_middle_of_each_field_within_the_cap():
    edit = {"file_path": "/repo/app.py", "old_string": "a" * 5000 + "OLD_END", "new_string": "NEW_START" + "b" * 5000}
    preview, exact = attention.input_preview(edit)
    assert not exact and len(preview) <= 4000
    assert '"file_path": "/repo/app.py"' in preview and "OLD_END" in preview and "NEW_START" in preview
    assert "the middle left out" in preview


@pytest.mark.parametrize("tool_input", [
    {"command": "x" * 300_000}, {f"{i}": "a" * 30 for i in range(500_000)}, {"a": "x" * 300_000, "b": "y" * 300_000},
    {"command": "echo " + "é" * 200_000}, {"command": "echo " + "\u00a0" * 200_000}, {"command": "\x01" * 200_000},
    {"command": "token=$( " * 29_000},
], ids=["past the budget", "many fields", "two big fields", "letters", "blanks", "controls", "token words"])
def test_the_preview_of_a_hostile_input_is_bounded(tool_input):
    started = time.perf_counter()
    preview, exact = attention.input_preview(tool_input)
    assert time.perf_counter() - started < 1.0 and len(preview) <= 4000


SECRET = "Zq9Xw7Vu5Ts3Rq1Po8Nm6Lk4Jh2Gf0Ed"


def _leaks(preview: str, secret: str) -> bool:
    return any(secret[i:i + 6] in preview for i in range(len(secret) - 5))


# --- G3: a call that may hold a credential is withheld -----------------------------------------


@pytest.mark.parametrize("tool_input", [
    bash(f"deploy --token ghp_{SECRET}"), bash(f"PGPASSWORD=x DB_PASSWORD={SECRET} psql"),
    {"command": "deploy", "env": [{"apiKey": SECRET}]}, {"content": f"{SECRET}\n-----END RSA PRIVATE KEY-----"},
    {"command": f"git clone https://ann:{SECRET}@x.test/r.git"}, {"content": SECRET + "x" * 300_000},
], ids=["token shape", "secret name", "nested key", "key block's end", "URL password", "past the budget"])
def test_g3_a_call_that_may_hold_a_credential_is_withheld_and_takes_deny_only(desktop, tool_input):
    assert attention.input_preview(tool_input) == ("", False)
    notice = hook("PermissionRequest", tool_name="Bash", tool_input=tool_input)
    assert notice["description"] == attention.PREVIEW_WITHHELD and not _leaks(json.dumps(notice), SECRET)
    t = desktop.clock.now
    desktop.store.notice(notice | {"at": t + 0.1})
    desktop.status("waiting", t + 0.05)
    desktop.store.notice(hook("Notification", notification_type="permission_prompt") | {"at": t + 6.1})
    desktop.clock.now = t + 7
    [item] = desktop.store._items
    now, desktop.clock.now = desktop.clock.now, item.created_at
    desktop.store.waiter("permission", CLAUDE, SESSION, attention.tool_key(tool_input), tool_name="Bash",
                         shown=attention.input_preview(tool_input)[0])
    desktop.clock.now = now
    [entry] = desktop.report()["items"]
    assert entry["detail"] == {"tool_name": "Bash", "description": attention.PREVIEW_WITHHELD}
    assert entry["actions"] == ["deny"]


def test_g3_an_ordinary_call_keeps_its_preview_and_allow(desktop):
    item = desktop.prompt("pytest tests/test_auth.py::test_max_tokens && cd $PWD", relay=False)
    desktop.permission(item, "pytest tests/test_auth.py::test_max_tokens && cd $PWD")
    assert "cd $PWD" in item.detail["input_preview"] and desktop.actions(item) == ["allow", "deny"]


# --- G2: only what shows as it reads counts as exact -------------------------------------------


SHOWN_AS_IS = "café naïve ß ñ 漢字 ✓ → € ½ ™"
HIDDEN = ["\u00a0", "\u2007", "\u3000", "\u0301", "\u20dd", "\ue000", "\u0378", "\ufeff", "\u200b", "\u202e",
          "\u2028", "\u2029", "\x7f", "\x85", "\ud800", "\u115f", "\u1160", "\u3164", "\uffa0", "\u2800",
          "\u034f", "\u17b4", "\u17b5", "\u180b", "\u180f", "\ufe00", "\ufe0f", "\U000e0100", "\U000e01ef",
          "\U000e0041"]
RIGHT_TO_LEFT = ["\u05e9\u05dc\u05d5\u05dd", "\u0645\u0631\u062d\u0628\u0627", "\u0661\u0662\u0663"]


def _whole(**entry):
    return store_module.questions_whole([{"question": "Pick one?", "header": "DB", "options": [{"label": "A"}],
                                          **entry}])


def test_g2_letters_numbers_punctuation_and_symbols_show_as_they_are():
    preview, exact = attention.input_preview({"command": f"echo {SHOWN_AS_IS}"})
    assert exact and SHOWN_AS_IS in preview
    assert _whole(question=f"Pick {SHOWN_AS_IS}?", header="Ü", options=[{"label": SHOWN_AS_IS}])


@pytest.mark.parametrize("char", HIDDEN, ids=lambda c: f"U+{ord(c):04X}")
def test_g2_a_hidden_or_blank_looking_character_is_shown_escaped_and_never_exact(char):
    escaped = json.dumps(char)[1:-1]
    preview, exact = attention.input_preview({"command": f"echo a{char}b"})
    assert not exact and f"a{escaped}b" in preview and char not in preview
    assert store_module.escape_hidden(f"a{char}b") == f"a{escaped}b"
    for entry in ({"question": f"Pick{char}?"}, {"header": f"D{char}B"}, {"options": [{"label": f"A{char}"}]}):
        assert not _whole(**entry)
    [shown] = shown_questions([{"question": f"Pick{char}?", "header": f"D{char}B", "options": [f"A{char}"]}])
    assert shown == {"question": f"Pick{escaped}?", "header": f"D{escaped}B", "options": [f"A{escaped}"]}


@pytest.mark.parametrize("text", RIGHT_TO_LEFT)
def test_g2_right_to_left_text_is_shown_as_it_is_but_never_exact(text):
    preview, exact = attention.input_preview({"command": f"echo {text}"})
    assert not exact and text in preview
    for entry in ({"question": f"Pick {text}?"}, {"header": text}, {"options": [{"label": text}]}):
        assert not _whole(**entry)


@pytest.mark.parametrize(("question", "whole"), [
    ("One\n\nTwo", True), ("One\r\n\r\nTwo", True), ("One\n\r\nTwo\n\nThree", True),
    ("One\n\n\nTwo", False), ("One\n \n\t\nTwo", False), ("One\r\n\r\n\r\nTwo", False), ("One\r\r\rTwo", False),
    ("A" + " " * 16 + "B", True), ("A" + " " * 17 + "B", False), (" Leading", False), ("Bell\x07", False),
    ("Q" * 1000, True), ("Q" * 1001, False), ("Uses Bearer authentication?", True),
    (f"Use ghp_{SECRET}?", False), ("Send Authorization: Basic Zq9?", False),
])
def test_g2_question_text_is_whole_only_while_nothing_could_hide_or_change_part_of_it(question, whole):
    assert _whole(question=question) is whole


@pytest.mark.parametrize("entry", [
    {"header": "H" * 101}, {"header": " DB"}, {"header": "Line\nbreak"}, {"header": "A" + " " * 17 + "B"},
    {"header": None}, {"options": [{"label": "A" + " " * 17 + "B"}]}, {"options": [{"label": f"ghp_{SECRET}"}]},
])
def test_g2_a_header_or_option_that_could_hide_or_change_part_of_itself_takes_no_answer(entry):
    assert _whole(header="DB") and not _whole(**entry)


def test_g2_a_question_shows_its_token_shapes_masked():
    [shown] = shown_questions([{"question": f"Use ghp_{SECRET}?", "options": [f"ghp_{SECRET}"]}])
    assert shown == {"question": "Use [REDACTED]?", "options": ["[REDACTED]"]}


def test_g2_the_worker_takes_no_exact_or_whole_claim_for_text_that_reads_right_to_left(desktop):
    text = RIGHT_TO_LEFT[0]
    item = desktop.prompt(relay=False)
    desktop.permission(item, preview=f'{{ "command": "echo {text}" }}', exact=True)
    assert desktop.actions(item) == ["deny"]
    questions = [{"question": f"Pick {text}?", "options": [{"label": "A"}, {"label": "B"}]}]
    waiter = desktop.store.waiter("question", CLAUDE, SESSION, attention.question_key({"questions": questions}),
                                  shown=shown_questions(questions), whole=True)
    assert waiter is not None and not waiter.whole


# --- G4: notices from older hooks ---------------------------------------------------------------


def test_g4_an_older_hooks_notice_is_accepted_with_its_text_cleaned_and_its_claim_ignored():
    notice = validate_notice({
        "provider": "claude", "event": "PermissionRequest", "session_id": SESSION, "cwd": "/x",
        "tool_name": "AskUserQuestion", "tool_key": "a" * 32, "answerable": True,
        "questions": [{"question": "Pick\u00a0one?", "header": "D\u200bB", "options": ["A", "B\u202e"]}]})
    assert "answerable" not in notice
    assert notice["questions"] == [{"question": "Pick\\u00a0one?", "header": "D\\u200bB",
                                    "options": ["A", "B\\u202e"]}]


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_g4_the_worker_masks_the_prose_of_every_notice(provider):
    for event, field in (("PermissionRequest", "description"), ("Stop", "last_message")):
        notice = validate_notice({"provider": provider, "event": event, "session_id": SESSION, "cwd": "/x",
                                  field: f"Push ghp_{SECRET} with Cookie: sid=Zq9"})
        assert notice[field] == "Push [REDACTED] with Cookie: [REDACTED]"
