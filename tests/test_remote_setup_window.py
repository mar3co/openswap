"""The Remote tasks setup in the setup window, and Settings › Remote › Folders.

The steps are ``guided_setup``'s (the CLI's own tests pin their terminal
text); here they run through ``setup_window.WindowPrompts`` with a recording
window, and the pages are plain data. No AppKit.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar, menubar_display
from openswap.settings import WorkerSettings, configure_worker_service, load_worker_settings, update_worker_settings
from openswap.worker import cli, guided_setup, setup_window
from openswap.worker.guided_setup import Readiness, SetupClosed
from openswap.worker.setup_window import WindowPrompts, done_page, window_page
from tests.menubar_harness import SetupWindow, extract_class, settle_setup, setup_app
from tests.test_worker_accounts import ALICE, root  # noqa: F401 (fixture)
from tests.test_worker_guided_setup import URL, _builtin, _code, _repo, enable_calls, research_home  # noqa: F401 (fixtures)


def _drive(root, answers, *, chosen=(), steps=guided_setup.run):
    """Run ``steps`` on a worker thread through the window, clicking ``answers`` page by page."""
    window = answers if isinstance(answers, SetupWindow) else SetupWindow(answers)
    picks = list(chosen)
    ui = WindowPrompts(guided_setup.DialogPrompts(window.alert, lambda **_k: None), window,
                       lambda: picks.pop(0) if picks else None)
    ui.settle_wait_s = 0
    window.ui = ui

    def run():
        try:
            steps(root, ui)
        except SetupClosed:
            pass
        finally:
            ui.flush()
            ui.finished = True

    threading.Thread(target=run, daemon=True).start()
    deadline = time.monotonic() + 20
    while not ui.finished:
        assert time.monotonic() < deadline, "the steps did not finish"
        ui.serve()
        if window.fresh:
            window.fresh = False
            answer = window.answers.pop(0) if window.answers else "close"
            for action in [answer] if isinstance(answer, str) else answer:
                ui.answer(action)
        time.sleep(0.005)
    return window


def _notes(page):
    return [row["text"] for row in page["rows"] if row["kind"] == "note"]


def _choices(page):
    return next(row for row in page["rows"] if row["kind"] == "choices")["items"]


def _state(**kwargs):
    base = dict(paired_url=URL, worker="running", account="Codex 1 · alice@example.com", folders=("github",),
                execution="live", readable=("github (GitHub)",), connection="online")
    return Readiness(**{**base, **kwargs})


# --- pages ----------------------------------------------------------------------------------------


def test_the_worker_page_offers_start_skip_and_close_setup():
    page = window_page(0, "confirm", (guided_setup.WORKER_OFFER,), {}, [])
    assert page["title"] == "Start the worker" and page["body"] == setup_window.WORKER_BODY
    assert page["primary"] == {"title": "Start worker", "action": "yes", "enabled": True}
    assert page["skip"] == {"title": "Skip", "action": "skip"}
    assert page["leading"] == {"title": "Close Setup", "action": "close"} and page["progress"] == (1, 4)


def test_any_other_question_is_a_yes_no_page():
    page = window_page(2, "confirm", ("Work in ~/x itself, without a copy?",), {}, [])
    assert page["title"] == "Choose folders for tasks" and page["body"] == "Work in ~/x itself, without a copy?"
    assert (page["primary"]["action"], page["skip"]) == ("yes", {"title": "No", "action": "no"})


def test_a_held_page_shows_the_lines_with_continue_only():
    page = window_page(0, "hold", (), {}, ["✗ Could not start the worker.", "✓ fine"])
    assert page["skip"] is None and page["primary"]["action"] == "continue"
    assert [(row["text"], row["tone"]) for row in page["rows"]] == [
        ("✗ Could not start the worker.", "warning"), ("✓ fine", "muted")]


def test_the_account_page_continues_only_once_a_row_is_picked():
    options = ["Work · a@x.com · Codex", "b@x.com · Claude"]
    empty = window_page(1, "pick", ("Tasks run on the account you pick.", options), {"selected": None}, [])
    assert empty["primary"]["enabled"] is False and not any(item["selected"] for item in _choices(empty))
    picked = window_page(1, "pick", ("Tasks run on the account you pick.", options), {"selected": 1}, [])
    assert picked["primary"]["enabled"] is True
    assert [item["selected"] for item in _choices(picked)] == [False, True]
    assert _choices(picked)[1]["action"] == "select:1"


def test_the_folder_page_lists_rows_then_added_folders_and_add_folder(research_home):
    added = str(research_home.parent / "Notes")
    view = {"checked": [True, False, True], "added": [added]}
    page = window_page(2, "check_folders", ([("~/GitHub", ""), ("~/Code", "recommended")], [True, False]),
                       view, [])
    assert page["body"] == setup_window.FOLDERS_BODY and "itself" in page["body"]
    assert [(item["title"], item["detail"], item["selected"]) for item in _choices(page)] == [
        ("~/GitHub", "", True), ("~/Code", "recommended", False), ("~/Notes", "", True)]
    assert page["rows"][-1]["items"][0]["action"] == "add_folder"


@pytest.mark.parametrize("state, title, worker, lines", [
    (_state(), setup_window.SET_UP, ("running", True), []),
    (_state(worker="starting"), setup_window.ALMOST, ("connecting…", None),
     ["Wait a moment, then check Status in Settings → Remote."]),
    (_state(connection="offline"), setup_window.ALMOST, ("connecting…", None),
     ["Wait a moment, then check Status in Settings → Remote."]),
    (_state(worker="off", account=None), setup_window.ALMOST, ("off", False),
     ["Turn on Local worker in Settings → Remote.",
      "Pick an account under Account in Settings → Remote."]),
    (_state(worker="stopped"), setup_window.ALMOST, ("on but not running", False),
     ["Turn Local worker off and on in Settings → Remote to restart it."]),
    (_state(paused=True, execution="disabled"), setup_window.ALMOST, ("paused", False),
     ["Turn off Pause new tasks in Settings → Remote to resume."]),
    (_state(execution="disabled"), setup_window.ALMOST, ("running", True), [setup_window.LIVE_OFF]),
])
def test_the_done_page_checklist_names_settings_controls(state, title, worker, lines):
    page = done_page(state)
    items = page["rows"][0]["items"]
    assert page["title"] == title and page["primary"]["action"] == "done" and page["progress"] == (4, 4)
    assert items[0] == ("Worker", *worker)
    assert [label for label, *_rest in items] == ["Worker", "Account", "Folders", "Live tasks"]
    assert _notes(page) == lines


def test_the_done_page_names_refused_folders_and_the_claude_sign_in_note():
    page = done_page(_state(refused=(("site", "readable_permissions"),), provider="claude"))
    assert page["rows"][0]["items"][2][2] is False
    lines = _notes(page)
    assert lines[0] == 'Remove "site" under Folders in Settings → Remote.'
    assert lines[1].startswith('✗ "site" is blocked. Other users can change it.')
    assert lines[2] == setup_window.SIGN_IN_NOTE
    assert "`" not in " ".join(lines)


@pytest.mark.parametrize("connection, worker", [("revoked", "removed in OpenTag"), ("expired", "pairing expired")])
def test_a_lost_pairing_shows_on_the_worker_row_ahead_of_paused(connection, worker):
    page = done_page(_state(paused=True, connection=connection))
    assert page["rows"][0]["items"][0] == ("Worker", worker, False)


def test_a_held_page_at_the_summary_step_has_a_title():
    page = window_page(3, "hold", (), {}, [guided_setup.SUMMARY_FAILED])
    assert page["title"] == setup_window.ALMOST and page["progress"] == (4, 4)


# --- copy: the CLI keeps its commands, the window names Settings controls ------------------------


@pytest.mark.parametrize("state", [
    _state(paired_url=None, worker="off", account=None, folders=(), refused=(("x", "folder_invalid"),)),
    _state(worker="starting"), _state(worker="stopped"), _state(connection="revoked"),
    _state(connection="expired"), _state(paused=True),
])
def test_every_gap_has_a_command_line_and_a_settings_line(state):
    assert all("`openswap" in line for line in state.missing if not line.startswith("wait"))
    assert state.settings_missing and all("`" not in line and "openswap " not in line
                                          for line in state.settings_missing)


def test_the_window_names_no_command_where_the_cli_does():
    assert set(guided_setup.SETTINGS_NEXT) == {guided_setup.START_WORKER_NEXT, guided_setup.ACCOUNT_NEXT,
                                               guided_setup.FOLDER_NEXT}
    gui = [*guided_setup.SETTINGS_NEXT.values(), guided_setup.NO_ACCOUNT_SETTINGS, guided_setup.WORKER_PAUSED]
    gui += [guided_setup.workspace_message(code, gui=True) for code in cli._WORKSPACE_MESSAGES]
    gui += [guided_setup.account_message(code, gui=True) for code in cli._ACCOUNT_MESSAGES]
    gui += list(guided_setup._SETTINGS_ENABLE_MESSAGES.values())
    assert all("`" not in line and "openswap " not in line for line in gui)
    assert all(guided_setup.workspace_message(code) == text for code, text in cli._WORKSPACE_MESSAGES.items())
    assert all(guided_setup.account_message(code) == text for code, text in cli._ACCOUNT_MESSAGES.items())


# --- the worker step ------------------------------------------------------------------------------


@pytest.fixture
def worker_on(root, monkeypatch):
    update_worker_settings(root, enabled=True)

    def process(state):
        monkeypatch.setattr(cli, "read_status", lambda _root: {"process_state": state})
    return process


def test_a_running_worker_moves_straight_to_the_account(root, worker_on, enable_calls):
    worker_on("running")
    window = _drive(root, ["skip", "skip", "done"])
    assert window.shown[0]["title"] == "Choose the account for tasks" and enable_calls == []


def test_a_paused_worker_points_to_pause_admission(root, worker_on, enable_calls):
    worker_on("running")
    update_worker_settings(root, paused=True)
    window = _drive(root, ["continue", "skip", "skip", "done"])
    held = window.shown[0]
    assert held["title"] == "Start the worker" and held["skip"] is None
    assert _notes(held) == [guided_setup.WORKER_PAUSED] and enable_calls == []


def test_a_stopped_worker_shows_why_and_offers_start(root, worker_on, enable_calls):
    worker_on("stopped")
    window = _drive(root, ["yes", "skip", "skip", "done"])
    page = window.shown[0]
    assert _notes(page) == [guided_setup.WORKER_STOPPED] and page["primary"]["title"] == "Start worker"
    assert enable_calls == [root]


def test_a_refused_start_stays_on_the_worker_page_without_a_command(root, monkeypatch):
    def refuse(_root):
        raise cli.ClaudeSwitchError("worker_running_unmanaged")

    monkeypatch.setattr(cli, "enable_worker", refuse)
    window = _drive(root, ["yes", "continue", "skip", "skip", "done"])
    held = window.shown[1]
    assert held["title"] == "Start the worker" and held["primary"]["action"] == "continue"
    (line,) = _notes(held)
    assert line.startswith("Could not start the worker: another one started from Terminal is running")
    assert line.endswith(guided_setup.SETTINGS_NEXT[guided_setup.START_WORKER_NEXT])


RESTART = "Turn Local worker off and on in Settings → Remote to restart it."


def test_a_failed_restart_of_a_stopped_worker_points_to_the_switch(root, worker_on, monkeypatch):
    worker_on("stopped")

    def refuse(_root):
        raise cli.ClaudeSwitchError("worker_stop_unconfirmed")

    monkeypatch.setattr(cli, "enable_worker", refuse)
    (line,) = _notes(_drive(root, ["yes", "continue", "skip", "skip", "done"]).shown[1])
    assert line.endswith(f"(worker_stop_unconfirmed). {RESTART}") and guided_setup.SETTINGS_NEXT[guided_setup.START_WORKER_NEXT] not in line


def test_a_worker_from_terminal_blocking_a_restart_says_only_that(root, worker_on, monkeypatch):
    worker_on("stopped")

    def refuse(_root):
        raise cli.ClaudeSwitchError("worker_running_unmanaged")

    monkeypatch.setattr(cli, "enable_worker", refuse)
    assert _notes(_drive(root, ["yes", "continue", "skip", "skip", "done"]).shown[1]) == [
        "Could not start the worker: another one started from Terminal is running (worker_running_unmanaged)."]


def test_an_unreadable_worker_status_says_so_and_offers_start(root, monkeypatch, enable_calls):
    update_worker_settings(root, enabled=True)

    def unreadable(_root):
        raise OSError("health")

    monkeypatch.setattr(cli, "read_status", unreadable)
    page = _drive(root, ["yes", "skip", "skip", "done"]).shown[0]
    assert _notes(page) == [guided_setup.WORKER_UNREADABLE] and page["primary"]["title"] == "Start worker"
    assert enable_calls == [root]


@pytest.mark.parametrize("status", [{"process_state": "stopped"}, OSError("health")])
def test_skipping_the_start_of_an_enabled_worker_points_to_the_switch(root, monkeypatch, status):
    update_worker_settings(root, enabled=True)

    def read(_root):
        if isinstance(status, Exception):
            raise status
        return status

    monkeypatch.setattr(cli, "read_status", read)
    lines = []
    guided_setup.offer_worker(root, SimpleNamespace(gui=True, interactive=True, say=lines.append,
                                                    confirm=lambda *_a, **_k: None))
    assert lines[-1] == f"Not started. {RESTART}"


def test_the_page_is_disabled_while_its_answer_runs(root, enable_calls):
    window = _drive(root, ["yes", "skip", "skip", "done"])
    first = window.pages.index(window.shown[0])
    assert window.pages[first + 1] == {**window.shown[0], "busy": True}


# --- the account step -----------------------------------------------------------------------------


def test_a_valid_pin_is_preselected(root, enable_calls):
    cli.set_worker_account(root, ALICE)
    window = _drive(root, ["skip", "continue", "skip", "done"])
    account = window.shown[1]
    assert _choices(account)[0]["selected"] is True and account["primary"]["enabled"] is True
    assert load_worker_settings(root).pinned_account_ref == ALICE


def test_a_missing_pin_preselects_nothing_and_says_so(root, monkeypatch, enable_calls):
    real = cli.worker_account_choices
    monkeypatch.setattr(cli, "worker_account_choices",
                        lambda backup_root: dataclasses.replace(real(backup_root), pinned_ref="codex:gone"))
    account = _drive(root, ["skip", "skip", "skip", "done"]).shown[1]
    assert _notes(account) == ["Your pinned account is gone; pick another."]
    assert not any(item["selected"] for item in _choices(account)) and account["primary"]["enabled"] is False


def test_a_rejected_pin_keeps_the_owner_on_the_page_with_the_reason(root, monkeypatch, enable_calls):
    real, calls = cli.set_worker_account, []

    def pin(backup_root, selector):
        calls.append(selector)
        if len(calls) == 1:
            raise cli.AccountPinError("account_not_allowlisted")
        return real(backup_root, selector)

    monkeypatch.setattr(cli, "set_worker_account", pin)
    window = _drive(root, ["skip", ["select:0", "continue"], ["select:0", "continue"], "skip", "done"])
    again = window.shown[2]
    assert again["title"] == "Choose the account for tasks"
    assert _notes(again) == [guided_setup.account_message("account_not_allowlisted", gui=True)]
    assert load_worker_settings(root).pinned_account_ref == ALICE


# --- the folder step ------------------------------------------------------------------------------


def _folders(root, research_home, *names, saved=()):
    home = research_home.parent
    made = {name: _repo(_code(home, name)) for name in names}
    for name in saved:
        cli.add_work_folder(root, made[name])
    return made


def test_saved_folders_are_checked_and_found_ones_are_not(root, research_home):
    _folders(root, research_home, "GitHub", "Projects", saved=("Projects",))
    rows = guided_setup.folder_rows(root, load_worker_settings(root).workspaces)
    assert [(row.label, row.checked, row.workspace is not None) for row in rows] == [
        ("~/Projects", True, True), ("~/GitHub", False, False)]


def test_nothing_saved_checks_the_recommended_folder(root, research_home):
    _folders(root, research_home, "Code", "GitHub")
    rows = guided_setup.folder_rows(root, load_worker_settings(root).workspaces)
    assert [(row.label, row.note, row.checked) for row in rows] == [
        ("~/GitHub", "recommended", True), ("~/Code", "", False)]


def test_a_direct_folder_says_it_works_in_the_folder_itself(root, research_home):
    folder = _repo(_code(research_home.parent, "Site"))
    cli.add_work_folder(root, folder, mode="direct")
    (row,) = guided_setup.folder_rows(root, load_worker_settings(root).workspaces)
    assert row.label == "~/Site (works in the folder itself)"


def test_continue_adds_new_folders_before_removing_unchecked_ones(root, research_home, monkeypatch, enable_calls):
    _folders(root, research_home, "GitHub", "Projects", "Code", saved=("GitHub", "Projects"))
    order = []
    add, remove = cli.add_work_folder, cli.remove_worker_workspace
    monkeypatch.setattr(cli, "add_work_folder", lambda r, f, **k: order.append(("add", f.name)) or add(r, f, **k))
    monkeypatch.setattr(cli, "remove_worker_workspace", lambda r, i: order.append(("remove", i)) or remove(r, i))
    # Rows: ~/GitHub ✓, ~/Projects ✓, ~/Code ☐ → keep GitHub, drop Projects, add Code.
    window = _drive(root, ["skip", "skip", ["toggle:1", "toggle:2", "continue"], "continue", "done"])
    assert order == [("add", "Code"), ("remove", "projects")]
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github", "code"]
    results = window.shown[3]
    assert results["title"] == "Choose folders for tasks"
    assert _notes(results) == ["✓ ~/Code (code)", "✓ Removed ~/Projects (its files are kept)."]
    done = window.shown[4]
    assert done["rows"][0]["items"][2][1] == "github (GitHub), code (Code)"


def test_unchecking_every_folder_is_refused_before_anything_changes(root, research_home, enable_calls):
    _folders(root, research_home, "GitHub", saved=("GitHub",))
    window = _drive(root, ["skip", "skip", ["toggle:0", "continue"], "skip", "done"])
    again = window.shown[3]
    assert _notes(again) == ["Keep at least one folder."] and _choices(again)[0]["selected"] is True
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github"]


def test_a_checked_repo_inside_an_unchecked_folder_of_repos_stays_allowed(root, research_home, enable_calls):
    home = research_home.parent
    _repo(_code(home, "Code/app"), _code(home, "Code/lib"))
    notes = _repo(_code(home, "Notes"))
    cli.add_work_folder(root, home / "Code")
    cli.add_work_folder(root, notes)
    # Rows: ~/Code ✓ (a folder of repos), ~/Notes ✓; uncheck ~/Code, add ~/Code/app.
    _drive(root, ["skip", "skip", ["toggle:0", "add_folder", "continue"], "continue", "done"],
           chosen=[str(home / "Code" / "app")])
    workspaces = load_worker_settings(root).workspaces
    assert [w.work_root.name for w in workspaces] == ["Notes", "app"]


def test_a_checked_repo_inside_an_unchecked_read_only_folder_is_added_after_it(root, research_home, enable_calls):
    home = research_home.parent
    app = _repo(_code(home, "GitHub/app"))
    cli.add_readable_folder(root, home / "GitHub")
    cli.add_work_folder(root, _repo(_code(home, "Notes")))
    # Rows: ~/GitHub (read only) ✓, ~/Notes ✓; uncheck ~/GitHub, add ~/GitHub/app.
    window = _drive(root, ["skip", "skip", ["toggle:0", "add_folder", "continue"], "continue", "done"],
                    chosen=[str(app)])
    assert [w.work_root.name for w in load_worker_settings(root).workspaces] == ["Notes", "app"]
    assert _notes(window.shown[3]) == ["✓ Removed ~/GitHub (read only) (its files are kept).", "✓ ~/GitHub/app (app)"]


def test_switching_the_only_read_only_folder_to_a_repo_in_it_says_how(root, research_home, enable_calls):
    home = research_home.parent
    app = _repo(_code(home, "GitHub/app"))
    cli.add_readable_folder(root, home / "GitHub")
    window = _drive(root, ["skip", "skip", ["toggle:0", "add_folder", "continue"], "continue", "done"],
                    chosen=[str(app)])
    assert _notes(window.shown[3]) == [
        "To switch to ~/GitHub/app, add another folder first, then remove ~/GitHub (read only)."]
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github"]


def test_the_only_folder_of_repos_kept_still_says_why(root, research_home, enable_calls):
    home = research_home.parent
    _repo(_code(home, "Code/app"))
    cli.add_work_folder(root, home / "Code")
    window = _drive(root, ["skip", "skip", ["toggle:0", "add_folder", "continue"], "continue", "done"],
                    chosen=[str(home / "Code" / "app")])
    lines = _notes(window.shown[3])
    assert lines[-1] == f"✗ ~/Code: {cli._WORKSPACE_MESSAGES['last_workspace']}" and len(lines) == 2


class _LockWindow(SetupWindow):
    """Records whether Settings' folder buttons wait, for each page shown and each folder write."""

    def __init__(self, answers):
        super().__init__(answers)
        self.locks = []

    def show(self, page):
        self.locks.append((page["title"], self.ui.folders_locked))
        super().show(page)


def test_settings_folders_wait_only_while_the_folder_step_may_write(root, research_home, monkeypatch, enable_calls):
    _folders(root, research_home, "GitHub", "Code", saved=("GitHub",))
    window = _LockWindow(["skip", "skip", ["toggle:1", "continue"], "continue", "done"])
    add = cli.add_work_folder
    monkeypatch.setattr(cli, "add_work_folder",
                        lambda r, f, **k: window.locks.append(("add", window.ui.folders_locked)) or add(r, f, **k))
    _drive(root, window)
    assert window.locks == [("Start the worker", False), ("Choose the account for tasks", False),
                            ("Choose folders for tasks", True), ("add", True), ("Choose folders for tasks", False),
                            (setup_window.ALMOST, False)]


def test_a_skipped_folder_step_frees_the_settings_folders(root, research_home, enable_calls):
    _folders(root, research_home, "GitHub", saved=("GitHub",))
    window = _drive(root, _LockWindow(["skip", "skip", "skip", "done"]))
    assert window.locks[2:] == [("Choose folders for tasks", True), (setup_window.ALMOST, False)]
    assert window.ui.folders_locked is False


def test_a_busy_worker_lock_on_a_folder_add_says_try_again(root, research_home, monkeypatch, enable_calls):
    _folders(root, research_home, "GitHub", "Code", saved=("GitHub",))

    def busy(*_args, **_kwargs):
        raise cli.ClaudeSwitchError("worker_lifecycle_busy")

    monkeypatch.setattr(cli, "add_work_folder", busy)
    window = _drive(root, ["skip", "skip", ["toggle:1", "continue"], "continue", "done"])
    assert _notes(window.shown[3]) == [f"~/Code: {cli.BUSY_MESSAGE}"]


def test_the_terminal_keeps_its_line_when_a_folder_add_fails(root, monkeypatch):
    def busy(*_args, **_kwargs):
        raise cli.ClaudeSwitchError("worker_lifecycle_busy")

    monkeypatch.setattr(cli, "add_work_folder", busy)
    lines = []
    guided_setup._add_folders(root, SimpleNamespace(say=lines.append), ["Code"])
    assert lines == [f"Code: {cli._WORKSPACE_MESSAGES['settings_unavailable']}"]


# --- the summary and closing ----------------------------------------------------------------------


def test_the_summary_waits_for_a_starting_worker_before_the_done_page(root, monkeypatch):
    states = iter([_state(worker="starting"), _state()])
    monkeypatch.setattr(guided_setup, "readiness", lambda _root: next(states))
    seen = []
    guided_setup.summary(root, SimpleNamespace(done=seen.append, advanced=False), start_wait_s=5)
    assert seen == [_state()]


def test_closing_answers_the_waiting_question_and_every_later_one_as_close_setup():
    window = SetupWindow()
    ui = WindowPrompts(guided_setup.DialogPrompts(None, None), window, lambda: None)
    ui.section("Step 1 of 4 · Worker")
    outcome = []

    def ask(question):
        try:
            outcome.append(question())
        except SetupClosed:
            outcome.append("closed")

    def asked(question):
        window.fresh = False
        thread = threading.Thread(target=ask, args=(question,))
        thread.start()
        while thread.is_alive() and not window.fresh:
            ui.serve()
            time.sleep(0.005)
        return thread

    thread = asked(lambda: ui.confirm(guided_setup.WORKER_OFFER))
    ui.front()
    assert window.shown[-1] == window.shown[0]  # choosing setup again brings it forward
    ui.answer("close")
    thread.join(5)
    assert outcome == ["closed"] and not window.visible
    asked(lambda: ui.pick("Tasks run on the account you pick.", ["a"])).join(5)
    assert outcome == ["closed", "closed"] and len(window.shown) == 2


def test_pairing_lines_show_on_the_first_page_drawn(root, worker_on, enable_calls):
    worker_on("running")

    def steps(backup_root, ui):
        ui.say("✓ Paired this Mac (worker-1).")
        guided_setup.run(backup_root, ui)

    account = _drive(root, ["skip", "skip", "done"], steps=steps).shown[0]
    assert account["title"] == "Choose the account for tasks"
    assert _notes(account) == ["✓ Paired this Mac (worker-1).", "✓ Worker running."]


def test_a_failed_summary_is_shown_before_the_window_closes(root, monkeypatch, enable_calls):
    def unreadable(_root):
        raise OSError("settings")

    monkeypatch.setattr(guided_setup, "readiness", unreadable)
    window = _drive(root, ["skip", "skip", "skip", "continue"])
    last = window.shown[-1]
    assert last["title"] == setup_window.ALMOST and _notes(last) == [guided_setup.SUMMARY_FAILED]


class _BrokenOnce(SetupWindow):
    """A window whose first page can't be shown."""

    broken = True

    def show(self, page):
        if self.broken:
            self.broken = False
            raise RuntimeError("no window")
        super().show(page)


def test_a_page_that_cannot_show_ends_the_run_with_its_lines_in_a_dialog(root, enable_calls):
    window = _drive(root, _BrokenOnce([]))
    assert window.alerts == [setup_window.PAGE_FAILED] and window.shown == [] and window.closed >= 1


def test_closing_while_a_folder_write_runs_lets_it_finish(root, research_home, monkeypatch, enable_calls):
    _folders(root, research_home, "GitHub", "Code", saved=("GitHub",))
    window = SetupWindow(["skip", "skip", ["toggle:1", "continue"]])
    add = cli.add_work_folder
    monkeypatch.setattr(cli, "add_work_folder", lambda r, f, **k: window.ui.answer("close") or add(r, f, **k))
    _drive(root, window)
    busy = [page for page in window.pages if page.get("busy")][-1]
    assert busy["title"] == "Choose folders for tasks" and busy["leading"]["action"] == "close"
    assert len(window.shown) == 3 and not window.visible and window.alerts == []
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github", "code"]


def _menu(root, window):
    return setup_app(root, window, _alert=window.alert, _prompt=None, _choose_folder=None, _wait_dialog=None)


def test_choosing_setup_again_brings_the_running_one_forward(root, enable_calls):
    configure_worker_service(root, URL, "worker-1")
    window = SetupWindow(["skip", "skip", "skip", "done"])
    app = _menu(root, window)
    app._on_setting("remote_tasks_setup", None)
    running = app._guided_setup
    while not window.shown:
        app._drain_guided_setup()
        time.sleep(0.005)
    for row_id in ("remote_tasks_setup", "remote_tasks_connect"):
        app._on_setting(row_id, None)
        assert app._guided_setup is running and window.shown[-1] == window.shown[0]
    window.shown[1:] = []
    settle_setup(app)
    assert app.refreshed == 1


def test_setup_chosen_again_after_a_page_failed_runs(root, enable_calls):
    configure_worker_service(root, URL, "worker-1")
    window = _BrokenOnce(["skip", "skip", "skip", "done"])
    app = _menu(root, window)
    app._on_setting("remote_tasks_setup", None)
    settle_setup(app)
    assert window.alerts == [setup_window.PAGE_FAILED] and app.refreshed == 1
    app._on_setting("remote_tasks_setup", None)
    settle_setup(app)
    assert [page["title"] for page in window.shown] == [
        "Start the worker", "Choose the account for tasks", "Choose folders for tasks", setup_window.ALMOST]


def test_setup_chosen_while_a_closed_run_finishes_a_change_says_so(root):
    app = _menu(root, SetupWindow())
    ui = WindowPrompts(guided_setup.DialogPrompts(None, None), app._remote_setup_window, lambda: None)
    ui.close()
    app._guided_setup = ui
    app._on_setting("remote_tasks_setup", None)
    assert app._guided_setup is ui and app._worker_status_cache["setup_note"] == "Setup is finishing a change…"


@pytest.mark.parametrize("row_id", ["remote_tasks_setup", "remote_tasks_connect", "remote_tasks_connect_other"])
def test_setup_chosen_while_a_settings_change_runs_says_so(root, row_id):
    app = _menu(root, SetupWindow())
    app._worker_operation = "worker_folder_update"
    app._on_setting(row_id, None)
    assert getattr(app, "_guided_setup", None) is None
    assert app._worker_status_cache["setup_note"] == cli.BUSY_MESSAGE
    rows = _remote(paired=False, **app._worker_status_cache)
    assert rows[1]["id"] == "remote_tasks_setup_note" and rows[1]["label"] == cli.BUSY_MESSAGE


# --- Settings › Remote › Folders ------------------------------------------------------------------


def _remote(paired=True, expanded=(), enabled=False, paused=False, **status):
    return menubar.settings_page_rows(menubar.MenuBarSettings(), strategy="best", threshold=90,
                                      section=menubar.SETTINGS_SECTION_REMOTE, worker_paired=paired,
                                      worker_enabled=enabled, worker_paused=paused,
                                      worker_status=status, expanded_sections=set(expanded))


GITHUB = ("github", "GitHub", "~/GitHub", "")
GONE = "⚠ That folder was already removed."


def _sections(rows):
    return [row["label"] for row in rows if row.get("style") == "section"]


def test_settings_remote_has_four_sections_with_short_labels():
    rows = _remote(folders=[GITHUB])
    assert _sections(rows) == ["Remote tasks", "Account", "Folders", "Status"]
    labels = {row["id"]: (row["label"], row.get("title")) for row in rows}
    assert labels["remote_tasks_enabled"][0] == "Local worker"
    assert labels["remote_tasks_paused"][0] == "Pause new tasks"
    assert labels["remote_tasks_setup"] == ("Setup", "Set Up…")
    assert labels["remote_tasks_account"][0] == "Tasks use"
    assert labels["remote_tasks_web_choice"][0] == "Web choice"


def test_settings_remote_before_pairing_shows_connect_and_no_folders_section():
    rows = _remote(paired=False, folders=[GITHUB])
    assert _sections(rows) == ["Remote tasks", "Account", "Status"]
    ids = [row["id"] for row in rows]
    assert ids[1:3] == ["remote_tasks_connect", "remote_tasks_connect_other"]
    assert not any(row_id.startswith("remote_tasks_folder") for row_id in ids)


def test_settings_lists_each_folder_by_name_and_path_then_add_folder():
    rows = _remote(folders=[GITHUB, ("notes", "Notes", "~/Notes", "read only")])
    ids = [row["id"] for row in rows]
    start = ids.index("remote_tasks_folders")
    assert ids[start:start + 4] == ["remote_tasks_folders", "remote_tasks_folder_remove",
                                    "remote_tasks_folder_remove", "remote_tasks_folder_add"]
    assert ids.index("remote_tasks_folder_add") < ids.index("group_remote_status")
    github, notes = rows[start + 1], rows[start + 2]
    assert (github["label"], github["detail"], github["title"], github["value"], github["disabled"]) == (
        "GitHub", "~/GitHub", "Remove", "github", False)
    assert (notes["label"], notes["detail"], notes["full_label"]) == ("Notes", "~/Notes · read only",
                                                                     "Notes · ~/Notes · read only")
    assert rows[start + 3]["title"] == "Add Folder…" and rows[start + 3]["label"] == ""


def test_settings_without_folders_says_tasks_need_one():
    rows = _remote(folders=[])
    (hint,) = [row for row in rows if row["id"] == "remote_tasks_folders_note"]
    assert hint["label"] == menubar.REMOTE_NO_FOLDERS and "tone" not in hint
    assert "remote_tasks_folder_add" in [row["id"] for row in rows]


def test_settings_shows_no_folders_before_pairing_or_before_the_first_read():
    for rows in (_remote(paired=False, folders=[GITHUB]), _remote()):
        assert not any(row["id"].startswith("remote_tasks_folder") for row in rows)


def test_settings_folders_wait_while_the_setup_window_changes_them():
    rows = _remote(folders=[GITHUB], setup_changing_folders=True)
    (hint,) = [row for row in rows if row["id"] == "remote_tasks_folders_note"]
    assert hint["label"] == menubar.REMOTE_SETUP_CHANGING_FOLDERS
    assert all(row["disabled"] for row in rows if row["id"] in ("remote_tasks_folder_remove",
                                                                "remote_tasks_folder_add"))


def test_settings_says_when_the_folder_list_cannot_be_read():
    rows = _remote(folders=None, folder_notice="⚠ That folder was already removed.")
    assert [row["label"] for row in rows if row["id"] == "remote_tasks_folders_note"] == [
        menubar.REMOTE_FOLDERS_UNREADABLE, "⚠ That folder was already removed."]
    assert "remote_tasks_folder_remove" not in [row["id"] for row in rows]
    assert next(row for row in rows if row["id"] == "remote_tasks_folder_add")["disabled"] is False
    assert all(row["tone"] == "warning" for row in rows if row["id"] == "remote_tasks_folders_note")


def test_a_long_folder_path_keeps_its_end_its_use_and_its_full_text():
    paths = "~/" + "/".join(["a-rather-long-folder-name"] * 4)
    (remove,) = [row for row in _remote(folders=[("site", "Site", paths, "read only")])
                 if row["id"] == "remote_tasks_folder_remove"]
    assert remove["full_label"] == f"Site · {paths} · read only"
    assert len(remove["label"]) + len(remove["detail"]) <= menubar.REMOTE_FOLDER_LABEL_CHARS
    assert remove["detail"].startswith("~/a-rather") and remove["detail"].endswith("name · read only")
    assert "…" in remove["detail"]


def test_a_folder_message_shows_once_under_the_list_and_a_refusal_is_a_warning():
    rows = _remote(folders=[GITHUB], folder_notice="⚠ ~/Desktop: no git repos in it.",
                   operation="worker_folder_update")
    ids = [row["id"] for row in rows]
    (hint,) = [row for row in rows if row["id"] == "remote_tasks_folders_note"]
    assert hint["label"] == "⚠ ~/Desktop: no git repos in it." and hint["tone"] == "warning"
    assert ids.index("remote_tasks_folders_note") == ids.index("remote_tasks_folder_add") + 1
    assert all(row["disabled"] for row in rows if row["id"] in ("remote_tasks_folder_remove",
                                                                "remote_tasks_folder_add"))
    (plain,) = [row for row in _remote(folders=[GITHUB], folder_notice="Added 2 folders from ~/GitHub.")
                if row["id"] == "remote_tasks_folders_note"]
    assert "tone" not in plain


# --- Settings › Remote › Status ---------------------------------------------------------------------

SOON = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
RUNNING = {"process_state": "running", "provider": {"available": True}, "remote_connectivity": "online"}
JOB = {"job_id": "job-1", "state": "waiting_for_approval", "origin": {"kind": "assistant", "label": "ChatGPT"}}


@pytest.mark.parametrize("status, enabled, paused, expected", [
    (RUNNING, True, False, ("ok", "Running · online · idle")),
    ({**RUNNING, "remote_connectivity": None}, True, False, ("ok", "Running · idle")),
    ({**RUNNING, "active_job": JOB}, True, False, ("ok", "Running · waiting for approval · from ChatGPT (assistant)")),
    ({**RUNNING, "active_job": {"job_id": "job-1", "state": "running"}}, True, False, ("ok", "Running · task running")),
    ({"process_state": "starting"}, True, False, ("muted", "Starting…")),
    ({"process_state": "stopping"}, True, False, ("muted", "Stopping…")),
    ({**RUNNING, "operation": "worker_folder_update"}, True, False, ("muted", "Applying a change…")),
    ({**RUNNING, "operation": "worker_status_checking"}, True, False, ("muted", "Checking…")),
    ({"process_state": "stopped"}, True, False, ("crit", "Not running")),
    ({"process_state": "stale"}, True, False, ("crit", "Not running")),
    ({"process_state": "unavailable"}, True, False, ("warn", "Status unavailable")),
    (RUNNING, False, False, ("muted", "Off")),
    (RUNNING, True, True, ("warn", "Paused")),
    ({**RUNNING, "remote_connectivity": "offline"}, True, False, ("warn", "Running · offline")),
    ({**RUNNING, "provider": {"available": False, "diagnostic_code": "live_adapter_disabled"}}, True, False,
     ("warn", "Running · live tasks off")),
    ({**RUNNING, "provider": {"available": False, "diagnostic_code": "provider_unavailable"}}, True, False,
     ("warn", "Running · account unavailable")),
    ({**RUNNING, "provider": None}, True, False, ("warn", "Running · tasks can't run")),
    ({**RUNNING, "remote_connectivity": "revoked"}, True, True, ("crit", "Running · removed in OpenTag")),
    ({**RUNNING, "remote_connectivity": "expired"}, True, True, ("crit", "Running · key expired · paused")),
    ({**RUNNING, "remote_connectivity": "offline"}, True, True, ("warn", "Running · offline · paused")),
    ({**RUNNING, "provider": {"available": False, "diagnostic_code": "live_adapter_disabled"}}, True, True,
     ("warn", "Running · live tasks off · paused")),
    ({**RUNNING, "active_job": JOB}, True, True, ("warn", "Paused · waiting for approval · from ChatGPT (assistant)")),
    ({**RUNNING, "active_job": JOB, "provider": {"available": False, "diagnostic_code": "provider_unavailable"}},
     True, False, ("warn", "Running · account unavailable")),
    ({**RUNNING, "remote_key_expires_at": PAST, "remote_key_renews": True}, True, False,
     ("crit", "Running · key expired")),
    ({**RUNNING, "remote_key_expires_at": SOON, "remote_key_renews": False}, True, False,
     ("warn", "Running · online · idle")),
    ({**RUNNING, "remote_key_expires_at": SOON, "remote_key_renews": True}, True, False,
     ("ok", "Running · online · idle")),
])
def test_the_status_line_names_the_actionable_state(status, enabled, paused, expected):
    assert menubar.remote_status_line(status, enabled=enabled, paused=paused) == expected


def test_status_details_hold_the_full_fields_and_routine_key_renewal():
    status = {**RUNNING, "queue_depth": 2, "remote_key_expires_at": SOON, "remote_key_renews": True}
    collapsed = {row["id"]: row for row in _remote(enabled=True, **status)}
    assert collapsed["remote_tasks_status"]["value"] == "Running · online · idle"
    assert collapsed["remote_tasks_details"]["value"] is False
    assert "remote_tasks_status_details" not in collapsed and "remote_tasks_key" not in collapsed
    rows = _remote(enabled=True, expanded={"remote_tasks_details"}, **status)
    ids = [row["id"] for row in rows]
    assert ids[-3:] == ["remote_tasks_details", "remote_tasks_status_details", "remote_tasks_key"]
    assert rows[-2]["label"] == menubar_display._remote_tasks_status_copy(status, enabled=True, paused=False)
    assert rows[-1]["label"] == menubar.REMOTE_KEY_RENEWS_NOTE


def test_a_key_that_needs_reconnecting_stays_visible_under_the_status_line():
    rows = _remote(enabled=True, **{**RUNNING, "remote_key_expires_at": SOON, "remote_key_renews": False})
    ids = [row["id"] for row in rows]
    assert ids.index("remote_tasks_key") == ids.index("remote_tasks_status") + 1
    assert rows[ids.index("remote_tasks_key")]["label"].startswith("Key expires in")
    assert rows[ids.index("remote_tasks_key")]["tone"] == "warning"
    later = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    (far,) = [row for row in _remote(enabled=True, **{**RUNNING, "remote_key_expires_at": later,
                                                      "remote_key_renews": False})
              if row["id"] == "remote_tasks_key"]
    assert far["label"].startswith("Key expires in") and "tone" not in far


def test_active_job_reads_none_with_stop_disabled_until_a_job_runs():
    idle = next(row for row in _remote(enabled=True, **RUNNING) if row["id"] == "remote_tasks_stop")
    assert (idle["label"], idle["detail"], idle["title"], idle["disabled"]) == ("Active job", "none", "Stop", True)
    busy = next(row for row in _remote(enabled=True, **RUNNING, active_job=JOB) if row["id"] == "remote_tasks_stop")
    assert (busy["detail"], busy["value"], busy["disabled"]) == ("waiting for approval", "job-1", False)


def _settings_app(root):
    app = extract_class(menubar.__file__, "MenuBarApp",
                        {"_worker_action", "_worker_action_worker", "_with_account_picker", "_with_folders",
                         "_drain_worker_result", "_on_setting", "_choose_task_folder", "_reload_remote_settings",
                         "_setup_changing_folders", "_remote_status"},
                        {"threading": threading})()
    app.switcher = SimpleNamespace(backup_dir=root)
    app._worker_generation = 0
    app._worker_result_lock = threading.Lock()
    app._worker_status_inflight = False
    app._worker_operation = None
    app._worker_policy = WorkerSettings()
    app._worker_status_cache = {"process_state": "unavailable"}
    app._worker_result = None
    app._panel = None
    app.picked = []
    app._choose_folder = lambda **_kwargs: app.picked.pop(0) if app.picked else None
    return app


def _settings(app, row_id, value=None):
    app._on_setting(row_id, value)
    deadline = time.monotonic() + 20
    while app._worker_operation is not None:
        assert time.monotonic() < deadline
        app._drain_worker_result()
        time.sleep(0.005)
    return app._worker_status_cache


@pytest.fixture
def settings_app(root, monkeypatch):
    monkeypatch.setattr(cli, "read_status", lambda _root: {"process_state": "stopped"})
    return _settings_app(root)


def test_settings_add_and_remove_use_the_cli_functions(root, research_home, settings_app):
    made = _folders(root, research_home, "GitHub", "Notes", saved=("GitHub",))
    settings_app.picked = [str(made["Notes"])]
    status = _settings(settings_app, "remote_tasks_folder_add")
    assert status["folders"] == [GITHUB, ("notes", "Notes", "~/Notes", "")] and "folder_notice" not in status
    status = _settings(settings_app, "remote_tasks_folder_remove", "github")
    assert status["folders"] == [("notes", "Notes", "~/Notes", "")]


def test_settings_add_of_an_unusable_folder_adds_nothing(root, research_home, settings_app):
    _folders(root, research_home, "GitHub", saved=("GitHub",))
    settings_app.picked = [str(research_home.parent / "Missing")]
    status = _settings(settings_app, "remote_tasks_folder_add")
    assert status["folder_notice"] == "⚠ ~/Missing: it doesn't exist or can't be opened."
    assert status["folders"] == [GITHUB]


def test_settings_add_of_a_folder_already_covered_says_through_which(root, research_home, settings_app):
    home = research_home.parent
    _repo(_code(home, "Code/app"))
    cli.add_work_folder(root, home / "Code")
    settings_app.picked = [str(home / "Code" / "app")]
    status = _settings(settings_app, "remote_tasks_folder_add")
    assert status["folder_notice"] == "Already allowed through ~/Code."
    assert [entry[0] for entry in status["folders"]] == ["code"]


def test_settings_remove_of_a_folder_in_use_is_refused_and_it_stays(root, research_home, settings_app, monkeypatch):
    _folders(root, research_home, "GitHub", "Notes", saved=("GitHub", "Notes"))
    monkeypatch.setattr(cli, "_folder_in_use", lambda *_args: True)
    status = _settings(settings_app, "remote_tasks_folder_remove", "github")
    assert status["folder_notice"] == f"⚠ {cli._WORKSPACE_MESSAGES['workspace_in_use']}"
    assert [entry[0] for entry in status["folders"]] == ["github", "notes"]


def test_settings_remove_of_a_folder_already_gone_refreshes_the_list(root, research_home, settings_app):
    _folders(root, research_home, "GitHub", "Notes", saved=("GitHub", "Notes"))
    cli.remove_worker_workspace(root, "notes")
    status = _settings(settings_app, "remote_tasks_folder_remove", "notes")
    assert status["folder_notice"] == GONE
    assert status["folders"] == [GITHUB]
    # The message outlives the next status read, until the next folder change.
    settings_app._worker_result = (settings_app._worker_generation, {"process_state": "stopped"}, None, None)
    settings_app._drain_worker_result()
    assert settings_app._worker_status_cache["folder_notice"] == GONE


def test_settings_cancelled_chooser_changes_nothing(root, settings_app):
    settings_app._on_setting("remote_tasks_folder_add", None)
    assert settings_app._worker_operation is None


def test_settings_folder_buttons_write_nothing_while_the_setup_window_changes_folders(root, research_home,
                                                                                      settings_app):
    _folders(root, research_home, "GitHub", "Notes", saved=("GitHub", "Notes"))
    settings_app._guided_setup = SimpleNamespace(folders_locked=True)
    settings_app.picked = [str(research_home.parent / "Notes")]
    settings_app._on_setting("remote_tasks_folder_remove", "github")
    settings_app._on_setting("remote_tasks_folder_add", None)
    assert settings_app._worker_operation is None and settings_app.picked
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github", "notes"]
    assert settings_app._remote_status()["setup_changing_folders"] is True


@pytest.mark.parametrize("row_id, value", [("remote_tasks_folder_add", None), ("remote_tasks_folder_remove", "github")])
def test_settings_folder_change_during_another_worker_change_says_try_again(root, research_home, settings_app,
                                                                            monkeypatch, row_id, value):
    made = _folders(root, research_home, "GitHub", "Notes", saved=("GitHub",))

    def busy(*_args, **_kwargs):
        raise cli.ClaudeSwitchError("worker_lifecycle_busy")

    monkeypatch.setattr(cli, "add_work_folder", busy)
    monkeypatch.setattr(cli, "remove_worker_workspace", busy)
    settings_app.picked = [str(made["Notes"])]
    assert _settings(settings_app, row_id, value)["folder_notice"] == f"⚠ {cli.BUSY_MESSAGE}"


def _poll(app, **snapshot):
    """A status read draining (``folders`` absent: the settings couldn't be read)."""
    app._worker_result = (app._worker_generation, {"process_state": "stopped", **snapshot}, None, None)
    app._drain_worker_result()
    return app._worker_status_cache


def test_a_folder_message_goes_once_a_refresh_reads_another_folder_list(root, research_home, settings_app):
    _folders(root, research_home, "GitHub", "Notes", saved=("GitHub", "Notes"))
    cli.remove_worker_workspace(root, "notes")
    status = _settings(settings_app, "remote_tasks_folder_remove", "notes")
    assert status["folder_notice"] == GONE
    assert _poll(settings_app)["folder_notice"] == GONE
    assert _poll(settings_app, folders=[GITHUB])["folder_notice"] == GONE
    status = _poll(settings_app, folders=[GITHUB, ("code", "Code", "~/Code", "")])
    assert "folder_notice" not in status


def test_a_folder_message_stays_through_an_unreadable_reread(root, research_home, settings_app, monkeypatch):
    _folders(root, research_home, "GitHub", saved=("GitHub",))
    import openswap.settings

    def unreadable(_root):
        raise OSError("settings")

    monkeypatch.setattr(openswap.settings, "load_worker_settings", unreadable)
    status = _settings(settings_app, "remote_tasks_folder_remove", "gone")
    assert status["folders"] is None and status["folder_notice"] == GONE
    status = _poll(settings_app, folders=[GITHUB])
    assert status["folder_notice"] == GONE
    assert "folder_notice" not in _poll(settings_app, folders=[])


def test_a_status_read_without_the_settings_keeps_the_last_folders(root, research_home, settings_app):
    _folders(root, research_home, "GitHub", saved=("GitHub",))
    _settings(settings_app, "remote_tasks_folder_remove", "gone")
    assert _poll(settings_app)["folders"] == [GITHUB]


# --- a folder of org folders (~/GitHub/<org>/<repo>): the window and Settings only -----------------


def _orgs(research_home, *repos):
    """``~/GitHub/<org>/<repo>`` for each ``"org/repo"``; returns ~/GitHub."""
    home = research_home.parent
    _repo(*(_code(home, f"GitHub/{repo}") for repo in repos))
    return home / "GitHub"


def _saved(root):
    return [(w.workspace_id, w.work_root.name, w.repos) for w in load_worker_settings(root).workspaces]


def test_settings_add_of_a_folder_of_org_folders_adds_each_org(root, research_home, settings_app):
    github = _orgs(research_home, "opensoft/app", "opensoft/site", "veba/api")
    (github / "empty").mkdir()
    settings_app.picked = [str(github)]
    status = _settings(settings_app, "remote_tasks_folder_add")
    assert status["folder_notice"] == "Added 2 folders from ~/GitHub."
    assert _saved(root) == [("opensoft", "opensoft", True), ("veba", "veba", True)]
    assert status["folders"] == [("opensoft", "opensoft", "~/GitHub/opensoft", ""),
                                 ("veba", "veba", "~/GitHub/veba", "")]


def test_settings_add_of_org_folders_skips_saved_ones_and_says_when_all_are(root, research_home, settings_app):
    github = _orgs(research_home, "opensoft/app", "veba/api")
    cli.add_work_folder(root, github / "opensoft")
    settings_app.picked = [str(github), str(github)]
    assert _settings(settings_app, "remote_tasks_folder_add")["folder_notice"] == "Added 1 folder from ~/GitHub."
    assert _settings(settings_app, "remote_tasks_folder_add")["folder_notice"] == "Already allowed."
    assert [entry[0] for entry in _saved(root)] == ["opensoft", "veba"]


def test_a_refused_org_keeps_the_ones_added_and_is_named(root, research_home, settings_app, monkeypatch):
    github = _orgs(research_home, "acme/app", "opensoft/app", "veba/api")
    add = cli.add_work_folder

    def full(backup_root, folder, **kwargs):
        if Path(folder).name in ("opensoft", "veba"):
            raise cli.WorkspaceError("too_many_workspaces")
        return add(backup_root, folder, **kwargs)

    monkeypatch.setattr(cli, "add_work_folder", full)
    settings_app.picked = [str(github)]
    assert _settings(settings_app, "remote_tasks_folder_add")["folder_notice"] == (
        "⚠ Added 1 folder from ~/GitHub; 2 not added: opensoft (at most 16 folders can be added), "
        "veba (at most 16 folders can be added).")
    assert [entry[0] for entry in _saved(root)] == ["acme"]


def test_org_folders_all_refused_show_the_first_refusal(root, research_home, settings_app, monkeypatch):
    github = _orgs(research_home, "opensoft/app", "veba/api")
    add = cli.add_work_folder

    def full(backup_root, folder, **kwargs):
        if Path(folder) != github:
            raise cli.WorkspaceError("too_many_workspaces")
        return add(backup_root, folder, **kwargs)

    monkeypatch.setattr(cli, "add_work_folder", full)
    settings_app.picked = [str(github)]
    assert _settings(settings_app, "remote_tasks_folder_add")["folder_notice"] == (
        "⚠ No folders added from ~/GitHub; 2 not added: opensoft (at most 16 folders can be added), "
        "veba (at most 16 folders can be added).")


def test_a_folder_whose_subfolders_hold_no_repos_is_refused_as_before(root, research_home, settings_app):
    desktop = _code(research_home.parent, "Desktop/notes")
    settings_app.picked = [str(desktop.parent)]
    status = _settings(settings_app, "remote_tasks_folder_add")
    assert status["folder_notice"] == "⚠ ~/Desktop: no git repos in it."
    assert _builtin(root)


def test_a_read_only_org_folder_becomes_a_work_folder(root, research_home, settings_app):
    github = _orgs(research_home, "opensoft/app", "veba/api")
    cli.add_readable_folder(root, github / "opensoft")
    settings_app.picked = [str(github)]
    assert _settings(settings_app, "remote_tasks_folder_add")["folder_notice"] == "Added 2 folders from ~/GitHub."
    assert _saved(root) == [("opensoft", "opensoft", True), ("veba", "veba", True)]


def test_a_folder_holding_a_repo_itself_is_a_plain_folder_of_repos(root, research_home, settings_app):
    github = _orgs(research_home, "opensoft/app", "stray")
    settings_app.picked = [str(github)]
    assert "folder_notice" not in _settings(settings_app, "remote_tasks_folder_add")
    assert _saved(root) == [("github", "GitHub", True)]


FULL = "At most 16 folders can be added."


@pytest.mark.parametrize("added, saved, refused, line", [
    (3, 0, [], "Added 3 folders from ~/GitHub."),
    (1, 2, [], "Added 1 folder from ~/GitHub."),
    (0, 2, [], "Already allowed."),
    (0, 0, [("veba", "That folder isn't yours.")], "~/GitHub/veba: that folder isn't yours."),
    (0, 0, [("veba", "That folder isn't yours."), ("acme", "No git repos in it.")],
     "No folders added from ~/GitHub; 2 not added: veba (that folder isn't yours), acme (no git repos in it)."),
    (0, 1, [("veba", FULL)], "1 already allowed from ~/GitHub; 1 not added: veba (at most 16 folders can be added)."),
    (2, 0, [("veba", FULL)], "Added 2 folders from ~/GitHub; 1 not added: veba (at most 16 folders can be added)."),
    (2, 1, [("veba", FULL)],
     "Added 2 folders from ~/GitHub; 1 already allowed; 1 not added: veba (at most 16 folders can be added)."),
    (1, 0, [(name, "That folder isn't yours.") for name in ("a", "b", "c", "d", "e")],
     "Added 1 folder from ~/GitHub; 5 not added: a (that folder isn't yours), b (that folder isn't yours), "
     "c (that folder isn't yours) and 2 more."),
])
def test_the_org_folders_line(research_home, added, saved, refused, line):
    github = research_home.parent / "GitHub"
    refused = [(github / name, why) for name, why in refused]
    assert guided_setup.repo_groups_message(github, added, saved, refused) == line


def test_a_long_org_folders_line_reaches_settings_whole(research_home):
    github = research_home.parent / "GitHub"
    reason = guided_setup.workspace_message("readable_permissions", gui=True)
    line = guided_setup.repo_groups_message(github, 9, 4, [(github / f"org-with-a-long-name-{n}", reason)
                                                            for n in range(16)])
    (hint,) = [row for row in _remote(folders=[GITHUB], folder_notice=f"⚠ {line}")
               if row["id"] == "remote_tasks_folders_note"]
    assert hint["label"] == f"⚠ {line}" and line.endswith("and 13 more.")
    shared = guided_setup.workspace_message("folder_permissions", gui=True)
    line = guided_setup.repo_groups_message(github, 0, 0, [(github / ("x" * 60 + str(n)), shared) for n in range(5)])
    (hint,) = [row for row in _remote(folders=[GITHUB], folder_notice=f"⚠ {line}")
               if row["id"] == "remote_tasks_folders_note"]
    assert hint["label"] == f"⚠ {line}" and line.endswith("and 2 more.")


def test_the_window_recommends_and_checks_a_folder_of_org_folders(root, research_home):
    _orgs(research_home, "opensoft/app")
    _repo(_code(research_home.parent, "Code/lib"))
    rows = guided_setup.folder_rows(root, load_worker_settings(root).workspaces)
    assert [(row.label, row.note, row.checked) for row in rows] == [
        ("~/GitHub", "recommended", True), ("~/Code", "", False), ("~/Code/lib", "git repo", False)]
    # The terminal's numbered menu keeps its order: ~/GitHub holds no repo itself.
    menu = guided_setup.folder_menu(root, load_worker_settings(root).workspaces)
    assert [folder.name for folder in menu.folders] == ["Code", "lib", "GitHub"] and menu.recommended is None


def test_the_window_never_recommends_a_folder_that_cannot_be_added(root, research_home):
    _code(research_home.parent, "GitHub/opensoft")
    (row,) = guided_setup.folder_rows(root, load_worker_settings(root).workspaces)
    assert (row.label, row.note) == ("~/GitHub", "")
    assert guided_setup.folder_menu(root, load_worker_settings(root).workspaces).recommended == 0


def test_the_window_hides_a_folder_of_org_folders_once_every_org_is_saved(root, research_home):
    github = _orgs(research_home, "opensoft/app", "veba/api")
    cli.add_work_folder(root, github / "opensoft")
    assert [row.label for row in guided_setup.folder_rows(root, load_worker_settings(root).workspaces)] == [
        "~/GitHub/opensoft", "~/GitHub"]
    cli.add_work_folder(root, github / "veba")
    assert [row.label for row in guided_setup.folder_rows(root, load_worker_settings(root).workspaces)] == [
        "~/GitHub/opensoft", "~/GitHub/veba"]


def test_continue_with_a_folder_of_org_folders_adds_each_org(root, research_home, enable_calls):
    _orgs(research_home, "opensoft/app", "veba/api")
    window = _drive(root, ["skip", "skip", "continue", "continue", "done"])
    assert _notes(window.shown[3]) == ["✓ Added 2 folders from ~/GitHub."]
    assert [entry[0] for entry in _saved(root)] == ["opensoft", "veba"]


def test_the_terminal_and_cli_still_refuse_a_folder_of_org_folders(root, research_home, capsys):
    github = _orgs(research_home, "opensoft/app")
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_work_folder(root, github)
    assert refused.value.code == "work_not_a_repo"
    assert cli.main(["workspace", "add", "--work", str(github)], backup_root=root) != 0
    assert cli._WORKSPACE_MESSAGES["work_not_a_repo"] in capsys.readouterr().err
    lines = []
    guided_setup._add_folders(root, SimpleNamespace(say=lines.append), [github])
    assert lines == [f"~/GitHub: {cli._WORKSPACE_MESSAGES['work_not_a_repo']}"] and _builtin(root)


# --- the built-in research folder --------------------------------------------------------------------


def test_settings_hide_the_untouched_built_in_folder(root):
    assert guided_setup.settings_folders(root, load_worker_settings(root).workspaces) == []


def test_settings_list_the_built_in_folder_kept_beside_a_real_one(root, research_home, monkeypatch):
    monkeypatch.setattr(cli, "_workspace_in_use", lambda *_args: True)
    cli.add_work_folder(root, _repo(_code(research_home.parent, "Site")))
    (research, site) = guided_setup.settings_folders(root, load_worker_settings(root).workspaces)
    assert research[:2] == ("research", "research") and research[3] == "results only"
    assert site == ("site", "Site", "~/Site", "")


def test_a_direct_folder_is_named_by_its_label_and_says_in_place(root, research_home):
    cli.add_work_folder(root, _repo(_code(research_home.parent, "Site")), mode="direct", label="Web")
    assert guided_setup.settings_folders(root, load_worker_settings(root).workspaces) == [
        ("site", "Web", "~/Site", "in place")]


# --- a control's result under the status line ------------------------------------------------------


@pytest.mark.parametrize("code, words", [
    ("pause_refused", "That change didn't go through."),
    ("worker_control_failed", "That change didn't go through."),
    ("status_read_failed", "Couldn't read the worker status after the change."),
    ("job_identity_unknown", "The running task couldn't be identified."),
    ("worker_lifecycle_busy", cli.BUSY_MESSAGE),
    ("account_not_found", "That account is gone. Pick another."),
    ("job_not_found", "Couldn't stop the task: no task has that ID."),
    ("job_still_active", "Couldn't turn off the worker: a task is still running."),
])
def test_a_control_code_reads_as_words(code, words):
    assert guided_setup.control_message(code) == words


def test_a_refused_control_shows_under_the_status_line_as_a_warning():
    rows = _remote(enabled=True, **{**RUNNING, "diagnostic_notice": "pause_refused"})
    ids = [row["id"] for row in rows]
    notice = rows[ids.index("remote_tasks_status") + 1]
    assert (notice["id"], notice["label"], notice["tone"]) == (
        "remote_tasks_notice", f"⚠ {"That change didn't go through."}", "warning")
    assert rows[ids.index("remote_tasks_status")]["value"] == "Running · online · idle"
    assert "remote_tasks_notice" not in [row["id"] for row in _remote(enabled=True, **RUNNING)]


def test_a_control_notice_outlives_refreshes_until_the_state_or_the_next_action_changes(root, settings_app,
                                                                                       monkeypatch):
    monkeypatch.setattr(cli, "request_pause", lambda *_args: {"accepted": False})
    assert _settings(settings_app, "remote_tasks_paused")["diagnostic_notice"] == "pause_refused"
    assert _poll(settings_app)["diagnostic_notice"] == "pause_refused"
    settings_app._worker_result = (settings_app._worker_generation, {"process_state": "stopped"},
                                   WorkerSettings(paused=True), None)
    settings_app._drain_worker_result()
    assert settings_app._worker_status_cache["diagnostic_notice"] is None
    _settings(settings_app, "remote_tasks_paused")
    monkeypatch.setattr(cli, "request_pause", lambda *_args: {"accepted": True})
    assert _settings(settings_app, "remote_tasks_paused")["diagnostic_notice"] is None


def test_a_status_read_failing_after_a_change_does_not_say_the_change_failed(root, settings_app, monkeypatch):
    monkeypatch.setattr(cli, "request_pause", lambda *_args: {"accepted": True})

    def unreadable(_root):
        raise OSError("status")

    monkeypatch.setattr(cli, "read_status", unreadable)
    assert _settings(settings_app, "remote_tasks_paused")["diagnostic_notice"] == "status_read_failed"
    monkeypatch.setattr(cli, "request_pause", lambda *_args: (_ for _ in ()).throw(OSError("pause")))
    assert _settings(settings_app, "remote_tasks_paused")["diagnostic_notice"] == "worker_control_failed"


def test_a_status_read_notice_goes_once_the_status_reads_again(settings_app, monkeypatch):
    monkeypatch.setattr(cli, "request_pause", lambda *_args: {"accepted": True})
    monkeypatch.setattr(cli, "read_status", lambda _root: (_ for _ in ()).throw(OSError("status")))
    _settings(settings_app, "remote_tasks_paused")
    assert _poll(settings_app, process_state="unavailable")["diagnostic_notice"] == "status_read_failed"
    assert _poll(settings_app, process_state="running")["diagnostic_notice"] is None


def test_a_new_action_clears_the_last_notice_while_it_runs(settings_app, monkeypatch):
    monkeypatch.setattr(cli, "request_pause", lambda *_args: {"accepted": False})
    assert _settings(settings_app, "remote_tasks_paused")["diagnostic_notice"] == "pause_refused"
    started = threading.Event()
    release = threading.Event()

    def waiting(*_args):
        started.set()
        release.wait(5)
        return {"accepted": True}

    monkeypatch.setattr(cli, "request_pause", waiting)
    settings_app._on_setting("remote_tasks_paused", None)
    assert started.wait(5)
    assert "diagnostic_notice" not in settings_app._worker_status_cache
    release.set()
    deadline = time.monotonic() + 20
    while settings_app._worker_operation is not None:
        assert time.monotonic() < deadline
        settings_app._drain_worker_result()
        time.sleep(0.005)
    assert settings_app._worker_status_cache["diagnostic_notice"] is None


def test_the_window_treats_a_symlinked_repo_as_no_repo_of_the_folder(root, research_home, tmp_path):
    github = _orgs(research_home, "opensoft/app")
    (github / "link").symlink_to(_repo(_code(research_home.parent, "Elsewhere/tool")))
    rows = guided_setup.folder_rows(root, load_worker_settings(root).workspaces)
    assert (rows[0].label, rows[0].note) == ("~/GitHub", "recommended")
    cli.add_work_folder(root, github / "opensoft")
    assert "~/GitHub" not in [row.label for row in guided_setup.folder_rows(root, load_worker_settings(root).workspaces)]


def test_unchecking_the_only_org_while_adding_its_parent_only_removes_it(root, research_home, enable_calls):
    github = _orgs(research_home, "opensoft/app")
    cli.add_work_folder(root, github / "opensoft")
    cli.add_work_folder(root, _repo(_code(research_home.parent, "Notes")))
    window = _drive(root, ["skip", "skip", ["toggle:0", "add_folder", "continue"], "continue", "done"],
                    chosen=[str(github)])
    assert _notes(window.shown[3]) == ["✓ Removed ~/GitHub/opensoft (its files are kept)."]
    assert [entry[0] for entry in _saved(root)] == ["notes"]
