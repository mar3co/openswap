"""The setup wizard's controller in the menu bar app (plan 021).

No rumps or AppKit: the methods are lifted out of ``MenuBarApp`` with
``tests.menubar_harness`` and the window is a recording stand-in.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar, setup_wizard
from openswap.exceptions import ClaudeSwitchError
from tests.menubar_harness import extract_class

METHODS = {
    "_open_setup_wizard",
    "_wizard_page",
    "_wizard_render",
    "_wizard_cards",
    "_codex_signing_in",
    "_wizard_account_signature",
    "_wizard_facts",
    "_on_wizard_action",
    "_wizard_tick",
    "_wizard_move_app",
    "_wizard_finish",
    "_has_codex",
}


class _Window:
    def __init__(self):
        self.pages = []
        self.visible = False
        self.closed = 0

    def show(self, page):
        self.pages.append(page)
        self.visible = True

    def render(self, page):
        self.pages.append(page)

    def close(self):
        self.visible = False
        self.closed += 1

    def is_visible(self):
        return self.visible


def _row(num, email, alias=""):
    return (num, email, False, None, None, alias, "", False, None)


@pytest.fixture
def wiz(monkeypatch, tmp_path):
    window = _Window()
    fake = types.ModuleType("openswap.menubar_wizard")
    fake.make_setup_wizard = lambda on_action: window
    monkeypatch.setitem(sys.modules, "openswap.menubar_wizard", fake)
    scope = dict(vars(menubar))
    scope["resolve_claude_bin"] = lambda: "/usr/local/bin/claude"
    app = extract_class(menubar.__file__, "MenuBarApp", METHODS, scope)()
    events = []
    app.settings = menubar.MenuBarSettings()
    app.switcher = SimpleNamespace(
        backup_dir=tmp_path,
        _logger=SimpleNamespace(
            debug=lambda *a, **k: None, info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
        ),
    )
    app.snapshot = {"accounts": []}
    app.codex = None
    app._account_states = {"claude": "ready", "chatgpt": "ready"}
    app._claude_login = None
    app._login_ui_state = {"stage": "idle"}
    app._login_item_busy = False
    app._wizard = None
    app._wizard_step = None
    app._wizard_choices = None
    app._wizard_move = None
    app._wizard_seen = None
    app._login_item_runs_this = lambda: False
    app._status_line_state = lambda: "off"
    app._on_empty_action = lambda provider, action: events.append(("empty", provider, action))
    app._on_welcome_continue = lambda open_at_login: events.append(("continue", open_at_login))
    app._show_error = lambda message: events.append(("error", message))
    app.show_popover = lambda: events.append("show_popover")
    app.on_quit = lambda _sender: events.append("quit")
    return SimpleNamespace(app=app, window=window, events=events)


def _page(w):
    return w.window.pages[-1]


def test_opening_draws_welcome_and_starts_with_fresh_choices(wiz):
    wiz.app._open_setup_wizard()
    assert wiz.app._wizard_step == setup_wizard.WELCOME
    assert _page(wiz)["title"] == "Welcome to OpenSwap"
    assert wiz.window.visible
    assert wiz.app._wizard_choices == setup_wizard.WizardChoices(open_at_login=True, status_line=False)


def test_opening_on_a_step_that_does_not_apply_starts_at_the_first(wiz):
    wiz.app._open_setup_wizard(setup_wizard.CODEX)  # no Codex here
    assert wiz.app._wizard_step == setup_wizard.WELCOME


def test_next_and_back_walk_the_steps(wiz):
    wiz.app._open_setup_wizard()
    wiz.app._on_wizard_action("next")
    assert _page(wiz)["step"] == setup_wizard.CLAUDE
    wiz.app._on_wizard_action("next")
    assert _page(wiz)["step"] == setup_wizard.LOGIN  # no Codex step
    wiz.app._on_wizard_action("back")
    assert _page(wiz)["step"] == setup_wizard.CLAUDE


def test_codex_gets_its_step_when_the_provider_is_there(wiz):
    wiz.app.codex = object()
    wiz.app._open_setup_wizard(setup_wizard.CLAUDE)
    wiz.app._on_wizard_action("next")
    assert _page(wiz)["step"] == setup_wizard.CODEX


@pytest.mark.parametrize("action, expected", [
    ("claude_start", ("claude", "start")),
    ("claude_add", ("claude", "add")),
    ("claude_retry", ("claude", "retry")),
    ("codex_start", ("chatgpt", "start")),
    ("codex_capture", ("chatgpt", "capture")),
])
def test_account_buttons_are_the_popovers_empty_state_actions(wiz, action, expected):
    wiz.app._open_setup_wizard(setup_wizard.CLAUDE)
    drawn = len(wiz.window.pages)
    wiz.app._on_wizard_action(action)
    assert wiz.events == [("empty", *expected)]
    assert len(wiz.window.pages) == drawn + 1  # redrawn


def test_switches_flip_the_choices(wiz):
    wiz.app._open_setup_wizard(setup_wizard.LOGIN)
    wiz.app._on_wizard_action("toggle_open_at_login")
    assert wiz.app._wizard_choices.open_at_login is False
    assert _page(wiz)["rows"][0]["value"] is False
    wiz.app._on_wizard_action("toggle_status_line")
    assert wiz.app._wizard_choices.status_line is True


def test_close_hides_it_and_changes_nothing(wiz):
    wiz.app._open_setup_wizard()
    wiz.app._on_wizard_action("close")
    assert not wiz.window.visible
    assert wiz.app._wizard_step is None
    assert wiz.events == []
    wiz.app._on_wizard_action("next")  # a stray click after closing
    assert wiz.app._wizard_step is None


def test_the_tick_redraws_an_account_step_only_when_it_changed(wiz):
    wiz.app._open_setup_wizard(setup_wizard.CLAUDE)
    drawn = len(wiz.window.pages)
    wiz.app._wizard_tick()
    assert len(wiz.window.pages) == drawn
    wiz.app.snapshot = {"accounts": [_row("1", "you@example.com")]}
    wiz.app._wizard_tick()
    assert len(wiz.window.pages) == drawn + 1
    assert _page(wiz)["title"] == "Your Claude accounts"
    wiz.app._wizard_tick()
    assert len(wiz.window.pages) == drawn + 1
    # A sign-in that started shows too.
    wiz.app._claude_login = object()
    wiz.app._wizard_tick()
    assert len(wiz.window.pages) == drawn + 2


def test_the_tick_leaves_other_steps_and_a_closed_window_alone(wiz):
    wiz.app._wizard_tick()  # never opened
    wiz.app._open_setup_wizard(setup_wizard.LOGIN)
    drawn = len(wiz.window.pages)
    wiz.app.snapshot = {"accounts": [_row("1", "you@example.com")]}
    wiz.app._wizard_tick()
    assert len(wiz.window.pages) == drawn


def test_finish_applies_the_status_line_then_login_and_opens_the_popover(wiz, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "openswap.statusline.set_installed",
        lambda config_home, backup, enable: calls.append(enable),
    )
    monkeypatch.setattr("openswap.paths.get_claude_config_home", lambda: Path("/c"))
    wiz.app._open_setup_wizard(setup_wizard.EXTRAS)
    wiz.app._on_wizard_action("toggle_status_line")
    wiz.app._on_wizard_action("finish")
    assert calls == [True]
    assert wiz.events == [("continue", True), "show_popover"]
    assert not wiz.window.visible
    assert wiz.app._wizard_step is None


def test_finish_leaves_a_status_line_already_as_chosen(wiz, monkeypatch):
    calls = []
    monkeypatch.setattr("openswap.statusline.set_installed", lambda *a: calls.append(a))
    wiz.app._status_line_state = lambda: "on"
    wiz.app._open_setup_wizard(setup_wizard.DONE)
    wiz.app._on_wizard_action("finish")
    assert calls == []


def test_finish_waits_for_a_login_item_change_before_the_popover(wiz):
    wiz.app._on_welcome_continue = lambda open_at_login: setattr(wiz.app, "_login_item_busy", True)
    wiz.app._open_setup_wizard(setup_wizard.DONE)
    wiz.app._on_wizard_action("finish")
    # _drain_login_item_result opens it (or hands over) when the change lands.
    assert "show_popover" not in wiz.events


def test_a_failed_status_line_is_reported_and_setup_still_finishes(wiz, monkeypatch):
    def refuse(*a):
        raise ClaudeSwitchError("bad hook")

    monkeypatch.setattr("openswap.statusline.set_installed", refuse)
    monkeypatch.setattr("openswap.paths.get_claude_config_home", lambda: Path("/c"))
    wiz.app._open_setup_wizard(setup_wizard.EXTRAS)
    wiz.app._on_wizard_action("toggle_status_line")
    wiz.app._on_wizard_action("finish")
    assert wiz.events[0] == ("error", "Couldn't change the Claude Code status line: bad hook")
    assert ("continue", True) in wiz.events


# ---- Move ---------------------------------------------------------------


@pytest.fixture
def temporary(wiz, frozen_app, monkeypatch):
    from openswap import bundle

    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
    plan = {
        "action": "move", "source": "disk_image", "eject": Path("/Volumes/OpenSwap"),
        "destination": "/Applications", "target": "/Applications/OpenSwap.app",
        "existing_version": None, "homebrew": False,
    }
    monkeypatch.setattr(bundle, "move_plan", lambda app, dest: dict(plan))
    monkeypatch.setattr(bundle, "applications_dir", lambda: Path("/Applications"))
    copies, relaunches = [], []

    def copy(app, plan):
        copies.append((app, plan["action"]))
        return Path(plan["target"])

    monkeypatch.setattr(bundle, "copy_to_applications", copy)
    monkeypatch.setattr(
        bundle, "relaunch_after_exit", lambda app, pid, eject=None: relaunches.append((app, eject)),
    )
    return SimpleNamespace(plan=plan, copies=copies, relaunches=relaunches, app=frozen_app)


def test_a_temporary_copy_opens_on_move(wiz, temporary):
    wiz.app._open_setup_wizard()
    assert wiz.app._wizard_step == setup_wizard.MOVE
    assert _page(wiz)["primary"]["title"] == "Move to Applications"


def test_move_copies_reopens_from_applications_and_quits(wiz, temporary):
    wiz.app._open_setup_wizard()
    wiz.app._on_wizard_action("move")
    assert temporary.copies == [(temporary.app, "move")]
    assert temporary.relaunches == [(Path("/Applications/OpenSwap.app"), Path("/Volumes/OpenSwap"))]
    assert wiz.events == ["quit"]
    assert not wiz.window.visible


def test_open_existing_reopens_that_copy_without_copying(wiz, temporary, monkeypatch):
    from openswap import bundle

    temporary.plan.update(action="open_existing", homebrew=True)
    monkeypatch.setattr(bundle, "move_plan", lambda app, dest: dict(temporary.plan))
    wiz.app._open_setup_wizard()
    wiz.app._on_wizard_action("open_existing")
    assert temporary.copies == []
    assert temporary.relaunches == [(Path("/Applications/OpenSwap.app"), Path("/Volumes/OpenSwap"))]
    assert wiz.events == ["quit"]


def test_a_failed_move_is_reported_and_nothing_quits(wiz, temporary, monkeypatch):
    from openswap import bundle

    def fail(app, plan):
        raise ClaudeSwitchError("Could not copy OpenSwap to /Applications: disk full")

    monkeypatch.setattr(bundle, "copy_to_applications", fail)
    wiz.app._open_setup_wizard()
    wiz.app._on_wizard_action("move")
    assert wiz.events == [("error", "Could not copy OpenSwap to /Applications: disk full")]
    assert temporary.relaunches == []
    assert wiz.app._wizard_step == setup_wizard.MOVE  # still there to retry


def test_an_unplannable_move_says_why(wiz, temporary, monkeypatch):
    from openswap import bundle

    def broken(app, dest):
        raise OSError("hdiutil exploded")

    monkeypatch.setattr(bundle, "move_plan", broken)
    wiz.app._open_setup_wizard()
    page = _page(wiz)
    assert page["primary"] is None
    assert "hdiutil exploded" in " ".join(r.get("text", "") for r in page["rows"])


# ---- facts --------------------------------------------------------------


def test_facts_come_from_the_same_sources_as_the_popover(wiz, frozen_app, monkeypatch):
    from openswap import bundle

    monkeypatch.setattr(bundle, "installed_by_homebrew", lambda home=None: False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    wiz.app.snapshot = {"accounts": [_row("1", "you@example.com", alias="work")]}
    wiz.app._account_states = {"claude": "ready", "chatgpt": "loading"}
    wiz.app._login_ui_state = {"stage": "starting"}
    wiz.app.codex = object()
    facts = wiz.app._wizard_facts()
    assert facts.claude_accounts and facts.claude_accounts[0][0] == "work"
    assert facts.codex_available is True
    assert facts.codex_state == "loading"
    assert facts.codex_signing_in is True
    assert facts.claude_installed is True
    assert facts.temporary is False
    assert facts.widget is True
    assert facts.command_on_path is False
    assert facts.command == str(frozen_app / "Contents" / "MacOS" / "OpenSwap")


def test_the_command_counts_as_on_path_only_when_it_is_this_app(wiz, frozen_app, monkeypatch):
    from openswap import bundle

    monkeypatch.setattr(bundle, "installed_by_homebrew", lambda home=None: False)
    exe = frozen_app / "Contents" / "MacOS" / "OpenSwap"
    monkeypatch.setattr("shutil.which", lambda name: str(exe))
    assert wiz.app._wizard_facts().command_on_path is True
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/openswap")
    assert wiz.app._wizard_facts().command_on_path is False
