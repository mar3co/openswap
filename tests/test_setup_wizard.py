"""The setup wizard's steps and pages (plan 021). No AppKit."""

from __future__ import annotations

import pytest

from openswap.setup_wizard import (
    ACTION_CLOSE,
    ACTION_FINISH,
    ACTION_MOVE,
    ACTION_NEXT,
    ACTION_OPEN_EXISTING,
    ACTION_TOGGLE_OPEN_AT_LOGIN,
    ACTION_TOGGLE_STATUS_LINE,
    CLAUDE,
    CODEX,
    DONE,
    EXTRAS,
    LOGIN,
    MOVE,
    WELCOME,
    WizardChoices,
    WizardFacts,
    initial_choices,
    next_step,
    previous_step,
    terminal_command,
    wizard_page,
    wizard_steps,
)


def _rows(page, kind):
    return [row for row in page["rows"] if row["kind"] == kind]


def _actions(page):
    return [item["action"] for row in _rows(page, "actions") for item in row["items"]]


def _notes(page):
    return " ".join(row["text"] for row in _rows(page, "note"))


# ---- steps --------------------------------------------------------------


def test_a_temporary_copy_shows_only_move():
    assert wizard_steps(WizardFacts(temporary=True)) == (MOVE,)


def test_a_stable_copy_walks_every_step_codex_only_when_available():
    assert wizard_steps(WizardFacts()) == (WELCOME, CLAUDE, LOGIN, EXTRAS, DONE)
    assert wizard_steps(WizardFacts(codex_available=True)) == (
        WELCOME, CLAUDE, CODEX, LOGIN, EXTRAS, DONE,
    )


def test_next_and_back_stop_at_the_ends_and_skip_codex():
    facts = WizardFacts()
    assert next_step(WELCOME, facts) == CLAUDE
    assert next_step(CLAUDE, facts) == LOGIN
    assert next_step(DONE, facts) == DONE
    assert previous_step(LOGIN, facts) == CLAUDE
    assert previous_step(WELCOME, facts) == WELCOME
    # A step that does not apply falls back to the first one.
    assert next_step(CODEX, facts) == WELCOME
    assert previous_step(MOVE, facts) == WELCOME


def test_progress_counts_the_shown_steps():
    facts = WizardFacts(codex_available=True)
    assert wizard_page(WELCOME, facts, WizardChoices())["progress"] == (1, 6)
    assert wizard_page(CODEX, facts, WizardChoices())["progress"] == (3, 6)
    assert wizard_page(MOVE, WizardFacts(temporary=True), WizardChoices())["progress"] is None


def test_a_step_that_does_not_apply_draws_the_first_one():
    page = wizard_page(CLAUDE, WizardFacts(temporary=True, move={"action": "move"}), WizardChoices())
    assert page["step"] == MOVE


def test_choices_start_with_login_on_and_the_status_line_as_on_disk():
    assert initial_choices(WizardFacts()) == WizardChoices(open_at_login=True, status_line=False)
    assert initial_choices(WizardFacts(status_line="on")).status_line is True
    # A hook for another OpenSwap is off here: turning it on repoints it.
    assert initial_choices(WizardFacts(status_line="other")).status_line is False


# ---- Move ---------------------------------------------------------------


def _move(**plan):
    facts = WizardFacts(temporary=True, move={"destination": "/Applications", **plan})
    return wizard_page(MOVE, facts, WizardChoices())


def test_move_from_a_disk_image_moves_and_ejects():
    page = _move(action="move", source="disk_image")
    assert page["primary"] == {"title": "Move to Applications", "action": ACTION_MOVE, "enabled": True}
    assert page["secondary"]["action"] == ACTION_CLOSE
    assert "disk image" in page["body"]
    assert "/Applications" in _notes(page)
    assert "ejected" in _notes(page)
    assert page["back"] is False


def test_move_from_a_download_says_so_and_ejects_nothing():
    page = _move(action="move", source="download")
    assert "download" in page["body"]
    assert "ejected" not in _notes(page)


def test_an_older_copy_is_replaced_into_the_trash():
    page = _move(action="replace", source="disk_image", existing_version="0.1.0")
    assert page["primary"]["title"] == "Replace and Open"
    assert page["primary"]["action"] == ACTION_MOVE
    assert "0.1.0" in _notes(page) and "Trash" in _notes(page)


def test_the_same_or_a_newer_copy_is_opened_instead():
    page = _move(action="open_existing", source="disk_image", existing_version="0.3.0")
    assert page["primary"] == {"title": "Open Installed Copy", "action": ACTION_OPEN_EXISTING, "enabled": True}
    assert "0.3.0" in _notes(page)


def test_a_homebrew_copy_is_opened_and_named_as_homebrews():
    page = _move(action="open_existing", source="disk_image", homebrew=True)
    assert page["primary"]["action"] == ACTION_OPEN_EXISTING
    assert "Homebrew" in _notes(page)
    assert "brew upgrade --cask openswap" in _notes(page)


def test_a_failed_plan_offers_only_not_now():
    page = _move(action="error", error="disk full", source="download")
    assert page["primary"] is None
    assert page["secondary"]["action"] == ACTION_CLOSE
    assert "disk full" in _notes(page)


# ---- Welcome and accounts -----------------------------------------------


def test_welcome_names_what_openswap_does_and_starts():
    page = wizard_page(WELCOME, WizardFacts(), WizardChoices())
    assert page["title"] == "Welcome to OpenSwap"
    assert len(_rows(page, "features")[0]["items"]) == 3
    assert page["primary"] == {"title": "Get Started", "action": ACTION_NEXT, "enabled": True}
    assert page["back"] is False
    assert page["icon"] is True


def test_no_claude_account_offers_sign_in_and_save_and_can_skip():
    page = wizard_page(CLAUDE, WizardFacts(), WizardChoices())
    assert page["title"] == "Add your Claude account"
    items = _rows(page, "actions")[0]["items"]
    assert [i["action"] for i in items] == ["claude_start", "claude_add"]
    # Sign in is the one accent button; Skip is plain text in the footer.
    assert items[0]["primary"] is True and "primary" not in items[1]
    assert page["primary"] is None
    assert page["skip"] == {"title": "Skip for Now", "action": ACTION_NEXT}
    assert page["back"] is True


def test_saved_claude_accounts_are_listed_and_continue():
    facts = WizardFacts(claude_accounts=(("work", "you@acme.com"), ("you@example.com", "")))
    page = wizard_page(CLAUDE, facts, WizardChoices())
    assert page["title"] == "Your Claude accounts"
    assert _rows(page, "accounts")[0]["items"] == [("work", "you@acme.com"), ("you@example.com", "")]
    assert page["primary"]["title"] == "Continue"
    assert page["skip"] is None
    assert "claude_start" in _actions(page)
    assert _rows(page, "accounts")  # Continue is the accent button now
    assert _rows(page, "actions")[0]["items"][0]["primary"] is False


def test_a_sign_in_in_progress_says_where_to_finish_it():
    page = wizard_page(CLAUDE, WizardFacts(claude_signing_in=True), WizardChoices())
    assert "browser" in _notes(page)
    items = _rows(page, "actions")[0]["items"]
    assert items[0] == {"title": "Open Sign-in Again", "action": "claude_start", "enabled": True,
                        "primary": True}
    assert items[1]["enabled"] is False  # Save Current Login waits


@pytest.mark.parametrize("state", ["loading", "error"])
def test_an_unread_roster_is_never_an_empty_one(state):
    page = wizard_page(CLAUDE, WizardFacts(claude_state=state), WizardChoices())
    assert "claude_start" not in _actions(page)
    if state == "error":
        assert _actions(page) == ["claude_retry"]


def test_without_claude_code_there_is_nothing_to_sign_in_with():
    page = wizard_page(CLAUDE, WizardFacts(claude_installed=False), WizardChoices())
    assert "isn’t installed" in _notes(page)
    assert _actions(page) == []
    assert page["primary"] is None
    assert page["skip"]["action"] == ACTION_NEXT


def test_codex_offers_chatgpt_sign_in_and_capture():
    page = wizard_page(CODEX, WizardFacts(codex_available=True), WizardChoices())
    assert page["title"] == "Add a Codex account"
    assert _actions(page) == ["codex_start", "codex_capture"]
    assert "Optional" in page["body"]


def test_an_unavailable_codex_store_says_restart():
    facts = WizardFacts(codex_available=True, codex_state="unavailable")
    page = wizard_page(CODEX, facts, WizardChoices())
    assert "Restart OpenSwap" in _notes(page)
    assert _actions(page) == []


# ---- Login, extras, done ------------------------------------------------


def test_open_at_login_is_a_switch_that_starts_on():
    page = wizard_page(LOGIN, WizardFacts(), WizardChoices())
    switch = _rows(page, "switch")[0]
    assert switch["action"] == ACTION_TOGGLE_OPEN_AT_LOGIN
    assert switch["value"] is True
    page = wizard_page(LOGIN, WizardFacts(login_item=True), WizardChoices(open_at_login=False))
    switch = _rows(page, "switch")[0]
    assert switch["value"] is False
    assert switch["hint"] == "OpenSwap already opens at login."


def test_extras_offer_the_status_line_and_the_widget():
    page = wizard_page(EXTRAS, WizardFacts(), WizardChoices())
    switch = _rows(page, "switch")[0]
    assert switch["action"] == ACTION_TOGGLE_STATUS_LINE
    assert switch["value"] is False
    assert _rows(page, "item")[0]["title"] == "Widget"
    assert _rows(page, "command") == []


def test_a_hook_for_another_openswap_says_turning_on_points_it_here():
    page = wizard_page(EXTRAS, WizardFacts(status_line="other"), WizardChoices())
    assert "another OpenSwap" in _rows(page, "switch")[0]["hint"]


def test_a_finder_install_without_the_command_gets_the_link_line():
    exe = "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap"
    facts = WizardFacts(command_on_path=False, command=exe)
    command = _rows(wizard_page(EXTRAS, facts, WizardChoices()), "command")[0]
    assert command["command"] == f"ln -s {exe} ~/.local/bin/openswap"
    # Homebrew links it already.
    facts = WizardFacts(command_on_path=False, command=exe, homebrew=True)
    assert _rows(wizard_page(EXTRAS, facts, WizardChoices()), "command") == []


def test_the_terminal_command_quotes_a_path_with_spaces():
    assert terminal_command("/Users/a b/OpenSwap.app/Contents/MacOS/OpenSwap") == (
        "ln -s '/Users/a b/OpenSwap.app/Contents/MacOS/OpenSwap' ~/.local/bin/openswap"
    )


def test_done_summarizes_the_choices_and_finishes():
    facts = WizardFacts(claude_accounts=(("a", ""),), codex_available=True)
    page = wizard_page(DONE, facts, WizardChoices(open_at_login=False, status_line=True))
    assert _rows(page, "summary")[0]["items"] == [
        ("Claude accounts", "1"),
        ("Codex accounts", "None yet"),
        ("Open at login", "Off"),
        ("Claude Code status line", "On"),
    ]
    assert page["primary"]["action"] == ACTION_FINISH
    assert page["back"] is False  # nobody goes back from "all set"


def test_every_account_button_is_an_action_the_app_knows():
    from openswap.setup_wizard import ACCOUNT_ACTIONS

    seen = set()
    for step in (CLAUDE, CODEX):
        for facts in (
            WizardFacts(codex_available=True),
            WizardFacts(codex_available=True, claude_state="error", codex_state="error"),
        ):
            seen.update(_actions(wizard_page(step, facts, WizardChoices())))
    assert seen <= set(ACCOUNT_ACTIONS)
    assert {"claude_start", "claude_add", "claude_retry", "codex_start", "codex_capture"} <= seen


def test_every_page_has_at_most_one_accent_button():
    """One filled accent button per page: the footer's primary, or the
    actions item marked primary, never both."""
    cases = [
        (MOVE, WizardFacts(temporary=True, move={"action": "move"})),
        (WELCOME, WizardFacts()), (CLAUDE, WizardFacts()),
        (CLAUDE, WizardFacts(claude_accounts=(("a", ""),))),
        (CLAUDE, WizardFacts(claude_signing_in=True)),
        (CODEX, WizardFacts(codex_available=True)),
        (LOGIN, WizardFacts()), (EXTRAS, WizardFacts()), (DONE, WizardFacts()),
    ]
    for step, facts in cases:
        page = wizard_page(step, facts, WizardChoices())
        accent = int(page["primary"] is not None) + sum(
            int(bool(item.get("primary")))
            for row in _rows(page, "actions") for item in row["items"]
        )
        assert accent == 1, (step, accent)
