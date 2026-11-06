"""Session attention (plan 025, phase S1): hooks, items, IPC notices and the report."""

from __future__ import annotations

import io
import json
import os
import socket
import sys
import threading
import time
import tomllib
from datetime import datetime, timezone
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
    AttentionStore, Item, NoticeError, QUESTION_FALLBACK_SECONDS, approved_folder_label,
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
        self.now = 1_800_000_000.0

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


def notification(kind, session=SESSION):
    return hook("Notification", session=session, notification_type=kind, message="Claude needs you")


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
    items.notice(permission(command="b"))
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


def test_question_publishes_on_permission_prompt_or_after_the_fallback(store):
    items, clock = store
    items.notice(question())
    items.tick()
    assert published(items) == []
    clock.now += QUESTION_FALLBACK_SECONDS
    items.tick()
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
    items.prune()
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


def test_codex_turns_supersede_and_expire(store):
    items, clock = store
    turn = {"provider": "codex", "event": "agent-turn-complete", "thread_id": "019a-thread", "cwd": "/r",
            "last_message": "Done"}
    items.notice(validate_notice(dict(turn)))
    items.notice(validate_notice(dict(turn)))
    [item] = published(items)
    assert (item["kind"], item["provider"], item["session_label"]) == ("finished", "codex", "Codex · 019a")
    assert [c["reason"] for c in closes(items)] == ["superseded"]
    clock.now += 24 * 3600
    items.tick()
    assert published(items) == [] and closes(items)[-1] == {"id": item["id"], "reason": "expired"}


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
    {"provider": "codex", "event": "agent-turn-complete", "cwd": "/x"},
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


# --- Codex notify ------------------------------------------------------------------


COMPUTER_USE = ["/Users/o/.codex/computer-use/Sky.app/Contents/MacOS/SkyComputerUseClient", "turn-ended"]


def _codex(tmp_path, text):
    home = tmp_path / "codex"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(text)
    return home


def _notify(home):
    return tomllib.loads((home / "config.toml").read_text()).get("notify")


def test_chaining_keeps_the_previous_program_and_the_rest_of_the_file(tmp_path):
    original = ('# my settings\nmodel = "gpt-5"\nnotify = [\n  "%s",\n  "turn-ended",\n]\n\n'
                '[profiles.fast]\nnotify = ["keep"]\nmodel = "mini"  # inline\n') % COMPUTER_USE[0]
    home = _codex(tmp_path, original)
    assert attention.chain_codex_notify(home, EXE) is True
    text = (home / "config.toml").read_text()
    assert _notify(home) == [EXE, "attention", "codex-notify", "--", *COMPUTER_USE]
    assert "# my settings" in text and "# inline" in text
    assert tomllib.loads(text)["profiles"] == {"fast": {"notify": ["keep"], "model": "mini"}}
    assert attention.chain_codex_notify(home, EXE) is False
    assert attention.unchain_codex_notify(home) is True
    assert _notify(home) == COMPUTER_USE
    assert tomllib.loads((home / "config.toml").read_text()) == tomllib.loads(original)


def test_a_rewritten_notify_is_chained_again_keeping_the_new_program(tmp_path):
    home = _codex(tmp_path, 'notify = ["first"]\n')
    attention.chain_codex_notify(home, EXE)
    (home / "config.toml").write_text('notify = ["second", "arg"]\n')  # Codex Computer Use updated
    attention.chain_codex_notify(home, EXE)
    assert _notify(home) == [EXE, "attention", "codex-notify", "--", "second", "arg"]


def test_unchain_restores_only_while_notify_still_names_openswap(tmp_path):
    home = _codex(tmp_path, f'notify = ["{EXE}", "attention", "codex-notify", "--"]\n')
    assert attention.unchain_codex_notify(home) is True
    assert _notify(home) is None
    (home / "config.toml").write_text('notify = ["someone-else"]\n')
    assert attention.unchain_codex_notify(home) is False
    assert _notify(home) == ["someone-else"]


def test_chaining_without_a_notify_or_without_codex(tmp_path):
    home = _codex(tmp_path, '[tui]\ntheme = "dark"\n')
    attention.chain_codex_notify(home, EXE)
    assert tomllib.loads((home / "config.toml").read_text()) == {
        "notify": [EXE, "attention", "codex-notify", "--"], "tui": {"theme": "dark"}}
    assert attention.chain_codex_notify(tmp_path / "missing", EXE) is False


@pytest.mark.parametrize("text", ['notify = "a string"\n', "notify = [\n", 'notify = [1, 2]\n'])
def test_a_notify_this_cannot_rewrite_is_left_alone(tmp_path, text):
    home = _codex(tmp_path, text)
    with pytest.raises(ConfigError):
        attention.chain_codex_notify(home, EXE)
    assert (home / "config.toml").read_text() == text
    if text == "notify = [\n":
        with pytest.raises(ConfigError):  # uninstall must say it could not check
            attention.unchain_codex_notify(home)


def test_codex_notify_runs_the_previous_program_with_the_same_arguments(tmp_path, monkeypatch):
    out = tmp_path / "argv.json"
    script = tmp_path / "previous.py"
    script.write_text(f"import json, sys\nopen({str(out)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
    payload = json.dumps({"type": "agent-turn-complete", "thread-id": "019a", "cwd": "/r",
                          "last-assistant-message": "Done", "input-messages": ["typed by the owner"]})
    sent = []
    monkeypatch.setattr(attention, "send_notice", sent.append)
    code, _ = _run_main(monkeypatch, ["codex-notify", "--", sys.executable, str(script), "turn-ended", payload])
    assert code == 0
    for _ in range(50):
        if out.exists():
            break
        time.sleep(0.1)
    assert json.loads(out.read_text()) == ["turn-ended", payload]
    assert sent == [{"provider": "codex", "event": "agent-turn-complete", "thread_id": "019a", "cwd": "/r",
                     "last_message": "Done"}]
    validate_notice(sent[0])


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
