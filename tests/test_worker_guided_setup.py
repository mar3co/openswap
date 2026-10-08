"""The guided Remote tasks setup and the optional readiness report.

``openswap worker pair``/``setup`` and the menu bar's "Set up Remote tasks…"
walk the owner through the same steps: start the worker, confirm the Codex
account, approve a research folder (``~/OpenSwap Research`` as ``research``
by default), then a summary. The worker then reports its approved folders
(ID and label, never a path) and its execution mode to the control service
through the ``readiness`` extension. No Keychain, launchctl or provider auth
is touched: the research folder default points into the test's temp dir.
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
OFFER = "Start the Remote tasks worker now so this Mac can accept approved tasks? [Y/n] "
KEEP = "Keep this account? [Y/n] "
ACCOUNT = "Account (slot, email or alias; Enter to skip): "
ANOTHER = "Another folder to approve (path; Enter to finish): "


@pytest.fixture(autouse=True)
def research_home(tmp_path, monkeypatch):
    """The default folder lives in the test's temp dir, never the real home."""
    folder = tmp_path / "home" / "OpenSwap Research"
    folder.parent.mkdir()
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


# --- pair: the guided steps, in order -------------------------------------------------------


def test_pair_walks_worker_account_then_folder_then_summary(root, keychain, monkeypatch, capsys,
                                                           enable_calls, research_home):
    assert _pair(root, monkeypatch, interactive=True, answers=["y", "2", "y", ""]) == 0
    out = capsys.readouterr().out
    assert enable_calls == [root]
    assert guided_setup.WORKER_ONLINE in out
    # Worker first, then the account, then the folder, then the summary.
    assert (out.index(OFFER) < out.index("Choose the account remote jobs run on") < out.index("Create and approve")
            < out.index("Remote tasks setup:"))
    # Codex and Claude accounts are both offered; the API-key Codex slot is not.
    assert "  1 · alice@example.com (work)  (Codex)" in out and "  3 · " not in out
    assert "  claude:4 · carol@example.com (claudey)  (Claude)" in out
    assert "aren't supported" not in out
    assert "Pinned Codex account 2 · bob@example.com" in out
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref == BOB
    (workspace,) = policy.workspaces
    assert workspace.workspace_id == "research" and workspace.output_root == research_home.resolve()
    assert workspace.display_label == "OpenSwap Research"
    if os.name == "posix":
        assert stat.S_IMODE(research_home.stat().st_mode) == 0o700
    assert '(the portal shows "OpenSwap Research")' in out
    assert "  Research folders: research (OpenSwap Research)" in out
    assert "  Execution: disabled" in out and guided_setup.EXECUTION_OFF_NOTE in out
    assert SECRET not in out


def test_pair_on_a_tty_can_skip_every_step(root, keychain, monkeypatch, capsys, enable_calls):
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "", "n", ""]) == 0
    out = capsys.readouterr().out
    assert "Not started. Start it later with `openswap worker enable`." in out
    assert "Skipped. Pin one later" in out
    assert guided_setup.FOLDER_NEXT in out
    assert enable_calls == []
    policy = load_worker_settings(root)
    assert policy.pinned_account_ref is None and policy.enabled is False
    assert policy.control_service_url == "http://localhost"
    assert _builtin(root)
    assert "Before Slack can start tasks on this Mac: start the worker" in out
    assert "pin an account" in out


@pytest.mark.parametrize("answers", [["n"], ["no"], ["later"], []], ids=["n", "no", "other", "eof"])
def test_pair_offer_no_or_eof_leaves_the_worker_off(root, keychain, monkeypatch, capsys, enable_calls, answers):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=answers) == 0
    out = capsys.readouterr().out
    assert OFFER in out and "Not started. Start it later with `openswap worker enable`." in out
    assert enable_calls == [] and load_worker_settings(root).enabled is False


def test_pair_with_a_pin_asks_to_keep_it(root, keychain, monkeypatch, capsys, enable_calls):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=["n", ""]) == 0
    out = capsys.readouterr().out
    assert "Remote tasks uses Codex account 1 · alice@example.com (work)." in out
    assert KEEP in out and ACCOUNT not in out
    assert load_worker_settings(root).pinned_account_ref == ALICE


def test_pair_with_a_pin_can_switch_account(root, keychain, monkeypatch, capsys, enable_calls):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "n", "2"]) == 0
    assert "Pinned Codex account 2 · bob@example.com" in capsys.readouterr().out
    assert load_worker_settings(root).pinned_account_ref == BOB


def test_declining_the_pin_then_skipping_keeps_it_and_says_so(root, keychain, monkeypatch, capsys, enable_calls):
    cli.set_worker_account(root, "1")
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "n", ""]) == 0
    out = capsys.readouterr().out
    assert ("Skipped. 1 · alice@example.com (work) stays selected. Change it later with "
            "`openswap worker account <slot|email|alias>`.") in out
    assert "Pin one later" not in out
    assert load_worker_settings(root).pinned_account_ref == ALICE


def test_pair_without_a_tty_prints_each_next_step(root, keychain, monkeypatch, capsys, enable_calls):
    assert _pair(root, monkeypatch, interactive=False, answers=["y", "1", "y"]) == 0  # never read
    out = capsys.readouterr().out
    assert OFFER not in out and ACCOUNT not in out and "Create and approve" not in out
    for line in (guided_setup.START_WORKER_NEXT, guided_setup.ACCOUNT_NEXT, guided_setup.FOLDER_NEXT,
                 guided_setup.EXECUTION_OFF_NOTE):
        assert line in out
    assert "openswap worker workspace add <id> <folder>" in out
    assert enable_calls == [] and _builtin(root)
    assert load_worker_settings(root).pinned_account_ref is None


@pytest.mark.parametrize("process, expected", [
    ("running", "The Remote tasks worker is already running on this Mac."),
    ("stopped", "The Remote tasks worker is enabled but not running. Run `openswap worker enable` "
                "to start it again, or `openswap worker status` to check."),
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
    assert f"  Worker: {'running' if process == 'running' else 'enabled but not running'}" in out


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
    assert expected in out and "Start it later with `openswap worker enable`." in out
    assert guided_setup.WORKER_ONLINE not in out and "/Users/someone/secret" not in out
    assert load_worker_settings(root).control_service_url == "http://localhost"


@pytest.mark.parametrize("step, fallback", [
    ("offer_worker", guided_setup.START_WORKER_NEXT),
    ("confirm_account", guided_setup.ACCOUNT_NEXT),
    ("approve_folders", guided_setup.FOLDER_NEXT),
])
def test_a_failing_step_prints_its_command_and_the_rest_still_run(root, keychain, monkeypatch, capsys,
                                                                  enable_calls, step, fallback):
    monkeypatch.setattr(guided_setup, step, lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _pair(root, monkeypatch, interactive=False) == 0
    out = capsys.readouterr().out
    assert fallback in out and "Remote tasks setup:" in out
    assert load_worker_settings(root).control_service_url == "http://localhost"


def test_pairing_succeeds_even_if_the_whole_setup_fails(root, keychain, monkeypatch, capsys):
    monkeypatch.setattr(guided_setup, "run", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _pair(root, monkeypatch, interactive=True) == 0
    out = capsys.readouterr().out
    assert "Paired worker worker." in out and "`openswap worker setup`" in out
    assert load_worker_settings(root).control_service_url == "http://localhost"


def test_summary_is_ready_only_when_admission_is_open(root, monkeypatch, capsys, enable_calls, research_home):
    cli.set_worker_account(root, "1")
    update_worker_settings(root, enabled=True, paused=True)
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": True, "process_state": "running", "remote_connectivity": "online"})
    assert _setup(root, monkeypatch, ["", "y", ""]) == 0
    out = capsys.readouterr().out
    assert "  Worker: running (admission paused)" in out
    assert "Before Slack can start tasks on this Mac: reopen admission (`openswap worker pause --off`)." in out
    assert "Ready for Slack" not in out
    update_worker_settings(root, paused=False)
    assert _setup(root, monkeypatch, ["", ""]) == 0
    out = capsys.readouterr().out
    assert "  Worker: running\n" in out and "Ready for Slack" in out


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
        "starting": "wait for the worker to finish starting (`openswap worker status`)",
        "stopped": "start the worker (`openswap worker enable`)",
    }[worker]
    worker_steps = {"wait for the worker to finish starting (`openswap worker status`)",
                    "start the worker (`openswap worker enable`)"}
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
    assert "  Worker: running\n" in out and "wait for the worker" not in out


def test_summary_never_calls_a_stuck_starting_worker_ready(root, monkeypatch, capsys, research_home):
    cli.set_worker_account(root, "1")
    cli.add_worker_workspace(root, "research", research_home, replace_builtin_default=True)
    update_worker_settings(root, enabled=True)
    monkeypatch.setattr(cli, "read_status", lambda _root: {"enabled": True, "process_state": "starting"})
    guided_setup.summary(root, _Say(),
                         start_wait_s=0)
    out = capsys.readouterr().out
    assert "  Worker: starting" in out and "Ready for Slack" not in out
    assert "wait for the worker to finish starting" in out


@pytest.mark.parametrize(("connection", "step"), [
    ("online", None),
    ("offline", "wait for the worker to connect to the service (`openswap worker status`)"),
    (None, "wait for the worker to connect to the service (`openswap worker status`)"),
    ("revoked", "pair this Mac again: the service revoked it (`openswap worker pair <url> <code>`)"),
    ("expired", "pair this Mac again: its pairing expired (`openswap worker pair <url> <code>`)"),
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
    assert ("Ready for Slack" in out) is (step is None)


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
    assert f"  Service: {URL} (online)" in out and "Ready for Slack" in out


def test_allowed_accounts_without_a_pinned_default_are_not_ready(root, monkeypatch):
    cli.allow_worker_account(root, "1")
    cli.set_worker_account(root, None)
    assert guided_setup.readiness(root).account is None
    assert "pin an account (`openswap worker account <slot>`, or `claude:<slot>`)" in \
        guided_setup.readiness(root).missing


class _Say:
    def say(self, text):
        print(text)


def test_a_failing_pin_keeps_the_setup_going(root, keychain, monkeypatch, capsys, enable_calls):
    monkeypatch.setattr(cli, "set_worker_account", lambda *_: (_ for _ in ()).throw(OSError("disk")))
    assert _pair(root, monkeypatch, interactive=True, answers=["n", "1", "y"]) == 0
    out = capsys.readouterr().out
    assert "Could not pin that account" in out
    assert load_worker_settings(root).workspaces[0].workspace_id == "research" and not _builtin(root)


# --- the folder step ------------------------------------------------------------------------


def _setup(root, monkeypatch, answers):
    configure_worker_service(root, URL, "worker-1")
    _answers(monkeypatch, answers)
    return _run(root, "setup")


def test_setup_needs_a_pairing(root, capsys):
    assert _run(root, "setup") == 1
    assert "`openswap worker pair <url> <code>`" in capsys.readouterr().err


def test_setup_reruns_the_steps_on_a_paired_mac(root, monkeypatch, capsys, enable_calls, research_home):
    cli.set_worker_account(root, "1")
    assert _setup(root, monkeypatch, ["y", "", "y", ""]) == 0
    out = capsys.readouterr().out
    assert enable_calls == [root] and "Create and approve" in out
    assert load_worker_settings(root).workspaces[0].output_root == research_home.resolve()
    # Run again: the default is approved now, so it lists folders and offers others.
    assert _setup(root, monkeypatch, ["n", "", ""]) == 0
    out = capsys.readouterr().out
    assert "Create and approve" not in out
    assert "Approved research folders: research (OpenSwap Research)." in out and ANOTHER in out


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_folder_step_refuses_an_existing_folder_others_can_read(root, monkeypatch, capsys, enable_calls,
                                                                research_home):
    research_home.mkdir()
    os.chmod(research_home, 0o755)
    assert _setup(root, monkeypatch, ["n", "", "y", ""]) == 0
    assert "chmod 700" in capsys.readouterr().out
    assert _builtin(root)
    assert stat.S_IMODE(research_home.stat().st_mode) == 0o755  # never changed for the owner


def test_folder_step_adds_other_folders_with_a_suggested_id(root, monkeypatch, capsys, enable_calls, tmp_path):
    docs = tmp_path / "Team Docs!"
    assert _setup(root, monkeypatch, ["n", "", "y", str(docs), "", str(tmp_path / "bad"), "Bad ID",
                                      ""]) == 0
    out = capsys.readouterr().out
    assert "Folder ID [team-docs] " in out
    assert cli._WORKSPACE_MESSAGES["workspace_id_invalid"] in out
    ids = [(w.workspace_id, w.display_label) for w in load_worker_settings(root).workspaces]
    assert ids == [("research", "OpenSwap Research"), ("team-docs", "Team Docs!")]


def test_suggested_ids_are_valid_and_free():
    taken = {"research", "notes"}
    assert guided_setup.suggested_folder_id(Path("/x/Notes"), taken) == "notes-2"
    assert guided_setup.suggested_folder_id(Path("/x/Résumé 2026"), taken) == "r-sum-2026"
    assert guided_setup.suggested_folder_id(Path("/x/___"), taken) == "folder"
    assert len(guided_setup.suggested_folder_id(Path("/x/" + "a" * 90), set())) <= 64


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
    assert 'docs "docs":' in out and 'research "research":' in out


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


def test_loopback_pair_setup_and_report(root, keychain, monkeypatch, capsys, research_home):  # noqa: F811
    store = ControlStore(root.parent / "service" / "db")
    monkeypatch.setattr(cli, "enable_worker", lambda _root: update_worker_settings(_root, enabled=True))
    with make_server(store, port=0) as server:
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            _answers(monkeypatch, ["y", "1", "y", ""])
            assert _run(root, "pair", url, store.issue_code()) == 0
            from openswap.worker.pairing import load_enrollment

            enrollment = load_enrollment(url)
            runtime = WorkerRuntime(root, adapter=FakeAdapter())
            remote = RemoteClient(runtime, url, enrollment.device_key, worker_id=enrollment.worker_id)
            remote.tick()
            assert remote.state == "online"
            assert store.readiness(enrollment.worker_id) == {
                "folders": [{"id": "research", "label": "OpenSwap Research"}], "execution": "disabled",
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
                                                                          enable_calls, research_home):
    monkeypatch.setattr(pairing, "Transport", lambda *_: PairTransport())
    dialogs = _Dialogs([
        (1, "not a command"),
        (1, "openswap worker pair http://localhost one-use"),
        1,              # start the worker
        (1, "1"),       # account
        1,              # approve ~/OpenSwap Research
        (0, ""),        # folder chooser cancelled: no other folder
        1,              # Done
    ])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert enable_calls == [root]
    policy = load_worker_settings(root)
    assert policy.control_service_url == "http://localhost" and policy.pinned_account_ref == ALICE
    assert policy.workspaces[0].output_root == research_home.resolve()
    messages = [kwargs["message"] for _kind, kwargs in dialogs.shown]
    assert "That is not a pairing command" in messages[1]
    assert "Paired worker worker." in messages[2] and "Start the Remote tasks worker now" in messages[2]
    assert dialogs.shown[-1][1]["ok"] == "Done" and "Remote tasks setup:" in messages[-1]
    assert app.refreshed == 1


def test_menu_setup_cancelled_at_pairing_does_nothing(root, keychain):
    dialogs = _Dialogs([(0, "")])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert len(dialogs.shown) == 1 and load_worker_settings(root).control_service_url is None


def test_menu_setup_on_a_paired_mac_skips_pairing(root, enable_calls):
    configure_worker_service(root, URL, "worker-1")
    dialogs = _Dialogs([0, (0, ""), 0, (0, ""), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert "Start the Remote tasks worker now" in dialogs.shown[0][1]["message"]
    assert enable_calls == [] and _builtin(root)


def test_menu_setup_reports_a_refused_code(root, keychain, monkeypatch):
    class Refusing:
        def request(self, *_):
            raise ProtocolError("invalid_code")

    monkeypatch.setattr(pairing, "Transport", lambda *_: Refusing())
    dialogs = _Dialogs([(1, "http://localhost used"), (0, ""), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    assert "Could not pair: invalid_code." in dialogs.shown[1][1]["message"]
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
    assert "Pinned Claude account 4 · carol@example.com (claudey)" in out
    assert load_worker_settings(root).pinned_account_ref.startswith("claude:")
    assert "  Account: Claude 4 · carol@example.com (claudey)" in out
    assert guided_setup.CLAUDE_EXECUTION_OFF_NOTE in out and guided_setup.EXECUTION_OFF_NOTE not in out
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
    configure_worker_service(root, URL, "worker-1")
    notes = tmp_path / "Notes"
    dialogs = _Dialogs([0, (0, ""), 0, (1, str(notes)), (1, ""), (0, ""), 1])
    app = _menu(root, dialogs)
    app._on_setting("remote_tasks_setup", None)
    _settle(app)
    kinds = [kind for kind, _kwargs in dialogs.shown]
    assert kinds.count("choose_folder") == 2
    chooser = dialogs.shown[kinds.index("choose_folder")][1]
    assert chooser["title"] == "Set up Remote tasks" and "Another folder to approve" in chooser["message"]
    ids = [w.workspace_id for w in load_worker_settings(root).workspaces]
    assert "notes" in ids


def test_dialog_prompts_type_the_folder_without_a_chooser():
    dialogs = _Dialogs([(1, "/tmp/x")])
    ui = guided_setup.DialogPrompts(dialogs.alert, dialogs.prompt)
    assert ui.choose_folder("Folder?") == "/tmp/x"
    assert dialogs.shown[0][0] == "prompt"
