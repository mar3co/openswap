"""Native sessions own their return; unbound URLs must never pick a profile."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap.claude_desktop_links import DesktopSignInLinks, MAX_AGE
from tests.test_claude_desktop_signin import GOOGLE, MAGIC, SSO


class Native:
    application = Path('/Applications/OpenSwap.app')
    def __init__(self, receive, launched):
        self.receive, self.launched = receive, launched
        self.owner = Path('/Applications/Claude.app')
        self.saved = self.owner
        self.denied = False
        self.requests = []
        self.forward = Mock()

    def current(self): return self.owner
    def previous(self): return self.saved
    def remember(self, path): self.saved = path
    def paused(self): return self.denied
    def pause(self, value): self.denied = value
    def set_handler(self, path, callback): self.requests.append((path, callback))
    def respond(self, ok):
        path, callback = self.requests[-1]
        if ok: self.owner = path
        callback(ok)


@pytest.fixture
def env():
    now, enabled, needed = [0.0], [True], [True]
    error, received = Mock(), Mock()
    router = DesktopSignInLinks(enabled=lambda: enabled[0], needed=lambda: needed[0],
        receive_signin=received, show_error=error, native_factory=Native, clock=lambda: now[0])
    return SimpleNamespace(router=router, native=router.native, now=now, enabled=enabled,
                           needed=needed, error=error, received=received)


def claim(e):
    e.router.prepare()
    e.now[0] += 3
    e.router.poll()
    e.native.respond(True)
    e.router.poll()


def test_register_only_after_launch_and_keep_previous_choice(env):
    e = env
    e.native.owner = Path('/Applications/Existing Router.app')
    e.router.prepare()
    e.router.poll()
    assert e.native.requests == []
    e.now[0] = 3
    e.router.poll()
    assert e.native.saved == Path('/Applications/Existing Router.app')
    assert e.native.requests[0][0] == e.native.application
    assert e.native.paused()  # uncertain/grant pending pauses automatic retries
    e.native.respond(True)
    e.router.poll()
    assert not e.native.paused()
    e.error.assert_not_called()


@pytest.mark.parametrize('condition', ['disabled', 'not_needed', 'paused', 'already_owned'])
def test_no_unnecessary_permission_dialogs(env, condition):
    e = env
    if condition == 'disabled': e.enabled[0] = False
    if condition == 'not_needed': e.needed[0] = False
    if condition == 'paused': e.native.pause(True)
    if condition == 'already_owned': e.native.owner = e.native.application
    e.router.prepare()
    e.now[0] = 3
    e.router.poll()
    assert e.native.requests == []


def test_denied_permission_stays_paused_until_explicit_retry(env):
    e = env
    e.router.prepare()
    e.now[0] = 3
    e.router.poll()
    e.native.respond(False)
    e.router.poll()
    assert e.native.paused()
    for _ in range(3):
        e.router.launched()
        e.now[0] += 3
        e.router.poll()
    assert len(e.native.requests) == 1
    e.router.prepare(retry=True)
    e.now[0] += 3
    e.router.poll()
    assert len(e.native.requests) == 2
    assert e.error.call_count == 1


def test_new_claude_instance_registering_again_reconnects_after_startup(env):
    e = env
    claim(e)
    e.native.owner = e.native.saved
    e.native.launched()
    e.now[0] += 3
    e.router.poll()
    assert len(e.native.requests) == 2


def test_quitting_restores_previous_before_completion(env):
    e = env
    claim(e)
    done = Mock()
    e.router.restore(done)
    done.assert_not_called()
    assert e.native.requests[-1][0] == e.native.saved
    e.native.respond(True)
    e.router.poll()
    done.assert_called_once()
    assert e.native.current() == e.native.saved


def test_disabling_during_os_prompt_undoes_late_grant(env):
    e = env
    e.router.prepare()
    e.now[0] = 3
    e.router.poll()
    e.enabled[0] = False
    done = Mock()
    e.router.restore(done)
    assert len(e.native.requests) == 1
    e.native.respond(True)
    e.router.poll()
    assert len(e.native.requests) == 2
    done.assert_not_called()
    e.native.respond(True)
    e.router.poll()
    done.assert_called_once()


def test_restore_does_not_overwrite_later_user_choice(env):
    e = env
    claim(e)
    e.native.owner = Path('/Applications/User Choice.app')
    done = Mock()
    e.router.restore(done)
    done.assert_called_once()
    assert len(e.native.requests) == 1


@pytest.mark.parametrize('value', [GOOGLE, MAGIC, SSO])
def test_unbound_callback_asks_without_forwarding_to_default(env, value):
    e = env
    e.native.receive(value)
    e.router.poll()
    assert e.received.call_args.args[0] == value
    assert e.received.call_args.args[1]()
    e.native.forward.assert_not_called()
    e.now[0] += MAX_AGE + 1
    assert not e.received.call_args.args[1]()


def test_queue_drops_duplicates_and_expired_and_does_not_interrupt_work(env):
    e = env
    e.router.receive(GOOGLE)
    e.router.receive(GOOGLE)
    e.router.poll(idle=False)
    e.received.assert_not_called()
    e.router.poll()
    e.router.poll()
    assert e.received.call_count == 1
    e.router.receive(MAGIC)
    e.now[0] = MAX_AGE + 1
    e.router.poll()
    assert e.received.call_count == 1
    assert GOOGLE not in repr(e.router.seen)


def test_queue_is_bounded(env):
    for n in range(100): env.router.receive(GOOGLE + '&n=' + str(n))
    assert env.router.events.qsize() == 8


@pytest.mark.parametrize('link', ['', None, 'https://claude.ai', 'file:///tmp/x', GOOGLE + '\n', GOOGLE + 'a' * 16384])
def test_invalid_inputs_are_dropped_without_logging_or_forwarding(env, link):
    e = env
    e.router.receive(link)
    e.router.poll()
    e.received.assert_not_called()
    e.native.forward.assert_not_called()
    e.error.assert_not_called()


@pytest.mark.parametrize('link', [GOOGLE.replace('&hop_nonce=fixture-nonce', ''),
    'claude://claude.ai/mcp-auth-callback/../login?code=fixture-secret',
    'claude://login/new-auth?code=fixture-secret'])
def test_unknown_auth_routes_never_fall_through_to_default(env, link):
    e = env
    e.router.receive(link)
    e.router.poll()
    e.received.assert_not_called()
    e.native.forward.assert_not_called()
    assert e.error.call_count == 1
    assert 'fixture-secret' not in repr(e.error.call_args)


def test_ordinary_links_go_to_previous_handler_without_scheme_recursion(env):
    env.router.receive('claude://claude.ai/chat/fixture')
    env.router.poll()
    env.native.forward.assert_called_once_with('claude://claude.ai/chat/fixture')
    env.received.assert_not_called()


def test_disabled_callback_cannot_modify_default_account(env):
    env.enabled[0] = False
    env.router.receive(MAGIC)
    env.router.poll()
    env.native.forward.assert_not_called()
    env.received.assert_not_called()
    env.error.assert_called_once()


def test_callback_exception_is_sanitized(env):
    env.received.side_effect = RuntimeError(GOOGLE)
    env.router.receive(GOOGLE)
    env.router.poll()
    assert GOOGLE not in repr(env.error.call_args)


def test_busy_queue_still_discards_expired_credentials(env):
    env.router.receive(GOOGLE)
    env.now[0] = MAX_AGE + 1
    env.router.poll(idle=False)
    assert env.router.events.empty()
    env.received.assert_not_called()


def test_uninstall_preserves_another_handler(monkeypatch):
    from openswap import claude_desktop_links as links
    native = Mock()
    monkeypatch.setattr(links, 'owns_system_handler', lambda: False)
    monkeypatch.setattr(links, 'MacOSLinks', native)
    assert not links.restore_for_uninstall()
    native.assert_not_called()


@pytest.mark.parametrize('success', [True, False])
def test_uninstall_restores_only_owned_registration_and_reports_denial(monkeypatch, success):
    import sys
    from openswap import claude_desktop_links as links
    from openswap.claude_desktop import ClaudeDesktopError
    n = Native(None, None)
    n.owner = n.application
    def set_handler(path, callback):
        n.owner = path if success else n.application
        callback(success)
    n.set_handler = set_handler
    monkeypatch.setattr(links, 'owns_system_handler', lambda: True)
    monkeypatch.setattr(links, 'MacOSLinks', lambda *args: n)
    monkeypatch.setitem(sys.modules, 'Foundation', SimpleNamespace(NSDate=Mock(), NSRunLoop=Mock()))
    if success:
        assert links.restore_for_uninstall()
        assert n.owner == n.saved
    else:
        with pytest.raises(ClaudeDesktopError, match='restore Claude links'):
            links.restore_for_uninstall()


@pytest.mark.parametrize('owner', ['self', 'other_copy', 'claude', 'none'])
def test_cold_start_handover_guard_uses_exact_registered_bundle(monkeypatch, owner):
    import sys
    from openswap import claude_desktop_links as links
    bundle = SimpleNamespace(bundleIdentifier=lambda: 'com.opensoft.openswap.dev',
                             bundlePath=lambda: '/Applications/OpenSwap.app')
    paths = {'self': '/Applications/OpenSwap.app', 'other_copy': '/other/OpenSwap.app',
             'claude': '/Applications/Claude.app'}
    target = None if owner == 'none' else SimpleNamespace(path=lambda: paths[owner])
    workspace = SimpleNamespace(URLForApplicationToOpenURL_=lambda url: target)
    monkeypatch.setitem(sys.modules, 'AppKit', SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)))
    monkeypatch.setitem(sys.modules, 'Foundation', SimpleNamespace(NSBundle=SimpleNamespace(mainBundle=lambda: bundle),
        NSURL=SimpleNamespace(URLWithString_=lambda value: value)))
    assert links.owns_system_handler() is (owner == 'self')


def test_nested_native_event_loop_cannot_open_two_choosers(env):
    e = env
    seen = []
    def receive(value, fresh):
        seen.append(value)
        if len(seen) == 1:
            e.router.receive(MAGIC)
            e.router.poll()
            assert seen == [GOOGLE]
    e.router.receive_signin = receive
    e.router.receive(GOOGLE)
    e.router.poll()
    assert seen == [GOOGLE]
    e.router.poll()
    assert seen == [GOOGLE, MAGIC]


@pytest.mark.parametrize('eligible', [True, False])
def test_native_registers_bundle_and_verifies_eligibility_before_request(monkeypatch, eligible):
    import sys
    from openswap import claude_desktop_links as links
    from openswap.claude_desktop import ClaudeDesktopError
    calls = []
    path = Path('/Applications/OpenSwap.app')
    url = SimpleNamespace(path=lambda: str(path))
    def candidates(_url):
        calls.append('candidates')
        return [url] if eligible else []
    workspace = SimpleNamespace(URLsForApplicationsToOpenURL_=candidates,
        setDefaultApplicationAtURL_toOpenURLsWithScheme_completionHandler_=lambda *args: calls.append('request'))
    native = object.__new__(links.MacOSLinks)
    native.workspace = workspace
    monkeypatch.setattr(links, 'register_application', lambda value: calls.append(('register', value)))
    monkeypatch.setitem(sys.modules, 'Foundation', SimpleNamespace(NSURL=SimpleNamespace(
        URLWithString_=lambda value: value, fileURLWithPath_=lambda value: value)))
    if eligible:
        native.set_handler(path, Mock())
        assert calls == [('register', path), 'candidates', 'request']
    else:
        with pytest.raises(ClaudeDesktopError, match='Move OpenSwap to Applications'):
            native.set_handler(path, Mock())
        assert calls == [('register', path), 'candidates']


@pytest.mark.parametrize('reason', ['success', 'native_failure', 'no_url'])
def test_native_registration_releases_cf_url_and_never_changes_default(monkeypatch, reason):
    import ctypes
    from openswap import claude_desktop_links as links
    from openswap.claude_desktop import ClaudeDesktopError
    cf, ls = Mock(), Mock()
    cf.CFURLCreateFromFileSystemRepresentation.return_value = None if reason == 'no_url' else 123
    ls.LSRegisterURL.return_value = -50 if reason == 'native_failure' else 0
    monkeypatch.setattr(ctypes, 'CDLL', lambda name: cf if 'CoreFoundation' in name else ls)
    if reason == 'success':
        links.register_application(Path('/Applications/OpenSwap.app'))
    else:
        with pytest.raises(ClaudeDesktopError): links.register_application(Path('/Applications/OpenSwap.app'))
    if reason == 'no_url':
        cf.CFRelease.assert_not_called()
        ls.LSRegisterURL.assert_not_called()
    else:
        cf.CFRelease.assert_called_once_with(123)
        ls.LSRegisterURL.assert_called_once_with(123, True)
    ls.LSSetDefaultHandlerForURLScheme.assert_not_called()


def test_registration_failure_is_explained_without_mislabeling_permission(env):
    from openswap.claude_desktop import ClaudeDesktopError
    e = env
    e.native.set_handler = Mock(side_effect=ClaudeDesktopError('Move OpenSwap to Applications'))
    e.router.prepare()
    e.now[0] += 3
    e.router.poll()
    e.error.assert_called_once_with('Move OpenSwap to Applications')
    assert not e.router.changing
    assert e.native.paused()
    e.router.poll()
    assert e.native.set_handler.call_count == 1
