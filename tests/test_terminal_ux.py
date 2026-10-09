"""Terminal presentation of the Remote tasks commands.

Numbered selection in the guided setup, the shared marks and columns, ANSI
styling only on a colour terminal (never under ``NO_COLOR``, a pipe or
``--json``), and the "did you mean" for a mistyped command. Roster files
hold metadata only; no Keychain, launchctl or provider auth is touched.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from openswap import cli as main_cli
from openswap import printer
from openswap.worker import cli, guided_setup, live_check, live_cli
from tests.test_cli import _subprocess_env
from tests.test_worker_accounts import SECRET, root  # noqa: F401 (fixture)

BOLD = "\x1b[1m"


def _run(root, *argv):
    return cli.main(list(argv), backup_root=root)


# --- shared helpers -------------------------------------------------------------------------


def test_marks_pair_a_glyph_with_a_state():
    assert (printer.mark(True), printer.mark(False), printer.mark(None)) == ("✓", "✗", "•")


def test_columns_align_plain_cells_and_drop_trailing_padding():
    lines = printer.columns([("✓ 1", "Codex", "a@x.com", ""), ("• 10", "Claude", "bob@x.com", "current")])
    assert lines == ["  ✓ 1   Codex   a@x.com", "  • 10  Claude  bob@x.com  current"]
    assert all(not line.endswith(" ") for line in lines)
    assert printer.columns([]) == []


def test_heading_and_next_step_are_bold_only_with_colours(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    printer._colors_enabled = None
    assert printer.heading("Remote tasks account") == f"{BOLD}Remote tasks account\x1b[0m"
    assert printer.next_step("pin an account") == f"{BOLD}Next:\x1b[0m pin an account"
    monkeypatch.setenv("NO_COLOR", "1")  # NO_COLOR wins over FORCE_COLOR
    printer._colors_enabled = None
    assert printer.heading("Remote tasks account") == "Remote tasks account"
    assert printer.next_step("pin an account") == "Next: pin an account"


# --- colours: on a colour terminal only, and never in --json -------------------------------


def test_account_listing_is_plain_when_piped(root, capsys):
    # capsys is not a TTY: exactly what a pipe or CI sees.
    printer._colors_enabled = None
    assert _run(root, "account") == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out and out.startswith("Remote tasks account\n")
    assert "  • 1  alice@example.com   (work)" in out


def test_account_listing_styles_headings_on_a_colour_terminal_but_not_json(root, capsys, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    printer._colors_enabled = None
    assert _run(root, "account") == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{BOLD}Remote tasks account\x1b[0m\n")
    assert f"{BOLD}Codex (pin by slot, email or alias)\x1b[0m" in out
    assert "  • 1  alice@example.com   (work)" in out  # rows stay plain: marks and words carry the meaning
    assert _run(root, "account", "--json") == 0
    raw = capsys.readouterr().out
    assert "\x1b[" not in raw
    assert json.loads(raw)["pinned_account_ref"] is None
    assert _run(root, "status", "--json") == 0
    assert "\x1b[" not in capsys.readouterr().out


def test_no_color_disables_styling_even_on_a_colour_terminal(root, capsys, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    printer._colors_enabled = None
    assert _run(root, "account") == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out and "Next: `openswap worker account 1` to pin" in out


def test_step_headers_are_bold_on_a_colour_terminal_only(monkeypatch):
    lines = []
    ui = guided_setup.TerminalPrompts(interactive=False, write=lines.append)
    monkeypatch.setenv("NO_COLOR", "1")
    printer._colors_enabled = None
    ui.section("Step 2 of 4 · Account")
    assert lines == ["", "Step 2 of 4 · Account"]
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("FORCE_COLOR", "1")
    printer._colors_enabled = None
    ui.section("Step 3 of 4 · Folders")
    assert lines[-1] == f"{BOLD}Step 3 of 4 · Folders\x1b[0m"


def test_prompts_render_the_default_and_a_colon(monkeypatch):
    seen = []
    ui = guided_setup.TerminalPrompts(interactive=True, read_line=lambda prompt: seen.append(prompt) or "")
    assert ui.ask("Account", default="2") == "2"
    assert ui.ask("Folders (numbers or a path)", default="1") == "1"
    assert ui.ask("Type the path to your code folder, for example ~/GitHub (Enter skips)") == ""
    assert ui.confirm("Keep going?") is True
    assert seen == ["Account [2]: ",
                    "Folders (numbers or a path) [1]: ",
                    "Type the path to your code folder, for example ~/GitHub (Enter skips): ",
                    "Keep going? [Y/n] "]


def test_setup_without_a_tty_prints_headers_and_commands_but_asks_nothing(root, capsys, monkeypatch):
    from openswap.settings import configure_worker_service

    configure_worker_service(root, "http://127.0.0.1:8765", "worker-1")
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli, "_managed_worker_loaded", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("no prompt without a terminal"))
    assert _run(root, "setup") == 0
    out = capsys.readouterr().out
    for title in ("Step 1 of 4 · Worker", "Step 2 of 4 · Account", "Step 3 of 4 · Folders",
                  "Step 4 of 4 · Summary"):
        assert title in out
    assert guided_setup.ACCOUNT_NEXT in out and "Account (" not in out
    assert "  ✗ Account     none" in out and "\x1b[" not in out and SECRET not in out


# --- the account menu --------------------------------------------------------------------------


def test_account_menu_numbers_both_providers_in_order(root):
    cli.set_worker_account(root, "claude:4")
    choices = cli.worker_account_choices(root)
    menu, lines = guided_setup.account_menu(choices, choices.pinned)
    assert [(c.provider, c.number) for c in menu] == [
        ("codex", "1"), ("codex", "2"), ("codex", "5"), ("codex", "6"), ("claude", "1"), ("claude", "4"),
    ]
    assert lines[0] == "  • 1  Codex   alice@example.com   (work)     slot 1"
    assert lines[-1] == "  ✓ 6  Claude  carol@example.com   (claudey)  slot 4  current"
    assert all("\x1b[" not in line for line in lines)


# --- status and live-check output -----------------------------------------------------------------


def test_status_rows_are_marked_and_worded(root, monkeypatch, capsys):
    from openswap.settings import configure_worker_service

    configure_worker_service(root, "https://opentag.me", "worker-1")
    snapshot = {"enabled": True, "paused": True, "process_state": "running",
                "provider": {"available": True}, "remote_connectivity": "online",
                "remote_last_seen_at": "2026-10-08T09:00:00Z", "active_job": {"job_id": "abc", "state": "running"}}
    monkeypatch.setattr(cli, "read_status", lambda _root: snapshot)
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert out == (
        "Remote tasks\n"
        "  ✓ Worker        running\n"
        "  ✗ Taking tasks  paused\n"
        "  ✓ Live tasks    on\n"
        "  ✓ Service       online (seen 2026-10-08T09:00:00Z)\n"
        "  • Task          abc (running)\n"
        "Next: `openswap worker pause --off` to take tasks again.\n"
    )
    # Not paused, live tasks off: the one next step is the pinned kind's live check.
    snapshot.update(paused=False, provider={"available": False, "diagnostic_code": "live_adapter_disabled"},
                    active_job=None)
    cli.set_worker_account(root, "claude:4")
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert "  • Live tasks    off\n" in out and "  • Task          none\n" in out
    # The live check refuses while the worker takes tasks: pause comes first.
    assert out.endswith("Next: `openswap worker pause`, then `openswap worker claude pin`, "
                        "`openswap worker claude prepare`, then "
                        "`openswap worker live-check --provider claude` to turn on live tasks.\n")
    snapshot["paused"] = True
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert "  ✗ Taking tasks  paused\n" in out and "`openswap worker pause`," not in out
    assert out.endswith("`openswap worker live-check --provider claude` to turn on live tasks.\n")
    snapshot["paused"] = False
    # Any other reason live tasks are off is shown with the row, not restated.
    snapshot["provider"] = {"available": False, "diagnostic_code": "provider_auth_unavailable"}
    monkeypatch.setattr(cli, "read_status", lambda _root: {**snapshot, "enabled": False, "process_state": "stopped"})
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert "  ✗ Worker        off\n" in out and "  • Live tasks    off (provider_auth_unavailable)\n" in out
    # A worker that is not running admits nothing, whatever the pause flag says.
    assert "  • Taking tasks  no (worker not running)\n" in out
    assert out.count("Next:") == 1 and "`openswap worker enable`" in out


def test_status_points_the_live_check_at_an_unchecked_allowed_account(root, monkeypatch, capsys):
    from openswap.settings import configure_worker_service
    from openswap.worker import live
    from tests.test_worker_accounts import ALICE

    configure_worker_service(root, "https://opentag.me", "worker-1")
    cli.set_worker_account(root, "1")
    cli.allow_worker_account(root, "claude:4")
    snapshot = {"enabled": True, "paused": True, "process_state": "running",
                "provider": {"available": False, "diagnostic_code": "live_adapter_disabled"},
                "remote_connectivity": "online", "active_job": None}
    monkeypatch.setattr(cli, "read_status", lambda _root: snapshot)
    modes = {"codex": {"execution_mode": "live", "checked_accounts": [ALICE]},
             "claude": {"execution_mode": "live", "checked_accounts": []}}
    monkeypatch.setattr(live, "live_status", lambda _root, provider="codex": modes[provider])
    # Codex is live and the pin was checked; the allowed Claude account was not: check it.
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert out.endswith("Next: `openswap worker live-check --provider claude --account claude:4` "
                        "to check account 4 too.\n")
    # Taking tasks: the pause still comes first.
    snapshot["paused"] = False
    assert _run(root, "status") == 0
    assert capsys.readouterr().out.endswith("Next: `openswap worker pause`, then `openswap worker live-check "
                                            "--provider claude --account claude:4` to check account 4 too.\n")
    # The allowed account's kind has live tasks off altogether: its own path comes first.
    modes["claude"] = {"execution_mode": "disabled", "checked_accounts": []}
    assert _run(root, "status") == 0
    assert capsys.readouterr().out.endswith("`openswap worker claude pin`, `openswap worker claude prepare`, then "
                                            "`openswap worker live-check --provider claude` to turn on live tasks.\n")
    # Codex itself off: the pinned path, whatever the allowed accounts.
    modes["codex"] = {"execution_mode": "disabled", "checked_accounts": []}
    assert _run(root, "status") == 0
    assert capsys.readouterr().out.endswith("Next: `openswap worker pause`, then `openswap worker live-check` "
                                            "to turn on live tasks.\n")
    # The allowed account left its roster: no check can cover it, so drop it by reference,
    # even while its kind's live tasks are off.
    modes["codex"] = {"execution_mode": "live", "checked_accounts": [ALICE]}
    modes["claude"] = {"execution_mode": "disabled", "checked_accounts": []}
    (reference,) = [entry.account_ref for entry in cli.worker_account_choices(root).allowlist
                    if entry.identity.startswith("claude:")]
    roster = json.loads((root / "sequence.json").read_text(encoding="utf-8"))
    del roster["accounts"]["4"]
    (root / "sequence.json").write_text(json.dumps(roster), encoding="utf-8")
    snapshot["paused"] = True
    assert _run(root, "status") == 0
    assert capsys.readouterr().out.endswith(f"Next: `openswap worker account disallow {reference}` to drop an "
                                            "allowed account that left its roster.\n")


def test_status_gives_one_next_step_even_with_a_blocked_folder(root, monkeypatch, capsys):
    from openswap.settings import configure_worker_service

    snapshot = {"enabled": True, "paused": False, "process_state": "running",
                "provider": {"available": False, "diagnostic_code": "live_adapter_disabled"},
                "remote_connectivity": "online", "active_job": None}
    monkeypatch.setattr(cli, "read_status", lambda _root: snapshot)
    monkeypatch.setattr(cli, "refused_workspaces", lambda _root: [("a", "folder_overlaps_readable")])
    # Unpaired: the blocked folder is the one step.
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert '✗ "a" is blocked (folder_overlaps_readable).' in out and out.count("Next:") == 1
    assert out.endswith("Next: `openswap worker workspace remove <id>` to drop a blocked folder "
                        "(`openswap worker workspace list` shows them).\n")
    # Paired and running with live tasks off: the folder still comes before the live check.
    configure_worker_service(root, "https://opentag.me", "worker-1")
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert out.count("Next:") == 1 and "workspace remove" in out and "live-check" not in out
    # The worker being off outranks it.
    monkeypatch.setattr(cli, "read_status", lambda _root: {**snapshot, "enabled": False, "process_state": "stopped"})
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert out.count("Next:") == 1 and out.endswith(
        "Next: `openswap worker enable` to start the worker (paired with https://opentag.me).\n")


def test_a_refused_pause_says_what_still_holds(root, monkeypatch, capsys):
    monkeypatch.setattr(cli, "request_pause", lambda _root, paused: {
        "accepted": False, "paused": not paused, "diagnostic_code": "settings_unavailable"})
    assert _run(root, "pause") == 1
    out, err = capsys.readouterr()
    assert out == "" and err == "✗ Not changed (settings_unavailable); the worker is still taking tasks.\n"
    assert _run(root, "pause", "--off") == 1
    assert capsys.readouterr().err.endswith("the worker is still paused.\n")
    assert _run(root, "pause", "--json") == 1
    assert json.loads(capsys.readouterr().out)["accepted"] is False


def test_blocked_disable_names_the_lease_store_that_is_held(root, monkeypatch, capsys):
    from types import SimpleNamespace

    held = {"claude"}
    monkeypatch.setattr(cli.AccountLeaseStore, "read_current",
                        lambda self: SimpleNamespace(state="active") if self.provider in held else None)
    monkeypatch.setattr(cli, "disable_worker", lambda _root: (False, {}, "lease_state_unknown"))
    assert _run(root, "disable") == 1
    assert capsys.readouterr().err.endswith("Next: `openswap worker lease release --provider claude` to free it.\n")
    held.add("codex")
    assert _run(root, "disable") == 1
    assert capsys.readouterr().err.endswith(
        "Next: `openswap worker lease release` or `openswap worker lease release --provider claude` to free it.\n")


def test_live_mode_refusals_point_at_a_retry_or_a_new_live_check():
    from openswap.worker.live import LiveModeError

    # A lock failure can come from `live disable` too: never send the owner to a quota-using check.
    assert live_cli._live_mode_message(LiveModeError("live_lock_busy"), "codex") == (
        "Live tasks are being changed; try again in a moment.")
    assert "live-check" not in live_cli._live_mode_message(LiveModeError("live_lock_unavailable"), "claude")
    # Evidence problems only come from enabling: a new live check is the fix.
    assert live_cli._live_mode_message(LiveModeError("evidence_not_passing", ("stop",)), "claude") == (
        "The live check did not pass, or no longer matches this Mac (stop). "
        "Next: `openswap worker live-check --provider claude`.")
    assert live_cli._live_mode_message(LiveModeError("evidence_invalid"), "codex").endswith(
        "Next: `openswap worker live-check`.")
    # An Intel Mac: a live check would refuse too, so no next step is offered.
    assert live_cli._live_mode_message(LiveModeError("unsupported_platform"), "codex") == (
        "Live tasks run only on Apple silicon Macs.")


def test_workspace_list_says_what_tasks_do_in_each_folder(root, tmp_path, monkeypatch):
    from openswap.settings import WorkerWorkspace

    home = tmp_path / "home"
    monkeypatch.setattr(cli, "home_folder", lambda: home)
    results = home / "OpenSwap Research"
    out = cli._format_workspaces((
        WorkerWorkspace("research", results / "research", ()),
        WorkerWorkspace("docs", results / "docs", (home / "Docs",)),
        WorkerWorkspace("mixed", tmp_path / "out", (home / "src", home / "notes"), "Mixed"),
        WorkerWorkspace("github", results / "github", (), None, home / "GitHub", "worktree", True),
        WorkerWorkspace("app", results / "app", (), None, home / "GitHub" / "app", "direct", False),
    ))
    assert out.splitlines() == [
        "Folders",
        '  ✓ research  "research"  writes results in ~/OpenSwap Research/research',
        '  ✓ docs      "docs"      reads ~/Docs; writes results in ~/OpenSwap Research/docs',
        f'  ✓ mixed     "Mixed"     reads ~/src, ~/notes; writes results in {tmp_path / "out"}',
        '  ✓ github    "GitHub"    works in each repo in ~/GitHub (own worktree per task)',
        '  ✓ app       "app"       works in ~/GitHub/app (works in the folder itself)',
        "Next: `openswap worker workspace add --work <folder>` adds one; "
        "`openswap worker workspace remove <id>` drops one.",
    ]


def test_codex_and_claude_status_point_at_the_next_step():
    codex = {"cli": {"installed": True, "version": "codex-cli 0.157.1"}, "execution_mode": "disabled",
             "accounts": [{"slot": "1", "alias": "work", "pinned": True, "allowed": True, "isolated_sign_in": False},
                          {"slot": "2", "alias": None, "pinned": False, "allowed": False, "isolated_sign_in": True}]}
    out = live_cli._format_codex_status(codex)
    assert out == (
        "Codex for Remote tasks\n"
        "  ✓ Codex CLI   codex-cli 0.157.1 (verified)\n"
        "  • Live tasks  off\n"
        "Accounts\n"
        "  ✗ 1  (work)  not signed in  pinned, allowed\n"
        "  ✓ 2          signed in\n"
        "Next: `openswap worker codex login` to sign the pinned account in."
    )
    codex["accounts"][0]["isolated_sign_in"] = True
    assert live_cli._format_codex_status(codex).endswith("Next: `openswap worker live-check` to turn on live tasks.")
    codex["cli"] = {"installed": False, "problem": "not_installed"}
    assert live_cli._format_codex_status(codex).endswith("`openswap worker codex install` to install the Codex CLI.")

    claude = {"cli": {"pinned": False, "problem": "not_pinned"}, "execution_mode": "disabled",
              "accounts": [{"slot": "4", "alias": "claudey", "pinned": True, "allowed": False, "profile_ready": False}]}
    out = live_cli._format_claude_status(claude)
    assert "  ✗ Claude Code  not ready (not_pinned)" in out
    assert "  ✗ 4  (claudey)  not ready  pinned" in out
    assert out.endswith("Next: `openswap worker claude pin` to pin the installed Claude Code.")
    claude["cli"] = {"pinned": True, "version": "2.1.285 (Claude Code)"}
    # "ready" covers both a missing sign-in and a managed policy: `prepare` signs in or names the policy.
    assert live_cli._format_claude_status(claude).endswith("`openswap worker claude prepare` to get the pinned account ready.")
    claude["accounts"][0]["profile_ready"] = True
    claude["execution_mode"] = "live"
    out = live_cli._format_claude_status(claude)
    # Live tasks on for every account: nothing to do, so no `Next:` at all.
    assert "  ✓ Live tasks   on" in out and "Next:" not in out


def test_live_check_gates_fold_to_one_line_unless_failed_or_verbose():
    gates = {name: {"passed": True, "steps_ran": 3} for name in live_check.REQUIRED_GATES}
    assert live_check._format({"gates": gates}) == (
        "  ✓ passed: the pinned CLI, the account, your own login untouched, tools, sandbox, a research task, "
        "sandbox (a task), stop, kill recovery, worktree"
    )
    gates["sandbox_wrapper"] = {"passed": False, "reason": "sandbox leaked", "write_outside": "/tmp/x",
                                "network_denied": True, "steps_ran": 2}
    out = live_check._format({"gates": gates})
    assert out.splitlines() == [
        "  ✓ passed: the pinned CLI, the account, your own login untouched, tools, a research task, "
        "sandbox (a task), stop, kill recovery, worktree",
        "  ✗ failed: sandbox (reason: sandbox leaked; write_outside: /tmp/x)",
    ]
    verbose = live_check._format({"gates": gates}, verbose=True).splitlines()
    assert len(verbose) == len(live_check.REQUIRED_GATES)
    assert verbose[0] == "  ✓ passed  the pinned CLI"
    assert verbose[4] == "  ✗ failed  sandbox                   reason: sandbox leaked; write_outside: /tmp/x"
    assert all(("passed" in line or "failed" in line) and line[2] in "✓✗" for line in verbose)


# --- discoverability -----------------------------------------------------------------------------


def test_mistyped_worker_command_suggests_the_closest_one(root, capsys):
    assert _run(root, "acount") == 2
    assert capsys.readouterr().err.strip() == (
        "openswap worker: unknown command 'acount'. Did you mean `openswap worker account`?"
    )
    assert _run(root, "zzz") == 2
    assert "Run `openswap worker --help`" in capsys.readouterr().err


@pytest.mark.parametrize("word, expected", [
    ("account", "`openswap worker account`"),
    ("pair", "`openswap worker pair`"),
    ("lsit", "`openswap list`"),
    ("swicth", "`openswap switch`"),
])
def test_unknown_top_level_command_suggests_a_command(word, expected, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["openswap", word])
    assert expected in main_cli.unknown_command_message(word)


def test_openswap_account_points_at_worker_account(tmp_path):
    home = str(tmp_path / "home")
    result = subprocess.run(
        [sys.executable, "-m", "openswap", "account"],
        capture_output=True, text=True, env=_subprocess_env(HOME=home, USERPROFILE=home),
    )
    assert result.returncode == 2
    assert "unknown command 'account'" in result.stderr
    assert "openswap worker account" in result.stderr
