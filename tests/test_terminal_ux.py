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
    assert f"{BOLD}Codex accounts (pin with the slot number, email or alias)\x1b[0m" in out
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
    assert "\x1b[" not in out and "Next: pin an account" in out


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
    ui.section("Step 3 of 4 · Research folder")
    assert lines[-1] == f"{BOLD}Step 3 of 4 · Research folder\x1b[0m"


def test_prompts_render_the_default_and_a_colon(monkeypatch):
    seen = []
    ui = guided_setup.TerminalPrompts(interactive=True, read_line=lambda prompt: seen.append(prompt) or "")
    assert ui.ask("Account number (1-6; Enter keeps the current one)", default="2") == "2"
    assert ui.ask("Folder ID", default="team-docs") == "team-docs"
    assert ui.ask("Another folder to approve (path; Enter to finish)") == ""
    assert ui.confirm("Keep going?") is True
    assert seen == ["Account number (1-6; Enter keeps the current one) [2]: ", "Folder ID [team-docs]: ",
                    "Another folder to approve (path; Enter to finish): ", "Keep going? [Y/n] "]


def test_setup_without_a_tty_prints_headers_and_commands_but_asks_nothing(root, capsys, monkeypatch):
    from openswap.settings import configure_worker_service

    configure_worker_service(root, "http://127.0.0.1:8765", "worker-1")
    monkeypatch.setattr(cli, "_interactive_terminal", lambda: False)
    monkeypatch.setattr(cli, "_managed_worker_loaded", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("no prompt without a terminal"))
    assert _run(root, "setup") == 0
    out = capsys.readouterr().out
    for title in ("Step 1 of 4 · Worker", "Step 2 of 4 · Account", "Step 3 of 4 · Research folder",
                  "Step 4 of 4 · Summary"):
        assert title in out
    assert guided_setup.ACCOUNT_NEXT in out and "Account number" not in out
    assert "  ✗ Account    none pinned" in out and "\x1b[" not in out and SECRET not in out


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
    snapshot = {"enabled": True, "paused": True, "process_state": "running",
                "provider": {"available": True}, "remote_connectivity": "online",
                "remote_last_seen_at": "2026-10-08T09:00:00Z", "active_job": {"job_id": "abc", "state": "running"}}
    monkeypatch.setattr(cli, "read_status", lambda _root: snapshot)
    assert _run(root, "status") == 0
    out = capsys.readouterr().out
    assert out == (
        "Remote tasks worker\n"
        "  ✓ Remote tasks  enabled\n"
        "  ✓ Worker        running\n"
        "  ✗ Admission     paused\n"
        "  ✓ Provider      available\n"
        "  ✓ Service       online (last seen 2026-10-08T09:00:00Z)\n"
        "  • Job           abc (running)\n"
    )


def test_codex_and_claude_status_point_at_the_next_step():
    codex = {"cli": {"installed": True, "version": "codex-cli 0.157.1"}, "execution_mode": "disabled",
             "accounts": [{"slot": "1", "alias": "work", "pinned": True, "allowed": True, "isolated_sign_in": False},
                          {"slot": "2", "alias": None, "pinned": False, "allowed": False, "isolated_sign_in": True}]}
    out = live_cli._format_codex_status(codex)
    assert "  ✓ Pinned CLI      codex-cli 0.157.1 (verified)" in out
    assert "  • Live execution  disabled" in out
    assert "  ✗ 1  (work)  not signed in  default, allowed" in out
    assert "  ✓ 2          signed in" in out
    assert out.endswith("Next: sign the default account in: `openswap worker codex login`.")
    codex["accounts"][0]["isolated_sign_in"] = True
    assert live_cli._format_codex_status(codex).endswith("`openswap worker live-check`.")
    codex["cli"] = {"installed": False, "problem": "not_installed"}
    assert live_cli._format_codex_status(codex).endswith("`openswap worker codex install`.")

    claude = {"cli": {"pinned": False, "problem": "not_pinned"}, "execution_mode": "disabled",
              "accounts": [{"slot": "4", "alias": "claudey", "pinned": True, "allowed": False, "profile_ready": False}]}
    out = live_cli._format_claude_status(claude)
    assert "  ✗ Pinned binary   not ready (not_pinned)" in out
    assert "  ✗ 4  (claudey)  profile not prepared  default" in out
    assert out.endswith("Next: pin the installed Claude Code: `openswap worker claude pin`.")
    claude["cli"] = {"pinned": True, "version": "2.1.285 (Claude Code)"}
    assert live_cli._format_claude_status(claude).endswith("`openswap worker claude prepare`.")
    claude["accounts"][0]["profile_ready"] = True
    claude["execution_mode"] = "live"
    assert "live execution is on" in live_cli._format_claude_status(claude)


def test_live_check_gates_are_marked_pass_or_fail():
    gates = {name: {"passed": True} for name in live_check.REQUIRED_GATES}
    first = live_check.REQUIRED_GATES[0]
    gates[first] = {"passed": False, "reason": "sandbox leaked"}
    out = live_check._format({"gates": gates})
    lines = out.splitlines()
    assert lines[0] == f"  ✗ FAIL  {first}" and lines[1] == "            reason: sandbox leaked"
    assert lines[2].startswith("  ✓ PASS  ")


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
