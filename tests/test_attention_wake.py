"""Typed replies without a channel (plan 028, Task 2): a session's own
``openswap attention wake`` hook, waiting since the turn's ``Stop``, takes the
watch's text and starts a turn with it. Each "tN" test pins outcome TN of the
task's notes; "row N" ones pin plan 028's Required outcome N."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from openswap import attention
from openswap.settings import AttentionSettings
from openswap.worker import attention as store_module
from openswap.worker import channel_link, ipc, peers
from openswap.worker.attention import AttentionStore
from openswap.worker.peers import Process
from tests.test_attention_hook_replies import Desktop, _worker, desktop  # noqa: F401 (fixtures)
from tests.test_attention_replies import (  # noqa: F401 (fixtures)
    CLAUDE, OTHER, PID, SESSION, _hooks_run_under_the_test, _running, env, hook,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the worker and its hooks are POSIX-only")

NOTE = "Reply from the owner's phone or watch:"


def stop(env, offset=0.0, **fields):
    """The turn's ``Stop`` notice, dated ``offset`` from now."""
    entry = {"entrypoint": env.entrypoint} if getattr(env, "entrypoint", "") else {}
    env.store.notice(hook("Stop", last_assistant_message="Done.", **fields) | entry | env.at(offset))


def wake(env, claude=CLAUDE, session=SESSION):
    return env.store.wake_waiter(claude, session)


def finished(env, *, slack=0.0):
    """A finished turn: its session file says ``idle`` (``slack`` after the turn's
    ``Stop``), its ``Stop`` notice and its wake hook arrive, and a minute later
    the idle item is published. Returns (item, wake hook)."""
    t = env.clock.now
    env.status("idle", t + slack)
    stop(env)
    waker = wake(env)
    if env.entrypoint == "cli":
        env.store.notice(hook("Notification", notification_type="idle_prompt") | {"at": t + 60})
    env.clock.now = t + 61
    env.store.tick()
    return next(i for i in env.store._items if i.kind == "idle"), waker


def woken(text):
    """The woken turn's prompt as Claude Code 2.1.325 gives ``UserPromptSubmit`` (measured)."""
    return (f"<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n<system-reminder>\n"
            f'Stop hook blocking error from command "Stop": {NOTE}\n{text}\n</system-reminder>')


def sent(env, item, waker, text="Run the tests"):
    """The watch's text, handed over to the wake hook."""
    env.reply(item.id, "message", text=text)
    assert waker.done.is_set() and waker.result == text
    env.store.handed_over(waker.reply_id)
    env.store.end_waiter(waker, handed=True)
    assert env.outcomes() == {"r1": "sent"}


@pytest.fixture(params=["claude-desktop", "cli"])
def surface(request, tmp_path):
    return Desktop(tmp_path, request.param)


# --- T1 and T2: when an idle item takes a message --------------------------------------------


def test_t1_an_idle_session_with_its_one_wake_hook_takes_a_message(surface):
    item, waker = finished(surface)
    assert surface.actions(item) == ["message"]
    surface.reply(item.id, "message", text="Run the tests")
    assert waker.result == "Run the tests" and surface.outcomes() == {}  # "sent" once the hook took it


def test_t1_slack_between_the_stop_and_the_session_files_idle_is_not_staleness(desktop):
    item, waker = finished(desktop, slack=4.0)  # the owner's own Stop hooks took seconds
    sent(desktop, item, waker)


def test_t2_no_wake_hook_no_message(surface):
    t = surface.clock.now
    surface.status("idle", t)
    stop(surface)
    if surface.entrypoint == "cli":
        surface.store.notice(hook("Notification", notification_type="idle_prompt") | {"at": t + 60})
    surface.clock.now += 61
    surface.store.tick()
    [item] = surface.store._items
    assert surface.actions(item) == []


@pytest.mark.parametrize("status", ["busy", "waiting"])
def test_t2_a_session_that_is_not_idle_takes_no_message(desktop, status):
    item, _ = finished(desktop)
    desktop.status(status, desktop.clock.now)
    assert desktop.actions(item) == []


def test_t2_a_wake_hook_that_pairs_with_no_stop_takes_no_message(desktop):
    desktop.status("idle", desktop.clock.now)
    waker = wake(desktop)  # no Stop of its own: a command the session ran, say
    stop(desktop, offset=store_module.PAIR_SECONDS + 1)
    desktop.clock.now += 61 + store_module.PAIR_SECONDS
    desktop.store.tick()
    [item] = desktop.store._items
    assert waker is not None and desktop.actions(item) == []


def test_t2_a_stop_that_arrives_after_its_wake_hook_still_pairs(desktop):
    desktop.status("idle", desktop.clock.now)
    waker = wake(desktop)
    stop(desktop, offset=0.2)
    desktop.clock.now += 61
    desktop.store.tick()
    [item] = desktop.store._items
    assert desktop.actions(item) == ["message"]
    sent(desktop, item, waker)


def test_t2_vs_code_takes_no_wake_hook(tmp_path):
    vscode = Desktop(tmp_path, "claude-vscode")
    vscode.status("idle", vscode.clock.now)
    stop(vscode)
    assert wake(vscode) is None


def test_t2_a_channel_session_keeps_its_channel_message_and_takes_no_wake_hook(env):
    item = env.idle()
    assert wake(env) is None
    assert env.actions(item) == ["message"]
    env.reply(item.id, "message", text="Run the tests")
    assert env.link.sent[0]["message"]["content"] == "Run the tests"


def test_t2_a_wake_hook_of_another_session_is_not_served(desktop):
    desktop.status("idle", desktop.clock.now)
    assert wake(desktop, session=OTHER) is None
    stranger = Process(PID, 1, "Sat Nov  7 09:00:00 2026", ("claude",))
    (desktop.home / "sessions" / f"{PID}.json").write_text(json.dumps(
        {"pid": PID, "sessionId": SESSION, "status": "idle", "entrypoint": "claude-desktop",
         "procStart": "Fri Nov  6 14:00:00 2026"}))
    assert wake(desktop, claude=stranger) is None  # a reused PID


# --- T3: one wake hook per process ------------------------------------------------------------


def test_t3_the_next_stops_wake_hook_releases_the_older_one(desktop):
    item, older = finished(desktop)
    stop(desktop)  # a background task's turn ended: a new item stands for it
    newer = wake(desktop)
    assert older.done.is_set() and older.result is None and not newer.done.is_set()
    [current] = desktop.store._items
    assert current.id != item.id and desktop.actions(current) == ["message"]
    desktop.reply(item.id, "message", text="x")
    assert desktop.outcomes() == {"r1": "item_closed"}


def test_t3_a_wake_hook_that_pairs_with_no_stop_never_replaces_one(desktop):
    item, waker = finished(desktop)
    assert wake(desktop) is None
    assert not waker.done.is_set() and desktop.actions(item) == ["message"]


def test_t3_a_stop_from_before_the_latest_prompt_does_not_pair(desktop):
    desktop.status("idle", desktop.clock.now)
    desktop.store.notice(hook("UserPromptSubmit", prompt="hello") | desktop.at(0))
    stop(desktop, offset=-0.5)  # a late Stop of the earlier turn
    assert wake(desktop) is not None
    desktop.store.notice(hook("Notification", notification_type="idle_prompt") | desktop.at(60))
    desktop.clock.now += 61
    [item] = desktop.store._items
    assert desktop.actions(item) == []


# --- T4 and T5: how a handed-over message settles ----------------------------------------------


def test_t4_the_woken_turns_prompt_applies_the_reply(surface):
    item, waker = finished(surface)
    sent(surface, item, waker, "Run the tests")
    surface.status("busy", surface.clock.now + 0.1)  # the woken turn runs: decides nothing
    assert item in surface.store._items
    surface.store.notice(hook("UserPromptSubmit", prompt=woken("Run the tests")) | surface.at(0.2))
    assert surface.closes() == {item.id: "answered_from_watch"} and surface.outcomes() == {"r1": "applied"}


def test_t4_another_turns_stop_never_supersedes_a_handed_over_message(desktop):
    item, waker = finished(desktop)
    sent(desktop, item, waker)
    stop(desktop, offset=0.5)  # its UserPromptSubmit is late
    desktop.store.notice(hook("Notification", notification_type="idle_prompt") | desktop.at(1))
    assert [i.id for i in desktop.store._items] == [item.id] and desktop.closes() == {}
    desktop.store.notice(hook("UserPromptSubmit", prompt=woken("Run the tests")) | desktop.at(0.2))
    assert desktop.closes() == {item.id: "answered_from_watch"} and desktop.outcomes() == {"r1": "applied"}


@pytest.mark.parametrize("prompt", [
    "<task-notification>\n<summary>Background task done</summary>\n</task-notification>",
    woken("Run the other tests"),
], ids=["background task", "another text"])
def test_t4_a_task_notification_without_the_note_and_text_decides_nothing(desktop, prompt):
    item, waker = finished(desktop)
    sent(desktop, item, waker)
    desktop.store.notice(hook("UserPromptSubmit", prompt=prompt) | desktop.at(0.2))
    assert item in desktop.store._items and desktop.outcomes() == {}


def test_t4_the_owner_typing_first_closes_it_on_the_mac_and_the_reply_stays_sent(desktop):
    item, waker = finished(desktop)
    sent(desktop, item, waker)
    desktop.store.notice(hook("UserPromptSubmit", prompt=f"{NOTE}\nRun the tests") | desktop.at(0.2))
    assert desktop.closes() == {item.id: "answered_on_mac"} and desktop.outcomes() == {}


@pytest.mark.parametrize("ending", ["exit", "clear"])
def test_t4_the_session_ending_first_closes_it_and_the_reply_stays_sent(desktop, ending):
    item, waker = finished(desktop)
    sent(desktop, item, waker)
    desktop.store.notice(hook("SessionEnd", reason=ending) | desktop.at(0.2))
    assert desktop.closes() == {item.id: "session_ended"} and desktop.outcomes() == {}


@pytest.mark.parametrize("text", ["Run the tests", "a\tb\r\nc\n\nd", "é" * 1000, "ends with a newline\n"],
                         ids=["plain", "tab, CR and LF", "1000 characters", "trailing newline"])
def test_t5_the_hook_names_the_delivered_text_and_only_in_a_task_notification(text):
    notice = hook("UserPromptSubmit", prompt=woken(text))
    assert notice["prompt_kind"] == "task" and notice["reply"] == store_module.reply_key(text)
    assert "reply" not in hook("UserPromptSubmit", prompt=f"{NOTE}\n{text}")
    assert "reply" not in hook("UserPromptSubmit", prompt="<task-notification>\nx\n</task-notification>")


def test_t5_trailing_whitespace_claude_code_may_trim_still_matches():
    trimmed = woken("Run the tests\n\n").replace("tests\n\n\n</system", "tests\n</system")
    assert hook("UserPromptSubmit", prompt=trimmed)["reply"] == store_module.reply_key("Run the tests\n\n")


# --- T6 and T7: replies the session no longer waits for ---------------------------------------


@pytest.mark.parametrize("status", ["busy", "waiting"])
def test_t6_a_message_arriving_while_the_session_is_not_idle_is_refused(desktop, status):
    item, waker = finished(desktop)
    desktop.accepted()
    (desktop.home / "sessions" / f"{PID}.json").write_text(json.dumps(
        {"pid": PID, "sessionId": SESSION, "status": status, "statusUpdatedAt": desktop.clock.now * 1000,
         "entrypoint": "claude-desktop"}))
    desktop.reply(item.id, "message", text="x", offered=False)
    assert desktop.outcomes() == {"r1": "refused"} and not waker.done.is_set()


@pytest.mark.parametrize("prompt", ["hello", "/cost"])
def test_t7_the_owner_typing_closes_the_item_on_the_mac_and_releases_the_hook(desktop, prompt):
    item, waker = finished(desktop)
    desktop.store.notice(hook("UserPromptSubmit", prompt=prompt) | desktop.at(0))
    assert desktop.closes() == {item.id: "answered_on_mac"}
    assert waker.done.is_set() and waker.result is None


def test_t7_a_text_handed_over_just_before_the_owner_typed_stays_sent(desktop):
    item, waker = finished(desktop)
    desktop.reply(item.id, "message", text="Run the tests")
    desktop.store.notice(hook("UserPromptSubmit", prompt="hello") | desktop.at(0))
    assert waker.result == "Run the tests"  # still handed over: it runs after the owner's turn
    desktop.store.handed_over(waker.reply_id)
    desktop.store.end_waiter(waker, handed=True)
    assert desktop.closes() == {item.id: "answered_on_mac"} and desktop.outcomes() == {"r1": "sent"}
    desktop.store.notice(hook("UserPromptSubmit", prompt=woken("Run the tests")) | desktop.at(30))
    assert desktop.outcomes() == {}


def test_t7_a_prompt_from_before_the_hook_registered_does_not_release_it(desktop):
    _, waker = finished(desktop)
    desktop.store.notice(hook("UserPromptSubmit", prompt="hello") | desktop.at(-120))
    assert not waker.done.is_set()


# --- T8: the hour -----------------------------------------------------------------------------


def test_t8_the_hour_passes_the_item_stays_open_without_a_message(desktop):
    item, waker = finished(desktop)
    desktop.clock.now = waker.at + store_module.WAKE_SECONDS - 61
    assert desktop.actions(item) == ["message"] and not desktop.store.waiter_over(waker)
    desktop.accepted()
    desktop.clock.now = waker.at + store_module.WAKE_SECONDS - 60
    assert desktop.actions(item) == []
    desktop.reply(item.id, "message", text="x", offered=False)
    assert desktop.outcomes() == {"r1": "refused"} and not waker.done.is_set()
    desktop.clock.now = waker.at + store_module.WAKE_SECONDS
    assert desktop.store.waiter_over(waker) and waker.result is None
    desktop.store.end_waiter(waker)
    desktop.store.tick()
    assert item in desktop.store._items and desktop.actions(item) == []


def test_t8_the_wake_hook_is_not_bound_by_the_reply_hooks_ten_minutes(desktop):
    _, waker = finished(desktop)
    desktop.clock.now = waker.at + store_module.HOOK_WAIT_SECONDS + 1
    assert not desktop.store.waiter_over(waker)


def _holding_worker(tmp_path):
    """A worker socket that takes one waiting hook's request and never answers."""
    root = tmp_path / "w"
    path = ipc.socket_path(root)
    path.parent.mkdir(parents=True, mode=0o700)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    seen, held = [], []

    def serve():
        connection, _ = server.accept()
        held.append(connection)
        seen.append(json.loads(connection.makefile("rb").readline()))

    threading.Thread(target=serve, daemon=True).start()
    return root, seen, held


def test_t8_the_hook_gives_up_at_its_hour_by_the_wall_clock(tmp_path, monkeypatch):
    root, seen, held = _holding_worker(tmp_path)
    real, calls = time.time, []

    def later():
        calls.append(1)
        return real() + (attention.WAKE_WAIT_SECONDS + 1 if len(calls) > 1 else 0)

    monkeypatch.setattr(time, "time", later)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps({"session_id": SESSION}).encode())))
    exits = []
    hook_thread = threading.Thread(target=lambda: exits.append(attention.wake_main(backup_root=root)), daemon=True)
    hook_thread.start()
    hook_thread.join(3)
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert exits == [0] and held  # the worker still holds the connection: only the hour ended it
    assert seen == [{"version": 1, "command": "wake", "session_id": SESSION}]


# --- T9: releases -----------------------------------------------------------------------------


@pytest.mark.parametrize(("notices", "replies"), [(False, True), (True, False)])
def test_t9_replies_turned_off_release_the_hook(desktop, notices, replies):
    _, waker = finished(desktop)
    desktop.switches = AttentionSettings(notices=notices, details=True, replies=replies)
    desktop.store.tick()
    assert desktop.store.waiter_over(waker) and waker.result is None


def test_t9_unreadable_settings_keep_the_hook_waiting_and_refuse_messages(desktop):
    item, waker = finished(desktop)
    desktop.accepted()
    desktop.switches = AttentionSettings(notices=False, details=False, replies=False, readable=False)
    desktop.store.tick()
    assert not desktop.store.waiter_over(waker)
    desktop.reply(item.id, "message", text="x", offered=False)
    assert desktop.outcomes() == {"r1": "refused"} and not waker.done.is_set()


@pytest.mark.parametrize("ending", ["session end", "clear", "file gone", "process ended"])
def test_t9_the_session_ending_or_moving_releases_the_hook(desktop, monkeypatch, ending):
    _, waker = finished(desktop)
    desktop.clock.now += 1
    if ending == "session end":
        desktop.store.notice(hook("SessionEnd", reason="exit") | desktop.at(0))
    elif ending == "clear":
        desktop.status("idle", desktop.clock.now, session=OTHER)
    elif ending == "file gone":
        desktop.store.observe({}, True)
    else:
        monkeypatch.setattr("openswap.process_detection.is_pid_alive", lambda pid: False)
    assert desktop.store.waiter_over(waker)


def test_t9_an_unreadable_session_file_is_not_a_gone_one(desktop):
    _, waker = finished(desktop)
    desktop.clock.now += 1
    desktop.store.observe({}, False)
    assert not desktop.store.waiter_over(waker)


def test_t9_an_idle_session_never_releases_a_wake_hook_early(desktop):
    desktop.status("idle", desktop.clock.now)
    waker = wake(desktop)  # its Stop and item not seen yet (a terminal's item comes 60 s after its Stop)
    desktop.clock.now += store_module.PAIR_SECONDS + 60
    assert not desktop.store.waiter_over(waker)


# --- T10: a worker restart --------------------------------------------------------------------


def _restart(desktop, tmp_path):
    desktop.store = AttentionStore(tmp_path / "root", clock=desktop.clock, switches=lambda: desktop.switches,
                                   homes=lambda: [desktop.home])
    desktop.status("idle", desktop.clock.now - 70)


def test_t10_after_a_restart_the_item_takes_no_message_until_the_next_stops_hook(desktop, tmp_path):
    item, _ = finished(desktop)
    _restart(desktop, tmp_path)
    item = next(i for i in desktop.store._items if i.id == item.id)
    assert desktop.actions(item) == []
    assert wake(desktop) is not None and desktop.actions(item) == []  # pairs with no Stop


def test_t10_a_text_sent_before_a_restart_is_never_handed_over_again(desktop, tmp_path):
    item, waker = finished(desktop)
    desktop.reply(item.id, "message", text="Run the tests")
    _restart(desktop, tmp_path)
    item = next(i for i in desktop.store._items if i.id == item.id)
    stop(desktop)  # the woken turn's Stop: a new item, and a new hook that pairs with it
    again = wake(desktop)
    [current] = desktop.store._items
    assert current.id != item.id
    desktop.reply(item.id, "message", text="Run the tests")
    assert again is not None and not again.done.is_set() and desktop.outcomes() == {"r1": "sent"}


# --- T11: what the hook gives Claude Code -----------------------------------------------------


def _program(tmp_path, root, *, noise=False):
    """An ``openswap`` that runs this build's wake hook against the worker at ``root``."""
    program = tmp_path / "bin" / "openswap"
    program.parent.mkdir()
    program.write_text(f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
                       + ("sys.stderr.write('noise\\n')\nimport warnings\nwarnings.warn('loud')\n" if noise else "")
                       + "from openswap import attention\n"
                       + f"sys.exit(attention.wake_main(backup_root=Path({str(root)!r})))\n")
    program.chmod(0o755)
    return str(program)


def _run_wrapped(program, stdin=None):
    return subprocess.run(["/bin/sh", "-c", attention.hook_command("wake", program)],
                          input=stdin if stdin is not None else json.dumps({"session_id": SESSION}).encode(),
                          capture_output=True, timeout=30)


@pytest.mark.parametrize("text", ["Run the tests", "a\tb\r\nc\n\n", "é漢✓" * 333 + "x"],
                         ids=["plain", "tab, CR and LF", "1000 characters"])
def test_t11_a_message_reaches_claude_code_as_the_note_and_the_text_verbatim(tmp_path, text):
    root, seen = _worker(tmp_path, {"message": text})
    ran = _run_wrapped(_program(tmp_path, root, noise=True))
    assert ran.returncode == 2 and ran.stdout == b""
    assert ran.stderr == f"{NOTE}\n{text}".encode()
    assert seen == [{"version": 1, "command": "wake", "session_id": SESSION}]


@pytest.mark.parametrize("answer", [{"message": None}, {}, {"message": ""}, {"message": 3}, None])
def test_t11_anything_but_a_message_ends_the_hook_silently(tmp_path, answer):
    root, _ = _worker(tmp_path, answer)
    ran = _run_wrapped(_program(tmp_path, root))
    assert (ran.returncode, ran.stdout, ran.stderr) == (0, b"", b"")


def test_t11_without_a_worker_or_with_bad_input_the_hook_exits_silently(tmp_path):
    program = _program(tmp_path, tmp_path / "none")
    for stdin in (None, b"", b"not json", b"[]", json.dumps({"session_id": 3}).encode()):
        ran = _run_wrapped(program, stdin)
        assert (ran.returncode, ran.stdout, ran.stderr) == (0, b"", b"")


def test_t11_a_missing_or_failing_program_never_wakes_the_session(tmp_path):
    failing = tmp_path / "old-openswap"
    failing.write_text("#!/bin/sh\necho 'usage: openswap ...' >&2\nexit 2\n")
    failing.chmod(0o755)
    for program in (str(tmp_path / "gone" / "openswap"), str(failing), "openswap-not-on-path"):
        ran = _run_wrapped(program)
        assert (ran.returncode, ran.stdout, ran.stderr) == (0, b"", b"")


def test_the_hook_waits_after_the_woken_turns_own_stop_too(tmp_path, monkeypatch):
    root, seen = _worker(tmp_path, {"message": None})
    assert _wake_hook(monkeypatch, {"session_id": SESSION, "stop_hook_active": True}, root) == 0
    assert seen == [{"version": 1, "command": "wake", "session_id": SESSION}]


def _wake_hook(monkeypatch, data, root) -> int:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(data).encode())))
    return attention.wake_main(backup_root=root)


# --- T12: caps --------------------------------------------------------------------------------


def test_t12_wake_hooks_have_their_own_cap_in_all(desktop, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_WAKERS", 1)
    other = Process(os.getppid(), 1, "Fri Nov  6 14:00:00 2026", ("claude",))
    (desktop.home / "sessions" / f"{other.pid}.json").write_text(json.dumps(
        {"pid": other.pid, "sessionId": OTHER, "status": "idle", "entrypoint": "claude-desktop"}))
    _, waker = finished(desktop)
    assert waker is not None and wake(desktop, claude=other, session=OTHER) is None


def test_t12_wake_hooks_never_take_the_places_reply_hooks_need(desktop, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_WAITERS", 1)
    monkeypatch.setattr(store_module, "MAX_WAITERS_PER_PROCESS", 1)
    _, waker = finished(desktop)
    item = desktop.prompt(relay=False)
    assert waker is not None and desktop.permission(item) is not None


def test_t12_reply_hooks_never_take_the_places_wake_hooks_need(desktop, monkeypatch):
    monkeypatch.setattr(store_module, "MAX_WAITERS", 1)
    desktop.permission(desktop.prompt(relay=False))
    desktop.status("idle", desktop.clock.now)
    stop(desktop)
    assert wake(desktop) is not None


# --- the hook's installation and its peer check ------------------------------------------------


def test_the_wake_hook_is_installed_only_with_replies_and_removed_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    mine = {"matcher": "", "hooks": [{"type": "command", "command": "mine"}]}
    (home / "settings.json").write_text(json.dumps({"hooks": {"Stop": [mine]}}))
    attention.set_hooks(home, "/usr/local/bin/openswap", replies=True)
    groups = json.loads((home / "settings.json").read_text())["hooks"]["Stop"]
    assert groups[0] == mine and groups[2] == {"matcher": "", "hooks": [{
        "type": "command", "command": attention.hook_command("wake", "/usr/local/bin/openswap"),
        "asyncRewake": True, "timeout": 3660}]}
    assert len(groups) == 3
    attention.set_hooks(home, "/usr/local/bin/openswap", replies=False)
    groups = json.loads((home / "settings.json").read_text())["hooks"]["Stop"]
    assert len(groups) == 2 and groups[0] == mine and groups[1]["hooks"][0]["async"] is True
    attention.set_hooks(home, None)
    assert json.loads((home / "settings.json").read_text()) == {"hooks": {"Stop": [mine]}}


def _serve(store, request):
    stop_serving = threading.Event()
    a, b = socket.socketpair()
    serving = threading.Thread(target=channel_link.serve, args=(store, a, request, stop_serving, lambda: None))
    serving.start()
    return a, b, serving, stop_serving


def test_only_openswaps_wake_command_under_claude_code_is_served(desktop, monkeypatch):
    desktop.status("idle", desktop.clock.now)
    stranger = Process(PID + 7, PID, "Fri Nov  6 14:00:02 2026", ("python3", "attention", "permission"))
    monkeypatch.setattr(peers, "peer_pid", lambda _c: stranger.pid)
    monkeypatch.setattr(peers, "hook_claude", lambda pid: CLAUDE)
    _running(monkeypatch, stranger)
    a, b = socket.socketpair()
    with a, b:
        channel_link.serve(desktop.store, a, {"version": 1, "command": "wake", "session_id": SESSION},
                           threading.Event(), lambda: None)
        assert json.loads(b.recv(4096)) == {"message": None}
    assert desktop.store._wakers == []


def test_a_wake_hook_takes_the_message_over_its_connection(desktop, monkeypatch):
    process = Process(PID + 9, PID, "Fri Nov  6 14:00:03 2026", ("openswap", "attention", "wake"))
    monkeypatch.setattr(peers, "peer_pid", lambda _c: process.pid)
    monkeypatch.setattr(peers, "hook_claude", lambda pid: CLAUDE)
    _running(monkeypatch, process)
    t = desktop.clock.now
    desktop.status("idle", t)
    stop(desktop)
    a, b, serving, stop_serving = _serve(desktop.store, {"version": 1, "command": "wake", "session_id": SESSION})
    with a, b:
        deadline = time.monotonic() + 5
        while not desktop.store._wakers and time.monotonic() < deadline:
            time.sleep(0.01)
        desktop.clock.now = t + 61
        desktop.store.tick()
        [item] = desktop.store._items
        desktop.reply(item.id, "message", text="Run the tests")
        assert json.loads(b.makefile("rb").readline()) == {"message": "Run the tests"}
        serving.join(5)
        assert desktop.outcomes() == {"r1": "sent"} and desktop.store._wakers == []
        stop_serving.set()


# --- row 11: a second typed reply ---------------------------------------------------------------


def test_row11_the_woken_turns_stop_brings_a_new_hook_and_a_second_reply_works(desktop):
    item, waker = finished(desktop)
    sent(desktop, item, waker)
    desktop.store.notice(hook("UserPromptSubmit", prompt=woken("Run the tests")) | desktop.at(0.1))
    desktop.status("busy", desktop.clock.now + 0.1)
    desktop.clock.now += 20
    second, again = finished(desktop)
    assert second.id != item.id and desktop.actions(second) == ["message"]
    desktop.reply(second.id, "message", reply_id="r2", text="Now the linter")
    assert again.result == "Now the linter"

