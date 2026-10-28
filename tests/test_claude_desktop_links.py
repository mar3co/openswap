"""Native sessions own their return; unbound URLs must never pick a profile."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from openswap.claude_desktop_links import (
    DesktopSignInLinks, MAX_AGE, OWNERSHIP_CHECK, OWNERSHIP_LOST, PAUSED, RECHECK, SETUP_FAILED, RESTORE_TIMEOUT, claim_step, ordinary_link, ownership_lost)
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
def env(tmp_path):
    now, enabled, needed = [0.0], [True], [True]
    error, received = Mock(), Mock()
    router = DesktopSignInLinks(enabled=lambda: enabled[0], needed=lambda: needed[0],
        receive_signin=received, show_error=error, native_factory=Native, clock=lambda: now[0])
    # The previous handler must exist on disk for a handback to be attempted.
    claude = tmp_path / 'Claude.app'
    claude.mkdir()
    router.native.owner = router.native.saved = claude
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


def test_new_claude_instance_registering_again_is_reported_not_reclaimed_to_avoid_repeated_prompts(env):
    e = env
    claim(e)
    e.native.owner = e.native.saved
    e.native.launched()
    e.now[0] += 3
    e.router.poll()
    assert len(e.native.requests) == 1
    e.error.assert_called_once_with(OWNERSHIP_LOST)
    assert e.native.paused()
    e.now[0] += OWNERSHIP_CHECK
    e.router.poll()
    e.error.assert_called_once()
    e.router.prepare(retry=True)
    e.now[0] += 3
    e.router.poll()
    assert len(e.native.requests) == 2
    e.error.assert_called_once()


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


@pytest.mark.parametrize('link, ordinary', [
    ('claude://claude.ai/chat/fixture', True),
    ('claude://claude.ai/chat/%zz%ff%', True),
    ('claude://login/new-auth?code=fixture-secret', False),
    ('claude://claude.ai/m%61gic-link#fixture:fixture-secret', False),
    ('claude://%6Cogin/new-auth?code=fixture-secret', False),
    ('claude://claude.ai/%4C%4F%47%49%4E/%zz', False)])
def test_auth_routes_are_recognized_after_percent_decoding(link, ordinary):
    assert ordinary_link(link) is ordinary


def test_repeated_ordinary_link_is_forwarded_each_time_but_a_repeated_callback_is_not(env):
    e = env
    for _ in range(2):
        e.router.receive('claude://claude.ai/chat/fixture')
        e.router.poll()
    assert e.native.forward.call_count == 2
    for _ in range(2):
        e.router.receive(SSO)
        e.router.poll()
    assert e.received.call_count == 1


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


@pytest.mark.parametrize('outcome', ['restored', 'denied', 'refused', 'claude_gone'])
def test_uninstall_restores_only_owned_registration_and_reports_denial(monkeypatch, tmp_path, outcome):
    import sys
    from openswap import claude_desktop_links as links
    from openswap.claude_desktop import ClaudeDesktopError
    n = Native(None, None)
    n.owner = n.application
    n.saved = tmp_path / 'Claude.app'
    if outcome != 'claude_gone':
        n.saved.mkdir()
    def set_handler(path, callback):
        if outcome == 'refused':
            raise ClaudeDesktopError('Could not restore Claude links. Open Claude once to restore its link handling, or reinstall Claude.')
        n.owner = path if outcome == 'restored' else n.application
        callback(outcome == 'restored')
    n.set_handler = Mock(side_effect=set_handler)
    monkeypatch.setattr(links, 'owns_system_handler', lambda: True)
    monkeypatch.setattr(links, 'MacOSLinks', lambda *args: n)
    monkeypatch.setitem(sys.modules, 'Foundation', SimpleNamespace(NSDate=Mock(), NSRunLoop=Mock()))
    if outcome == 'restored':
        assert links.restore_for_uninstall()
        assert n.owner == n.saved
    elif outcome == 'claude_gone':
        assert links.restore_for_uninstall() is False
        n.set_handler.assert_not_called()
    else:
        with pytest.raises(ClaudeDesktopError, match='restore Claude links') as error:
            links.restore_for_uninstall()
        assert ('reinstall Claude' in str(error.value)) is (outcome == 'refused')


@pytest.mark.parametrize('owner', ['self', 'other_copy', 'claude', 'none', 'not_frozen'])
def test_cold_start_handover_guard_uses_exact_registered_bundle(monkeypatch, tmp_path, owner):
    import sys
    from openswap import bundle, claude_desktop_links as links
    app = tmp_path / 'OpenSwap.app'
    monkeypatch.setattr(bundle, 'app_path', lambda: None if owner == 'not_frozen' else app)
    paths = {'self': str(app), 'other_copy': '/other/OpenSwap.app', 'claude': '/Applications/Claude.app'}
    target = None if owner in ('none', 'not_frozen') else SimpleNamespace(path=lambda: paths[owner])
    workspace = SimpleNamespace(URLForApplicationToOpenURL_=lambda url: target)
    monkeypatch.setitem(sys.modules, 'AppKit', SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda: workspace)))
    monkeypatch.setitem(sys.modules, 'Foundation', SimpleNamespace(NSURL=SimpleNamespace(URLWithString_=lambda value: value)))
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


@pytest.mark.parametrize('target', ['eligible', 'openswap', 'claude', 'openswap_unregistered', 'claude_unregistered'])
def test_native_registers_bundle_and_verifies_eligibility_before_request(monkeypatch, target):
    import sys
    from openswap import claude_desktop_links as links
    from openswap.claude_desktop import ClaudeDesktopError
    calls = []
    native = object.__new__(links.MacOSLinks)
    native.application = Path('/Applications/OpenSwap.app').resolve()
    path = Path('/Applications/Claude.app') if target.startswith('claude') else native.application
    url = SimpleNamespace(path=lambda: str(path))
    def candidates(_url):
        calls.append('candidates')
        return [url] if target == 'eligible' else []
    def register(value):
        calls.append(('register', value))
        if target.endswith('unregistered'):
            raise ClaudeDesktopError('macOS could not register OpenSwap for sign-in. Reopen OpenSwap and try again.')
    native.workspace = SimpleNamespace(URLsForApplicationsToOpenURL_=candidates,
        setDefaultApplicationAtURL_toOpenURLsWithScheme_completionHandler_=lambda *args: calls.append('request'))
    monkeypatch.setattr(links, 'register_application', register)
    monkeypatch.setitem(sys.modules, 'Foundation', SimpleNamespace(NSURL=SimpleNamespace(
        URLWithString_=lambda value: value, fileURLWithPath_=lambda value: value)))
    if target == 'eligible':
        native.set_handler(path, Mock())
        assert calls == [('register', path), 'candidates', 'request']
        return
    remedy = {'openswap': 'Move OpenSwap to Applications', 'openswap_unregistered': 'Reopen OpenSwap'}.get(
        target, 'Open Claude once.*or reinstall Claude')
    with pytest.raises(ClaudeDesktopError, match=remedy) as error:
        native.set_handler(path, Mock())
    assert ('OpenSwap' in str(error.value)) is target.startswith('openswap')
    assert calls == [('register', path)] + ([] if target.endswith('unregistered') else ['candidates'])


@pytest.mark.parametrize('outcome', ['restored', 'denied', 'refused', 'no_answer'])
def test_quit_always_finishes_whatever_the_restore_does(env, outcome):
    from openswap.claude_desktop import ClaudeDesktopError
    e = env
    claim(e)
    if outcome == 'refused':
        e.native.set_handler = Mock(side_effect=ClaudeDesktopError('Could not restore Claude links. Reinstall Claude.'))
    done = Mock()
    e.router.restore(done)
    if outcome == 'refused':
        done.assert_called_once()
        e.error.assert_called_once_with('Could not restore Claude links. Reinstall Claude.')
        assert not e.router.changing
        return
    done.assert_not_called()
    if outcome == 'no_answer':
        e.now[0] += RESTORE_TIMEOUT - 1
        e.router.poll()
        done.assert_not_called()
        e.now[0] += 1
    else:
        e.native.respond(outcome == 'restored')
    e.router.poll()
    done.assert_called_once()
    assert e.error.call_count == (1 if outcome == 'denied' else 0)


def test_quit_during_a_pending_claim_is_bounded_from_the_quit_itself(env):
    e = env
    e.router.prepare()
    e.now[0] = 3
    e.router.poll()
    done = Mock()
    e.router.restore(done)
    e.now[0] = 3 + RESTORE_TIMEOUT - 2
    e.native.respond(True)
    e.router.poll()
    assert e.native.requests[-1][0] == e.native.saved
    done.assert_not_called()
    e.now[0] += 2
    e.router.poll()
    done.assert_called_once()
    e.error.assert_not_called()


def test_failed_restore_leaves_the_coordinator_usable(env):
    e = env
    claim(e)
    e.native.set_handler = Mock(side_effect=RuntimeError('native failure'))
    done = Mock()
    e.router.restore(done)
    done.assert_called_once()
    assert not e.router.changing
    again = Mock()
    e.router.restore(again)
    again.assert_called_once()
    # Stopped by the quit: a Claude launch alone must not claim again.
    e.native.set_handler = Mock()
    e.router.launched()
    e.now[0] += 3
    e.router.poll()
    e.native.set_handler.assert_not_called()
    e.router.prepare()
    e.router.receive(GOOGLE)
    e.now[0] += 3
    e.router.poll()
    e.received.assert_called_once()


def test_handler_returns_when_no_profile_needs_links_and_comes_back_when_one_does(env):
    e = env
    claim(e)
    e.needed[0] = False
    e.router.poll()
    e.now[0] += RECHECK
    e.router.poll()
    assert e.native.requests[-1][0] == e.native.saved
    e.native.respond(True)
    e.router.poll()
    assert e.native.current() == e.native.saved
    e.error.assert_not_called()
    e.needed[0] = True
    e.now[0] += RECHECK
    e.router.poll()
    e.now[0] += 2
    e.router.poll()
    assert e.native.requests[-1][0] == e.native.application
    assert len(e.native.requests) == 3


def test_handler_returns_after_a_claude_launch_reclaims_it_too(env):
    e = env
    claim(e)
    e.needed[0] = False
    e.now[0] += RECHECK
    e.router.poll()
    e.native.respond(True)
    e.router.poll()
    e.needed[0] = True
    e.router.launched()
    e.now[0] += 2
    e.router.poll()
    assert [path for path, _ in e.native.requests] == [e.native.application, e.native.saved, e.native.application]


def test_unknown_profile_state_changes_nothing(env):
    e = env
    claim(e)
    e.router.needed = Mock(side_effect=OSError('registry unreadable'))
    e.now[0] += RECHECK
    e.router.poll()
    e.router.launched()
    e.now[0] += 3
    e.router.poll()
    assert len(e.native.requests) == 1
    assert e.native.current() == e.native.application
    e.error.assert_not_called()


def test_a_refused_handback_is_quiet_and_not_retried(env):
    e = env
    claim(e)
    e.needed[0] = False
    e.now[0] += RECHECK
    e.router.poll()
    e.native.respond(False)
    e.router.poll()
    e.error.assert_not_called()
    e.now[0] += RECHECK
    e.router.poll()
    assert len(e.native.requests) == 2


def test_a_background_handback_that_cannot_register_claude_is_quiet(env):
    from openswap.claude_desktop import ClaudeDesktopError
    e = env
    claim(e)
    e.native.set_handler = Mock(side_effect=ClaudeDesktopError('Could not restore Claude links. Reinstall Claude.'))
    e.needed[0] = False
    e.now[0] += RECHECK
    e.router.poll()
    e.error.assert_not_called()
    assert not e.router.changing
    e.now[0] += RECHECK
    e.router.poll()
    assert e.native.set_handler.call_count == 1


def test_an_explicit_restore_waiting_on_a_background_handback_still_alerts(env):
    e = env
    claim(e)
    e.needed[0] = False
    e.now[0] += RECHECK
    e.router.poll()
    done = Mock()
    e.router.restore(done)
    assert len(e.native.requests) == 2
    e.native.respond(False)
    e.router.poll()
    e.error.assert_called_once_with('Could not restore Claude links. Open Claude once before signing in again.')
    done.assert_called_once()


@pytest.mark.parametrize('route', ['background', 'quit', 'disable'])
def test_no_handback_is_attempted_when_the_previous_handler_is_gone(env, tmp_path, route):
    e = env
    claim(e)
    e.native.saved = tmp_path / 'Gone.app'
    done = Mock()
    if route == 'background':
        e.needed[0] = False
        e.now[0] += RECHECK
        e.router.poll()
    elif route == 'quit':
        e.router.restore(done)
        done.assert_called_once()
    else:
        e.router.restore()
    assert len(e.native.requests) == 1
    assert e.native.current() == e.native.application
    e.error.assert_not_called()
    assert not e.router.changing


def test_startup_handback_still_forwards_a_queued_ordinary_link_and_holds_callbacks(env):
    e = env
    e.native.owner = e.native.application
    e.needed[0] = False
    e.router.prepare()
    e.router.receive('claude://claude.ai/chat/fixture')
    e.router.poll()
    assert e.native.requests[-1][0] == e.native.saved
    e.native.forward.assert_called_once_with('claude://claude.ai/chat/fixture')
    e.router.receive(GOOGLE)
    e.now[0] += MAX_AGE + 1
    e.router.receive('claude://claude.ai/chat/later')
    e.router.poll()
    e.received.assert_not_called()
    assert e.native.forward.call_count == 2
    e.native.respond(True)
    e.router.poll()
    e.received.assert_not_called()  # the callback aged out while the sheet waited; nothing stale is sent
    e.router.receive(MAGIC)
    e.router.poll()
    e.received.assert_called_once()
    e.error.assert_not_called()


def test_quit_forwards_queued_ordinary_links_but_drops_callbacks(env):
    e = env
    claim(e)
    e.router.receive('claude://claude.ai/chat/fixture')
    e.router.receive(GOOGLE)
    done = Mock()
    e.router.restore(done)
    e.native.forward.assert_called_once_with('claude://claude.ai/chat/fixture')
    e.native.respond(True)
    e.router.poll()
    done.assert_called_once()
    e.received.assert_not_called()
    assert e.router.events.empty()


@pytest.mark.parametrize('route', ['restored', 'declined', 'raised', 'deadline'])
def test_a_finished_quit_sends_no_second_request_and_no_further_alert(env, route):
    from openswap.claude_desktop import ClaudeDesktopError
    e = env
    claim(e)
    if route == 'raised':
        e.native.set_handler = Mock(side_effect=ClaudeDesktopError('Could not restore Claude links. Reinstall Claude.'))
    # Quitting suspends the Desktop section, which asks for a restore again.
    done = Mock(side_effect=lambda: e.router.restore())
    e.router.restore(done)
    if route == 'deadline':
        e.now[0] += RESTORE_TIMEOUT
        e.router.poll()
    elif route != 'raised':
        e.native.respond(route == 'restored')
        e.router.poll()
    done.assert_called_once()
    requests = len(e.native.requests) if route != 'raised' else e.native.set_handler.call_count
    assert requests == (1 if route == 'raised' else 2)
    alerts = e.error.call_count
    assert alerts == (1 if route in ('declined', 'raised') else 0)
    if route == 'deadline':
        e.native.respond(False)  # the sheet answered after the quit went ahead
    e.now[0] += RECHECK
    e.router.poll()
    e.router.restore()
    assert e.error.call_count == alerts
    assert (len(e.native.requests) if route != 'raised' else e.native.set_handler.call_count) == requests


@pytest.mark.parametrize('reason', ['disabled', 'not_needed'])
def test_startup_returns_a_handler_kept_from_an_earlier_session(env, reason):
    e = env
    e.native.owner = e.native.application
    (e.enabled if reason == 'disabled' else e.needed)[0] = False
    e.router.prepare()
    e.router.poll()
    assert e.native.requests == [(e.native.saved, e.native.requests[0][1])]
    e.native.respond(True)
    e.router.poll()
    e.now[0] += RECHECK
    e.router.poll()
    assert len(e.native.requests) == 1


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


OPENSWAP, CLAUDE = Path('/Applications/OpenSwap.app'), Path('/Applications/Claude.app')


def gone():
    raise OSError('Launch Services unavailable')


@pytest.mark.parametrize('held, ended, changing, needed, current, lost', [
    (True, False, False, True, lambda: CLAUDE, True),
    (True, False, False, True, lambda: OPENSWAP, False),
    (True, False, False, True, lambda: None, None),
    (True, False, False, True, gone, None),
    (None, False, False, True, lambda: CLAUDE, False),
    (False, False, False, True, lambda: CLAUDE, False),
    (True, True, False, True, lambda: CLAUDE, False),
    (True, True, True, True, lambda: CLAUDE, False),
    (True, False, True, True, lambda: CLAUDE, None),
    (True, False, False, False, lambda: CLAUDE, False),
    (True, False, False, None, lambda: CLAUDE, None)])
def test_ownership_is_lost_only_when_a_held_handler_is_taken_while_needed(held, ended, changing, needed, current, lost):
    assert ownership_lost(held=held, ended=ended, changing=changing, needed=needed,
                          current=current, application=OPENSWAP) is lost


@pytest.mark.parametrize('current, held, reconnecting, paused, noticed, step', [
    (OPENSWAP, None, False, False, False, 'held'),
    (OPENSWAP, True, True, False, False, 'held'),
    (OPENSWAP, None, False, True, False, 'held'),
    (CLAUDE, None, False, False, False, 'claim'),
    (CLAUDE, False, False, False, False, 'claim'),
    (CLAUDE, True, True, False, False, 'claim'),
    (None, True, True, False, False, 'claim'),
    (CLAUDE, True, False, False, False, 'lost'),
    (None, True, False, False, False, 'wait'),
    (None, None, False, False, False, 'claim'),
    (CLAUDE, None, False, True, False, 'notice'),
    (None, None, False, True, False, 'notice'),
    (CLAUDE, True, True, True, False, 'notice'),
    (CLAUDE, None, False, True, True, 'paused'),
    (CLAUDE, True, True, True, True, 'paused')])
def test_only_reconnect_claims_a_handler_taken_after_it_was_held(current, held, reconnecting, paused, noticed, step):
    assert claim_step(current=current, application=OPENSWAP, held=held, reconnecting=reconnecting,
                      paused=paused, noticed=noticed) == step


def take_over(e):
    e.native.owner = e.native.saved
    e.now[0] += OWNERSHIP_CHECK
    e.router.poll()


def relaunch(e):
    e.native.launched()
    e.now[0] += OWNERSHIP_CHECK
    e.router.poll()


@pytest.mark.parametrize('start', ['claimed', 'already_owned'])
def test_lost_handler_is_reported_once_and_only_reconnect_claims_it_again(env, start):
    e = env
    if start == 'claimed':
        claim(e)
    else:
        e.native.owner = e.native.application
        e.router.prepare()
        e.now[0] += 3
        e.router.poll()
    requests = len(e.native.requests)
    take_over(e)
    e.error.assert_called_once_with(OWNERSHIP_LOST)
    for _ in range(3):
        relaunch(e)
    assert len(e.native.requests) == requests
    e.error.assert_called_once()
    e.router.prepare(retry=True)
    e.now[0] += 3
    e.router.poll()
    e.native.respond(True)
    e.router.poll()
    take_over(e)
    assert e.error.call_count == 2


def test_a_kept_handler_is_not_reported(env):
    e = env
    claim(e)
    relaunch(e)
    e.error.assert_not_called()


def test_a_declined_claim_reports_only_the_claim_failure(env):
    e = env
    e.router.prepare()
    e.now[0] += 3
    e.router.poll()
    e.native.respond(False)
    take_over(e)
    e.error.assert_called_once()
    assert e.error.call_args.args[0] != OWNERSHIP_LOST


@pytest.mark.parametrize('state', ['claim_pending', 'disabled', 'not_needed', 'quitting'])
def test_no_loss_check_while_the_handler_is_in_flux_or_unneeded(env, state):
    e = env
    claim(e)
    e.native.launched()
    if state == 'claim_pending':
        e.native.owner = e.native.saved
        e.router.prepare(retry=True)
        e.now[0] += 3
        e.router.poll()
        assert e.router.changing
    if state == 'disabled': e.enabled[0] = False
    if state == 'not_needed': e.needed[0] = False
    if state == 'quitting': e.router.restore(Mock())
    e.native.owner = e.native.saved
    e.now[0] += OWNERSHIP_CHECK
    e.router.poll()
    assert OWNERSHIP_LOST not in [call.args[0] for call in e.error.call_args_list]


def test_a_failed_reconnect_claim_shows_only_its_own_error(env):
    from openswap.claude_desktop import ClaudeDesktopError
    e = env
    claim(e)
    e.native.owner = e.native.saved
    e.native.set_handler = Mock(side_effect=ClaudeDesktopError('Move OpenSwap to Applications'))
    e.router.prepare(retry=True)
    e.now[0] += 3
    e.router.poll()
    e.now[0] += OWNERSHIP_CHECK
    e.router.poll()
    e.error.assert_called_once_with('Move OpenSwap to Applications')


@pytest.mark.parametrize('obstacle', ['needed_unknown', 'handler_unreadable'])
def test_a_loss_check_that_cannot_run_yet_waits_and_then_reports(env, obstacle):
    e = env
    claim(e)
    e.native.owner = e.native.saved
    if obstacle == 'needed_unknown':
        e.router.needed = Mock(side_effect=OSError('registry unreadable'))
    else:
        e.native.current = Mock(side_effect=OSError('Launch Services unavailable'))
    e.now[0] += OWNERSHIP_CHECK
    e.router.poll()
    e.router.poll()
    e.error.assert_not_called()
    assert len(e.native.requests) == 1
    assert e.router.verify_at is not None
    if obstacle == 'needed_unknown':
        e.router.needed = lambda: e.needed[0]
    else:
        del e.native.current
    e.router.poll()
    e.error.assert_called_once_with(OWNERSHIP_LOST)
    assert len(e.native.requests) == 1


@pytest.mark.parametrize('owner, notice', [('claude', True), ('openswap', False)])
def test_an_unknown_owner_after_holding_waits_instead_of_claiming(env, owner, notice):
    e = env
    claim(e)
    e.native.owner = None
    e.native.launched()
    e.now[0] += 3
    e.router.poll()
    e.now[0] += OWNERSHIP_CHECK
    e.router.poll()
    assert len(e.native.requests) == 1
    e.error.assert_not_called()
    assert e.router.verify_at is not None
    e.native.owner = e.native.saved if owner == 'claude' else e.native.application
    e.router.poll()
    assert len(e.native.requests) == 1
    assert e.error.call_args_list == ([call(OWNERSHIP_LOST)] if notice else [])
    assert e.router.verify_at is None


def take_over_unnoticed(e):
    claim(e)
    e.native.owner = e.native.saved
    e.router.verify_at = None


@pytest.mark.parametrize('blocked', ['at_the_claim', 'when_chosen'])
def test_a_reconnect_the_profile_store_holds_up_claims_once_the_store_answers(env, blocked):
    e = env
    take_over_unnoticed(e)
    store = e.router.needed
    if blocked == 'when_chosen':
        e.router.needed = Mock(side_effect=OSError('registry unreadable'))
    e.router.prepare(retry=True)
    e.router.needed = Mock(side_effect=OSError('registry unreadable'))
    e.now[0] += 3
    e.router.poll()
    assert len(e.native.requests) == 1
    e.router.needed = store
    e.now[0] += RECHECK
    e.router.poll()
    e.now[0] += 3
    e.router.poll()
    assert len(e.native.requests) == 2
    e.native.respond(True)
    e.router.poll()
    e.error.assert_not_called()


def test_a_reconnect_the_profile_store_never_answers_reports_the_setup_failure(env):
    e = env
    take_over_unnoticed(e)
    e.router.needed = Mock(side_effect=OSError('registry unreadable'))
    e.router.prepare(retry=True)
    for _ in range(3):
        e.now[0] += RECHECK
        e.router.poll()
    e.error.assert_called_once_with(SETUP_FAILED)
    assert len(e.native.requests) == 1


def test_a_pause_after_a_reconnect_cancels_it_with_only_its_own_notice(env):
    e = env
    take_over_unnoticed(e)
    store = e.router.needed
    e.router.needed = Mock(side_effect=OSError('registry unreadable'))
    e.router.prepare(retry=True)
    e.router._lost()
    e.router.needed = store
    e.now[0] += RECHECK
    e.router.poll()
    relaunch(e)
    assert len(e.native.requests) == 1
    e.error.assert_called_once_with(OWNERSHIP_LOST)


@pytest.mark.parametrize('state, notice', [
    ('taken', True), ('missing', True), ('owned', False), ('disabled', False), ('not_needed', False)])
def test_a_pause_from_an_earlier_run_is_explained_once_and_never_claims(env, state, notice):
    e = env
    e.native.pause(True)
    if state == 'missing': e.native.owner = None
    if state == 'owned': e.native.owner = e.native.application
    if state == 'disabled': e.enabled[0] = False
    if state == 'not_needed': e.needed[0] = False
    e.router.prepare()
    e.now[0] += 3
    e.router.poll()
    for _ in range(3):
        relaunch(e)
    assert e.native.requests == []
    assert e.error.call_args_list == ([((PAUSED,),)] if notice else [])
