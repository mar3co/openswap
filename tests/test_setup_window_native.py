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


def _buttons(surface):
    return [view for view in surface.subviews() if isinstance(view, AppKit.NSButton)]


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
    assert add.title() == "Add folder…" and add.frame().origin.y < surface.footer_y


def test_a_busy_page_disables_every_button():
    page = {**window_page(0, "confirm", (guided_setup.WORKER_OFFER,), {}, []), "busy": True}
    _wizard, surface, _actions = _drawn(page)
    assert _buttons(surface) and not any(b.isEnabled() for b in _buttons(surface))


def test_the_done_checklist_marks_each_row():
    state = guided_setup.Readiness(paired_url="https://x", worker="running", account=None, folders=(),
                                   execution="disabled", connection="online")
    _wizard, surface, _actions = _drawn(done_page(state))
    labels = {view.accessibilityLabel() for view in surface.subviews() if view.accessibilityLabel()}
    assert {"Worker: running, done", "Account: none, missing", "Live tasks: off"} <= labels
