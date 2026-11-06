"""The Remote tasks setup's pages drawn in the real setup window (requires the macOS menu bar extra)."""

import os
import sys

import pytest

if sys.platform != "darwin":
    pytest.skip("macOS window", allow_module_level=True)
# These open real windows: each xdist worker gets a Dock icon and steals focus.
if os.environ.get("OPENSWAP_NATIVE_UI_TESTS") != "1":
    pytest.skip("set OPENSWAP_NATIVE_UI_TESTS=1 to run native window tests", allow_module_level=True)
AppKit = pytest.importorskip("AppKit")

from openswap.menubar_wizard import WIDTH, make_setup_wizard
from openswap.worker import guided_setup
from openswap.worker.setup_window import done_page, window_page


@pytest.fixture(autouse=True)
def native_app():
    AppKit.NSApplication.sharedApplication()


def _drawn(page):
    actions = []
    wizard = make_setup_wizard(actions.append, "Set Up Remote Tasks")
    wizard.render(page)
    return wizard, wizard.window().contentView(), actions


def _buttons(view):
    found = []
    for child in view.subviews():
        found += [child] if isinstance(child, AppKit.NSButton) else _buttons(child)
    return found


def test_account_rows_are_radio_buttons_that_report_their_row():
    page = window_page(1, "pick", ("Tasks run on the account you pick.", ["Work · a@x.com · Codex", "b@x.com"]),
                       {"selected": 1}, [])
    wizard, surface, actions = _drawn(page)
    assert wizard.window().title() == "Set Up Remote Tasks"
    radios = [b for b in _buttons(surface) if getattr(b, "wizard_action", "").startswith("select:")]
    assert [b.accessibilityLabel() for b in radios] == ["Work · a@x.com · Codex", "b@x.com"]
    assert [b.state() for b in radios] == [AppKit.NSControlStateValueOff, AppKit.NSControlStateValueOn]
    radios[0].performClick_(None)
    assert actions == ["select:0"]
    close = next(b for b in _buttons(surface) if getattr(b, "wizard_action", None) == "close")
    assert close.title() == "Close Setup" and close.frame().origin.x < WIDTH / 2


def test_folder_rows_are_checkboxes_with_their_note_and_add_folder():
    page = window_page(2, "check_folders", ([("~/GitHub", "recommended")], [True]),
                       {"checked": [True], "added": []}, [])
    _wizard, surface, actions = _drawn(page)
    (box,) = [b for b in _buttons(surface) if getattr(b, "wizard_action", "").startswith("toggle:")]
    assert box.accessibilityLabel() == "~/GitHub, recommended" and box.state() == AppKit.NSControlStateValueOn
    add = next(b for b in _buttons(surface) if getattr(b, "wizard_action", None) == "add_folder")
    assert add.title() == "Add Folder…" and add.frame().origin.y < surface.footer_y


def test_a_busy_page_disables_every_button_but_close_setup():
    page = {**window_page(0, "confirm", (guided_setup.WORKER_OFFER,), {}, []), "busy": True}
    _wizard, surface, actions = _drawn(page)
    enabled = [b for b in _buttons(surface) if b.isEnabled()]
    assert len(_buttons(surface)) > 1 and [b.title() for b in enabled] == ["Close Setup"]
    enabled[0].performClick_(None)
    assert actions == ["close"]


def test_a_held_page_at_the_summary_step_draws():
    page = window_page(3, "hold", (), {}, [guided_setup.SUMMARY_FAILED])
    _wizard, surface, _actions = _drawn(page)
    assert any(getattr(b, "wizard_action", None) == "continue" for b in _buttons(surface))


def test_the_done_checklist_marks_each_row():
    state = guided_setup.Readiness(paired_url="https://x", worker="running", account=None, folders=(),
                                   execution="disabled", connection="online")
    _wizard, surface, _actions = _drawn(done_page(state))
    labels = {view.accessibilityLabel() for view in surface.subviews() if view.accessibilityLabel()}
    assert {"Worker: running, done", "Account: none, missing", "Live tasks: off"} <= labels


def test_a_long_folder_list_scrolls_and_keeps_the_footer_on_screen():
    folders = [(f"~/Code/repo{i}", "") for i in range(30)]
    page = window_page(2, "check_folders", (folders, [False] * 30), {"checked": [False] * 30, "added": []}, [])
    wizard, surface, actions = _drawn(page)
    (scroll,) = [v for v in surface.subviews() if isinstance(v, AppKit.NSScrollView)]
    assert scroll.frame().size.height < scroll.documentView().frame().size.height
    assert wizard.window().frame().size.height < 700
    boxes = [b for b in _buttons(surface) if getattr(b, "wizard_action", "").startswith("toggle:")]
    assert len(boxes) == 30
    boxes[-1].performClick_(None)
    assert actions == ["toggle:29"]


def test_a_scrolled_folder_list_keeps_its_place_and_shows_an_added_folder():
    folders = [(f"~/Code/repo{i}", "") for i in range(30)]
    page = window_page(2, "check_folders", (folders, [False] * 30), {"checked": [False] * 30, "added": []}, [])
    wizard, surface, _actions = _drawn(page)
    surface.choices_scroll.contentView().scrollToPoint_((0, 200))
    checked = [False] * 30
    checked[25] = True
    wizard.render(window_page(2, "check_folders", (folders, [False] * 30), {"checked": checked, "added": []}, []))
    scroll = wizard.window().contentView().choices_scroll
    assert scroll.contentView().bounds().origin.y == 200
    assert scroll.documentView().frame().size.width == scroll.contentSize().width
    added = {"checked": checked + [True], "added": ["~/New"]}
    wizard.render(window_page(2, "check_folders", (folders, [False] * 30), added, []))
    scroll = wizard.window().contentView().choices_scroll
    bottom = scroll.documentView().frame().size.height - scroll.contentSize().height
    assert scroll.contentView().bounds().origin.y == bottom > 200


def test_a_list_that_starts_scrolling_shows_the_added_folder_and_a_new_step_starts_at_the_top():
    folders = [(f"~/Code/repo{i}", "") for i in range(9)]
    wizard, _surface, _actions = _drawn(
        window_page(2, "check_folders", (folders, [False] * 9), {"checked": [False] * 9, "added": []}, []))
    assert getattr(wizard.window().contentView(), "choices_scroll", None) is None
    many = [f"~/New{i}" for i in range(12)]
    wizard.render(window_page(2, "check_folders", (folders, [False] * 9),
                              {"checked": [False] * 9 + [True] * 12, "added": many}, []))
    scroll = wizard.window().contentView().choices_scroll
    assert scroll.contentView().bounds().origin.y > 0
    accounts = [f"a{i}@x.com" for i in range(30)]
    wizard.render(window_page(1, "pick", ("Pick.", accounts), {"selected": None}, []))
    wizard.window().contentView().choices_scroll.contentView().scrollToPoint_((0, 300))
    wizard.render(window_page(2, "check_folders", ([(f"~/r{i}", "") for i in range(40)], [False] * 40),
                              {"checked": [False] * 40, "added": []}, []))
    assert wizard.window().contentView().choices_scroll.contentView().bounds().origin.y == 0
