"""The guided Remote tasks setup and the optional readiness report.

``openswap worker pair``/``setup`` and the menu bar's "Set up Remote tasks…"
walk the owner through the same steps: start the worker, confirm the Codex
account, choose the folders tasks may read (a GitHub folder is recommended;
results go to ``~/OpenSwap Research/<id>``), then a summary. The worker then
reports its approved folders (ID and label, never a path) and its execution
mode to the control service through the ``readiness`` extension. No Keychain,
launchctl or provider auth is touched: the home folder is the test's temp dir.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import stat
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar
from openswap.settings import (
    WorkerWorkspace,
    configure_worker_service,
    load_worker_settings,
    settings_path,
    update_worker_settings,
)
from openswap.worker import cli, guided_setup, pairing
from openswap.worker.adapter import UnavailableCodexAdapter, execution_mode, production_adapter
from openswap.worker.protocol import ProtocolError, ReportedFolder, execution_mode as wire_mode, reported_folders
from openswap.worker.refserver import ControlStore, make_server
from openswap.worker.remote import RemoteClient, Transport
from openswap.worker.runtime import WorkerRuntime
from tests.test_worker_accounts import ALICE, BOB, SECRET, root  # noqa: F401 (fixture)
from tests.test_worker_pairing_status import PairTransport, keychain  # noqa: F401 (fixture)
from tests.test_worker_remote import FakeAdapter, StoreTransport

URL = "http://127.0.0.1:8765"
OFFER = "Start the worker now? [Y/n] "
# The fixture roster has six eligible accounts: Codex 1, 2, 5, 6 and Claude 1, 4.
ACCOUNT = "Account (1-6, Enter skips): "
KEEP = "Account [1]: "
PICK = "Folders (numbers or a path) [1]: "
KEEP_FOLDERS = "Folders (Enter keeps current): "
TYPE_PATH = "Type the path to your code folder, for example ~/GitHub (Enter skips): "
SUMMARY = "Step 4 of 4 · Summary"


@pytest.fixture(autouse=True)
def research_home(tmp_path, monkeypatch):
    """The home folder (and ~/OpenSwap Research in it) is the test's temp dir, never the real home."""
    folder = tmp_path / "home" / "OpenSwap Research"
    folder.parent.mkdir()
    monkeypatch.setattr(cli, "home_folder", lambda: folder.parent)
    monkeypatch.setattr(cli, "default_research_folder", lambda: folder)
    # The summary checks whether the LaunchAgent is loaded; never ask launchctl.
    monkeypatch.setattr(cli, "_managed_worker_loaded", lambda: False)
    return folder


@pytest.fixture
def enable_calls(monkeypatch):
    """Stub `worker enable`'s function: no launchctl, no settings change."""
    calls = []
    monkeypatch.setattr(cli, "enable_worker", lambda backup_root: calls.append(backup_root) or {"enabled": True})
    return calls


def _run(root, *argv):
    return cli.main(list(argv), backup_root=root)


def _answers(monkeypatch, answers, *, interactive=True):
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: interactive)
    replies = iter(answers)

    def fake_input(prompt=""):
        print(prompt, end="")
        try:
            return next(replies)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", fake_input)


def _pair(root, monkeypatch, *, interactive, answers=()):
    monkeypatch.setattr(pairing, "Transport", lambda *_: PairTransport())
    _answers(monkeypatch, answers, interactive=interactive)
    return _run(root, "pair", "http://localhost", "one-use")


def _builtin(root):
    return cli.is_builtin_default_registry(root, load_worker_settings(root).workspaces)


def _code(home, *names, mode=0o755):
    """Folders (and ``.git`` dirs for repos) in the fake home; returns the first one."""
    made = []
    for name in names:
        folder = home / name
        folder.mkdir(parents=True, exist_ok=True)
        for path in [folder, *folder.parents]:
            if path == home:
                break
            os.chmod(path, mode)
        made.append(folder)
    return made[0]


@pytest.fixture
def github(research_home):
    """``~/GitHub`` in the fake home, as most owners have it."""
    return _code(research_home.parent, "GitHub")


# --- pair: the guided steps, in order -------------------------------------------------------


def test_pair_walks_worker_account_then_folder_then_summary(root, keychain, monkeypatch, capsys,
                                                           enable_calls, research_home, github):
    assert _pair(root, monkeypatch, interactive=True, answers=["y", "2", ""]) == 0
    out = capsys.readouterr().out
    assert enable_calls == [root]
    assert guided_setup.WORKER_ONLINE in out
    # Worker first, then the account, then the folder, then the summary.
    assert (out.index("Step 1 of 4 · Worker") < out.index(OFFER) < out.index("Step 2 of 4 · Account")
            < out.index("Tasks run on the account you pick.") < out.index("Step 3 of 4 · Folders")
            < out.index(PICK) < out.index(SUMMARY))
    # Codex and Claude accounts are both offered, numbered by menu position with
    # the slot beside them; the API-key Codex slot is not.
    assert "  • 1  Codex   alice@example.com   (work)     slot 1" in out
    assert "  • 2  Codex   bob@example.com                slot 2  out of rotation" in out
    assert "  • 6  Claude  carol@example.com   (claudey)  slot 4" in out
    assert "slot 3" not in out and "aren't supported" not in out
    assert ACCOUNT in out
    assert "✓ Codex 2 · bob@example.com" in out
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref == BOB
    # The built-in `research` folder is replaced: tasks read ~/GitHub and never
    # change it; results go to ~/OpenSwap Research/github, created owner-only.
    assert f"{guided_setup.FOLDER_USE}. Results go to ~/OpenSwap Research." in out
    assert "  • 1  ~/GitHub  (recommended)" in out
    (workspace,) = policy.workspaces
    assert workspace.workspace_id == "github" and workspace.display_label == "GitHub"
    assert workspace.readonly_roots == (github.resolve(),)
    assert workspace.output_root == (research_home / "github").resolve()
    if os.name == "posix":
        assert stat.S_IMODE(research_home.stat().st_mode) == 0o700
        assert stat.S_IMODE(workspace.output_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(github.stat().st_mode) == 0o755  # never changed
    assert "✓ ~/GitHub (github)" in out
    assert "  ✓ Folders     github (GitHub)" in out
    assert "  • Live tasks  off" in out
    assert SECRET not in out


def test_pair_on_a_tty_can_skip_every_step(root, keychain, monkeypatch, capsys, enable_calls):
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "", ""]) == 0
    out = capsys.readouterr().out
    assert f"Not started. {guided_setup.START_WORKER_NEXT}" in out
    assert f"Skipped. {guided_setup.ACCOUNT_NEXT}" in out
    assert f"No folder added. {guided_setup.FOLDER_NEXT}" in out
    assert enable_calls == []
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref is None and policy.enabled is False
    assert policy.control_service_url == "http://localhost"
    assert _builtin(root)
    # The checklist shows each gap; one Next names the first.
    assert "Next: `openswap worker enable` to start the worker." in out
    assert "  ✗ Account     none" in out and out.count("Next:") == 4


@pytest.mark.parametrize("answers", [["n"], ["no"], ["later"], []], ids=["n", "no", "other", "eof"])
def test_pair_offer_no_or_eof_leaves_the_worker_off(root, keychain, monkeypatch, capsys, enable_calls, answers):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=answers) == 0
    out = capsys.readouterr().out
    assert OFFER in out and f"Not started. {guided_setup.START_WORKER_NEXT}" in out
    assert enable_calls == [] and load_worker_settings(root).enabled is False


def test_pair_with_a_pin_keeps_it_on_enter(root, keychain, monkeypatch, capsys, enable_calls):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=["n", ""]) == 0
    out = capsys.readouterr().out
    # The current pin is marked in the menu and is the prompt's default.
    assert "  ✓ 1  Codex   alice@example.com   (work)     slot 1  current" in out
    assert KEEP in out and ACCOUNT not in out
    assert "✓ Kept Codex 1 · alice@example.com (work)" in out
    assert load_worker_settings(root).pinned_account_ref == ALICE


def test_pair_with_a_pin_can_switch_account_by_number(root, keychain, monkeypatch, capsys, enable_calls):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "2"]) == 0
    assert "✓ Codex 2 · bob@example.com" in capsys.readouterr().out
    assert load_worker_settings(root).pinned_account_ref == BOB


def test_menu_numbers_are_positions_not_slots(root, keychain, monkeypatch, capsys, enable_calls):
    # Menu 6 is Claude slot 4 (carol); menu 3 is Codex slot 5. Neither equals its slot.
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "6"]) == 0
    assert "✓ Claude 4 · carol@example.com (claudey)" in capsys.readouterr().out
    assert load_worker_settings(root).pinned_account_ref.startswith("claude:")
    assert _setup(root, monkeypatch, ["n", "3", ""]) == 0
    assert "✓ Codex 5 · shared@example.com" in capsys.readouterr().out


def test_a_non_ascii_digit_is_asked_again_not_a_crash(root, keychain, monkeypatch, capsys, enable_calls):
    # "²".isdigit() is True but int("²") raises; it must not end the account step.
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "²", "6"]) == 0
    out = capsys.readouterr().out
    assert "✓ Claude 4 · carol@example.com (claudey)" in out
    assert load_worker_settings(root).pinned_account_ref.startswith("claude:")


def test_a_number_outside_the_menu_is_asked_again(root, keychain, monkeypatch, capsys, enable_calls):
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "0", "7", "work"]) == 0
    out = capsys.readouterr().out
    assert out.count("Type a number from 1 to 6.") == 2
    # Email, alias and claude:<slot> still work beside the number.
    assert "✓ Codex 1 · alice@example.com (work)" in out
    assert load_worker_settings(root).pinned_account_ref == ALICE


def test_eof_at_the_menu_keeps_the_pin_and_says_so(root, keychain, monkeypatch, capsys, enable_calls):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=["n"]) == 0
    out = capsys.readouterr().out
    assert "Kept Codex 1 · alice@example.com (work)." in out
    assert "Skipped." not in out
    assert load_worker_settings(root).pinned_account_ref == ALICE


def test_pair_without_a_tty_prints_each_next_step(root, keychain, monkeypatch, capsys, enable_calls):
    assert _pair(root, monkeypatch, interactive=False, answers=["y", "1", "y"]) == 0  # never read
    out = capsys.readouterr().out
    assert OFFER not in out and ACCOUNT not in out and "Folders (" not in out
    for line in (guided_setup.START_WORKER_NEXT, guided_setup.ACCOUNT_NEXT, guided_setup.FOLDER_NEXT):
        assert line in out
    # The summary ends with one Next: the first gap (the worker), not the live check.
    assert out.rstrip().endswith("Next: `openswap worker enable` to start the worker.")
    assert "openswap worker workspace add --read <folder>" in out
    assert "  • Folders     none" in out
    assert enable_calls == [] and _builtin(root)
    assert load_worker_settings(root).pinned_account_ref is None


@pytest.mark.parametrize("process, expected", [
    ("running", "✓ Worker running."),
    ("stopped", "The worker is on but not running. Next: `openswap worker enable`."),
])
def test_pair_when_already_enabled_toggles_nothing(root, keychain, monkeypatch, capsys, enable_calls,
                                                   process, expected):
    cli.set_worker_account(root, "1")
    update_worker_settings(root, enabled=True)
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": True, "process_state": process})
    assert _pair(root, monkeypatch, interactive=True, answers=[]) == 0
    out = capsys.readouterr().out
    assert OFFER not in out and expected in out
    assert enable_calls == [] and load_worker_settings(root).enabled is True
    assert ("  ✓ Worker      running" if process == "running" else "  ✗ Worker      enabled but not running") in out


@pytest.mark.parametrize("error, expected", [
    (cli.ClaudeSwitchError("kickoff_in_progress"), "Could not enable worker (kickoff_in_progress)."),
    (cli.ClaudeSwitchError("worker_stop_unconfirmed"),
     "Worker is still stopping; wait for it to exit before enabling (worker_stop_unconfirmed)."),
    # Detail that is not one of enable_worker's codes (paths, launchctl text) is not echoed.
    (cli.ClaudeSwitchError("Could not write the worker LaunchAgent: /Users/someone/secret"),
     "Could not enable worker."),
    (OSError("disk"), "Could not enable worker."),
])
def test_pair_reports_a_refused_enable_and_the_manual_command(root, keychain, monkeypatch, capsys, error, expected):
    def refuse(_root):
        raise error

    monkeypatch.setattr(cli, "enable_worker", refuse)
    assert _pair(root, monkeypatch, interactive=True, answers=["y"]) == 0
    out = capsys.readouterr().out
    assert f"{expected} {guided_setup.START_WORKER_NEXT}" in out
    assert guided_setup.WORKER_ONLINE not in out and "/Users/someone/secret" not in out
    assert load_worker_settings(root).control_service_url == "http://localhost"


@pytest.mark.parametrize("step, fallback", [
    ("offer_worker", guided_setup.START_WORKER_NEXT),
    ("confirm_account", guided_setup.ACCOUNT_NEXT),
    ("choose_folders", guided_setup.FOLDER_NEXT),
])
def test_a_failing_step_prints_its_command_and_the_rest_still_run(root, keychain, monkeypatch, capsys,
                                                                  enable_calls, step, fallback):
    monkeypatch.setattr(guided_setup, step, lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _pair(root, monkeypatch, interactive=False) == 0
    out = capsys.readouterr().out
    assert fallback in out and SUMMARY in out
    assert load_worker_settings(root).control_service_url == "http://localhost"


def test_pairing_succeeds_even_if_the_whole_setup_fails(root, keychain, monkeypatch, capsys):
    monkeypatch.setattr(guided_setup, "run", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _pair(root, monkeypatch, interactive=True) == 0
    out = capsys.readouterr().out
    assert "✓ Paired this Mac (worker)." in out and "`openswap worker setup`" in out
    assert load_worker_settings(root).control_service_url == "http://localhost"


def test_summary_is_ready_only_when_admission_is_open(root, monkeypatch, capsys, enable_calls, research_home):
    cli.set_worker_account(root, "1")
    update_worker_settings(root, enabled=True, paused=True)
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": True, "process_state": "running", "remote_connectivity": "online"})
    assert _setup(root, monkeypatch, ["", "y", ""]) == 0
    out = capsys.readouterr().out
    assert "  ✗ Worker      running (paused)" in out
    assert "Next: `openswap worker pause --off` to resume." in out
    assert guided_setup.EXECUTION_OFF_NOTE not in out
    update_worker_settings(root, paused=False)
    assert _setup(root, monkeypatch, ["", ""]) == 0
    out = capsys.readouterr().out
    assert "  ✓ Worker      running\n" in out and guided_setup.EXECUTION_OFF_NOTE in out


@pytest.mark.parametrize(("process", "loaded", "worker"), [
    ("running", False, "running"),
    ("starting", False, "starting"),
    (None, True, "starting"),  # just enabled: no status written yet
    ("stopped", True, "starting"),
    ("stale", True, "stopped"),  # a crashed worker can stay loaded
    ("unavailable", True, "stopped"),
    ("stopping", True, "stopped"),
    ("stopped", False, "stopped"),
])
def test_only_a_running_worker_counts_as_ready(root, monkeypatch, process, loaded, worker):
    cli.set_worker_account(root, "1")
    update_worker_settings(root, enabled=True)
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": True, "process_state": process})
    monkeypatch.setattr(cli, "_managed_worker_loaded", lambda: loaded)
    state = guided_setup.readiness(root)
    assert state.worker == worker
    expected = {
        "running": None,
        "starting": "wait a moment, then `openswap worker status`",
        "stopped": "`openswap worker enable` to start the worker",
    }[worker]
    worker_steps = {"wait a moment, then `openswap worker status`",
                    "`openswap worker enable` to start the worker"}
    assert worker_steps & set(state.missing) == ({expected} if expected else set())


def test_summary_waits_briefly_for_a_starting_worker(root, monkeypatch, capsys, research_home):
    cli.set_worker_account(root, "1")
    cli.add_worker_workspace(root, "research", research_home, replace_builtin_default=True)
    update_worker_settings(root, enabled=True)
    states = iter(["starting", "starting", "running"])
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": True, "process_state": next(states, "running")})
    monkeypatch.setattr(guided_setup.time, "sleep", lambda _s: None)
    guided_setup.summary(root, _Say())
    out = capsys.readouterr().out
    assert "  ✓ Worker      running\n" in out and "wait a moment" not in out


def test_summary_never_calls_a_stuck_starting_worker_ready(root, monkeypatch, capsys, research_home):
    cli.set_worker_account(root, "1")
    cli.add_worker_workspace(root, "research", research_home, replace_builtin_default=True)
    update_worker_settings(root, enabled=True)
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": True, "process_state": "starting"})
    guided_setup.summary(root, _Say(),
                         start_wait_s=0)
    out = capsys.readouterr().out
    assert "  • Worker      starting" in out and guided_setup.EXECUTION_OFF_NOTE not in out
    assert "wait a moment, then `openswap worker status`" in guided_setup.readiness(root).missing


@pytest.mark.parametrize(("connection", "step"), [
    ("online", None),
    ("offline", "wait a moment, then `openswap worker status`"),
    (None, "wait a moment, then `openswap worker status`"),
    ("revoked", "`openswap worker pair <url> <code>` to pair again (this Mac was removed)"),
    ("expired", "`openswap worker pair <url> <code>` to pair again (the pairing expired)"),
])
def test_a_paired_worker_is_ready_only_while_online(root, monkeypatch, capsys, research_home, connection, step):
    configure_worker_service(root, URL, "worker-1")
    cli.set_worker_account(root, "1")
    cli.add_worker_workspace(root, "research", research_home, replace_builtin_default=True)
    update_worker_settings(root, enabled=True)
    status = {"enabled": True, "process_state": "running"}
    if connection is not None:
        status["remote_connectivity"] = connection
    monkeypatch.setattr(cli, "read_status", lambda _root: status)
    assert guided_setup.readiness(root).missing == ((step,) if step else ())
    guided_setup.summary(root, _Say(), start_wait_s=0)
    out = capsys.readouterr().out
    assert (guided_setup.EXECUTION_OFF_NOTE in out) is (step is None)


def test_summary_waits_briefly_for_the_service_connection(root, monkeypatch, capsys, research_home):
    configure_worker_service(root, URL, "worker-1")
    cli.set_worker_account(root, "1")
    cli.add_worker_workspace(root, "research", research_home, replace_builtin_default=True)
    update_worker_settings(root, enabled=True)
    links = iter(["offline", "offline", "online"])
    monkeypatch.setattr(cli, "read_status", lambda _root: {
        "enabled": True, "process_state": "running", "remote_connectivity": next(links, "online")})
    monkeypatch.setattr(guided_setup.time, "sleep", lambda _s: None)
    guided_setup.summary(root, _Say())
    out = capsys.readouterr().out
    assert f"  ✓ Paired      {URL} (online)" in out and guided_setup.EXECUTION_OFF_NOTE in out


def test_allowed_accounts_without_a_pinned_default_are_not_ready(root, monkeypatch):
    cli.allow_worker_account(root, "1")
    cli.set_worker_account(root, None)
    assert guided_setup.readiness(root).account is None
    assert "`openswap worker account <slot>` to pick an account" in \
        guided_setup.readiness(root).missing


class _Say:
    def say(self, text):
        print(text)


def test_a_failing_pin_keeps_the_setup_going(root, keychain, monkeypatch, capsys, enable_calls, github):
    monkeypatch.setattr(cli, "set_worker_account", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "1", ""]) == 0
    out = capsys.readouterr().out
    assert "Could not pick that account" in out
    assert load_worker_settings(root).workspaces[0].workspace_id == "github" and not _builtin(root)


# --- the folder step ------------------------------------------------------------------------


def _setup(root, monkeypatch, answers):
    configure_worker_service(root, URL, "worker-1")
    _answers(monkeypatch, answers)
    return _run(root, "setup")


def test_setup_needs_a_pairing(root, capsys):
    assert _run(root, "setup") == 1
    assert "`openswap worker pair <url> <code>`" in capsys.readouterr().err


def test_setup_reruns_the_steps_on_a_paired_mac(root, monkeypatch, capsys, enable_calls, research_home, github):
    cli.set_worker_account(root, "1")
    assert _setup(root, monkeypatch, ["y", "", ""]) == 0
    out = capsys.readouterr().out
    assert enable_calls == [root] and PICK in out
    (workspace,) = load_worker_settings(root).workspaces
    assert workspace.workspace_id == "github"
    # Run again: ~/GitHub is readable now (✓); Enter keeps the current folders.
    assert _setup(root, monkeypatch, ["n", "", ""]) == 0
    out = capsys.readouterr().out
    assert "  ✓ 1  ~/GitHub  (recommended, added as github)" in out
    assert KEEP_FOLDERS in out and PICK not in out
    assert "Kept the current folders." in out
    assert load_worker_settings(root).workspaces == (workspace,)
    # Choosing it again changes nothing either.
    assert _setup(root, monkeypatch, ["n", "", "1"]) == 0
    assert "✓ ~/GitHub (github, already added)" in capsys.readouterr().out
    assert load_worker_settings(root).workspaces == (workspace,)


def test_setup_without_a_tty_lists_the_readable_folders(root, monkeypatch, capsys, enable_calls, github):
    cli.add_readable_folder(root, github)
    configure_worker_service(root, URL, "worker-1")
    _answers(monkeypatch, [], interactive=False)
    assert _run(root, "setup") == 0
    out = capsys.readouterr().out
    assert "Folders:" in out
    assert '  ✓ 1  github  "GitHub"  ~/GitHub' in out
    assert "  ✓ Folders     github (GitHub)" in out


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_folder_step_refuses_a_results_folder_others_can_read(root, monkeypatch, capsys, enable_calls,
                                                              research_home, github):
    results = research_home / "github"
    results.mkdir(parents=True)
    os.chmod(results, 0o755)
    assert _setup(root, monkeypatch, ["n", "", ""]) == 0
    assert "chmod 700" in capsys.readouterr().out
    assert _builtin(root)
    assert stat.S_IMODE(results.stat().st_mode) == 0o755  # never changed for the owner


# --- finding the folders ----------------------------------------------------------------------


def _menu_paths(root):
    return [guided_setup._display_path(folder) for folder in cli.detect_code_folders(root)]


@pytest.mark.skipif(os.name != "posix", reason="symlinks and POSIX permission bits")
def test_detection_lists_a_github_folder_first_with_its_few_repos(root, research_home):
    home = research_home.parent
    _code(home, "Developer", "Projects", "GitHub/openswap/.git", "GitHub/opentag/.git", "GitHub/notes",
          "Documents/GitHub")
    for number in range(4):  # more than a few repos: Projects is offered only as a whole
        _code(home, f"Projects/repo{number}/.git")
    (home / "Code").write_text("not a folder")
    (home / "src").symlink_to(home / "GitHub")  # the same folder twice is listed once
    _code(home, "dev", mode=0o777)  # others can change it: refused
    _code(home, "repos/.hidden/.git")
    assert _menu_paths(root) == ["~/GitHub", "~/GitHub/openswap", "~/GitHub/opentag", "~/Documents/GitHub",
                                 "~/Developer", "~/Projects", "~/repos"]
    menu = guided_setup.folder_menu(root, load_worker_settings(root).workspaces)
    assert menu.recommended == 0
    assert menu.lines[:3] == ("  • 1  ~/GitHub            (recommended)",
                              "  • 2  ~/GitHub/openswap   (git repo)",
                              "  • 3  ~/GitHub/opentag    (git repo)")
    assert "recommended" not in "".join(menu.lines[1:])


@pytest.mark.skipif(os.name != "posix", reason="symlinks and POSIX permission bits")
def test_a_folder_named_github_anywhere_is_recommended(root, research_home):
    home = research_home.parent
    _code(home, "Code", "work/github")
    (home / "src").symlink_to(home / "work" / "github")  # found after ~/Code, listed before it
    assert _menu_paths(root) == ["~/work/github", "~/Code"]
    assert guided_setup.folder_menu(root, ()).recommended == 0


def test_without_a_github_folder_the_first_other_one_is_the_default(root, monkeypatch, capsys, enable_calls,
                                                                     research_home):
    _code(research_home.parent, "Projects", "src")
    assert _setup(root, monkeypatch, ["n", "", ""]) == 0
    out = capsys.readouterr().out
    assert "  • 1  ~/Projects\n  • 2  ~/src\n" in out and "recommended" not in out
    assert PICK in out
    (workspace,) = load_worker_settings(root).workspaces
    assert (workspace.workspace_id, workspace.display_label) == ("projects", "Projects")


def test_with_nothing_found_the_step_asks_for_a_path(root, monkeypatch, capsys, enable_calls, tmp_path):
    work = _code(tmp_path, "My Code.v2")
    assert _setup(root, monkeypatch, ["n", "", "1", str(work)]) == 0
    out = capsys.readouterr().out
    assert TYPE_PATH in out and "Type a folder path." in out
    (workspace,) = load_worker_settings(root).workspaces
    assert workspace.workspace_id == "my-code-v2" and workspace.display_label == "My Code.v2"
    assert workspace.readonly_roots == (work.resolve(),)


def test_enter_with_nothing_found_keeps_the_builtin_results_folder(root, monkeypatch, capsys, enable_calls):
    assert _setup(root, monkeypatch, ["n", "", ""]) == 0
    out = capsys.readouterr().out
    assert TYPE_PATH in out and "No folder added." in out
    assert "`openswap worker workspace add --read <folder>`" in out
    assert _builtin(root)
    assert "  • Folders     none" in out


def test_several_numbers_make_one_workspace_each(root, monkeypatch, capsys, enable_calls, research_home):
    home = research_home.parent
    _code(home, "GitHub/site.io/.git", "GitHub/api/.git", "Projects")
    assert _setup(root, monkeypatch, ["n", "", "9", "3, 2 3"]) == 0
    out = capsys.readouterr().out
    assert "Type numbers from 1 to 4, or a folder path." in out
    workspaces = load_worker_settings(root).workspaces
    assert [(w.workspace_id, w.display_label) for w in workspaces] == [("site-io", "site.io"), ("api", "api")]
    assert [w.output_root for w in workspaces] == [(research_home / "site-io").resolve(),
                                                   (research_home / "api").resolve()]
    assert "  ✓ Folders     site-io (site.io), api (api)" in out


@pytest.mark.parametrize(("answer", "expected"), [
    ("1 3", [0, 2]), ("1,3", [0, 2]), (" 3, 1 ,3 ", [2, 0]), ("4", [3]),
    ("0", None), ("5", None), ("", None), ("1 x", "1 x"),
    ("²", "²"),  # not an ASCII number: read as a path, never int()
    ("~/My Code", "~/My Code"), ("/a/My\\ Code", "/a/My Code"), ("'/a/b c'", "/a/b c"), ("Bob's", "Bob's"),
    ("'/x/Bob'\\''s'", "/x/Bob's"), ('"/x/a b"', "/x/a b"),
])
def test_parse_folder_choice(answer, expected):
    if os.name == "nt" and "\\" in answer:
        pytest.skip("a backslash is the Windows path separator")
    assert guided_setup.parse_folder_choice(answer, 4) == expected


# --- what may be read, and the workspace each folder becomes ----------------------------------


def _refusal(root, folder):
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_readable_folder(root, folder)
    return refused.value.code


def test_refused_folders_change_nothing(root, research_home, tmp_path):
    home = research_home.parent
    _code(home, "Library/Code", ".config/repo", "Shared", "open")
    os.chmod(home / "Shared", 0o777)
    (home / "notes.txt").write_text("x")
    research_home.mkdir()
    (root / "worker").mkdir(mode=0o700, exist_ok=True)
    assert _refusal(root, home) == "readable_home"
    assert _refusal(root, tmp_path) == "readable_home"  # contains the home folder
    assert _refusal(root, home / "Library" / "Code") == "readable_private"
    assert _refusal(root, home / ".config" / "repo") == "readable_private"
    assert _refusal(root, root) == "readable_exposes_credentials"
    assert _refusal(root, root / "worker") == "readable_exposes_credentials"
    assert _refusal(root, research_home) == "readable_results"
    assert _refusal(root, home / "missing") == "readable_unavailable"
    assert _refusal(root, home / "notes.txt") == "readable_unsafe"
    if os.name == "posix":
        assert _refusal(root, "/") == "readable_system"
        assert _refusal(root, "/usr/bin") == "readable_system"
        assert _refusal(root, home / "Shared") == "readable_permissions"
    assert _builtin(root)
    for code in ("readable_home", "readable_system", "readable_private", "readable_exposes_credentials",
                 "readable_results", "readable_unavailable", "readable_unsafe", "readable_permissions",
                 "readable_not_owned"):
        assert code in cli._WORKSPACE_MESSAGES


def test_a_credential_home_inside_a_folder_is_refused(root, research_home, monkeypatch):
    home = research_home.parent
    code = _code(home, "GitHub")
    codex = _code(home, "GitHub/.codex-home")
    from openswap.codex import auth

    monkeypatch.setattr(auth, "codex_home", lambda: codex)
    assert _refusal(root, code) == "readable_exposes_credentials"
    assert _menu_paths(root) == []


def test_the_step_says_why_a_folder_is_refused_and_asks_again(root, monkeypatch, capsys, enable_calls,
                                                              research_home, github):
    assert _setup(root, monkeypatch, ["n", "", "~", "~/GitHub"]) == 0
    out = capsys.readouterr().out
    assert f"~: {cli._WORKSPACE_MESSAGES['readable_home']}" in out
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github"]


def test_the_first_folder_replaces_the_builtin_one_only_when_unused(root, research_home):
    from openswap.worker.journal import LocalJobStore
    from tests.test_worker_core import _submission

    home = research_home.parent
    first, second = _code(home, "GitHub"), _code(home, "Projects")
    store = LocalJobStore(root)
    store.create(_submission(), owner_ref="local-user", worker_epoch=store.current_epoch())
    # A job may still run or upload in the built-in folder: it stays beside the new one.
    result = cli.add_readable_folder(root, first)
    assert result.added and result.kept_builtin
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["research", "github"]
    result = cli.add_readable_folder(root, second)
    assert result.added and not result.kept_builtin
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["research", "github", "projects"]


def test_the_step_says_when_the_builtin_folder_stays(root, monkeypatch, capsys, enable_calls, github):
    from openswap.worker.journal import LocalJobStore
    from tests.test_worker_core import _submission

    store = LocalJobStore(root)
    store.create(_submission(), owner_ref="local-user", worker_epoch=store.current_epoch())
    assert _setup(root, monkeypatch, ["n", "", ""]) == 0
    out = capsys.readouterr().out
    assert '"research" stays until its running task ends.' in out
    assert "  ✓ Folders     github (GitHub)" in out


def test_folder_ids_never_collide(root, research_home, tmp_path):
    home = research_home.parent
    cli.add_worker_workspace(root, "notes", tmp_path / "results-only")
    one, two = _code(home, "a/Notes"), _code(home, "b/notes")
    first, second = cli.add_readable_folder(root, one), cli.add_readable_folder(root, two)
    assert first.workspace.workspace_id == "notes-2" and first.workspace.display_label == "Notes"
    assert second.workspace.workspace_id == "notes-3" and second.workspace.display_label == "notes"
    assert first.workspace.output_root == (research_home / "notes-2").resolve()
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["research", "notes", "notes-2",
                                                                               "notes-3"]


def test_a_readable_folder_never_overlaps_its_results(root, research_home):
    workspace = cli.add_readable_folder(root, _code(research_home.parent, "GitHub")).workspace
    (source,) = workspace.readonly_roots
    assert not workspace.output_root.is_relative_to(source) and not source.is_relative_to(workspace.output_root)


def test_apostrophes_in_an_existing_path_are_never_shell_syntax(root, monkeypatch, capsys, enable_calls,
                                                                 research_home, tmp_path):
    home = research_home.parent
    oneil = _code(tmp_path, "O'Neil's")
    moms = _code(home, "Kid's Stuff/Mom's")
    assert guided_setup.parse_folder_choice(str(oneil), 0) == str(oneil)
    assert guided_setup.parse_folder_choice("~/Kid's Stuff/Mom's", 0) == "~/Kid's Stuff/Mom's"
    # Not there as typed: an apostrophe that does not start the text is still literal.
    assert guided_setup.parse_folder_choice("/x/O'Neil's", 0) == "/x/O'Neil's"
    # A quoted name is shell syntax, and its own apostrophe survives.
    assert guided_setup.parse_folder_choice('"/x/Kid\'s Stuff"', 0) == "/x/Kid's Stuff"
    assert guided_setup.parse_folder_choice(f'"{oneil}"', 0) == str(oneil)
    assert _setup(root, monkeypatch, ["n", "", "~/Kid's Stuff/Mom's"]) == 0
    (workspace,) = load_worker_settings(root).workspaces
    assert workspace.readonly_roots == (moms.resolve(),) and workspace.display_label == "Mom's"
    assert workspace.workspace_id == "mom-s"


def test_a_doubled_slash_after_the_tilde_stays_in_home(root, research_home):
    code = _code(research_home.parent, "Code")
    assert cli.add_readable_folder(root, "~//Code").workspace.readonly_roots == (code.resolve(),)


# --- one folder, two names --------------------------------------------------------------------


@pytest.fixture
def case_insensitive(tmp_path):
    from tests.test_pathid import case_insensitive as probe

    if not probe(tmp_path):
        pytest.skip("the temp filesystem is case-sensitive")


def test_case_variants_never_dodge_a_refusal(root, research_home, case_insensitive):
    home = research_home.parent
    _code(home, "Library/Application Support", "GitHub")
    research_home.mkdir()
    assert _refusal(root, home / "library") == "readable_private"
    assert _refusal(root, home / "LIBRARY" / "Application Support") == "readable_private"
    assert _refusal(root, home.parent / "HOME") == "readable_home"
    assert _refusal(root, home / "openswap research") == "readable_results"
    assert _refusal(root, root.parent / "BACKUP") == "readable_exposes_credentials"
    assert _builtin(root)
    # The same folder in another case is the same workspace, not `github-2`.
    first = cli.add_readable_folder(root, home / "GitHub")
    again = cli.add_readable_folder(root, home / "github")
    assert again.added is False and again.workspace == first.workspace
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github"]


def test_a_case_variant_is_stored_as_spelled_on_disk(root, research_home, case_insensitive):
    github = _code(research_home.parent, "GitHub")
    workspace = cli.add_readable_folder(root, research_home.parent / "GITHUB").workspace
    assert workspace.readonly_roots == (github.resolve(),) and workspace.display_label == "GitHub"
    assert workspace.workspace_id == "github"


def test_a_credential_home_in_another_case_is_refused(root, research_home, monkeypatch, case_insensitive):
    from openswap.codex import auth

    work = _code(research_home.parent, "Work/.codex")
    monkeypatch.setattr(auth, "codex_home", lambda: work)
    assert _refusal(root, research_home.parent / "WORK") == "readable_exposes_credentials"


def test_launch_refuses_a_stored_source_the_policy_forbids(root, research_home):
    """A source saved before the policy (or edited into settings) never reaches the sandbox."""
    from openswap.settings import configure_worker_local_policy
    from openswap.worker.runtime import WorkerRuntime as Runtime

    library = _code(research_home.parent, "Library/Code")
    out = research_home / "lib"
    configure_worker_local_policy(root, pinned_account_ref=None,
                                  workspaces=(WorkerWorkspace("lib", out, (library,)),))
    with pytest.raises(ValueError, match="not allowed"):
        Runtime(root, adapter=FakeAdapter())._resolve_workspace("lib", "a" * 32)


def test_launch_refuses_a_case_variant_source(root, research_home, case_insensitive):
    from openswap.settings import configure_worker_local_policy
    from openswap.worker.runtime import WorkerRuntime as Runtime

    _code(research_home.parent, "Library/Code")
    configure_worker_local_policy(
        root, pinned_account_ref=None,
        workspaces=(WorkerWorkspace("lib", research_home / "lib", (research_home.parent / "LIBRARY",)),))
    with pytest.raises(ValueError, match="not allowed"):
        Runtime(root, adapter=FakeAdapter())._resolve_workspace("lib", "a" * 32)


# --- cloud drives in ~/Library -----------------------------------------------------------------


def test_cloud_drives_are_readable_but_not_the_rest_of_library(root, research_home):
    home = research_home.parent
    _code(home, "Library/CloudStorage/Dropbox/Work", "Library/CloudStorage/GoogleDrive-a@b.c/My Drive",
          "Library/CloudStorage/.hidden", "Library/Mobile Documents/com~apple~CloudDocs/Notes",
          "Library/Mobile Documents/iCloud~com~example~app", "Library/Application Support")
    cloud = home / "Library" / "CloudStorage"
    icloud = home / "Library" / "Mobile Documents"
    for allowed in (cloud / "Dropbox", cloud / "Dropbox" / "Work", cloud / "GoogleDrive-a@b.c" / "My Drive",
                    icloud / "com~apple~CloudDocs", icloud / "com~apple~CloudDocs" / "Notes"):
        assert cli.readable_folder_problem(root, allowed.resolve()) is None, allowed
    for refused in (home / "Library", cloud, cloud / ".hidden", icloud, icloud / "iCloud~com~example~app",
                    home / "Library" / "Application Support"):
        assert _refusal(root, refused) == "readable_private", refused
    workspace = cli.add_readable_folder(root, cloud / "Dropbox" / "Work").workspace
    assert (workspace.workspace_id, workspace.display_label) == ("work", "Work")


def test_a_case_variant_never_widens_the_cloud_exemption(root, research_home, case_insensitive):
    home = research_home.parent
    _code(home, "Library/CloudStorage/Dropbox/Work", "Library/Mobile Documents/com~apple~CloudDocs",
          "Library/Mobile Documents/other")
    assert cli.readable_folder_problem(root, home / "library" / "cloudstorage" / "DROPBOX" / "work") is None
    assert _refusal(root, home / "LIBRARY" / "CLOUDSTORAGE") == "readable_private"
    assert _refusal(root, home / "library" / "mobile documents") == "readable_private"
    assert _refusal(root, home / "Library" / "MOBILE DOCUMENTS" / "OTHER") == "readable_private"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("base", ["Library/CloudStorage", "Library/Mobile Documents",
                                  "Library/Mobile Documents/com~apple~CloudDocs"])
def test_a_cloud_base_symlinked_to_library_opens_nothing(root, research_home, base):
    """A cloud base that is a symlink to ~/Library must not turn ~/Library/Keychains readable."""
    home = research_home.parent
    library = home / "Library"
    _code(home, "Library/Keychains", "Library/com~apple~CloudDocs/Keychains")
    link = home / base
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(library, target_is_directory=True)
    for folder in (link / "Keychains", link / "com~apple~CloudDocs" / "Keychains"):
        if folder.exists():
            assert _refusal(root, folder) == "readable_private", folder
    # Handed the uncanonical path, the check still refuses a symlinked base.
    assert not cli._in_cloud_drive(link / "Dropbox" / "x", library)
    assert not cli._in_cloud_drive(library / "Mobile Documents" / "com~apple~CloudDocs" / "x", library)


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_cloud_base_in_another_case_opens_nothing(root, research_home, case_insensitive):
    home = research_home.parent
    library = _code(home, "Library/Keychains").parent
    (library / "CloudStorage").symlink_to(library, target_is_directory=True)
    assert _refusal(root, home / "LIBRARY" / "cloudstorage" / "KEYCHAINS") == "readable_private"
    assert _refusal(root, home / "library" / "CLOUDSTORAGE" / "keychains") == "readable_private"


def test_a_cloud_folder_approved_earlier_still_launches(root, research_home):
    from openswap.settings import configure_worker_local_policy
    from openswap.worker.runtime import WorkerRuntime as Runtime

    work = _code(research_home.parent, "Library/CloudStorage/Dropbox/Work")
    configure_worker_local_policy(root, pinned_account_ref=None,
                                  workspaces=(WorkerWorkspace("work", research_home / "work", (work,)),))
    resolved = Runtime(root, adapter=FakeAdapter())._resolve_workspace("work", "a" * 32)
    assert resolved.readonly_sources == (work.resolve(),)


# --- reading and writing never meet across workspaces -----------------------------------------


def _overlapping(root, research_home):
    """Settings main allowed: `a` writes inside ~/GitHub while `b` reads ~/GitHub."""
    from openswap.settings import configure_worker_local_policy

    github = _code(research_home.parent, "GitHub")
    configure_worker_local_policy(root, pinned_account_ref=None, workspaces=(
        WorkerWorkspace("a", github / "out", ()), WorkerWorkspace("b", research_home / "b", (github,))))
    return github


def test_overlapping_workspaces_still_load_but_never_launch(root, research_home):
    from openswap.worker.runtime import WorkerRuntime as Runtime, WorkspaceRefused

    github = _overlapping(root, research_home)
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["a", "b"]  # listable, fixable
    runtime = Runtime(root, adapter=FakeAdapter())
    for workspace_id, code in (("a", "folder_overlaps_readable"), ("b", "readonly_source_overlaps_results")):
        with pytest.raises(WorkspaceRefused) as refused:
            runtime._resolve_workspace(workspace_id, "a" * 32)
        assert refused.value.code == code
    # Refused before any folder is made.
    assert not (github / "out").exists() and not (research_home / "b").exists()
    assert cli.refused_workspaces(root) == [("a", "folder_overlaps_readable"),
                                            ("b", "readonly_source_overlaps_results")]


def test_status_and_summary_name_the_refused_workspaces(root, research_home, monkeypatch, capsys, enable_calls):
    _overlapping(root, research_home)
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": False})
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert ('✗ "a" is blocked (folder_overlaps_readable). '
            + cli._WORKSPACE_MESSAGES["folder_overlaps_readable"]) in out
    assert '"b" is blocked (readonly_source_overlaps_results)' in out
    assert "Next: fix or remove those workspaces" in out
    assert _run(root, "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["refused_workspaces"] == [
        {"workspace_id": "a", "diagnostic_code": "folder_overlaps_readable"},
        {"workspace_id": "b", "diagnostic_code": "readonly_source_overlaps_results"}]
    assert str(research_home) not in out
    assert _setup(root, monkeypatch, ["n", "", ""]) == 0
    out = capsys.readouterr().out
    assert "  ✗ Folders     b (b)" in out
    assert '"a" is blocked' in out
    assert '`openswap worker workspace list` to fix "a", "b"' in guided_setup.readiness(root).missing
    assert guided_setup.EXECUTION_OFF_NOTE not in out


def test_status_without_refusals_adds_nothing(root, monkeypatch, capsys):
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": False})
    assert _run(root, "status", "--json") == 0
    assert "refused_workspaces" not in json.loads(capsys.readouterr().out)


def test_a_folder_holding_another_workspaces_results_is_refused(root, research_home, tmp_path):
    home = research_home.parent
    docs = _code(home, "Documents")
    cli.add_worker_workspace(root, "notes", docs / "Research")
    assert _refusal(root, docs) == "readonly_source_overlaps_results"
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["research", "notes"]


def test_results_inside_a_readable_folder_are_refused(root, research_home):
    github = _code(research_home.parent, "GitHub")
    cli.add_readable_folder(root, github)
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_worker_workspace(root, "out", github / "out")
    assert refused.value.code == "folder_overlaps_readable"
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_worker_workspace(root, "up", research_home.parent)
    assert refused.value.code in {"folder_exposes_credentials", "folder_overlaps_readable"}
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github"]
    for code in ("readonly_source_overlaps_results", "folder_overlaps_readable"):
        assert code in cli._WORKSPACE_MESSAGES


def test_a_positional_read_only_source_follows_the_folder_policy(root, research_home, tmp_path, capsys):
    home = research_home.parent
    ssh = _code(home, ".ssh")
    library = _code(home, "Library/Mail")
    out = tmp_path / "out"
    for source, code in ((ssh, "readable_private"), (library, "readable_private"),
                         (home, "readable_home"),
                         (root, "readonly_source_exposes_credentials")):
        assert _run(root, "workspace", "add", "x", str(out), "--readonly-source", str(source), "--json") == 1
        assert json.loads(capsys.readouterr().out)["diagnostic_code"] == code
    assert _builtin(root)
    code = _code(home, "GitHub")
    assert _run(root, "workspace", "add", "x", str(out), "--readonly-source", str(code)) == 0


# --- `workspace add --read` -------------------------------------------------------------------


def test_workspace_add_read_makes_the_same_workspace_as_the_setup(root, capsys, research_home, github):
    assert _run(root, "workspace", "add", "--read", str(github), "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["accepted"] is True and payload["added"] is True
    assert payload["workspace"] == {
        "workspace_id": "github", "label": "GitHub", "output_root": str((research_home / "github").resolve()),
        "readonly_roots": [str(github.resolve())],
    }
    assert _run(root, "workspace", "add", "--read", str(github)) == 0
    assert "is already readable as workspace 'github'." in capsys.readouterr().out
    assert len(load_worker_settings(root).workspaces) == 1
    assert _run(root, "workspace", "add", "--read", str(research_home.parent), "--json") == 1
    assert json.loads(capsys.readouterr().out) == {"accepted": False, "diagnostic_code": "readable_home"}


def test_workspace_add_read_and_the_positional_form_are_exclusive(root, capsys, tmp_path, github):
    assert _run(root, "workspace", "add", "docs", str(tmp_path / "docs"), "--read", str(github)) == 2
    assert "not both" in capsys.readouterr().err
    assert _run(root, "workspace", "add", "docs") == 2
    assert "`--read DIR`" in capsys.readouterr().err
    assert _builtin(root)
    # The positional form is unchanged: the folder is where results are written.
    assert _run(root, "workspace", "add", "docs", str(tmp_path / "docs")) == 0
    assert "Approved research folder" in capsys.readouterr().out


def test_the_default_replaces_only_the_builtin_folder_and_not_while_in_use(root, monkeypatch, research_home):
    from openswap.worker.journal import LocalJobStore
    from tests.test_worker_core import _submission

    store = LocalJobStore(root)
    store.create(_submission(), owner_ref="local-user", worker_epoch=store.current_epoch())
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_worker_workspace(root, "research", research_home, replace_builtin_default=True)
    assert refused.value.code == "workspace_in_use" and _builtin(root)


def test_the_default_is_added_beside_explicit_folders(root, tmp_path, research_home):
    cli.add_worker_workspace(root, "alt", tmp_path / "alt")
    with pytest.raises(cli.WorkspaceError) as refused:
        cli.add_worker_workspace(root, "research", research_home, replace_builtin_default=True)
    assert refused.value.code == "workspace_exists"  # the built-in folder is no longer alone


# --- folder labels ----------------------------------------------------------------------------


def test_workspace_labels_add_rename_reset_and_list(root, tmp_path, capsys):
    assert _run(root, "workspace", "add", "docs", str(tmp_path / "docs"), "--label", "Team docs") == 0
    assert '"Team docs"' in capsys.readouterr().out
    saved = json.loads(settings_path(root).read_text())["worker"]["workspaces"]["docs"]
    assert saved["label"] == "Team docs"
    assert _run(root, "workspace", "label", "docs", "Shared research", "--json") == 0
    assert json.loads(capsys.readouterr().out)["workspace"]["label"] == "Shared research"
    assert _run(root, "workspace", "label", "docs", "--reset") == 0
    capsys.readouterr()
    assert "label" not in json.loads(settings_path(root).read_text())["worker"]["workspaces"]["docs"]
    assert _run(root, "workspace", "list") == 0
    out = capsys.readouterr().out
    assert '  ✓ docs      "docs"      ' in out and '  ✓ research  "research"  ' in out


@pytest.mark.parametrize("argv, code", [
    (("workspace", "add", "x", "{tmp}/x", "--label", ""), "label_invalid"),
    (("workspace", "add", "x", "{tmp}/x", "--label", "l" * 101), "label_invalid"),
    (("workspace", "add", "x", "{tmp}/x", "--label", "tab\there"), "label_invalid"),
    (("workspace", "add", "x", "{tmp}/x", "--label", "next\u0085line"), "label_invalid"),
    (("workspace", "label", "nope", "Label"), "workspace_not_found"),
    (("workspace", "label", "research", "del\u007f"), "label_invalid"),
])
def test_workspace_label_refusals(root, tmp_path, capsys, argv, code):
    before = load_worker_settings(root).workspaces
    assert _run(root, *[a.format(tmp=tmp_path) for a in argv], "--json") == 1
    assert json.loads(capsys.readouterr().out)["diagnostic_code"] == code
    assert load_worker_settings(root).workspaces == before


def test_workspace_label_needs_exactly_one_of_text_or_reset(root, capsys):
    assert _run(root, "workspace", "label", "research") == 2
    assert _run(root, "workspace", "label", "research", "X", "--reset") == 2


def test_an_invalid_stored_label_fails_closed(root, tmp_path):
    cli.add_worker_workspace(root, "docs", tmp_path / "docs", label="Docs")
    update_worker_settings(root, enabled=True)
    raw = json.loads(settings_path(root).read_text())
    raw["worker"]["workspaces"]["docs"]["label"] = "bad\nlabel"
    settings_path(root).write_text(json.dumps(raw))
    policy = load_worker_settings(root)
    assert policy.enabled is False and [w.workspace_id for w in policy.workspaces] == ["research"]


def test_display_label_defaults_to_the_folder_name():
    assert WorkerWorkspace("a", Path("/x/My Folder")).display_label == "My Folder"
    assert WorkerWorkspace("a", Path("/x/My Folder"), (), "Chosen").display_label == "Chosen"
    assert WorkerWorkspace("a", Path("/x/" + "n" * 150)).display_label == "n" * 100
    assert WorkerWorkspace("a", Path("/")).display_label == "a"


# --- the execution-mode hook ---------------------------------------------------------------------


def test_execution_mode_is_disabled_until_a_live_adapter_declares_itself(tmp_path):
    # The production adapter on a Mac is the live Codex adapter, which reports
    # "disabled" until the owner's opt-in is recorded.
    assert execution_mode(production_adapter(tmp_path)) == "disabled"
    assert execution_mode(UnavailableCodexAdapter()) == "disabled"
    assert execution_mode(FakeAdapter()) == "disabled"  # a test adapter is not live execution
    assert execution_mode(SimpleNamespace(execution_mode="live")) == "live"
    assert execution_mode(SimpleNamespace(execution_mode="LIVE")) == "disabled"


# --- wire validation ---------------------------------------------------------------------------


def _folders(*pairs):
    return [{"id": i, "label": label} for i, label in pairs]


def test_reported_folders_accept_bounded_unique_entries():
    assert reported_folders([]) == ()
    assert reported_folders(_folders(("research", "OpenSwap Research"), ("Team.Docs_2-x", "😀" * 100))) == (
        ReportedFolder("research", "OpenSwap Research"), ReportedFolder("Team.Docs_2-x", "😀" * 100),
    )
    assert len(reported_folders(_folders(*[(f"f{n}", "L") for n in range(20)]))) == 20
    assert wire_mode("disabled") == "disabled" and wire_mode("live") == "live"


@pytest.mark.parametrize("value", [
    None, {}, "[]", _folders(*[(f"f{n}", "L") for n in range(21)]),
    _folders(("a", "A"), ("a", "B")), [{"id": "a"}], [{"id": "a", "label": "A", "path": "/x"}], ["a"],
    _folders(("a/b", "A")), _folders(("a b", "A")), _folders(("", "A")), _folders(("x" * 201, "A")),
    [{"id": 7, "label": "A"}], _folders(("a", "")), _folders(("a", "l" * 101)), _folders(("a", "line\nbreak")),
    _folders(("a", "tab\t")), _folders(("a", "del\u007f")), _folders(("a", "next\u0085line")),
    [{"id": "a", "label": 5}],
])
def test_reported_folders_refuse_malformed_entries(value):
    with pytest.raises(ProtocolError) as refused:
        reported_folders(value)
    assert refused.value.code == "invalid_request"


@pytest.mark.parametrize("value", ["Live", "enabled", "", None, True, 1])
def test_execution_mode_on_the_wire_is_closed(value):
    with pytest.raises(ProtocolError):
        wire_mode(value)


# --- reference server ----------------------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(tmp_path / "service" / "db", clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    key = paired["device_key"]
    epoch = store.request("register", {}, key)["worker_epoch"]
    return store, paired["worker_id"], key, epoch


def _report(folders=(("research", "OpenSwap Research"),), execution="disabled", **extra):
    return {"folders": _folders(*folders), "execution": execution, **extra}


def test_refserver_stores_replaces_and_clears_the_report(service):
    store, worker_id, key, epoch = service
    assert store.readiness(worker_id) is None
    assert store.request("readiness", {"worker_epoch": epoch, **_report()}, key) == {"folder_count": 1}
    assert store.readiness(worker_id) == {"folders": [{"id": "research", "label": "OpenSwap Research"}],
                                          "execution": "disabled"}
    two = (("docs", "Docs"), ("research", "R"))
    assert store.request("readiness", {"worker_epoch": epoch, **_report(two, "live")}, key) == {"folder_count": 2}
    assert store.readiness(worker_id) == {"folders": _folders(*two), "execution": "live"}
    assert store.request("readiness", {"worker_epoch": epoch, **_report(())}, key) == {"folder_count": 0}
    assert store.readiness(worker_id) == {"folders": [], "execution": "disabled"}  # empty, not unreported
    # Refusals change nothing.
    for bad in ({"worker_epoch": epoch, **_report(execution="Live")}, {"worker_epoch": epoch, "folders": []},
                {"worker_epoch": epoch, **_report(), "extra": 1}, {"worker_epoch": epoch, **_report((("a/b", "A"),))}):
        with pytest.raises(ProtocolError) as refused:
            store.request("readiness", bad, key)
        assert refused.value.code == "invalid_request"
    with pytest.raises(ProtocolError) as stale:
        store.request("readiness", {"worker_epoch": epoch + 1, **_report()}, key)
    assert stale.value.code == "stale_epoch"
    assert store.readiness(worker_id) == {"folders": [], "execution": "disabled"}
    # A new registration clears it; the old registration can no longer report.
    new_epoch = store.request("register", {}, key)["worker_epoch"]
    assert store.readiness(worker_id) is None
    with pytest.raises(ProtocolError):
        store.request("readiness", {"worker_epoch": epoch, **_report()}, key)
    store.request("readiness", {"worker_epoch": new_epoch, **_report()}, key)
    store.revoke(worker_id)
    assert store.readiness(worker_id) is None


# --- client: sending the report ------------------------------------------------------------------


class _NoReadinessTransport(StoreTransport):
    code = "unsupported_version"

    def request(self, operation, data):
        if operation == "readiness":
            self.calls.append(operation)
            raise ProtocolError(self.code, 404)
        return super().request(operation, data)


@pytest.fixture
def reporting(root, tmp_path):
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(tmp_path / "service" / "db", clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    update_worker_settings(root, enabled=True)
    configure_worker_service(root, URL)
    cli.set_worker_account(root, "1")
    runtime = WorkerRuntime(root, adapter=FakeAdapter())
    transport = StoreTransport(store, paired["device_key"])
    remote = RemoteClient(runtime, URL, paired["device_key"], worker_id=paired["worker_id"], transport=transport)
    remote.tick()
    return remote, runtime, store, paired, transport


def _sent(transport):
    return [data for op, data in transport.requests if op == "readiness"]


def test_registration_reports_folder_ids_labels_and_mode_only(root, reporting):
    remote, _, store, paired, transport = reporting
    (body,) = _sent(transport)
    assert body == {"worker_epoch": remote.worker_epoch, "folders": [{"id": "research", "label": "research"}],
                    "execution": "disabled"}
    assert str(root) not in json.dumps(body) and "/" not in json.dumps(body)
    assert store.readiness(paired["worker_id"]) == {"folders": body["folders"], "execution": "disabled"}


def test_a_refused_workspace_is_not_advertised_until_it_is_fixed(root, reporting, research_home):
    from openswap.settings import configure_worker_local_policy

    remote, _, store, paired, transport = reporting
    github = _code(research_home.parent, "GitHub")
    pinned = load_worker_settings(root).pinned_account_ref
    good = WorkerWorkspace("good", research_home / "good", (github,), "GitHub")
    # `bad` writes inside the folder `good` reads: every job in either is refused.
    bad = WorkerWorkspace("bad", github / "out", ())
    configure_worker_local_policy(root, pinned_account_ref=pinned, workspaces=(good, bad))
    transport.requests.clear()
    remote.tick()
    (body,) = _sent(transport)
    assert body["folders"] == []
    # Fixed: `bad` now writes elsewhere, and both are offered again.
    configure_worker_local_policy(root, pinned_account_ref=pinned,
                                  workspaces=(good, WorkerWorkspace("bad", research_home / "bad", ())))
    transport.requests.clear()
    remote.tick()
    (body,) = _sent(transport)
    assert body["folders"] == [{"id": "good", "label": "GitHub"}, {"id": "bad", "label": "bad"}]
    assert store.readiness(paired["worker_id"])["folders"] == body["folders"]


def test_a_failing_refusal_check_leaves_out_only_that_workspace(root, reporting, research_home, monkeypatch):
    from openswap.settings import configure_worker_local_policy

    remote, _, _store, _paired, transport = reporting
    pinned = load_worker_settings(root).pinned_account_ref
    configure_worker_local_policy(root, pinned_account_ref=pinned, workspaces=(
        WorkerWorkspace("one", research_home / "one", ()), WorkerWorkspace("two", research_home / "two", ())))
    real = cli.workspace_refusal

    def flaky(backup_root, workspace, workspaces):
        if workspace.workspace_id == "one":
            raise OSError("unreadable")
        return real(backup_root, workspace, workspaces)

    monkeypatch.setattr(cli, "workspace_refusal", flaky)
    transport.requests.clear()
    remote.tick()
    (body,) = _sent(transport)
    assert body["folders"] == [{"id": "two", "label": "two"}]


def test_the_report_follows_the_readable_folder_ids(root, reporting, research_home):
    remote, _, store, paired, transport = reporting
    cli.add_readable_folder(root, _code(research_home.parent, "GitHub"))
    transport.requests.clear()
    remote.tick()
    (body,) = _sent(transport)
    assert body["folders"] == [{"id": "github", "label": "GitHub"}]
    assert "/" not in json.dumps(body["folders"])
    assert store.readiness(paired["worker_id"])["folders"] == [{"id": "github", "label": "GitHub"}]
    state = guided_setup.readiness(root)
    assert state.folders == state.readable == ("github (GitHub)",)


def test_the_report_follows_job_sync_and_a_stalled_route_never_delays_pickup(root, reporting, tmp_path, monkeypatch):
    remote, runtime, store, paired, transport = reporting
    cli.add_worker_workspace(root, "docs", tmp_path / "docs", label="Docs")
    transport.requests.clear()
    remote.tick()
    ops = [op for op, _data in transport.requests]
    assert "poll" in ops and "readiness" in ops and ops.index("poll") < ops.index("readiness")
    # A stalled readiness route times out after the claim pass, not before it.
    real = transport.request

    def stalling(op, data):
        if op == "readiness":
            raise ProtocolError("service_unavailable", 503)
        return real(op, data)

    monkeypatch.setattr(transport, "request", stalling)
    cli.label_worker_workspace(root, "docs", "Documents")
    transport.requests.clear()
    remote.tick()
    assert remote.state == "online" and "poll" in [op for op, _data in transport.requests]


def test_report_is_resent_only_on_change_and_after_registration(root, reporting, tmp_path, monkeypatch):
    remote, runtime, store, paired, transport = reporting
    for _ in range(3):
        remote.tick()
    assert len(_sent(transport)) == 1
    cli.add_worker_workspace(root, "docs", tmp_path / "docs", label="Docs")
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 2
    assert _sent(transport)[-1]["folders"] == [{"id": "research", "label": "research"},
                                              {"id": "docs", "label": "Docs"}]
    cli.label_worker_workspace(root, "docs", "Team docs")
    remote.tick()
    assert _sent(transport)[-1]["folders"][1]["label"] == "Team docs"
    # The execution mode comes from the one hook; a live adapter is reported as such.
    runtime.adapter.execution_mode = "live"
    remote.tick()
    assert len(_sent(transport)) == 4 and _sent(transport)[-1]["execution"] == "live"
    store.request("register", {}, paired["device_key"])
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 5 and _sent(transport)[-1]["worker_epoch"] == remote.worker_epoch
    assert store.readiness(paired["worker_id"])["execution"] == "live"


@pytest.mark.parametrize("code", ["unsupported_version", "not_found"])
def test_backend_without_the_extension_is_left_alone_until_reregistration(root, reporting, code):
    remote, runtime, store, paired, _ = reporting
    transport = _NoReadinessTransport(store, paired["device_key"])
    transport.code = code
    client = RemoteClient(runtime, URL, paired["device_key"], worker_id="other-binding", transport=transport)
    client.tick()
    client.tick()
    cli.label_worker_workspace(root, "research", "Changed")
    client.tick()
    assert transport.calls.count("readiness") == 1 and client.state == "online"
    assert "poll" in transport.calls  # claims carry on
    store.request("register", {}, paired["device_key"])
    client.tick()
    client.tick()
    assert transport.calls.count("readiness") == 2


def test_failed_or_miscounted_reports_are_retried_without_blocking(root, reporting):
    remote, _, _, _, transport = reporting
    cli.label_worker_workspace(root, "research", "Retry")
    transport.reject["readiness"] = ProtocolError("service_unavailable", 503)
    remote.tick()
    assert remote.state == "online" and "poll" in transport.calls
    remote.tick()
    assert _sent(transport)[-1]["folders"][0]["label"] == "Retry"
    sent = len(_sent(transport))
    remote.tick()
    assert len(_sent(transport)) == sent  # acknowledged: nothing more
    original = transport.request
    transport.request = lambda op, data: {"folder_count": 9} if op == "readiness" and original(op, data) else \
        original(op, data)
    cli.label_worker_workspace(root, "research", "Again")
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == sent + 2  # never accepted as acknowledged


def test_revocation_seen_while_reporting_is_final(root, reporting):
    remote, _, _, _, transport = reporting
    cli.label_worker_workspace(root, "research", "X")
    transport.reject["readiness"] = ProtocolError("revoked", 403)
    remote.sync_tick()
    assert remote.state == "revoked"


# --- end to end over loopback --------------------------------------------------------------------


def test_loopback_pair_setup_and_report(root, keychain, monkeypatch, capsys, research_home,  # noqa: F811
                                        github):
    store = ControlStore(root.parent / "service" / "db")
    monkeypatch.setattr(cli, "enable_worker", lambda _root: update_worker_settings(_root, enabled=True))
    with make_server(store, port=0) as server:
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            _answers(monkeypatch, ["y", "1", ""])
            assert _run(root, "pair", url, store.issue_code()) == 0
            from openswap.worker.pairing import load_enrollment

            enrollment = load_enrollment(url)
            runtime = WorkerRuntime(root, adapter=FakeAdapter())
            remote = RemoteClient(runtime, url, enrollment.device_key, worker_id=enrollment.worker_id)
            remote.tick()
            assert remote.state == "online"
            assert store.readiness(enrollment.worker_id) == {
                "folders": [{"id": "github", "label": "GitHub"}], "execution": "disabled",
            }
            # Over HTTP the same closed shape is enforced.
            with pytest.raises(ProtocolError) as refused:
                Transport(url, enrollment.device_key).request(
                    "readiness", {"worker_epoch": remote.worker_epoch, **_report(execution="on")})
            assert refused.value.code == "invalid_request"
        finally:
            server.shutdown()
            serving.join(20)
    assert not serving.is_alive()


# --- menu bar --------------------------------------------------------------------------------------


def _general_rows(**kwargs):
    return menubar.settings_page_rows(
        menubar.MenuBarSettings(), strategy="best", threshold=90,
        section=menubar.SETTINGS_SECTION_GENERAL, **kwargs,
    )


def test_menu_has_a_set_up_button_first_in_remote_tasks():
    rows = _general_rows()
    ids = [row["id"] for row in rows]
    assert ids.index("group_remote_tasks") + 1 == ids.index("remote_tasks_setup")
    row = rows[ids.index("remote_tasks_setup")]
    assert row["kind"] == "button" and row["label"] == "Set up Remote tasks…" and row["disabled"] is False
    busy = {r["id"]: r for r in _general_rows(worker_status={"operation": "worker_account_update"})}
    assert busy["remote_tasks_setup"]["disabled"] is True


class _Dialogs:
    """Scripted modal dialogs: answers are clicked button numbers or (clicked, text)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.shown = []

    def alert(self, **kwargs):
        self.shown.append(("alert", kwargs))
        return self.answers.pop(0) if self.answers else 0

    def prompt(self, **kwargs):
        self.shown.append(("prompt", kwargs))
        clicked, text = self.answers.pop(0) if self.answers else (0, "")
        return SimpleNamespace(clicked=clicked, text=text)

    def choose_folder(self, **kwargs):
        self.shown.append(("choose_folder", kwargs))
        clicked, text = self.answers.pop(0) if self.answers else (0, "")
        return text if clicked == 1 else None


def _menu(root, dialogs):
    from tests.menubar_harness import extract_class

    app_type = extract_class(menubar.__file__, "MenuBarApp",
                             {"_run_guided_setup", "_on_setting", "_drain_guided_setup"},
                             {"threading": threading})
    app = app_type()
    app.switcher = SimpleNamespace(backup_dir=root)
    app._worker_operation = None
    app._panel = None
    app._alert, app._prompt = dialogs.alert, dialogs.prompt
    app._choose_folder = dialogs.choose_folder
    app.refreshed = 0
    app._worker_view_active = lambda: setattr(app, "refreshed", app.refreshed + 1)
    return app


def _settle(app):
    """What on_sync_tick does: serve each dialog the setup thread asks for until it ends."""
    deadline = time.monotonic() + 20
    while getattr(app, "_guided_setup", None) is not None:
        assert time.monotonic() < deadline, "guided setup did not finish"
        app._drain_guided_setup()
        time.sleep(0.005)


def test_menu_setup_pairs_from_the_pasted_command_then_runs_the_same_steps(root, keychain, monkeypatch,
                                                                          enable_calls, research_home, github):
    monkeypatch.setattr(pairing, "Transport", lambda *_: PairTransport())
    dialogs = _Dialogs([
        (1, "not a command"),
        (1, "openswap worker pair http://localhost one-use"),
        1,              # start the worker
        (1, "1"),       # account
        (1, "1"),       # folders tasks may read: ~/GitHub
        1,              # Done
    ])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert enable_calls == [root]
    policy = load_worker_settings(root)
    assert policy.control_service_url == "http://localhost" and policy.pinned_account_ref == ALICE
    (workspace,) = policy.workspaces
    assert workspace.workspace_id == "github" and workspace.readonly_roots == (github.resolve(),)
    assert workspace.output_root == (research_home / "github").resolve()
    messages = [kwargs["message"] for _kind, kwargs in dialogs.shown]
    # The numbered list and the question share one dialog; Enter's default is prefilled.
    folders = dialogs.shown[4][1]
    assert "Step 3 of 4 · Folders" in folders["message"]
    assert f"{guided_setup.FOLDER_USE}. Results go to ~/OpenSwap Research." in folders["message"]
    assert "  • 1  ~/GitHub  (recommended)" in folders["message"]
    assert folders["message"].endswith("Folders (numbers or a path)")
    assert folders["default_text"] == "1" and folders["ok"] == "Continue"
    assert "That is not a pairing command" in messages[1]
    assert "✓ Paired this Mac (worker)." in messages[2] and "Start the worker now?" in messages[2]
    assert dialogs.shown[-1][1]["ok"] == "Done" and SUMMARY in messages[-1]
    # The dialogs carry the same step headers and plain menu rows: no ANSI codes.
    assert "Step 1 of 4 · Worker" in messages[2] and "Step 2 of 4 · Account" in messages[3]
    assert "  • 1  Codex   alice@example.com" in messages[3] and "\x1b[" not in "".join(messages)
    assert app.refreshed == 1


def test_menu_setup_cancelled_at_pairing_does_nothing(root, keychain):
    dialogs = _Dialogs([(0, "")])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert len(dialogs.shown) == 1 and load_worker_settings(root).control_service_url is None


def test_menu_setup_on_a_paired_mac_skips_pairing(root, enable_calls):
    configure_worker_service(root, URL, "worker-1")
    dialogs = _Dialogs([0, (0, ""), (0, ""), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert "Start the worker now?" in dialogs.shown[0][1]["message"]
    assert "Type the path to your code folder, for example ~/GitHub" in dialogs.shown[2][1]["message"]
    assert "No folder added." in dialogs.shown[3][1]["message"]
    assert enable_calls == [] and _builtin(root)


def test_menu_setup_reads_several_folders_by_number(root, enable_calls, research_home):
    configure_worker_service(root, URL, "worker-1")
    _code(research_home.parent, "GitHub", "Projects")
    dialogs = _Dialogs([0, (0, ""), (1, "1, 2"), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github", "projects"]
    assert "Folders     github (GitHub), projects (Projects)" in dialogs.shown[-1][1]["message"]


def test_menu_setup_reports_a_refused_code(root, keychain, monkeypatch):
    class Refusing:
        def request(self, *_):
            raise ProtocolError("invalid_code")

    monkeypatch.setattr(pairing, "Transport", lambda *_: Refusing())
    dialogs = _Dialogs([(1, "http://localhost used"), (0, ""), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert "Could not pair (invalid_code)." in dialogs.shown[1][1]["message"]
    assert load_worker_settings(root).control_service_url is None


def test_menu_setup_runs_every_step_off_the_ui_thread(root, keychain, monkeypatch):
    threads = []

    class Recording(PairTransport):
        def request(self, *args, **kwargs):
            threads.append(("pair", threading.current_thread()))
            return super().request(*args, **kwargs)

    monkeypatch.setattr(pairing, "Transport", lambda *_: Recording())
    monkeypatch.setattr(cli, "enable_worker",
                        lambda backup_root: threads.append(("enable", threading.current_thread())) or {"enabled": True})
    dialogs = _Dialogs([(1, "openswap worker pair http://localhost one-use"), 1, (0, ""), 0, (0, ""), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    assert app._guided_setup is not None and app.refreshed == 0  # the UI thread returned at once
    _settle(app)
    assert [name for name, _thread in threads] == ["pair", "enable"]
    assert all(thread is not threading.current_thread() for _name, thread in threads)
    assert load_worker_settings(root).control_service_url == "http://localhost" and app.refreshed == 1


@pytest.mark.parametrize("text, expected", [
    ("openswap worker pair https://opentag.me ABC-123", ("https://opentag.me", "ABC-123")),
    ("  https://opentag.me   ABC  ", ("https://opentag.me", "ABC")),
    ("http://localhost:8765 code", ("http://localhost:8765", "code")),
    ("openswap worker pair https://opentag.me", None),
    ("ftp://x code", None),
    ("openswap worker pair 'unterminated", None),
    ("", None),
])
def test_parse_pairing_command(text, expected):
    assert guided_setup.parse_pairing_command(text) == expected



def test_the_summary_reports_live_execution_from_the_adapter(root, monkeypatch, capsys, research_home):
    from openswap.worker import adapter

    monkeypatch.setattr(adapter, "production_adapter",
                        lambda backup_root=None: SimpleNamespace(execution_mode="live"))
    assert guided_setup.readiness(root).execution == "live"



def test_setup_pins_a_claude_account_and_points_at_the_claude_live_check(root, keychain, monkeypatch, capsys,
                                                                       enable_calls, research_home):
    assert _pair(root, monkeypatch, interactive=True, answers=["y", "claude:4", "y", ""]) == 0
    out = capsys.readouterr().out
    assert "✓ Claude 4 · carol@example.com (claudey)" in out
    assert load_worker_settings(root).pinned_account_ref.startswith("claude:")
    assert "  ✓ Account     Claude 4 · carol@example.com (claudey)" in out
    # Once nothing else is missing, the one Next is the Claude live check.
    update_worker_settings(root, enabled=True)
    monkeypatch.setattr(cli, "read_status", lambda _root: {
        "enabled": True, "process_state": "running", "remote_connectivity": "online"})
    guided_setup.summary(root, _Say(), start_wait_s=0)
    out = capsys.readouterr().out
    assert out.rstrip().endswith(guided_setup.CLAUDE_EXECUTION_OFF_NOTE)
    assert guided_setup.EXECUTION_OFF_NOTE not in out
    for command in ("openswap worker claude pin", "openswap worker claude prepare",
                    "openswap worker live-check --provider claude"):
        assert command in out


def test_a_claude_pin_with_a_passing_claude_check_reports_live(root, monkeypatch):
    from openswap.worker import adapter

    cli.set_worker_account(root, "claude:4")
    picked = []

    def claude_adapter(backup_root=None):
        picked.append("claude")
        return SimpleNamespace(execution_mode="live")

    monkeypatch.setattr(adapter, "production_claude_adapter", claude_adapter)
    monkeypatch.setattr(adapter, "production_adapter",
                        lambda backup_root=None: SimpleNamespace(execution_mode="disabled"))
    state = guided_setup.readiness(root)
    assert state.execution == "live" and state.provider == "claude" and picked == ["claude"]


def test_menu_setup_approves_a_folder_from_the_native_chooser(root, enable_calls, tmp_path):
    """With no code folder found, the native chooser picks the folder tasks may read."""
    configure_worker_service(root, URL, "worker-1")
    notes = _code(tmp_path, "Notes")
    dialogs = _Dialogs([0, (0, ""), (1, str(notes)), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    kinds = [kind for kind, _kwargs in dialogs.shown]
    assert kinds.count("choose_folder") == 1
    chooser = dialogs.shown[kinds.index("choose_folder")][1]
    assert chooser["title"] == "Set up Remote tasks"
    assert "Type the path to your code folder, for example ~/GitHub" in chooser["message"]
    (workspace,) = load_worker_settings(root).workspaces
    assert workspace.workspace_id == "notes" and workspace.readonly_roots == (notes.resolve(),)


def test_with_folders_found_the_menu_bar_asks_for_numbers_not_the_chooser(root, enable_calls, research_home):
    configure_worker_service(root, URL, "worker-1")
    _code(research_home.parent, "GitHub")
    dialogs = _Dialogs([0, (0, ""), (1, "1"), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert "choose_folder" not in [kind for kind, _kwargs in dialogs.shown]
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github"]


def test_dialog_prompts_type_the_folder_without_a_chooser():
    dialogs = _Dialogs([(1, "/tmp/x")])
    ui = guided_setup.DialogPrompts(dialogs.alert, dialogs.prompt)
    assert ui.choose_folder("Folder?") == "/tmp/x"
    assert dialogs.shown[0][0] == "prompt"


# --- the terminal folder search ------------------------------------------------------------------


class _Searching(guided_setup.TerminalPrompts):
    """A terminal whose folder search is scripted: each answer is what the picker returns."""

    def __init__(self, answers):
        super().__init__(interactive=True, read_line=lambda prompt: pytest.fail(f"asked {prompt!r}"))
        self.answers, self.calls = list(answers), []

    def can_search_folders(self):
        return True

    def search_folders(self, question, *, pinned=(), highlight=None):
        self.calls.append((question, list(pinned), highlight))
        return self.answers.pop(0) if self.answers else ""


def test_the_terminal_folder_step_searches_then_offers_another(root, research_home, capsys):
    home = research_home.parent
    github = _code(home, "GitHub/openswap/.git", "GitHub/opentag/.git").parents[1]
    ui = _Searching([str(github), "9", "2 3", ""])
    guided_setup.choose_folders(root, ui)
    out = capsys.readouterr().out
    # No numbered list or plain question: the picker shows the suggestions.
    assert "  • 1  ~/GitHub" not in out and "Folders (" not in out
    (question, pinned, highlight), *later = ui.calls
    assert question == "Folders" and highlight == 0
    assert [(guided_setup._display_path(s.path), s.note, s.checked) for s in pinned] == [
        ("~/GitHub", "(recommended)", False), ("~/GitHub/openswap", "(git repo)", False),
        ("~/GitHub/opentag", "(git repo)", False)]
    # After a pick the same search opens again, nothing highlighted, the pick ticked.
    assert all(call[0] == "Add another (Enter to finish)" and call[2] is None for call in later)
    assert [s.checked for s in later[0][1]] == [True, False, False]
    assert "✓ ~/GitHub (github)" in out and "Type numbers from 1 to 3, or a folder path." in out
    assert "✓ ~/GitHub/openswap (openswap)" in out and "✓ ~/GitHub/opentag (opentag)" in out
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github", "openswap", "opentag"]


def test_the_terminal_folder_search_refuses_like_any_answer(root, research_home, capsys):
    home = research_home.parent
    _code(home, "GitHub")
    ui = _Searching([str(home), "~/GitHub", ""])
    guided_setup.choose_folders(root, ui)
    out = capsys.readouterr().out
    assert f"~: {cli._WORKSPACE_MESSAGES['readable_home']}" in out
    assert [w.workspace_id for w in load_worker_settings(root).workspaces] == ["github"]
    assert len(ui.calls) == 3 and ui.calls[1][0] == "Folders"  # a refusal asks the same question again


def test_escape_at_the_first_search_adds_nothing(root, research_home, capsys):
    _code(research_home.parent, "GitHub")
    ui = _Searching([""])
    guided_setup.choose_folders(root, ui)
    assert f"No folder added. {guided_setup.FOLDER_NEXT}" in capsys.readouterr().out
    assert _builtin(root)


def test_with_folders_already_added_nothing_is_highlighted(root, research_home, capsys, github):
    cli.add_readable_folder(root, github)
    ui = _Searching([""])
    guided_setup.choose_folders(root, ui)
    assert ui.calls[0][2] is None and ui.calls[0][1][0].checked
    assert "Kept the current folders." in capsys.readouterr().out


def test_the_live_search_runs_only_on_a_real_terminal(monkeypatch):
    from openswap import folder_picker

    assert not guided_setup.TerminalPrompts(interactive=True, read_line=lambda _p: "").can_search_folders()
    monkeypatch.setattr(folder_picker, "_tty_available", lambda: False)
    assert not guided_setup.TerminalPrompts(interactive=True).can_search_folders()  # piped, or Windows
    monkeypatch.setattr(folder_picker, "_tty_available", lambda: True)
    ui = guided_setup.TerminalPrompts(interactive=True)
    assert ui.can_search_folders()
    seen = []
    monkeypatch.setattr(folder_picker, "pick_folder", lambda question, **kw: seen.append((question, kw)) or "")
    monkeypatch.setattr(folder_picker.FolderIndex, "start", lambda self: self)
    assert ui.search_folders("Folders", pinned=["p"], highlight=0) == ""
    ui.search_folders("Add another (Enter to finish)")
    (first, kw1), (second, kw2) = seen
    assert first == "Folders: " and kw1["pinned"] == ["p"] and kw1["highlight"] == 0
    assert second == "Add another (Enter to finish): " and kw2["index"] is kw1["index"]  # one scan per run


def test_the_menu_bar_keeps_the_numbered_dialog():
    for front_end in (guided_setup.DialogPrompts, guided_setup.ThreadedPrompts):
        assert not hasattr(front_end, "search_folders") and not hasattr(front_end, "can_search_folders")
