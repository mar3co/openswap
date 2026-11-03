"""Token form validation and submission, without native UI or real credentials."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import menubar
from tests.menubar_harness import extract_class


@pytest.mark.parametrize('text', [
    'sk-ant-api', 'sk-ant-oat', 'sk-ant-api03-one two',
    'sk-ant-api03-one\nsk-ant-api03-two', '"sk-ant-oat01-example"',
    'ANTHROPIC_API_KEY=sk-ant-api03-example', 'https://example.com/sk-ant-api03-x',
])
def test_rejects_incomplete_keys_and_accidental_multi_value_pastes(text):
    state = menubar.token_entry_state(text, [], {})
    assert not state['can_add']
    assert state['kind'] is None
    assert text not in state['hint']


def test_detection_describes_billing_and_save_without_claiming_verification():
    key = menubar.token_entry_state('sk-ant-api03-example', [], {})
    setup = menubar.token_entry_state('sk-ant-oat01-example', [], {})
    assert key['button'] == 'Save API key'
    assert 'Console billing' in key['hint']
    assert setup['button'] == 'Save setup token'
    assert 'subscription login' in setup['hint']
    assert not setup['show_parent']


def field(value):
    result = Mock()
    result.stringValue.side_effect = lambda: result.value
    result.setStringValue_.side_effect = lambda v: setattr(result, 'value', v)
    result.value = value
    result.window.return_value = None
    return result


def panel():
    cls = extract_class(Path(menubar.__file__).with_name('menubar_panel.py'), 'MenuBarPanel', {
        '_submit_token', '_show_token_error', '_release_token_field', '_on_global_click',
    }, {
        'token_entry_state': menubar.token_entry_state,
        'parent_link_choice': menubar.parent_link_choice,
        'STALE_PARENT': menubar.STALE_PARENT,
        'TOKEN_PAGE': menubar.TOKEN_PAGE,
    })
    p = cls()
    p._snapshot = lambda: {'identities': {'1': ('work@example.com', 'org')}}
    p._on_add_token = Mock(return_value=None)
    p._show_main = Mock()
    p._token_refresh = Mock()
    p._fill_parent_popup = Mock()
    return p


def controls(token='sk-ant-api03-example', picked=('', '', '')):
    popup = Mock()
    popup.selectedItem.return_value = None if picked is None else SimpleNamespace(representedObject=lambda:picked)
    return field(token), field(' me@example.com '), popup, Mock(), Mock()


def test_save_trims_input_preserves_identity_binding_and_confirms_success():
    p = panel()
    fields = controls(' sk-ant-api03-example\n', ('1','work@example.com','org'))
    p._submit_token(*fields)
    p._on_add_token.assert_called_once_with('sk-ant-api03-example', 'me@example.com', ('work@example.com', 'org'))
    assert fields[0].value == ''
    assert p._token_notice == 'API credit saved. Your account is linked.'
    p._show_main.assert_called_once()


def test_save_failure_keeps_draft_and_reflows_error_for_retry():
    p = panel()
    p._on_add_token.return_value = 'Could not save this token.'
    fields = controls()
    p._submit_token(*fields)
    assert fields[0].value == 'sk-ant-api03-example'
    assert fields[1].value == ' me@example.com '
    assert p._token_error == 'Could not save this token.'
    p._token_refresh.assert_called_once_with(clear_error=False)
    p._show_main.assert_not_called()


def test_changed_parent_requires_a_new_explicit_choice():
    p = panel()
    fields = controls(picked=('1', 'old@example.com', 'old-org'))
    p._submit_token(*fields)
    p._on_add_token.assert_not_called()
    p._fill_parent_popup.assert_called_once()
    fields[2].selectItemAtIndex_.assert_called_once_with(-1)
    assert 'account list changed' in p._token_error
    fields[2].selectedItem.return_value = None
    p._submit_token(*fields)
    p._on_add_token.assert_not_called()
    assert 'Choose an account' in p._token_error


def test_setup_token_never_carries_a_hidden_parent_choice():
    p = panel()
    p._submit_token(*controls('sk-ant-oat01-example', ('1', 'work@example.com', 'org')))
    p._on_add_token.assert_called_once_with('sk-ant-oat01-example', 'me@example.com', None)


def test_invalid_paste_cannot_submit():
    p = panel()
    p._submit_token(*controls('sk-ant-api03-first second'))
    p._on_add_token.assert_not_called()
    p._show_main.assert_not_called()


def test_leaving_the_form_clears_both_fields_and_callbacks():
    p = panel()
    fields = (field('sk-ant-oat01-example'), field('me@example.com'))
    p._token_fields = fields
    p._release_token_field()
    assert all(f.value == '' for f in fields)
    assert p._token_fields == ()
    assert p._token_refresh is None
    assert p._token_watcher is None


def test_switching_to_another_app_does_not_discard_the_form():
    p = panel()
    p.is_shown = lambda: True
    p._menu_open = False
    p._page = menubar.TOKEN_PAGE
    p.close = Mock()
    p._on_global_click(Mock())
    p.close.assert_not_called()
