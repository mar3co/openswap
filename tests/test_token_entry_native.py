"""Opt-in AppKit checks for the token form's reflow and editing lifecycle."""

import os
import sys
from unittest.mock import Mock

import pytest

if sys.platform != 'darwin' or os.environ.get('OPENSWAP_NATIVE_UI_TESTS') != '1':
    pytest.skip('set OPENSWAP_NATIVE_UI_TESTS=1 on macOS', allow_module_level=True)
AppKit = pytest.importorskip('AppKit')
from openswap import menubar_panel
from openswap.menubar import TOKEN_PAGE


def descendants(view):
    yield view
    for child in view.subviews():
        yield from descendants(child)


def button(root, text):
    return next(v for v in descendants(root) if isinstance(v, AppKit.NSButton) and text in str(v.title()))


@pytest.fixture
def form():
    AppKit.NSApplication.sharedApplication()
    snapshot = {
        'accounts': [(1, 'work@example.com', True, {}, {}, 'Work', '', False, None)],
        'kinds': {'1': 'oauth'}, 'identities': {'1': ('work@example.com', '')},
    }
    saved = Mock(return_value='Try again.')
    panel = menubar_panel.MenuBarPanel(
        on_switch=None, on_rotate=None, on_best=None, on_toggle_auto=None, on_more=None,
        auto_enabled=lambda: False, snapshot=lambda: snapshot, threshold=lambda: 90,
        on_add_token=saved,
    )
    panel._popover = AppKit.NSPopover.alloc().init()
    panel._page = TOKEN_PAGE
    panel.reload()
    yield panel, panel._controller.view(), saved, snapshot
    panel.close()


def edit(panel, value):
    panel._token_fields[0].setStringValue_(value)
    panel._token_watcher.controlTextDidChange_(None)


def test_form_reflows_without_replacing_fields_or_losing_email(form):
    panel, root, _, _ = form
    token, email = panel._token_fields
    empty_h = root.frame().size.height
    assert empty_h < 380
    assert not button(root, 'Save token').isEnabled()
    edit(panel, 'sk-ant-api03-example')
    key_h = root.frame().size.height
    assert key_h > empty_h
    assert button(root, 'Save API key').isEnabled()
    popup = next(v for v in descendants(root) if isinstance(v, AppKit.NSPopUpButton))
    popup.selectItemAtIndex_(1)
    panel._token_refresh()
    assert button(root, 'Save API credit').isEnabled()
    button(root, 'Email label').performClick_(None)
    email.setStringValue_('other@example.com')
    edit(panel, 'sk-ant-oat01-example')
    assert popup.isHidden()
    assert root.frame().size.height < key_h
    assert panel._token_fields[0] is token
    assert email.stringValue() == 'other@example.com'
    edit(panel, 'sk-ant-api03-new')
    assert popup.indexOfSelectedItem() == 0
    assert button(root, 'Save API key').isEnabled()
    button(root, 'Email label').performClick_(None)
    assert root.frame().size.height == key_h
    assert email.stringValue() == 'other@example.com'
    assert button(root, 'Email label (set)')


def test_error_is_visible_preserves_draft_and_clears_when_corrected(form):
    panel, root, saved, _ = form
    edit(panel, 'sk-ant-oat01-example')
    initial_h = root.frame().size.height
    button(root, 'Save setup token').performClick_(None)
    saved.assert_called_once_with('sk-ant-oat01-example', '', None)
    assert panel._token_fields[0].stringValue() == 'sk-ant-oat01-example'
    assert root.frame().size.height > initial_h
    assert panel._token_error == 'Try again.'
    edit(panel, 'not a token')
    assert not panel._token_error
    assert not button(root, 'Save token').isEnabled()
    edit(panel, 'sk-ant-oat01-corrected')
    assert root.frame().size.height == initial_h
    assert button(root, 'Save setup token').isEnabled()


def test_help_and_long_errors_scroll_but_save_stays_on_screen(form):
    panel, root, saved, _ = form
    panel._available_panel_height = lambda: 460
    edit(panel, 'sk-ant-api03-example')
    button(root, 'Where do I').performClick_(None)
    button(root, 'Email label').performClick_(None)
    saved.return_value = 'Could not save this credential. ' * 5
    button(root, 'Save API key').performClick_(None)
    scroll = next(v for v in root.subviews() if isinstance(v, AppKit.NSScrollView))
    assert scroll.documentView().frame().size.height > scroll.frame().size.height
    assert root.frame().size.height <= 460
    save = button(root, 'Save API key')
    assert save.superview() == root
    assert save.frame().origin.y >= scroll.frame().origin.y + scroll.frame().size.height
    assert save.frame().origin.y + save.frame().size.height < root.frame().size.height
    assert panel._token_fields[0].stringValue() == 'sk-ant-api03-example'


def test_stale_account_disables_save_until_user_chooses_again(form):
    panel, root, saved, snapshot = form
    edit(panel, 'sk-ant-api03-example')
    popup = next(v for v in descendants(root) if isinstance(v, AppKit.NSPopUpButton))
    popup.selectItemAtIndex_(1)
    panel._token_refresh()
    snapshot['identities']['1'] = ('someone-else@example.com', '')
    button(root, 'Save API credit').performClick_(None)
    saved.assert_not_called()
    assert popup.selectedItem() is None
    assert not button(root, 'Save API key').isEnabled()
    popup.selectItemAtIndex_(0)
    panel._token_refresh()
    assert button(root, 'Save API key').isEnabled()


def test_paste_reads_clipboard_only_on_click_and_does_not_submit(form, monkeypatch):
    panel, root, saved, _ = form
    clipboard = Mock()
    clipboard.stringForType_.return_value = ' sk-ant-oat01-example\n'
    pasteboard = Mock()
    pasteboard.generalPasteboard.return_value = clipboard
    monkeypatch.setattr(menubar_panel, 'NSPasteboard', pasteboard)
    edit(panel, '')
    pasteboard.generalPasteboard.assert_not_called()
    button(root, 'Paste').performClick_(None)
    assert panel._token_fields[0].stringValue() == 'sk-ant-oat01-example'
    assert button(root, 'Save setup token').isEnabled()
    saved.assert_not_called()


def test_back_and_escape_clear_sensitive_drafts(form):
    panel, root, saved, _ = form
    token, email = panel._token_fields
    edit(panel, 'sk-ant-oat01-example')
    email.setStringValue_('work@example.com')
    panel._token_watcher.control_textView_doCommandBySelector_(token, None, 'cancelOperation:')
    assert token.stringValue() == email.stringValue() == ''
    assert panel._token_refresh is None
    assert panel._page != TOKEN_PAGE
    saved.assert_not_called()
