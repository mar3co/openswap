"""Session attention replies (plan 025, phase S2): the channel, the question hook,
the reply gate and the heartbeat. Each "row N" test pins plan 025's Required
outcome N."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from openswap import attention, channel
from openswap.settings import AttentionSettings, configure_worker_service, update_worker_settings, \
    write_attention_settings
from openswap.worker import channel_link, ipc, peers
from openswap.worker.attention import AttentionStore, SessionRecord, validate_notice
from openswap.worker.peers import Process
from openswap.worker.protocol import ProtocolError, attention_reply, attention_report
from openswap.worker.refserver import ControlStore

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the worker and its hooks are POSIX-only")

SESSION = "3f2a9c1e-0000-4000-8000-000000000001"
OTHER = "77aa0000-0000-4000-8000-000000000002"
START = 1_800_000_000.0
PID = os.getpid()  # stands in for the Claude Code process: alive for the whole test
CLAUDE = Process(PID, 1, "Fri Nov  6 14:00:00 2026", ("claude", "--dangerously-load-development-channels",
                                                       "server:openswap"))
CHANNEL = Process(PID + 100000, PID, "Fri Nov  6 14:00:01 2026", ("openswap", "attention", "channel"))


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class FakeLink:
    def __init__(self, ok=True):
        self.sent, self.ok, self.closed = [], ok, False

    def send(self, message):
        self.sent.append(message)
        return self.ok

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _hooks_run_under_the_test(monkeypatch):
    monkeypatch.setattr(attention, "claude_pid", lambda: PID)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


def hook(event, session=SESSION, **fields):
    data = {"session_id": session, "cwd": "/Users/o/code/app", "hook_event_name": event, **fields}
    return validate_notice(attention._hook_notice(event, json.dumps(data).encode()))


def bash(command):
    return {"command": command, "description": "Run the tests"}


QUESTIONS = [{"question": "Which database?", "header": "DB", "multiSelect": False,
              "options": [{"label": "Postgres", "description": "x"}, {"label": "SQLite", "description": "y"}]}]


class Env:
    def __init__(self, tmp_path):
        self.clock = Clock()
        self.home = tmp_path / "home"
        (self.home / "sessions").mkdir(parents=True)
        self.switches = AttentionSettings(notices=True, details=True, replies=True)
        self.store = AttentionStore(tmp_path / "root", clock=self.clock, switches=lambda: self.switches,
                                    homes=lambda: [self.home])
        self.link = FakeLink()

    def at(self, seconds):
        return {"at": self.clock.now + seconds}

    def status(self, status, at, session=SESSION):
        """The session file now says ``status`` since ``at``, and the worker read it."""
        (self.home / "sessions" / f"{PID}.json").write_text(json.dumps(
            {"pid": PID, "sessionId": session, "status": status, "statusUpdatedAt": at * 1000}))
        self.store.observe({session: SessionRecord(session, PID, status, at)}, True)

    def register(self):
        self.status("busy", self.clock.now - 10)
        assert self.store.register_channel(CHANNEL, CLAUDE, self.link)

    def relay(self, tool_input, tool="Bash", request_id="abcde"):
        """The channel relays Claude Code's permission request for ``tool_input``."""
        self.store.channel_request(PID, self.link, {
            "request_id": request_id, "tool_name": tool, "description": "Run the tests",
            "input_preview": attention.channel_preview(tool_input)})

    def prompt(self, command="npm test", *, relay=True, agent=None, offset=0.0):
        """A permission dialog for ``command`` that waits and is published; returns its item."""
        t = self.clock.now + offset
        if relay:
            self.clock.now = t
            self.relay(bash(command))
        self.store.notice(hook("PermissionRequest", tool_name="Bash", tool_input=bash(command),
                               **({"agent_id": agent} if agent else {})) | {"at": t + 0.1})
        self.status("waiting", t + 0.05)
        self.store.notice(hook("Notification", notification_type="permission_prompt") | {"at": t + 6.1})
        self.clock.now = t + 7
        return next(i for i in self.store._items if i.created_at == t + 0.1)

    def question(self):
        t = self.clock.now
        self.store.notice(hook("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": QUESTIONS})
                          | {"at": t + 0.1})
        self.status("waiting", t + 0.05)
        self.store.notice(hook("Notification", notification_type="permission_prompt") | {"at": t + 6.1})
        self.clock.now = t + 7
        return next(i for i in self.store._items if i.kind == "question")

    def idle(self):
        self.status("idle", self.clock.now)
        self.store.notice(hook("Notification", notification_type="idle_prompt") | self.at(60))
        self.clock.now += 61
        return next(i for i in self.store._items if i.kind == "idle")

    def report(self):
        return self.store.report(notices=self.switches.notices, details=self.switches.details,
                                 replies=self.switches.replies, folder_label=lambda cwd: None)[0]

    def actions(self, item):
        return next(i["actions"] for i in self.report()["items"] if i["id"] == item.id)

    def reply(self, item_id, action, reply_id="r1", expires=600, **extra):
        self.store.queue_replies([{"id": reply_id, "item_id": item_id, "action": action,
                                   "expires_at": stamp(self.clock.now + expires), **extra}])
        self.store.apply_replies(self.clock.now)

    def outcomes(self):
        """The outcomes the next heartbeat carries (and the service then takes)."""
        acks = self.store.take_acks()
        self.store.acks_sent(acks)
        return {a["id"]: a["outcome"] for a in acks}

    def closes(self):
        return {c["id"]: c["reason"] for c in self.report()["closed"]}


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.register()
    return e


# --- how a channel previews a call -----------------------------------------------


def test_the_preview_is_rendered_exactly_as_claude_code_relays_it():
    """Shapes copied from Claude Code 2.1.311's real permission_request messages (phase 0)."""
    assert attention.channel_preview({"command": "touch /tmp/r1/ran-22", "description": "Touch marker file 22"}) \
        == '{ "command": "touch /tmp/r1/ran-22", "description": "Touch marker file 22" }'
    assert attention.channel_preview({"file_path": "/tmp/w.txt", "content": "token=x hunter2\n"}) \
        == '{ "file_path": "/tmp/w.txt", "content": "token=x hunter2\\n" }'
    assert attention.channel_preview({"a": {"b": [1, True, None]}}) == '{ "a": {"b":[1,true,null]} }'


@pytest.mark.parametrize("tool_input", [
    {"command": "rm -rf /tmp/a  b"},  # Claude Code collapses the double space: two inputs, one preview
    {"command": "echo café"},  # non-ASCII may be rewritten (look-alike quotes become ·)
    {"command": "echo [REDACTED]"},  # indistinguishable from a masked credential
    {"command": "x" * 3501},  # cut in the middle by Claude Code
    {"value": 1.5e300 * 1e10},  # not JSON
    ["not", "an", "object"],
])
def test_a_preview_that_would_not_show_the_input_exactly_has_no_key(tool_input):
    assert attention.channel_preview(tool_input) is None


# --- row 8: Allow never approves a call other than the one shown --------------------


def test_row8_a_paired_prompt_shows_the_channel_preview_and_allow_names_its_request(env):
    item = env.prompt("npm test")
    assert env.actions(item) == ["allow", "deny"]
    [shown] = env.report()["items"]
    assert shown["detail"] == {"tool_name": "Bash", "description": "Run the tests",
                               "input_preview": attention.channel_preview(bash("npm test"))}
    assert "request_id" not in json.dumps(env.report())  # the request ID never leaves the Mac
    env.reply(item.id, "allow")
    assert env.link.sent == [{"verdict": {"request_id": "abcde", "behavior": "allow"}}]
    assert env.outcomes() == {"r1": "sent"}


@pytest.mark.parametrize("case", ["other_input", "other_tool", "no_request", "no_preview", "late_request"])
def test_row8_a_prompt_without_its_own_request_is_notice_only(env, case):
    command = "echo café" if case == "no_preview" else "npm test"  # no exact preview: no key
    if case == "other_input":
        env.relay(bash("npm test -- --all"))
    elif case == "other_tool":
        env.relay(bash("npm test"), tool="Write")
    elif case == "no_preview":
        env.store.channel_request(PID, env.link, {"request_id": "abcde", "tool_name": "Bash",
                                                  "input_preview": '{ "command": "echo caf\\u00e9" }'})
    elif case == "late_request":
        env.clock.now -= 30
        env.relay(bash("npm test"))
        env.clock.now += 30
    item = env.prompt(command, relay=False)
    assert env.actions(item) == []
    env.reply(item.id, "allow")
    assert env.link.sent == [] and env.outcomes() == {"r1": "refused"}


def test_row8_two_waiting_prompts_with_the_same_preview_are_both_notice_only(env):
    """Two subagents asking for the same command: their requests cannot be told apart."""
    t = env.clock.now
    env.relay(bash("npm test"))
    env.store.notice(hook("PermissionRequest", tool_name="Bash", tool_input=bash("npm test"), agent_id="a1")
                     | {"at": t + 0.1})
    env.clock.now = t + 0.4
    env.relay(bash("npm test"), request_id="fghij")
    env.store.notice(hook("PermissionRequest", tool_name="Bash", tool_input=bash("npm test"), agent_id="a2")
                     | {"at": t + 0.5})
    env.status("waiting", t + 0.05)
    for seconds in (6.1, 6.5):
        env.store.notice(hook("Notification", notification_type="permission_prompt") | {"at": t + seconds})
    first, second = sorted(env.store._items, key=lambda i: i.created_at)
    assert first.published and second.published
    assert env.actions(first) == [] and env.actions(second) == []


def test_row8_allow_is_offered_only_while_details_show_the_call(env):
    item = env.prompt()
    env.switches = AttentionSettings(notices=True, details=False, replies=True)
    assert env.actions(item) == ["deny"]
    assert "detail" not in env.report()["items"][0]
    env.reply(item.id, "allow")
    assert env.outcomes() == {"r1": "refused"} and env.link.sent == []


# --- row 9: a request ID never carries over ---------------------------------------------------


def test_row9_a_closed_prompt_takes_its_request_id_with_it(env):
    item = env.prompt("npm test")
    env.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("npm test")) | env.at(0))
    assert env.closes() == {item.id: "answered_on_mac"}
    env.reply(item.id, "allow")
    assert env.outcomes() == {"r1": "item_closed"}
    assert env.store._attached == {} and env.store._requests == []
    # The same command asked again pairs with its own request, never the old one.
    env.clock.now += 30
    env.relay(bash("npm test"), request_id="zzzzz")
    again = env.prompt("npm test", relay=False, offset=0.0)
    env.reply(again.id, "allow", reply_id="r2")
    assert env.link.sent == [{"verdict": {"request_id": "zzzzz", "behavior": "allow"}}]


# --- rows 10-12, 15: settling a verdict ---------------------------------------------


def test_row10_a_deny_that_takes_effect_closes_from_the_watch_and_is_applied(env):
    item = env.prompt()
    env.reply(item.id, "deny")
    assert env.outcomes() == {"r1": "sent"}
    sent_at = env.clock.now
    env.store.notice(hook("Stop", last_assistant_message="Understood.") | {"at": sent_at + 0.3})
    assert env.closes() == {item.id: "answered_from_watch"}
    assert env.outcomes() == {"r1": "applied"}


def test_row10_a_deny_seen_only_in_the_session_file_is_applied(env):
    item = env.prompt()
    env.reply(item.id, "deny")
    env.status("idle", env.clock.now + 0.1)
    assert env.closes() == {item.id: "answered_from_watch"}
    assert env.outcomes() == {"r1": "applied"}


def test_an_allow_is_applied_when_the_dialog_closes_even_before_the_call_ends(env):
    item = env.prompt()
    env.reply(item.id, "allow")
    env.status("busy", env.clock.now + 0.05)
    assert env.closes() == {item.id: "answered_from_watch"}
    assert env.outcomes() == {"r1": "applied"}


@pytest.mark.parametrize("event", ["PostToolUse", "PostToolUseFailure"])
def test_row11_an_allowed_call_that_runs_or_fails_is_applied(env, event):
    item = env.prompt()
    env.reply(item.id, "allow")
    env.store.notice(hook(event, tool_name="Bash", tool_input=bash("npm test")) | env.at(0.5))
    assert env.closes() == {item.id: "answered_from_watch"}
    assert env.outcomes() == {"r1": "applied"}


def test_an_allow_whose_stop_overtakes_its_post_tool_use_is_still_applied(env):
    """Hooks are async: the turn's Stop may reach the worker before the call's PostToolUse."""
    item = env.prompt()
    env.reply(item.id, "allow")
    env.store.notice(hook("Stop", last_assistant_message="Done.") | env.at(0.6))
    assert item in env.store._items
    env.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("npm test")) | env.at(0.5))
    assert env.closes() == {item.id: "answered_from_watch"}


def test_row12_an_answer_at_the_mac_before_the_reply_arrives_sends_nothing(env):
    item = env.prompt()
    env.status("idle", env.clock.now - 0.5)  # "No" at the Mac
    env.reply(item.id, "allow")
    assert env.link.sent == [] and env.outcomes() == {"r1": "item_closed"}


def test_row12_a_dialog_closed_long_after_the_verdict_was_closed_at_the_mac(env):
    """The verdict never decided it (Claude Code dropped it): the Mac's Yes did, later."""
    item = env.prompt()
    env.reply(item.id, "allow")
    env.status("busy", env.clock.now + 30)
    env.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("npm test")) | env.at(31))
    assert env.closes() == {item.id: "answered_on_mac"}
    assert env.outcomes() == {"r1": "sent"}


def test_row12_a_deny_overruled_by_a_yes_at_the_mac_is_not_applied(env):
    item = env.prompt()
    env.reply(item.id, "deny")
    env.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("npm test")) | env.at(0.5))
    assert env.closes() == {item.id: "answered_on_mac"}
    assert env.outcomes() == {"r1": "sent"}


def test_row12_an_allow_overruled_by_a_no_at_the_mac_settles_after_the_grace(env):
    item = env.prompt()
    env.reply(item.id, "allow")
    env.status("idle", env.clock.now + 0.1)  # declined: the allow did not decide it
    env.clock.now += 5
    env.store.tick()
    assert env.closes() == {item.id: "answered_on_mac"}
    assert env.outcomes() == {"r1": "sent"}


def test_row15_a_reply_still_sent_when_the_owner_types_settles_as_not_applied(env):
    item = env.idle()
    env.reply(item.id, "message", text="Run the tests")
    assert env.outcomes() == {"r1": "sent"}
    env.store.notice(hook("UserPromptSubmit", prompt="never mind") | env.at(0.2))
    assert env.closes() == {item.id: "answered_on_mac"}
    assert env.outcomes() == {}


# --- rows 13, 14: subagents ----------------------------------------------------------------


def test_row13_another_agent_finishing_the_same_call_leaves_the_prompt_open(env):
    item = env.prompt("npm test")
    env.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("npm test"), agent_id="a9") | env.at(1))
    assert item in env.store._items
    env.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("npm test")) | env.at(2))
    assert env.closes() == {item.id: "answered_on_mac"}


def test_row14_a_denied_subagent_prompt_closes_with_the_prompt_not_the_main_turn(env):
    item = env.prompt("npm test", agent="a7")
    env.reply(item.id, "deny")
    # Another subagent's prompt keeps the file waiting; the main turn ends meanwhile.
    env.store.notice(hook("Stop", last_assistant_message="Launched.") | env.at(0.2))
    assert item in env.store._items
    # The subagent's next call shows its prompt is gone.
    env.store.notice(hook("PermissionRequest", tool_name="Bash", tool_input=bash("npm run lint"), agent_id="a7")
                     | env.at(1.0))
    assert env.closes() == {item.id: "answered_from_watch"}
    assert env.outcomes() == {"r1": "applied"}


# --- rows 16-18: messages and channel turns ----------------------------------------------


def test_a_message_starts_the_turn_and_is_applied_by_its_own_token(env):
    item = env.idle()
    assert env.actions(item) == ["message"]
    env.reply(item.id, "message", text="Run the tests")
    [sent] = env.link.sent
    token = sent["message"]["reply"]
    assert sent["message"]["content"] == "Run the tests"
    env.store.notice(hook("UserPromptSubmit", prompt=f'<channel source="openswap" reply="{token}">\nRun the '
                                                     'tests\n</channel>') | env.at(0.1))
    assert env.closes() == {item.id: "answered_from_watch"}
    assert env.outcomes() == {"r1": "applied"}


def test_a_channel_turn_with_another_token_applies_nothing(env):
    item = env.idle()
    env.reply(item.id, "message", text="Run the tests")
    env.store.notice(hook("UserPromptSubmit", prompt='<channel source="openswap" reply="0000">\nx\n</channel>')
                     | env.at(0.1))
    assert item in env.store._items and env.outcomes() == {"r1": "sent"}


@pytest.mark.parametrize("ending", ["typed", "clear", "session_end"])
def test_row16_a_reply_to_an_idle_item_closed_before_it_arrives_starts_nothing(env, ending):
    item = env.idle()
    if ending == "typed":
        env.store.notice(hook("UserPromptSubmit", prompt="hello") | env.at(0))
    else:
        env.store.notice(hook("SessionEnd", reason="clear" if ending == "clear" else "exit") | env.at(0))
    env.reply(item.id, "message", text="Run the tests")
    assert env.link.sent == [] and env.outcomes() == {"r1": "item_closed"}


def test_row16_a_message_for_a_session_the_channel_no_longer_serves_starts_nothing(env):
    item = env.idle()
    env.status("idle", env.clock.now, session=OTHER)  # /clear moved the binding; SessionEnd not seen yet
    env.store.observe({SESSION: SessionRecord(SESSION, PID, "idle", env.clock.now)}, True)
    env.reply(item.id, "message", text="x")
    assert env.link.sent == [] and env.outcomes() == {"r1": "item_closed"}


def test_row17_a_turn_from_another_channel_answers_like_typed_input(env):
    prompt = env.prompt()
    env.store.notice(hook("UserPromptSubmit", prompt='<channel source="telegram" chat_id="1">\nhi\n</channel>')
                     | env.at(0))
    assert env.closes() == {prompt.id: "answered_on_mac"}


def test_row18_openswaps_channel_turn_closes_a_stale_prompt_as_answered_on_the_mac(env):
    item = env.prompt()
    env.store.notice(hook("UserPromptSubmit", prompt='<channel source="openswap" reply="ab12">\nx\n</channel>')
                     | env.at(0))
    assert env.closes() == {item.id: "answered_on_mac"}


# --- questions -------------------------------------------------------------------------------


def test_a_question_takes_answers_only_while_its_hook_waits_and_applies_the_match(env):
    item = env.question()
    assert env.actions(item) == []  # no hook waiting: nothing to hand an answer to
    waiter = env.store.question_waiter(CLAUDE, SESSION, attention.question_key({"questions": QUESTIONS}))
    assert env.actions(item) == ["answer"]
    env.reply(item.id, "answer", answers=[{"question": 0, "option": 1}])
    assert waiter.done.is_set() and waiter.answers == [[0, 1]]
    assert env.outcomes() == {}  # "sent" only once the hook took it
    env.store.handed_over(waiter.reply_id)
    assert env.outcomes() == {"r1": "sent"} and env.link.sent == []
    answered = {"questions": QUESTIONS, "answers": {"Which database?": "SQLite"}}
    env.store.notice(hook("PostToolUse", tool_name="AskUserQuestion", tool_input=answered) | env.at(0.2))
    assert env.closes() == {item.id: "answered_from_watch"}
    assert env.outcomes() == {"r1": "applied"}


def test_a_question_answered_otherwise_at_the_mac_is_not_applied(env):
    item = env.question()
    waiter = env.store.question_waiter(CLAUDE, SESSION, attention.question_key({"questions": QUESTIONS}))
    env.reply(item.id, "answer", answers=[{"question": 0, "option": 1}])
    env.store.handed_over(waiter.reply_id)
    answered = {"questions": QUESTIONS, "answers": {"Which database?": "Postgres"}}
    env.store.notice(hook("PostToolUse", tool_name="AskUserQuestion", tool_input=answered) | env.at(0.2))
    assert env.closes() == {item.id: "answered_on_mac"} and env.outcomes() == {"r1": "sent"}


@pytest.mark.parametrize("answers", [[{"question": 0, "option": 2}], [{"question": 1, "option": 0}],
                                     [{"question": 0, "option": 0}, {"question": 1, "option": 0}]])
def test_an_answer_that_does_not_fit_the_question_is_refused(env, answers):
    item = env.question()
    waiter = env.store.question_waiter(CLAUDE, SESSION, attention.question_key({"questions": QUESTIONS}))
    try:
        attention_reply({"id": "r1", "item_id": item.id, "action": "answer", "answers": answers,
                         "expires_at": stamp(env.clock.now + 600)})
    except ProtocolError:
        pass
    env.reply(item.id, "answer", answers=answers)
    assert not waiter.done.is_set() and env.outcomes() == {"r1": "refused"}


def test_the_question_hook_is_served_only_in_its_own_channel_session(env):
    key = attention.question_key({"questions": QUESTIONS})
    assert env.store.question_waiter(CLAUDE, OTHER, key) is None  # another session's hook
    stranger = Process(PID + 5, 1, "Fri Nov  6 13:00:00 2026", ("claude",))
    assert env.store.question_waiter(stranger, SESSION, key) is None  # a Claude Code process with no channel
    env.switches = AttentionSettings(notices=True, details=True, replies=False)
    assert env.store.question_waiter(CLAUDE, SESSION, key) is None  # replies off
    env.switches = AttentionSettings(notices=True, details=True, replies=True)
    assert env.store.question_waiter(CLAUDE, SESSION, key) is not None


def test_a_waiting_question_hook_ends_when_its_question_closes_or_was_answered(env):
    key = attention.question_key({"questions": QUESTIONS})
    waiter = env.store.question_waiter(CLAUDE, SESSION, key)
    item = env.question()
    assert not env.store.waiter_over(waiter)
    env.store.notice(hook("PostToolUse", tool_name="AskUserQuestion", tool_input={"questions": QUESTIONS})
                     | env.at(1))
    assert item not in env.store._items and env.store.waiter_over(waiter)
    # Never matched to an item (its notice was lost): it ends once the session stopped waiting.
    lone = env.store.question_waiter(CLAUDE, SESSION, "f" * 32)
    assert not env.store.waiter_over(lone)
    env.status("busy", env.clock.now + 1)
    assert env.store.waiter_over(lone)


def test_a_waiting_question_hook_gives_up_before_its_own_timeout(env):
    waiter = env.store.question_waiter(CLAUDE, SESSION, "f" * 32)
    env.clock.now += 589
    assert not env.store.waiter_over(waiter)
    env.clock.now += 1
    assert env.store.waiter_over(waiter)


def test_answers_map_positions_to_the_raw_labels_by_position():
    raw = [{"question": "Pick\x07 one", "options": [{"label": ""}, {"label": "A"}, {"label": "B"}]}]
    assert attention._answers(raw, [[0, 1]]) == {"Pick\x07 one": "B"}  # the empty label was never shown
    assert attention._answers(raw, [[0, 2]]) == {}
    assert attention._answers(raw, []) == {}
    assert attention._answers(raw, None) == {}


def _fake_worker(tmp_path, answer):
    """A worker socket that answers one question request with ``answer``."""
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
            seen.append(json.loads(connection.makefile("rb").readline()))
            connection.sendall(json.dumps(answer).encode() + b"\n")
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return root, seen


@pytest.mark.parametrize(("answer", "printed"), [({"answers": [[0, 1]]}, True), ({"answers": None}, False),
                                                 ({"answers": [[0, 9]]}, False)])
def test_the_question_hook_prints_a_decision_only_for_a_fitting_answer(tmp_path, monkeypatch, answer, printed):
    root, seen = _fake_worker(tmp_path, answer)
    tool_input = {"questions": QUESTIONS}
    stdin = json.dumps({"session_id": SESSION, "tool_name": "AskUserQuestion", "tool_input": tool_input}).encode()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert attention.question_main(backup_root=root) == 0
    assert seen[0] == {"version": 1, "command": "question", "session_id": SESSION,
                       "tool_key": attention.question_key(tool_input)}
    if printed:
        decision = json.loads(out.getvalue())["hookSpecificOutput"]["decision"]
        assert decision == {"behavior": "allow", "updatedInput": {**tool_input,
                                                                   "answers": {"Which database?": "SQLite"}}}
    else:
        assert out.getvalue() == ""


def test_the_question_hook_without_a_worker_prints_nothing(tmp_path, monkeypatch):
    stdin = json.dumps({"session_id": SESSION, "tool_name": "AskUserQuestion",
                        "tool_input": {"questions": QUESTIONS}}).encode()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    assert attention.question_main(backup_root=tmp_path / "none") == 0 and out.getvalue() == ""


# --- the reply gate ---------------------------------------------------------------------


def test_a_reply_is_applied_at_most_once_and_answered_again_with_its_outcome(env):
    item = env.idle()
    env.reply(item.id, "message", text="Run the tests")
    assert env.outcomes() == {"r1": "sent"}
    env.reply(item.id, "message", text="Run the tests")  # the acknowledgement was lost: sent again
    assert len(env.link.sent) == 1 and env.outcomes() == {"r1": "sent"}


def test_a_handled_reply_survives_a_worker_restart(env, tmp_path):
    item = env.idle()
    env.reply(item.id, "message", text="Run the tests")
    again = AttentionStore(tmp_path / "root", clock=env.clock, switches=lambda: env.switches,
                           homes=lambda: [env.home])
    again.queue_replies([{"id": "r1", "item_id": item.id, "action": "message", "text": "Run the tests",
                          "expires_at": stamp(env.clock.now + 600)}])
    again.apply_replies(env.clock.now)
    assert {a["id"]: a["outcome"] for a in again.take_acks()} == {"r1": "sent"}


@pytest.mark.parametrize(("change", "outcome"), [("expired", "expired"), ("replies_off", "refused"),
                                                 ("notices_off", "refused"), ("unknown_item", "item_closed"),
                                                 ("wrong_action", "refused"), ("no_channel", "refused"),
                                                 ("send_fails", "refused")])
def test_the_gate_drops_what_it_cannot_deliver(env, change, outcome):
    item = env.idle()
    expires, item_id, action = 600, item.id, "message"
    if change == "expired":
        expires = -1
    elif change == "replies_off":
        env.switches = AttentionSettings(notices=True, details=True, replies=False)
    elif change == "notices_off":
        env.switches = AttentionSettings(notices=False, details=True, replies=True)
    elif change == "unknown_item":
        item_id = "nope"
    elif change == "wrong_action":
        action = "deny"
    elif change == "no_channel":
        env.store.unregister_channel(PID, env.link)
    elif change == "send_fails":
        env.link.ok = False
    if action == "message":
        env.reply(item_id, action, expires=expires, text="hello")
    else:
        env.reply(item_id, action, expires=expires)
    assert env.outcomes() == {"r1": outcome}
    assert item in env.store._items and env.store._sent == {}


def test_a_malformed_reply_with_an_id_is_refused_and_without_one_ignored(env):
    env.store.queue_replies([{"id": "r1", "item_id": "x", "action": "message", "expires_at": stamp(START)},
                             {"item_id": "x"}, "junk"])
    env.store.apply_replies(env.clock.now)
    assert env.outcomes() == {"r1": "refused"}


def test_acks_go_until_the_service_takes_them_and_applied_replaces_sent(env):
    item = env.prompt()
    env.reply(item.id, "allow")
    acks = env.store.take_acks()
    env.status("busy", env.clock.now + 0.05)
    env.store.acks_sent(acks)  # the "sent" heartbeat returns after "applied" was queued
    assert env.store.take_acks() == [{"id": "r1", "outcome": "applied"}]


# --- channel registration -----------------------------------------------------------------


def test_only_the_first_channel_of_a_claude_code_process_is_registered(env, tmp_path):
    impostor = Process(PID + 200000, PID, "Fri Nov  6 14:05:00 2026", ("openswap", "attention", "channel"))
    assert not env.store.register_channel(impostor, CLAUDE, FakeLink())
    # Its own channel reconnecting (the worker restarted, or the socket dropped) is fine.
    assert env.store.register_channel(CHANNEL, CLAUDE, FakeLink())
    # ...and the hold survives a worker restart.
    restarted = AttentionStore(tmp_path / "root", clock=env.clock, switches=lambda: env.switches,
                               homes=lambda: [env.home])
    assert not restarted.register_channel(impostor, CLAUDE, FakeLink())
    assert restarted.register_channel(CHANNEL, CLAUDE, FakeLink())


def test_a_reused_pid_starts_a_new_registration(env):
    later = Process(PID, 1, "Sat Nov  7 09:00:00 2026", CLAUDE.args)
    other = Process(PID + 300000, PID, "Sat Nov  7 09:00:01 2026", CHANNEL.args)
    assert env.store.register_channel(other, later, FakeLink())


def test_a_claude_code_process_outside_the_managed_homes_cannot_register(env):
    (env.home / "sessions" / f"{PID}.json").unlink()
    assert not env.store.register_channel(CHANNEL, CLAUDE, FakeLink())


@pytest.mark.parametrize(("args", "named"), [
    (("claude", "--dangerously-load-development-channels", "server:openswap"), True),
    (("claude", "--channels", "plugin:x@y,server:openswap", "-c"), True),
    (("claude", "--dangerously-load-development-channels=server:openswap"), True),
    (("claude", "--dangerously-load-development-channels", "server:openswap2"), False),
    (("claude", "--mcp-config", "server:openswap"), False),
    (("claude", "-c", "server:openswap"), False),
])
def test_the_channel_must_be_named_on_claude_codes_command_line(args, named):
    assert peers.names_channel(args) is named


class _Conn:
    """A connection whose peer is ``peer``; records what the worker answers."""

    def __init__(self, lines=()):
        self.out, self.lines = b"", list(lines)

    def sendall(self, data):
        self.out += data

    def settimeout(self, _):
        pass

    def recv(self, _):
        return self.lines.pop(0) if self.lines else b""

    def shutdown(self, _):
        pass


@pytest.mark.parametrize("tree", ["not_a_child", "not_named", "no_process"])
def test_a_channel_connection_outside_a_channel_session_is_refused(env, monkeypatch, tree):
    procs = {CHANNEL.pid: CHANNEL, PID: CLAUDE}
    if tree == "not_a_child":
        procs[CHANNEL.pid] = Process(CHANNEL.pid, 4242, CHANNEL.started, CHANNEL.args)
    elif tree == "not_named":
        procs[PID] = Process(PID, 1, CLAUDE.started, ("claude",))
    elif tree == "no_process":
        procs.clear()
    monkeypatch.setattr(peers, "peer_pid", lambda c: CHANNEL.pid)
    monkeypatch.setattr(peers, "process", procs.get)
    store = AttentionStore(env.home.parent / "fresh", clock=env.clock, homes=lambda: [env.home])
    connection, released = _Conn(), []
    channel_link.serve(store, connection, {"version": 1, "command": "channel"}, threading.Event(),
                       lambda: released.append(1))
    assert json.loads(connection.out) == {"version": 1, "ok": False, "error": "channel_refused"}
    assert released == [] and store._channels == {}


def test_a_channel_connection_relays_prompts_and_ends_cleanly(env, monkeypatch):
    monkeypatch.setattr(peers, "peer_pid", lambda c: CHANNEL.pid)
    monkeypatch.setattr(peers, "process", {CHANNEL.pid: CHANNEL, PID: CLAUDE}.get)
    store = AttentionStore(env.home.parent / "fresh", clock=env.clock, homes=lambda: [env.home])
    request = {"request_id": "abcde", "tool_name": "Bash", "input_preview": attention.channel_preview(bash("x"))}
    connection = _Conn([json.dumps({"permission_request": request}).encode() + b"\n"])
    channel_link.serve(store, connection, {"version": 1, "command": "channel"}, threading.Event(), lambda: None)
    assert json.loads(connection.out.splitlines()[0]) == {"version": 1, "ok": True, "result": {}}
    assert [r.request_id for r in store._requests] == ["abcde"]
    assert store._channels[PID].link is None  # gone with its connection, still held


def test_a_hook_connection_never_receives_anything_but_an_acknowledgement(env, monkeypatch):
    """Notices from any process (a forged one too) carry no actions and get only {}."""
    request = {"version": 1, "command": "notice", "notice": hook("Notification", notification_type="idle_prompt")}
    assert ipc._dispatch(request, None, env.store) == ({}, None)
    connection = _Conn()
    monkeypatch.setattr(peers, "peer_pid", lambda c: 4242)
    monkeypatch.setattr(peers, "hook_claude", lambda pid: Process(4241, 1, "Fri Nov  6 13:00:00 2026", ("claude",)))
    channel_link.serve(env.store, connection, {"version": 1, "command": "question", "session_id": SESSION,
                                               "tool_key": "a" * 32}, threading.Event(), lambda: None)
    assert json.loads(connection.out) == {"answers": None}  # a Claude Code process without a channel


# --- the channel server -------------------------------------------------------------------


def _rpc(server, message):
    server.stdout = io.StringIO()
    server.handle(message)
    return [json.loads(line) for line in server.stdout.getvalue().splitlines()]


def test_the_channel_pins_its_mcp_revision_and_declares_the_channel_capabilities():
    server = channel.Channel(stdout=io.StringIO())
    [answer] = _rpc(server, {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                             "params": {"protocolVersion": "2026-07-28"}})
    assert answer["result"]["protocolVersion"] == "2025-11-25"
    assert answer["result"]["capabilities"] == {"experimental": {"claude/channel": {},
                                                                 "claude/channel/permission": {}}}
    assert "paired iPhone or Apple Watch" in answer["result"]["instructions"]
    [refused] = _rpc(server, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    assert refused["error"]["code"] == -32601
    assert _rpc(server, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == []


def test_the_channel_forwards_prompts_and_applies_verdicts_and_messages():
    server = channel.Channel(stdout=io.StringIO())
    forwarded = []
    server.send_worker = forwarded.append
    _rpc(server, {"jsonrpc": "2.0", "method": "notifications/claude/channel/permission_request",
                  "params": {"request_id": "abcde", "tool_name": "Bash", "description": "d",
                             "input_preview": "x" * 4001, "extra": 1}})
    assert forwarded == [{"permission_request": {"request_id": "abcde", "tool_name": "Bash", "description": "d"}}]
    server.stdout = io.StringIO()
    server.from_worker({"verdict": {"request_id": "abcde", "behavior": "allow"}})
    server.from_worker({"verdict": {"request_id": "abcde", "behavior": "always"}})
    server.from_worker({"message": {"content": "Run the tests", "reply": "ab12"}})
    assert [json.loads(line) for line in server.stdout.getvalue().splitlines()] == [
        {"jsonrpc": "2.0", "method": "notifications/claude/channel/permission",
         "params": {"request_id": "abcde", "behavior": "allow"}},
        {"jsonrpc": "2.0", "method": "notifications/claude/channel",
         "params": {"content": "Run the tests", "meta": {"reply": "ab12"}}}]


def test_the_channel_registers_with_a_real_worker_socket(tmp_path):
    root = tmp_path / "w"
    path = ipc.socket_path(root)
    path.parent.mkdir(parents=True, mode=0o700)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    client = channel.Channel(stdout=io.StringIO(), backup_root=root)
    result = {}

    def accept():
        connection, _ = server.accept()
        reader = connection.makefile("rb")
        result["hello"] = json.loads(reader.readline())
        connection.sendall(b'{"version":1,"ok":true,"result":{}}\n')
        connection.sendall(b'{"verdict":{"request_id":"abcde","behavior":"deny"}}\n')
        connection.close()
        server.close()

    threading.Thread(target=accept, daemon=True).start()
    connection, reader = client._connect()
    assert result["hello"] == {"version": 1, "command": "channel"}
    client.from_worker(json.loads(reader.readline()))
    assert json.loads(client.stdout.getvalue())["params"] == {"request_id": "abcde", "behavior": "deny"}
    connection.close()


# --- the launcher and the hook installs ----------------------------------------------------


def test_the_launcher_names_the_channel_and_installs_nothing_into_config_homes(tmp_path, monkeypatch):
    monkeypatch.setattr(attention, "program", lambda: "/Apps/OpenSwap.app/Contents/MacOS/OpenSwap")
    calls = []
    code = attention.launch_claude(["-c", "--model", "x"], backup_root=tmp_path, which=lambda name: "/bin/claude",
                                   execv=lambda path, argv: calls.append((path, argv)))
    config = tmp_path / "worker" / "channel-mcp.json"
    assert code == 0 and calls == [("/bin/claude", ["/bin/claude", f"--mcp-config={config}",
                                                    "--dangerously-load-development-channels=server:openswap",
                                                    "-c", "--model", "x"])]
    # A prompt or subcommand after the launcher's own options stays Claude Code's.
    calls.clear()
    attention.launch_claude(["fix the failing test"], backup_root=tmp_path, which=lambda name: "/bin/claude",
                            execv=lambda path, argv: calls.append(argv))
    assert calls[0][-1] == "fix the failing test" and all("=" in a for a in calls[0][1:3])
    assert peers.names_channel(tuple(calls[0]))

    def broken(path, argv):
        raise PermissionError("not executable")

    assert attention.launch_claude([], backup_root=tmp_path, which=lambda name: "/bin/claude", execv=broken) == 126
    assert json.loads(config.read_text()) == {"mcpServers": {"openswap": {
        "command": "/Apps/OpenSwap.app/Contents/MacOS/OpenSwap", "args": ["attention", "channel"]}}}
    assert attention.launch_claude([], backup_root=tmp_path, which=lambda name: None,
                                   execv=lambda *a: calls.append(a)) == 127


def test_the_launcher_says_plainly_when_managed_settings_turn_channels_off(tmp_path, capsys, monkeypatch):
    managed = tmp_path / "managed.json"
    managed.write_text('{"channelsEnabled": false}')
    monkeypatch.setattr(attention, "MANAGED_SETTINGS", managed)
    monkeypatch.setattr(attention, "program", lambda: "openswap")
    attention.launch_claude([], backup_root=tmp_path, which=lambda name: "/bin/claude", execv=lambda *a: None)
    assert "turned Claude Code channels off" in capsys.readouterr().err


def test_the_question_hook_is_installed_only_with_replies_and_removed_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"hooks": {"PermissionRequest": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}]}}))
    attention.set_hooks(home, "/usr/local/bin/openswap", question=True)
    groups = json.loads((home / "settings.json").read_text())["hooks"]["PermissionRequest"]
    question = [g for g in groups if g["matcher"] == "AskUserQuestion"]
    assert question == [{"matcher": "AskUserQuestion", "hooks": [{
        "type": "command", "command": "/usr/local/bin/openswap attention question", "timeout": 600}]}]
    attention.set_hooks(home, "/usr/local/bin/openswap", question=False)
    groups = json.loads((home / "settings.json").read_text())["hooks"]["PermissionRequest"]
    assert [g["matcher"] for g in groups] == ["Bash", ""]
    attention.set_hooks(home, None)
    assert json.loads((home / "settings.json").read_text()) == {"hooks": {"PermissionRequest": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]}]}}


def test_the_replies_switch_is_saved_on_the_mac_and_fails_closed(tmp_path):
    from openswap.settings import load_attention_settings

    assert load_attention_settings(tmp_path).replies is False
    write_attention_settings(tmp_path, notices=True, replies=True)
    assert load_attention_settings(tmp_path) == AttentionSettings(notices=True, details=True, replies=True)
    settings = tmp_path / "settings.json"
    raw = json.loads(settings.read_text())
    raw["worker"]["attention"]["replies"] = "yes"
    settings.write_text(json.dumps(raw))
    assert load_attention_settings(tmp_path).replies is False


# --- the heartbeat and the reference service --------------------------------------------


class _Transport:
    def __init__(self, service, key):
        self.service, self.key, self.calls = service, key, []

    def request(self, operation, data):
        self.calls.append((operation, data))
        return self.service.request(operation, data, self.key)


@pytest.fixture
def online(tmp_path, env):
    from openswap.worker.leases import stable_account_identity
    from openswap.worker.remote import RemoteClient
    from openswap.worker.runtime import WorkerRuntime

    service = ControlStore(tmp_path / "service" / "db", clock=env.clock)
    paired = service.request("pair", {"code": service.issue_code()})
    root = tmp_path / "local"
    update_worker_settings(root, enabled=True)
    configure_worker_service(root, "http://127.0.0.1:8765")
    write_attention_settings(root, notices=True, replies=True)
    runtime = WorkerRuntime(root, account_identity=stable_account_identity("codex", "synthetic"))
    runtime.attention = env.store
    transport = _Transport(service, paired["device_key"])
    client = RemoteClient(runtime, "http://127.0.0.1:8765", paired["device_key"], transport=transport)
    client.tick()
    return client, service, paired["worker_id"], transport


def test_a_reply_travels_from_the_service_to_the_session_and_its_outcome_back(env, online):
    client, service, worker, transport = online
    item = env.prompt()
    client.sync_attention()
    report = service.attention(worker)
    assert report["replies_enabled"] is True and report["items"][0]["actions"] == ["allow", "deny"]
    attention_report({"worker_epoch": 1, **env.report()})
    reply_id = service.attention_reply(worker, item.id, "allow")
    client.heartbeat_tick()  # the heartbeat only queues it
    assert env.link.sent == []
    env.store.apply_replies(env.clock.now)
    assert env.link.sent == [{"verdict": {"request_id": "abcde", "behavior": "allow"}}]
    client.heartbeat_tick()
    assert transport.calls[-1][1]["attention_acks"] == [{"id": reply_id, "outcome": "sent"}]
    assert service.attention(worker)["replies"][0]["outcome"] == "sent"
    env.status("busy", env.clock.now + 0.05)
    client.heartbeat_tick()
    client.sync_attention()
    report = service.attention(worker)
    assert report["replies"][0]["outcome"] == "applied" and report["closed"] == {item.id: "answered_from_watch"}
    client.heartbeat_tick()
    assert "attention_acks" not in transport.calls[-1][1]  # nothing left to tell


def test_the_reference_service_refuses_replies_it_should_not_queue(env, online):
    client, service, worker, _ = online
    item = env.prompt(relay=False)  # notice-only
    client.sync_attention()
    with pytest.raises(ProtocolError, match="action_not_offered"):
        service.attention_reply(worker, item.id, "allow")
    with pytest.raises(ProtocolError, match="item_closed"):
        service.attention_reply(worker, "gone", "allow")


def test_acks_count_on_any_registration_in_order_and_never_fail_a_heartbeat(env, online):
    client, service, worker, transport = online
    item = env.prompt()
    client.sync_attention()
    reply_id = service.attention_reply(worker, item.id, "allow")
    key = transport.key
    epoch = service.request("register", {}, key)["worker_epoch"]  # capabilities not re-sent yet
    answer = service.request("heartbeat", {"worker_epoch": epoch, "attention_acks": [
        {"id": reply_id, "outcome": "sent"}, {"id": "x", "outcome": "done"}, "junk",
        {"id": reply_id, "outcome": "applied"}, {"id": reply_id, "outcome": "refused"}]}, key)
    assert "attention_replies" not in answer  # replies wait for the advertised registration
    assert service.attention(worker)["replies"][0]["outcome"] == "applied"


def test_the_worker_sends_its_outcomes_before_capabilities_are_resent(env, online):
    client, service, worker, transport = online
    item = env.prompt()
    client.sync_attention()
    service.attention_reply(worker, item.id, "allow")
    client.heartbeat_tick()
    env.store.apply_replies(env.clock.now)
    client._attention_epoch = client.worker_epoch - 1  # a new registration that has not advertised it yet
    client.heartbeat_tick()
    assert transport.calls[-1][1]["attention_acks"][0]["outcome"] == "sent"
    assert service.attention(worker)["replies"][0]["outcome"] == "sent"


def test_outcomes_never_keep_a_mac_offline_on_a_service_without_the_extension(env, online):
    client, service, worker, transport = online
    env.store._acks["r9"] = "sent"
    client._attention_epoch = None  # this service never accepted "attention"
    client.heartbeat_tick()
    assert "attention_acks" not in transport.calls[-1][1] and client.state == "online"

    class Refusing:
        key = transport.key

        def request(self, operation, data):
            if "attention_acks" in data:
                raise ProtocolError("invalid_request")
            return transport.request(operation, data)

    client.transport = Refusing()
    client._attention_epoch = client.worker_epoch
    assert client.heartbeat_tick() is False  # refused once: the outcomes are dropped
    assert env.store.take_acks() == [] and client.heartbeat_tick() is True


@pytest.mark.parametrize("reply", [
    {"id": "r", "item_id": "i", "action": "allow", "expires_at": "2026-11-06T18:00:00Z", "text": "x"},
    {"id": "r", "item_id": "i", "action": "answer", "expires_at": "2026-11-06T18:00:00Z"},
    {"id": "r", "item_id": "i", "action": "answer", "expires_at": "2026-11-06T18:00:00Z",
     "answers": [{"question": 0, "option": 0}, {"question": 0, "option": 1}]},
    {"id": "r", "item_id": "i", "action": "message", "expires_at": "2026-11-06T18:00:00Z", "text": ""},
    {"id": "r", "item_id": "i", "action": "always", "expires_at": "2026-11-06T18:00:00Z"},
    {"id": "r", "item_id": "i", "action": "allow", "expires_at": "2026-11-06T18:00:00Z", "request_id": "abcde"},
])
def test_the_wire_reply_is_a_closed_object(reply):
    with pytest.raises(ProtocolError):
        attention_reply(reply)


# --- review round 1 ----------------------------------------------------------------------


def test_sent_is_reported_only_once_the_verdict_went_out(env):
    item = env.prompt()
    seen = []

    class Watching(FakeLink):
        def send(self, message):
            seen.append(dict(env.store._acks))
            return False

    env.store._channels[PID].link = Watching()
    env.reply(item.id, "allow")
    assert seen == [{}] and env.outcomes() == {"r1": "refused"}


def test_a_verdict_while_another_prompt_waits_is_never_claimed(env):
    """The session file cannot tell which prompt closed: an Allow at the Mac for X
    while Z still waits looks the same as the watch's Allow."""
    x = env.prompt("npm test", agent="a1")
    env.clock.now = x.created_at + 0.3
    env.relay(bash("npm run lint"), request_id="fghij")
    env.store.notice(hook("PermissionRequest", tool_name="Bash", tool_input=bash("npm run lint"), agent_id="a2")
                     | {"at": x.created_at + 0.4})
    env.store.notice(hook("Notification", notification_type="permission_prompt") | {"at": x.created_at + 6.4})
    env.clock.now = x.created_at + 8
    env.reply(x.id, "allow")
    assert env.outcomes() == {"r1": "sent"}
    env.store.notice(hook("PostToolUse", tool_name="Bash", tool_input=bash("npm test"), agent_id="a1")
                     | env.at(0.5))
    assert env.closes() == {x.id: "answered_on_mac"} and env.outcomes() == {}


def test_an_answer_reaches_only_its_own_agents_question(env):
    key = attention.question_key({"questions": QUESTIONS})
    first = env.store.question_waiter(CLAUDE, SESSION, key, "a1")
    second = env.store.question_waiter(CLAUDE, SESSION, key, "a2")
    t = env.clock.now
    for agent, offset in (("a1", 0.1), ("a2", 0.2)):
        env.store.notice(hook("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": QUESTIONS},
                              agent_id=agent) | {"at": t + offset})
    env.status("waiting", t + 0.05)
    for offset in (6.1, 6.2):
        env.store.notice(hook("Notification", notification_type="permission_prompt") | {"at": t + offset})
    env.clock.now = t + 7
    a1, a2 = sorted(env.store._items, key=lambda i: i.created_at)
    env.reply(a1.id, "answer", answers=[{"question": 0, "option": 1}])
    assert first.done.is_set() and not second.done.is_set()
    # The other agent's question answered at the Mac closes only that one.
    env.store.notice(hook("PostToolUse", tool_name="AskUserQuestion", agent_id="a2", tool_input={
        "questions": QUESTIONS, "answers": {"Which database?": "Postgres"}}) | env.at(0.3))
    assert env.closes() == {a2.id: "answered_on_mac"} and a1 in env.store._items


def test_a_local_request_with_an_odd_command_still_gets_an_answer(env):
    connection = _Conn([json.dumps({"version": 1, "command": ["channel"]}).encode() + b"\n"])
    ipc._handle_connection(connection, None, env.store)
    assert json.loads(connection.out) == {"version": 1, "ok": False, "error": "unsupported_request"}


def test_queued_outcomes_survive_a_worker_restart(env, tmp_path):
    item = env.prompt()
    env.reply(item.id, "allow")
    env.outcomes()
    env.status("busy", env.clock.now + 0.05)  # applied, then the worker stops before its heartbeat
    again = AttentionStore(tmp_path / "root", clock=env.clock, switches=lambda: env.switches, homes=lambda: [env.home])
    assert again.take_acks() == [{"id": "r1", "outcome": "applied"}]


def test_registrations_of_ended_claude_code_processes_make_room(env, monkeypatch):
    from openswap.worker import attention as worker_attention

    monkeypatch.setattr(worker_attention, "MAX_CHANNELS", 1)
    env.store.unregister_channel(PID, env.link)
    later = Process(PID, 1, "Sat Nov  7 09:00:00 2026", CLAUDE.args)  # after a restart, the PID is reused
    monkeypatch.setattr(peers, "process", lambda pid: later)
    other = Process(PID + 1, 1, "Sat Nov  7 09:00:00 2026", CLAUDE.args)
    (env.home / "sessions" / f"{PID + 1}.json").write_text(json.dumps({"pid": PID + 1, "sessionId": OTHER}))
    assert env.store.register_channel(Process(PID + 7, PID + 1, "x", ()), other, FakeLink())
    assert list(env.store._channels) == [PID + 1]


def test_answers_never_leave_a_question_unanswered():
    hidden = [{"question": "Which database?", "options": [{"label": "A"}]}, {"question": " \x07 ", "options": []}]
    assert attention._answers(hidden, [[0, 0]]) == {}


def test_the_replies_switch_refuses_a_temporary_copy_while_notices_are_on(tmp_path, monkeypatch):
    from openswap import bundle
    from openswap.exceptions import ClaudeSwitchError
    from openswap.settings import load_attention_settings

    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
    monkeypatch.setattr(attention, "apply", lambda *a: pytest.fail("rewrote the hooks"))
    write_attention_settings(tmp_path, notices=True, replies=True)
    with pytest.raises(ClaudeSwitchError):
        attention.set_replies(tmp_path, True)
    assert attention.set_replies(tmp_path, False) == []  # off always works; the hooks stay as they are
    assert load_attention_settings(tmp_path).replies is False


def test_the_replies_key_is_written_only_once_used(tmp_path):
    write_attention_settings(tmp_path, notices=True)
    assert json.loads((tmp_path / "settings.json").read_text())["worker"]["attention"] == {
        "notices": True, "details": True}
    write_attention_settings(tmp_path, replies=True)
    write_attention_settings(tmp_path, replies=False)
    assert json.loads((tmp_path / "settings.json").read_text())["worker"]["attention"]["replies"] is False


def test_the_operator_command_checks_answer_positions():
    from openswap.worker.refserver.cli import _position

    assert _position("0:1") == {"question": 0, "option": 1}
    for bad in ("1", "a:b", "0:-1"):
        with pytest.raises(Exception):
            _position(bad)


def test_the_worker_store_follows_the_owners_switches_and_managed_homes(tmp_path, monkeypatch):
    from openswap.worker import runtime

    monkeypatch.setattr(attention, "reconcile", lambda root: [])
    monkeypatch.setattr(attention, "config_homes", lambda root, problems=None: [tmp_path / "home"])
    store = runtime._attention_store(tmp_path)
    assert store.switches() == AttentionSettings(notices=False, details=True, replies=False)
    write_attention_settings(tmp_path, notices=True, replies=True)
    assert store.switches().replies is True and store.homes() == [tmp_path / "home"]
    assert AttentionStore(tmp_path / "bare").switches().replies is False  # nothing wired: replies off


def test_a_question_that_cannot_be_shown_whole_takes_no_answer(env):
    partial = [*QUESTIONS, {"question": " \x07 ", "options": [{"label": "x"}]}]
    notice = hook("PermissionRequest", tool_name="AskUserQuestion", tool_input={"questions": partial})
    assert "questions" not in notice
    t = env.clock.now
    env.store.notice(notice | {"at": t + 0.1})
    env.status("waiting", t + 0.05)
    env.store.notice(hook("Notification", notification_type="permission_prompt") | {"at": t + 6.1})
    env.store.question_waiter(CLAUDE, SESSION, attention.question_key({"questions": partial}))
    [item] = env.store._items
    assert env.actions(item) == []


def test_a_question_closed_by_another_questions_answer_claims_nothing(env):
    item = env.question()
    waiter = env.store.question_waiter(CLAUDE, SESSION, attention.question_key({"questions": QUESTIONS}))
    env.reply(item.id, "answer", answers=[{"question": 0, "option": 1}])
    env.store.handed_over(waiter.reply_id)
    other = [{"question": "Which cache?", "options": [{"label": "Postgres"}, {"label": "SQLite"}]}]
    env.store.notice(hook("PostToolUse", tool_name="AskUserQuestion", tool_input={
        "questions": other, "answers": {"Which cache?": "SQLite"}}) | env.at(0.2))
    assert env.closes() == {item.id: "answered_on_mac"} and env.outcomes() == {"r1": "sent"}
