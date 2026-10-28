"""Explicit callback delivery must never fall back to another Claude instance."""

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import claude_desktop_signin as signin
from openswap import claude_desktop_menu as ui
from openswap.claude_desktop import BUNDLE_ID, ClaudeDesktopError
from tests.test_claude_desktop import app, store, menu, Item


GOOGLE = "claude://login/google-auth?code=fixture-secret&hop_nonce=fixture-nonce"
MAGIC = "claude://claude.ai/magic-link?anon_id=fixture#fixture:fixture-secret"
SSO = "claude://claude.ai/sso-callback?code=fixture-secret&state=fixture-state"


@pytest.mark.parametrize("link", [GOOGLE, MAGIC, SSO])
def test_accept_only_complete_signin_routes_without_rewriting(link):
    assert signin.signin_link(" " + link + "\n") == link


@pytest.mark.parametrize("link", [
    None, "", "https://claude.ai/login", "file:///tmp/file", "javascript:alert(1)",
    "claude://code/new?q=run", "claude://claude.ai/chat/123", "claude://claude.ai/mcp-auth-callback/sdk?code=x",
    GOOGLE.replace("login", "evil.test"), GOOGLE.replace("login", "user@login"), GOOGLE.replace("login", "login:42"),
    GOOGLE.replace("google-auth", "google-auth/"), GOOGLE.replace("google-auth", "%67oogle-auth"),
    GOOGLE + "#extra", GOOGLE + "&code=second", GOOGLE.replace("&hop_nonce=fixture-nonce", ""),
    GOOGLE.replace("code=fixture-secret", "code="), GOOGLE + "&x=bad\nvalue", GOOGLE + "&x=\\path",
    GOOGLE + "&x=" + "a" * 16384, MAGIC.replace("#fixture:fixture-secret", "#fixture"),
    MAGIC.replace("#fixture:fixture-secret", "#fixture:"), SSO.replace("&state=fixture-state", ""),
    "claude://claude.ai/sso-callback/ios?code=fixture-secret&state=s",
])
def test_invalid_urls_are_refused_without_echoing_credentials(link):
    with pytest.raises(ClaudeDesktopError) as error:
        signin.signin_link(link)
    assert str(error.value) == signin.INVALID_LINK


def process_list(app, store, profiles):
    lines = [f"11 {app.binary}"]
    for pid, profile in profiles:
        lines.append(f"{pid} {app.binary} --user-data-dir={store.paths(profile)[0]}")
    app.run.return_value.stdout = "\n".join(lines)


def test_link_goes_to_selected_profile_with_default_and_other_profile_running(store, app):
    a, b = store.add("A"), store.add("B")
    process_list(app, store, [(22, a), (33, b)])
    before = store.registry.read_bytes()
    sender = Mock()
    result = signin.finish_signin(store, b.id, GOOGLE, app=app, sender=sender)
    sender.assert_called_once_with(33, GOOGLE, app.binary)
    assert result["state"] == "signin_sent" and result["pid"] == 33
    assert "fixture-secret" not in repr(result)
    assert store.registry.read_bytes() == before
    app.popen.assert_not_called()
    assert all(GOOGLE not in repr(call) for call in app.run.call_args_list)


@pytest.mark.parametrize("reason", ["closed", "ambiguous", "archived", "conflicting_launch", "updated", "untrusted"])
def test_no_fallback_to_default_and_no_launch_on_invalid_target(store, app, reason):
    profile = store.add("Work")
    process_list(app, store, [] if reason == "closed" else [(33, profile)])
    if reason == "ambiguous":
        process_list(app, store, [(33, profile), (44, profile)])
    if reason == "archived": store.update(profile.id, archived=True)
    if reason == "conflicting_launch": app.run.return_value.stdout += " --unexpected-flag"
    if reason == "untrusted": app.run.return_value.returncode = 1
    if reason == "updated":
        validate = app.validate
        def changed():
            result = validate()
            (app.path / "Contents/Resources/app.asar").write_text("changed")
            return result
        app.validate = changed
    sender = Mock()
    with pytest.raises(ClaudeDesktopError):
        signin.finish_signin(store, profile.id, GOOGLE, app=app, sender=sender)
    sender.assert_not_called()
    app.popen.assert_not_called()


def test_invalid_link_does_not_inspect_apps_or_profiles(store, app):
    with pytest.raises(ClaudeDesktopError):
        signin.finish_signin(store, "unknown", "https://evil.test", app=app)
    app.run.assert_not_called()
    assert not store.root.exists()


def test_send_runs_outside_the_registry_lock(store, app):
    p = store.add("Work")
    process_list(app, store, [(33, p)])

    def sender(pid, value, binary):
        with store.lock():
            pass

    assert signin.finish_signin(store, p.id, GOOGLE, app=app, sender=sender)["state"] == "signin_sent"


def test_native_exception_cannot_expose_callback(store, app):
    profile = store.add("Work")
    process_list(app, store, [(33, profile)])
    with pytest.raises(ClaudeDesktopError) as error:
        signin.finish_signin(store, profile.id, GOOGLE, app=app, sender=Mock(side_effect=RuntimeError(GOOGLE)))
    assert "fixture-secret" not in str(error.value)


@pytest.fixture
def native(monkeypatch, tmp_path):
    binary = tmp_path / "Claude.app/Contents/MacOS/Claude"
    target = Mock()
    target.isTerminated.return_value = False
    target.bundleIdentifier.return_value = BUNDLE_ID
    target.executableURL.return_value.path.return_value = str(binary)
    running = Mock(return_value=target)
    descriptors = Mock()
    event = descriptors.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_.return_value
    reply = Mock()
    reply.paramDescriptorForKeyword_.return_value = None
    event.sendEventWithOptions_timeout_error_.return_value = reply, None
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSRunningApplication=SimpleNamespace(runningApplicationWithProcessIdentifier_=running)))
    monkeypatch.setitem(sys.modules, "Foundation", SimpleNamespace(NSAppleEventDescriptor=descriptors))
    return SimpleNamespace(binary=binary, target=target, running=running, descriptors=descriptors, event=event, reply=reply)


def test_native_event_targets_pid_and_does_not_use_shared_handler(native):
    n = native
    signin.send_to_process(33, GOOGLE, n.binary)
    n.running.assert_called_once_with(33)
    n.descriptors.descriptorWithProcessIdentifier_.assert_called_once_with(33)
    n.descriptors.descriptorWithString_.assert_called_once_with(GOOGLE)
    n.descriptors.appleEventWithEventClass_eventID_targetDescriptor_returnID_transactionID_.assert_called_once_with(
        signin.fourcc("GURL"), signin.fourcc("GURL"), n.descriptors.descriptorWithProcessIdentifier_.return_value, -1, 0)
    n.event.sendEventWithOptions_timeout_error_.assert_called_once_with(0x13, 5.0, None)


@pytest.mark.parametrize("reason", ["closed", "terminated", "wrong_bundle", "wrong_executable", "no_executable"])
def test_native_process_reuse_and_substitution_are_refused(native, reason):
    n = native
    if reason == "closed": n.running.return_value = None
    if reason == "terminated": n.target.isTerminated.return_value = True
    if reason == "wrong_bundle": n.target.bundleIdentifier.return_value = "other.app"
    if reason == "wrong_executable": n.target.executableURL.return_value.path.return_value = "/other/Claude"
    if reason == "no_executable": n.target.executableURL.return_value = None
    with pytest.raises(ClaudeDesktopError): signin.send_to_process(33, GOOGLE, n.binary)
    n.descriptors.descriptorWithString_.assert_not_called()


@pytest.mark.parametrize("reason", ["timeout", "consent", "no_reply", "handler_error"])
def test_native_delivery_failure_never_retries_or_claims_signin(native, reason):
    n = native
    if reason == "timeout": n.event.sendEventWithOptions_timeout_error_.return_value = None, Mock(code=Mock(return_value=-1712))
    if reason == "consent": n.event.sendEventWithOptions_timeout_error_.return_value = None, Mock(code=Mock(return_value=-1743))
    if reason == "no_reply": n.event.sendEventWithOptions_timeout_error_.return_value = None, None
    if reason == "handler_error": n.reply.paramDescriptorForKeyword_.return_value = Mock(int32Value=Mock(return_value=-1708))
    with pytest.raises(ClaudeDesktopError) as error: signin.send_to_process(33, GOOGLE, n.binary)
    assert "fixture-secret" not in str(error.value)
    assert ("Automation" in str(error.value)) is (reason in ("timeout", "consent"))
    n.event.sendEventWithOptions_timeout_error_.assert_called_once()


@pytest.mark.parametrize("code", [-1743, -1744, -1712, -600, None])
def test_consent_problems_name_the_automation_setting_and_a_restart(code):
    message = signin.delivery_error(code)
    consent = code in (-1743, -1744, -1712)
    assert ("System Settings → Privacy & Security → Automation" in message) is consent
    assert ("restart sign-in" in message) is consent
    assert ("try again" in message) is not consent


def test_unbound_callback_asks_for_profile_without_exposing_link_or_claiming_auth(menu, store, app, monkeypatch):
    p = store.add("Work")
    process_list(app, store, [(33, p)])
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.monitor = Mock()
    menu.monitor.poll.return_value = False
    menu.prompt.return_value = SimpleNamespace(clicked=1, choice=0)
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    focus = Mock()
    monkeypatch.setattr(ui, "focus_profile", focus)
    menu.receive_signin(GOOGLE, lambda: True)
    menu.prompt.assert_not_called()
    menu.poll()
    # One candidate is preselected; the chooser never offers a placeholder then.
    assert menu.prompt.call_args.kwargs["choices"] == ["Work"] and menu.prompt.call_args.kwargs["selected"] == 0
    assert GOOGLE not in repr(menu.prompt.call_args)
    assert menu.busy
    menu.poll()
    menu.signin_sender.assert_called_once_with(33, GOOGLE, app.binary)
    menu.monitor.refresh.assert_called_once_with(p.id)
    focus.assert_called_once_with(33)
    menu.notify.assert_called_once_with("Sign-in link sent", "Check sign-in in Work.")
    assert not menu.busy and not store.get(p.id).detected_at


@pytest.mark.parametrize("reason", ["cancel", "invalid", "busy", "disabled", "blocked", "archived", "expired", "closed"])
def test_menu_guards_do_not_send_or_launch(menu, store, app, reason, monkeypatch):
    p = store.add("Work")
    process_list(app, store, [] if reason == "closed" else [(33, p)])
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.prompt.return_value = SimpleNamespace(clicked=0 if reason == "cancel" else 1, choice=0)
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    if reason == "busy": menu.busy = True
    if reason == "disabled": menu.enabled = lambda: False
    if reason == "blocked": menu.blocked = lambda: True
    if reason == "archived": store.update(p.id, archived=True)
    menu.receive_signin("invalid" if reason == "invalid" else GOOGLE, lambda: reason != "expired")
    menu.poll()
    menu.poll()
    menu.signin_sender.assert_not_called()
    app.popen.assert_not_called()
    if reason == "closed":
        assert menu.show_error.call_args.args[0] == "Open the intended Claude profile and start again there."


def test_discovery_runs_off_the_main_thread_and_holds_further_links(menu, store, app, monkeypatch):
    p = store.add("Work")
    process_list(app, store, [(33, p)])
    menu._app_factory = lambda: app
    menu.signin_links = Mock(stopping=False)
    menu.monitor = Mock(poll=Mock(return_value=False))
    started = []
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: started.append(kw["target"])))
    menu.receive_signin(GOOGLE, lambda: True)
    assert menu.busy and menu.operation == "Checking open Claude profiles…"
    app.run.assert_not_called()
    menu.prompt.assert_not_called()
    menu.poll()
    assert menu.signin_links.poll.call_args.kwargs == {"idle": False}
    started.pop()()
    ps_calls = [c for c in app.run.call_args_list if c.args[0][0] == "/bin/ps"]
    assert len(ps_calls) == 1 and GOOGLE not in repr(app.run.call_args_list)
    menu.prompt.return_value = SimpleNamespace(clicked=0, choice=0)
    menu.poll()
    assert menu.prompt.call_args.kwargs["choices"] == ["Work"]
    assert not menu.busy and not menu._editing_details
    menu.poll()
    assert menu.signin_links.poll.call_args.kwargs == {"idle": True}


@pytest.mark.parametrize("failing", ["one", "all"])
def test_profile_whose_process_check_fails_is_skipped_not_fatal(menu, store, app, monkeypatch, failing):
    a, b = store.add("A"), store.add("B")
    lines = [f"11 {app.binary}", f"33 {app.binary} --user-data-dir={store.paths(b)[0]}",
             f"34 {app.binary} --user-data-dir={store.paths(b)[0]}"]
    if failing == "all":
        lines += [f"22 {app.binary} --user-data-dir={store.paths(a)[0]}", f"23 {app.binary} --user-data-dir={store.paths(a)[0]}"]
    else:
        lines.append(f"22 {app.binary} --user-data-dir={store.paths(a)[0]}")
    app.run.return_value.stdout = "\n".join(lines)
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.monitor = Mock(poll=Mock(return_value=False))
    menu.prompt.return_value = SimpleNamespace(clicked=0, choice=0)
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    menu.receive_signin(GOOGLE, lambda: True)
    menu.poll()
    if failing == "all":
        menu.prompt.assert_not_called()
        assert menu.show_error.call_args.args[0].startswith("Could not check the open Claude profiles.")
    else:
        assert menu.prompt.call_args.kwargs["choices"] == ["A"]
        menu.show_error.assert_not_called()
    assert not menu.busy


def test_unregistered_default_still_offers_managed_profiles_but_never_preselects(menu, store, app, monkeypatch):
    p = store.add("Work")
    process_list(app, store, [(33, p)])
    store.default_desktop = store.root.parent / "not-a-folder"
    store.default_desktop.write_text("")  # ensure_default raises: the ordinary profile cannot be read safely
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.monitor = Mock(poll=Mock(return_value=False))
    menu.prompt.return_value = SimpleNamespace(clicked=1, choice=1)
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    monkeypatch.setattr(ui, "focus_profile", Mock())
    menu.receive_signin(GOOGLE, lambda: True)
    menu.poll()
    assert menu.prompt.call_args.kwargs["choices"] == ["Choose a profile…", "Work"]
    assert "existing Claude profile could not be checked" in menu.prompt.call_args.kwargs["message"]
    menu.poll()
    menu.signin_sender.assert_called_once_with(33, GOOGLE, app.binary)
    menu.show_error.assert_not_called()


@pytest.mark.parametrize("route", ["error", "cancel", "suspend"])
def test_held_callback_is_released_however_discovery_ends(menu, store, app, monkeypatch, route):
    p = store.add("Work")
    process_list(app, store, [(33, p)])
    if route == "error":
        app.validate = Mock(side_effect=ClaudeDesktopError("Reinstall the official app."))
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.monitor = Mock(poll=Mock(return_value=False))
    menu.prompt.return_value = SimpleNamespace(clicked=0, choice=0)
    started = []
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=lambda: started.append(kw["target"])))
    menu.receive_signin(GOOGLE, lambda: True)
    assert menu._signin == (GOOGLE, menu._signin[1])
    if route == "suspend":
        menu.suspend_checks()
        assert menu._signin is None
    started.pop()()
    menu.poll()
    assert menu._signin is None and not menu.busy
    menu.signin_sender.assert_not_called()
    assert menu.prompt.call_count == (1 if route == "cancel" else 0)
    assert menu.show_error.call_count == (1 if route == "error" else 0)


@pytest.mark.parametrize("answers", [[(1, 0), (1, 2)], [(1, 0), (0, 0)]])
def test_several_candidates_start_unselected_and_the_placeholder_reprompts(menu, store, app, monkeypatch, answers):
    a, b = store.add("A"), store.add("B")
    process_list(app, store, [(22, a), (33, b)])
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.monitor = Mock(poll=Mock(return_value=False))
    menu.prompt.side_effect = [SimpleNamespace(clicked=clicked, choice=choice) for clicked, choice in answers]
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    monkeypatch.setattr(ui, "focus_profile", Mock())
    menu.receive_signin(GOOGLE, lambda: True)
    menu.poll()
    assert menu.prompt.call_count == 2
    for call in menu.prompt.call_args_list:
        assert call.kwargs["choices"] == ["Choose a profile…", "A", "B"] and call.kwargs["selected"] == 0
    menu.poll()
    if answers[-1][0] == 1:
        menu.signin_sender.assert_called_once_with(33, GOOGLE, app.binary)
    else:
        menu.signin_sender.assert_not_called()
    assert not menu.busy


def test_fallback_profile_menu_exposes_reconnect_without_paste(menu, store):
    store.add("Work")
    profile = next(x for x in menu.menu(SimpleNamespace(MenuItem=Item)).children if x.title == "Profile: Work")
    assert "Reconnect sign-in…" in [x.title for x in profile.children]
    assert "Finish sign-in…" not in [x.title for x in profile.children]


def test_reconnect_is_an_explicit_permission_retry(menu, store):
    p = store.add("Work")
    menu.signin_links = Mock()
    menu.open = Mock()
    menu.perform("reconnect_signin", p.id)
    menu.signin_links.prepare.assert_called_once_with(retry=True)
    menu.open.assert_called_once_with(p.id)
    menu.prompt.assert_not_called()


def test_selected_profile_restart_cannot_receive_old_callback(store, app):
    p = store.add("Work")
    process_list(app, store, [(44, p)])
    sender = Mock()
    with pytest.raises(ClaudeDesktopError):
        signin.finish_signin(store, p.id, GOOGLE, app=app, sender=sender, expected_pid=33)
    sender.assert_not_called()


@pytest.mark.parametrize("change", ["expired", "archived", "closed", "disabled", "switched_pid"])
def test_changes_while_chooser_open_do_not_misroute(menu, store, app, monkeypatch, change):
    a, b = store.add("A"), store.add("B")
    process_list(app, store, [(22, a), (33, b)])
    menu._app_factory = lambda: app
    menu.signin_sender = Mock()
    menu.monitor = Mock(poll=Mock(return_value=False))
    fresh = [True]
    def choose(**kw):
        assert kw["choices"] == ["Choose a profile…", "A", "B"]
        if change == "expired": fresh[0] = False
        if change == "archived": store.update(b.id, archived=True)
        if change == "closed": process_list(app, store, [(22, a)])
        if change == "disabled": menu.enabled = lambda: False
        if change == "switched_pid": process_list(app, store, [(22, a), (44, b)])
        return SimpleNamespace(clicked=1, choice=2)
    menu.prompt.side_effect = choose
    monkeypatch.setattr(ui.threading, "Thread", lambda **kw: SimpleNamespace(start=kw["target"]))
    menu.receive_signin(GOOGLE, lambda: fresh[0])
    menu.poll()
    menu.poll()
    menu.signin_sender.assert_not_called()
    app.popen.assert_not_called()
