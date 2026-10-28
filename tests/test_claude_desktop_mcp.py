"""MCP returns use explicit profile delivery, never the shared Claude handler."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import claude_desktop_signin as callbacks
from openswap import claude_desktop_menu as ui
from openswap.claude_desktop import ClaudeDesktopError
from tests.test_claude_desktop import app, store, menu
from tests.test_claude_desktop_links import env
from tests.test_claude_desktop_signin import process_list, GOOGLE

MCP = 'claude://claude.ai/mcp-auth-callback/sdk?code=fixture-mcp-secret&state=fixture-state'
FINISH = 'claude://claude.ai/mcp-auth-callback/11111111-1111-4111-8111-111111111111?step=finish'
DENIED = 'claude://claude.ai/mcp-auth-callback/sdk?error=access_denied&state=fixture-state'


@pytest.mark.parametrize('value', [MCP, FINISH, DENIED, MCP + '&scope=read%3Aorg%20read%3Aproject'])
def test_connector_callbacks_preserve_payload_and_use_profile_dispatch(env, value):
    assert callbacks.callback_link(value) == (value, 'connector')
    with pytest.raises(ClaudeDesktopError): callbacks.signin_link(value)
    env.router.receive(value)
    env.router.poll()
    assert env.received.call_args.args[0] == value
    env.native.forward.assert_not_called()
    env.error.assert_not_called()


@pytest.mark.parametrize('value', [
    MCP.replace('claude.ai', 'evil.test'), MCP.replace('claude.ai', 'user@claude.ai'),
    MCP.replace('claude.ai', 'claude.ai:443'), MCP.replace('/sdk', '/'),
    MCP.replace('/sdk', '/sdk/../../login'), MCP.replace('/sdk', '/%2e%2e'),
    MCP.replace('/sdk', '/%252e%252e'), MCP.replace('/sdk', '/sdk%2fother'),
    MCP.replace('/sdk', '//sdk'), MCP.replace('/sdk', '/sdk;other'),
    MCP + '#credential', MCP + '&code=duplicate', MCP + '\\other', MCP + '\nextra',
    MCP + '&' + '&'.join(f'p{i}=1' for i in range(40)), MCP + 'x' * 16384,
])
def test_malformed_connector_links_never_forward_or_expose_credentials(env, value):
    with pytest.raises(ClaudeDesktopError) as exc: callbacks.callback_link(value)
    assert 'fixture-mcp-secret' not in str(exc.value)
    env.router.receive(value)
    env.router.poll()
    env.native.forward.assert_not_called()
    env.received.assert_not_called()
    assert 'fixture-mcp-secret' not in repr(env.error.call_args)


def test_connection_return_selects_secondary_while_default_is_running(menu, store, app, monkeypatch):
    store.default_desktop.mkdir(parents=True)
    default = store.ensure_default()
    profile = store.add('Work')
    process_list(app, store, [(33, profile)])
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.monitor = Mock(poll=Mock(return_value=False))
    menu.prompt.return_value = SimpleNamespace(clicked=1, choice=1)
    monkeypatch.setattr(ui.threading, 'Thread', lambda **kw: SimpleNamespace(start=kw['target']))
    focus = Mock()
    monkeypatch.setattr(ui, 'focus_profile', focus)
    saved = store.registry.read_bytes()
    menu.receive_signin(MCP, lambda: True)
    assert menu.prompt.call_args.kwargs['choices'] == [default.name, 'Work']
    assert menu.prompt.call_args.kwargs['title'] == 'Finish connecting app'
    assert MCP not in repr(menu.prompt.call_args)
    menu.poll()
    menu.signin_sender.assert_called_once_with(33, MCP, app.binary)
    focus.assert_called_once_with(33)
    menu.monitor.refresh.assert_not_called()
    menu.notify.assert_called_once_with('Connection link sent', 'Check the connection in Work.')
    assert store.registry.read_bytes() == saved
    app.popen.assert_not_called()


@pytest.mark.parametrize('change', ['cancel', 'expired', 'archived', 'closed', 'disabled', 'restarted'])
def test_connector_selection_rechecks_target_and_expiry(menu, store, app, monkeypatch, change):
    profile = store.add('Work')
    process_list(app, store, [(33, profile)])
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.monitor = Mock(poll=Mock(return_value=False))
    fresh = [True]
    def choose(**kwargs):
        if change == 'expired': fresh[0] = False
        if change == 'archived': store.update(profile.id, archived=True)
        if change == 'closed': process_list(app, store, [])
        if change == 'disabled': menu.enabled = lambda: False
        if change == 'restarted': process_list(app, store, [(44, profile)])
        return SimpleNamespace(clicked=0 if change == 'cancel' else 1, choice=0)
    menu.prompt.side_effect = choose
    monkeypatch.setattr(ui.threading, 'Thread', lambda **kw: SimpleNamespace(start=kw['target']))
    menu.receive_signin(MCP, lambda: fresh[0])
    menu.poll()
    menu.signin_sender.assert_not_called()
    app.popen.assert_not_called()
    assert 'fixture-mcp-secret' not in repr(menu.show_error.call_args)


def test_connector_callbacks_expire_and_deduplicate_like_signin(env):
    env.router.receive(MCP)
    env.router.receive(MCP)
    env.router.poll(idle=False)
    env.received.assert_not_called()
    env.router.poll()
    env.router.poll()
    assert env.received.call_count == 1
    env.router.receive(FINISH)
    env.now[0] += 121
    env.router.poll()
    assert env.received.call_count == 1


def test_google_still_classified_as_signin():
    assert callbacks.callback_link(GOOGLE) == (GOOGLE, 'signin')
