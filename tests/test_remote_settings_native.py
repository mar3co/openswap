"""Settings › Remote drawn in the real panel (opt-in: OPENSWAP_NATIVE_UI_TESTS=1 on macOS)."""

import os
import sys

import pytest

if sys.platform != "darwin" or os.environ.get("OPENSWAP_NATIVE_UI_TESTS") != "1":
    pytest.skip("set OPENSWAP_NATIVE_UI_TESTS=1 on macOS", allow_module_level=True)
AppKit = pytest.importorskip("AppKit")

from openswap import menubar_panel
from openswap.menubar import SETTINGS_PAGE, SETTINGS_SECTION_REMOTE, MenuBarSettings

STATUS = {
    "process_state": "running", "provider": {"available": True}, "remote_connectivity": "online",
    "account_picker": {"pinned_account_ref": None, "codex": [], "claude": [], "allowlist": []},
    "folders": [("opensoft", "opensoft", "~/GitHub/opensoft", ""), ("veba", "veba", "~/GitHub/veba", "read only")],
    "folder_notice": "⚠ ~/Desktop: no git repos in it.",
}


def descendants(view):
    yield view
    for child in view.subviews():
        yield from descendants(child)


def make_panel(**status):
    AppKit.NSApplication.sharedApplication()
    panel = menubar_panel.MenuBarPanel(
        on_switch=None, on_rotate=None, on_best=None, on_toggle_auto=None, on_more=None,
        auto_enabled=lambda: False, snapshot=lambda: {"accounts": []}, threshold=lambda: 90,
        settings=MenuBarSettings, worker_enabled=lambda: True, worker_paused=lambda: False,
        worker_status=lambda: {**STATUS, **status}, worker_paired=lambda: True,
    )
    panel._popover = AppKit.NSPopover.alloc().init()
    panel._page = SETTINGS_PAGE
    panel._settings_section = SETTINGS_SECTION_REMOTE
    panel.reload()
    return panel


@pytest.fixture
def panel():
    panel = make_panel()
    yield panel
    panel.close()


def _labels(root):
    return {str(v.stringValue()): v for v in descendants(root)
            if isinstance(v, AppKit.NSTextField) and not v.isEditable() and v.stringValue()}


def test_remote_labels_fit_and_sections_are_drawn(panel):
    labels = _labels(panel._controller.view())
    for text in ("Remote tasks", "Account", "Folders", "Status", "Local worker", "Pause new tasks", "Setup",
                 "Tasks use", "Web choice", "opensoft", "Active job"):
        field = labels[text]
        needed = field.cell().cellSizeForBounds_(AppKit.NSMakeRect(0, 0, 10000, 100)).width
        assert needed <= field.frame().size.width + 0.5, text
    assert labels["~/GitHub/veba · read only"].toolTip() == "veba · ~/GitHub/veba · read only"
    assert labels["Running · online · idle"].accessibilityLabel() == "Status: Running · online · idle"
    assert labels["Folders"].frame().origin.y - labels["Web choice"].frame().origin.y > 40


def test_details_expand_to_the_full_status_fields(panel):
    root = panel._controller.view()
    details = next(v for v in descendants(root) if isinstance(v, AppKit.NSButton) and "Details" in str(v.title()))
    assert details.accessibilityValue() == "Collapsed"
    details.performClick_(None)
    assert any("provider available" in text for text in _labels(panel._controller.view()))


def test_a_long_remote_page_scrolls_within_the_screen():
    folders = [(f"org{n}", f"org{n}", f"~/GitHub/org{n}", "") for n in range(16)]
    panel = make_panel(folders=folders)
    try:
        panel._available_panel_height = lambda: 460
        panel.reload()
        assert panel._settings_scroll is not None
        assert panel._controller.view().frame().size.height <= 460
    finally:
        panel.close()
