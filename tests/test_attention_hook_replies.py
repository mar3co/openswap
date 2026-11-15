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
from types import SimpleNamespace

import pytest

from openswap import attention
from openswap.redact import mask_secrets
from openswap.settings import AttentionSettings
from openswap.worker import attention as store_module
from openswap.worker import channel_link, ipc, peers
from openswap.worker.attention import AttentionStore, SessionRecord
from openswap.worker.peers import Process
from tests.test_attention_replies import (  # noqa: F401 (fixtures)
    CLAUDE, PID, QUESTIONS, SESSION, Env, _hooks_run_under_the_test, _running, bash, env, hook,
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

    def permission(self, item, command="npm test", *, preview=None, agent="", tool_name="Bash", late=0.0):
        """The prompt's synchronous permission hook, connected when its PermissionRequest ran."""
        now, self.clock.now = self.clock.now, item.created_at + late
        try:
            tool_input = bash(command)
            return self.store.waiter("permission", CLAUDE, SESSION, attention.tool_key(tool_input), agent,
                                     tool_name=tool_name,
                                     preview=attention.input_preview(tool_input) if preview is None else preview)
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
    assert item.detail["input_preview"] == attention.input_preview(bash("npm test"))
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
    waiter = desktop.store.waiter("permission", CLAUDE, SESSION, attention.tool_key(tool_input), tool_name="Bash",
                                  preview=attention.input_preview(tool_input))
    desktop.clock.now -= 0.1
    item = desktop.prompt(relay=False)
    assert item.detail["input_preview"] == waiter.preview and desktop.actions(item) == ["allow", "deny"]


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
    hooks = [desktop.permission(item, preview=forged), desktop.permission(item)]
    if not forged_first:
        hooks.reverse()
    desktop.store.end_waiter(hooks[0])
    assert desktop.actions(item) == []
    desktop.reply(item.id, "allow")
    assert desktop.outcomes() == {"r1": "refused"} and not hooks[1].done.is_set()


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


@pytest.mark.parametrize(("action", "status", "won"), [
    ("allow", "busy", True), ("allow", "idle", False), ("deny", "busy", True), ("deny", "idle", False)])
def test_row4_and_row13_the_watch_wins_only_when_the_dialog_closed_its_way(desktop, action, status, won):
    item = desktop.prompt(relay=False)
    waiter = desktop.permission(item)
    desktop.reply(item.id, action)
    assert waiter.result == action
    desktop.store.handed_over(waiter.reply_id)
    desktop.outcomes()
    # A hook's deny lets Claude go on, like an allow; a No at the Mac ends the turn.
    desktop.status(status, desktop.clock.now + 0.5)
    desktop.clock.now += 2
    desktop.store.tick()
    assert desktop.closes() == {item.id: "answered_from_watch" if won else "answered_on_mac"}
    assert desktop.outcomes() == ({"r1": "applied"} if won else {})


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


# --- the hook process ---------------------------------------------------------------------


def _worker(tmp_path, answer):
    """A worker socket that answers one waiting hook with ``answer`` (None: hangs up)."""
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
                       "tool_key": attention.tool_key(tool_input), "tool_name": "Bash",
                       "preview": attention.input_preview(tool_input)}
    assert "abc123" not in seen[0]["preview"]
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


# --- what the watch shows before Allow (R1a) ------------------------------------------------


def test_the_preview_shows_the_call_in_claude_codes_channel_form():
    tool_input = {"command": "ls -la", "description": "List files", "timeout": 5}
    assert attention.input_preview(tool_input) == '{ "command": "ls -la", "description": "List files", "timeout": 5 }'
    assert attention.input_preview("ls") == "" and attention.input_preview({}) == ""


def test_a_long_call_is_cut_in_the_middle_of_each_field_within_the_cap():
    edit = {"file_path": "/repo/app.py", "old_string": "a" * 5000 + "OLD_END", "new_string": "NEW_START" + "b" * 5000}
    preview = attention.input_preview(edit)
    assert len(preview) <= 4000
    assert '"file_path": "/repo/app.py"' in preview and "OLD_END" in preview and "NEW_START" in preview
    assert "the middle left out" in preview


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx", "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "github_pat_11ABCDEFG0123456789_abcdefghij",
    "AKIAIOSFODNN7EXAMPLE", "xoxb-1234567890-abcdefghijkl", "sk_live_AbCdEfGhIjKlMnOp",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
])
def test_recognizable_keys_and_tokens_are_masked(secret):
    masked = mask_secrets(f"deploy --with {secret} now")
    assert secret not in masked and masked == "deploy --with [REDACTED] now"


@pytest.mark.parametrize(("text", "kept"), [
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----", ""),
    ("curl -H 'Authorization: Bearer abc.def-123' https://api.test", "Authorization: Bearer"),
    ("git clone https://oauth2:glpat-xyz@gitlab.test/repo.git", "https://[REDACTED]@gitlab.test/repo.git"),
    ("PGPASSWORD=hunter2 psql", "PGPASSWORD="), ('{"api_key": "abc123"}', '"api_key": "'),
    ("export GITHUB_TOKEN=abc123", "GITHUB_TOKEN="), ("tool --client-secret s3cr3t run", "--client-secret"),
])
def test_named_credentials_and_url_passwords_are_masked(text, kept):
    masked = mask_secrets(text)
    assert "[REDACTED]" in masked and kept in masked
    assert not any(s in masked for s in ("b3BlbnNzaC1rZXk", "abc.def-123", "glpat-xyz", "hunter2", "abc123", "s3cr3t"))


def test_ordinary_commands_are_left_as_they_are():
    for text in ("npm test -- --watch", "git push origin main", "ls ~/code && cat README.md",
                 "https://example.com/a@b", "pytest -k 'token_refresh'"):
        assert mask_secrets(text) == text


def test_a_value_under_a_credential_name_is_masked_whole():
    preview = attention.input_preview({"url": "https://api.test", "headers": {"X-Api-Key": "k-1", "Accept": "json"},
                                       "password": "correct horse"})
    assert "k-1" not in preview and "correct horse" not in preview and '"Accept":"json"' in preview
