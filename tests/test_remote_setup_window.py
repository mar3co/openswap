"""The Remote tasks setup in the setup window, and Settings › Remote › Folders.

The steps are ``guided_setup``'s (the CLI's own tests pin their terminal
text); here they run through ``setup_window.WindowPrompts`` with a recording
window, and the pages are plain data. No AppKit.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from types import SimpleNamespace

import pytest

from openswap import menubar
from openswap.settings import WorkerSettings, configure_worker_service, load_worker_settings, update_worker_settings
from openswap.worker import cli, guided_setup, setup_window
from openswap.worker.guided_setup import Readiness, SetupClosed
from openswap.worker.setup_window import WindowPrompts, done_page, window_page
from tests.menubar_harness import SETUP_METHODS, SetupWindow, extract_class, settle_setup
from tests.test_worker_accounts import ALICE, root  # noqa: F401 (fixture)
from tests.test_worker_guided_setup import URL, _code, _repo, enable_calls, research_home  # noqa: F401 (fixtures)

STEPS_BEFORE_DONE = 3


def _drive(root, answers, *, chosen=(), steps=guided_setup.run):
    """Run ``steps`` on a worker thread through the window, clicking ``answers`` page by page."""
    window = SetupWindow(answers)
    picks = list(chosen)
    ui = WindowPrompts(guided_setup.DialogPrompts(lambda **_k: 0, lambda **_k: None), window,
                       lambda: picks.pop(0) if picks else None)
    ui.settle_wait_s = 0

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
     ["Turn on Enable local worker in Settings → Remote.",
      "Pick an account under Account in Settings → Remote."]),
    (_state(worker="stopped"), setup_window.ALMOST, ("on but not running", False),
     ["Turn Enable local worker off and on in Settings → Remote to restart it."]),
    (_state(paused=True, execution="disabled"), setup_window.ALMOST, ("paused", False),
     ["Turn off Pause admission in Settings → Remote to resume."]),
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
    assert lines[1].startswith('✗ "site" is blocked. Other users can change that folder')
    assert lines[2] == setup_window.SIGN_IN_NOTE
    assert "`" not in " ".join(lines)


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


def _menu(root, window):
    app = extract_class(menubar.__file__, "MenuBarApp", SETUP_METHODS, {"threading": threading})()
    app.switcher = SimpleNamespace(backup_dir=root)
    app._worker_operation = None
    app._panel = None
    app._alert = app._prompt = app._choose_folder = app._wait_dialog = None
    app._remote_setup_window = window
    app.refreshed = 0
    app._worker_view_active = lambda: setattr(app, "refreshed", app.refreshed + 1)
    return app


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


# --- Settings › Remote › Folders ------------------------------------------------------------------


def _remote(paired=True, **status):
    return menubar.settings_page_rows(menubar.MenuBarSettings(), strategy="best", threshold=90,
                                      section=menubar.SETTINGS_SECTION_REMOTE, worker_paired=paired,
                                      worker_status=status)


def test_settings_lists_each_folder_with_remove_then_add_folder():
    rows = _remote(folders=[("github", "~/GitHub"), ("research", "~/OpenSwap Research/research (results only)")])
    ids = [row["id"] for row in rows]
    start = ids.index("remote_tasks_folders")
    assert ids[start:start + 4] == ["remote_tasks_folders", "remote_tasks_folder_remove",
                                    "remote_tasks_folder_remove", "remote_tasks_folder_add"]
    assert ids.index("remote_tasks_folder_add") < ids.index("remote_tasks_status")
    remove = rows[start + 1]
    assert (remove["label"], remove["title"], remove["value"], remove["disabled"]) == (
        "~/GitHub", "Remove", "github", False)
    assert rows[start + 3]["title"] == "Add folder…"


def test_settings_without_folders_says_tasks_need_one():
    rows = _remote(folders=[])
    (hint,) = [row for row in rows if row["id"] == "remote_tasks_folders_note"]
    assert hint["label"] == menubar.REMOTE_NO_FOLDERS
    assert "remote_tasks_folder_add" in [row["id"] for row in rows]


def test_settings_shows_no_folders_before_pairing_or_before_the_first_read():
    for rows in (_remote(paired=False, folders=[("github", "~/GitHub")]), _remote()):
        assert not any(row["id"].startswith("remote_tasks_folder") for row in rows)


def test_settings_shows_the_last_folder_message_and_disables_while_busy():
    rows = _remote(folders=[("github", "~/GitHub")], folder_notice="That folder was already removed.",
                   operation="worker_folder_update")
    (hint,) = [row for row in rows if row["id"] == "remote_tasks_folders_note"]
    assert hint["label"] == "That folder was already removed."
    assert all(row["disabled"] for row in rows if row["id"] in ("remote_tasks_folder_remove",
                                                                "remote_tasks_folder_add"))


def _settings_app(root):
    app = extract_class(menubar.__file__, "MenuBarApp",
                        {"_worker_action", "_worker_action_worker", "_with_account_picker", "_with_folders",
                         "_drain_worker_result", "_on_setting", "_choose_task_folder"},
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
    assert status["folders"] == [("github", "~/GitHub"), ("notes", "~/Notes")] and "folder_notice" not in status
    status = _settings(settings_app, "remote_tasks_folder_remove", "github")
    assert status["folders"] == [("notes", "~/Notes")]


def test_settings_add_of_an_unusable_folder_adds_nothing(root, research_home, settings_app):
    _folders(root, research_home, "GitHub", saved=("GitHub",))
    settings_app.picked = [str(research_home.parent / "Missing")]
    status = _settings(settings_app, "remote_tasks_folder_add")
    assert status["folder_notice"] == guided_setup.workspace_message("readable_unavailable", gui=True)
    assert status["folders"] == [("github", "~/GitHub")]


def test_settings_add_of_a_folder_already_covered_says_through_which(root, research_home, settings_app):
    home = research_home.parent
    _repo(_code(home, "Code/app"))
    cli.add_work_folder(root, home / "Code")
    settings_app.picked = [str(home / "Code" / "app")]
    status = _settings(settings_app, "remote_tasks_folder_add")
    assert status["folder_notice"] == "Already allowed through ~/Code."
    assert [workspace_id for workspace_id, _label in status["folders"]] == ["code"]


def test_settings_remove_of_a_folder_in_use_is_refused_and_it_stays(root, research_home, settings_app, monkeypatch):
    _folders(root, research_home, "GitHub", "Notes", saved=("GitHub", "Notes"))
    monkeypatch.setattr(cli, "_folder_in_use", lambda *_args: True)
    status = _settings(settings_app, "remote_tasks_folder_remove", "github")
    assert status["folder_notice"] == cli._WORKSPACE_MESSAGES["workspace_in_use"]
    assert [workspace_id for workspace_id, _label in status["folders"]] == ["github", "notes"]


def test_settings_remove_of_a_folder_already_gone_refreshes_the_list(root, research_home, settings_app):
    _folders(root, research_home, "GitHub", "Notes", saved=("GitHub", "Notes"))
    cli.remove_worker_workspace(root, "notes")
    status = _settings(settings_app, "remote_tasks_folder_remove", "notes")
    assert status["folder_notice"] == "That folder was already removed."
    assert status["folders"] == [("github", "~/GitHub")]
    # The message outlives the next status read, until the next folder change.
    settings_app._worker_result = (settings_app._worker_generation, {"process_state": "stopped"}, None, None)
    settings_app._drain_worker_result()
    assert settings_app._worker_status_cache["folder_notice"] == "That folder was already removed."


def test_settings_cancelled_chooser_changes_nothing(root, settings_app):
    settings_app._on_setting("remote_tasks_folder_add", None)
    assert settings_app._worker_operation is None
