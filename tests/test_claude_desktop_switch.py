"""Explicit profile links and coordinated selection without real logins."""
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from openswap import claude_desktop as cd
from openswap.claude_desktop_switch import ClaudeDesktopSwitch, DesktopSelectionError
from openswap.claude_desktop_accounts import ClaudeDesktopAccounts
from tests.test_claude_desktop import store

@pytest.fixture
def engine(tmp_path):
    e = Mock(backup_dir=tmp_path / 'Code store')
    ids = {'1': ('same@example.test', 'personal'), '2': ('same@example.test', 'team'), '3': ('key@example.test', 'api')}
    e.slot_identity.side_effect = lambda n: ids.get(str(n))
    e.account_kind_for.side_effect = lambda n: 'api_key' if str(n) == '3' else 'oauth'
    e.switchable_account_numbers.return_value = ['1', '2', '3']
    live = [ids['2']]
    e.live_identity.side_effect = lambda: live[0]
    def switch(n, **kwargs):
        assert kwargs['expected_identity'] == ids[n]
        live[0] = ids[n]
        return {'switched': True, 'to': {'number': int(n), 'email': ids[n][0]}}
    e.switch_to.side_effect = switch
    return e

@pytest.fixture
def app():
    a = Mock(binary=Path('/Applications/Claude.app/Contents/MacOS/Claude'))
    a.validate.return_value = {'version': 'test'}
    a._running.return_value = None
    a.open.return_value = {'pid': 42, 'state': 'launched'}
    return a

@pytest.fixture
def coordinator(engine, store, app):
    return ClaudeDesktopSwitch(engine, store, enabled=lambda: True, app_factory=lambda: app, completed=Mock())

def link(store, coordinator, number='1', name='Personal'):
    return store.link(store.add(name).id, coordinator.account(number))

def test_same_email_different_orgs_and_stores_have_distinct_links(coordinator, store, engine, tmp_path):
    personal, team = link(store, coordinator), link(store, coordinator, '2', 'Team')
    assert store.linked(coordinator.account('1')) == personal
    assert store.linked(coordinator.account('2')) == team
    assert store.linked(cd.CodeAccount.for_identity(tmp_path / 'Other', engine.slot_identity('1'))) is None

def test_v1_upgrade_keeps_data_and_writes_versioned_link(store, coordinator):
    p = store.add('Old')
    (store.paths(p)[0] / 'conversation').write_text('kept')
    store.registry.write_text(json.dumps({'version': 1, 'profiles': [{'id': p.id, 'name': p.name, 'archived': False}]}))
    assert store.get(p.id).code_account is None
    store.link(p.id, coordinator.account('1'))
    assert json.loads(store.registry.read_text())['version'] == 2
    assert (store.paths(p)[0] / 'conversation').read_text() == 'kept'

def test_unique_links_include_archived_and_assign_moves_atomically(store, coordinator):
    a, b = link(store, coordinator), store.add('Replacement')
    store.update(a.id, archived=True)
    with pytest.raises(cd.ClaudeDesktopError, match='already has'):
        store.link(b.id, a.code_account)
    store.assign(b.id, a.code_account)
    assert store.get(a.id, include_archived=True).code_account is None
    store.update(b.id, name='Renamed')
    assert store.linked(a.code_account).name == 'Renamed'

def test_cannot_take_another_accounts_profile(store, coordinator):
    p = link(store, coordinator, '2', 'Work')
    original = store.registry.read_bytes()
    with pytest.raises(cd.ClaudeDesktopError, match='another Code account'):
        store.assign(p.id, coordinator.account('1'))
    assert store.registry.read_bytes() == original

@pytest.mark.parametrize('change', ['duplicate', 'invalid'])
def test_corrupt_links_fail_closed(store, coordinator, change):
    p = link(store, coordinator)
    store.add('Other')
    data = json.loads(store.registry.read_text())
    data['profiles'][1]['code_account'] = asdict(p.code_account) if change == 'duplicate' else {'email': 'x'}
    store.registry.write_text(json.dumps(data))
    with pytest.raises(cd.ClaudeDesktopError, match='metadata is invalid'):
        store.list()

def test_success_switches_code_then_opens_without_copying_credentials(store, coordinator, engine, app):
    p = link(store, coordinator)
    order, original = [], engine.switch_to.side_effect
    engine.switch_to.side_effect = lambda *a, **k: (order.append('code'), original(*a, **k))[1]
    app.open.side_effect = lambda *a: (order.append('desktop'), {'pid': 42, 'state': 'launched'})[1]
    result = coordinator.select('1', expected=p.code_account, expected_profile=p.id)
    assert order == ['code', 'desktop'] and not result['identity_verified']
    assert result['desktop_result']['profile']['id'] == p.id
    assert list(store.paths(p)[1].iterdir()) == []
    engine.switch_to.assert_called_once_with('1', json_output=True, expected_identity=('same@example.test', 'personal'))

@pytest.mark.parametrize('failure', ['unlinked', 'archived', 'app', 'missing-data', 'conflict', 'account', 'profile', 'disabled'])
def test_preflight_failures_never_switch_or_open(store, coordinator, engine, app, failure):
    p, options = link(store, coordinator), {}
    if failure == 'unlinked': store.link(p.id, None)
    elif failure == 'archived': store.update(p.id, archived=True)
    elif failure == 'app': app.validate.side_effect = cd.ClaudeDesktopError('App unavailable')
    elif failure == 'missing-data': store.paths(p)[0].rmdir()
    elif failure == 'conflict': app._running.side_effect = cd.ClaudeDesktopError('Conflict')
    elif failure == 'account': options['expected'] = coordinator.account('2')
    elif failure == 'profile': options['expected_profile'] = 'different'
    else: engine.switchable_account_numbers.return_value = ['2']
    with pytest.raises(DesktopSelectionError): coordinator.select('1', **options)
    engine.switch_to.assert_not_called()
    app.open.assert_not_called()
    assert not coordinator.busy

def test_link_changed_during_validation_refused(store, coordinator, engine, app):
    p = link(store, coordinator)
    app.validate.side_effect = lambda: (store.link(p.id, None), {'version': 'test'})[1]
    with pytest.raises(DesktopSelectionError, match='changed'): coordinator.select('1')
    engine.switch_to.assert_not_called()

def test_mode_disabled_during_preflight_refused(store, coordinator, engine, app):
    link(store, coordinator)
    enabled = [True]
    coordinator.enabled = lambda: enabled[0]
    app.validate.side_effect = lambda: (enabled.__setitem__(0, False), {'version': 'test'})[1]
    with pytest.raises(DesktopSelectionError, match='turned off'): coordinator.select('1')
    engine.switch_to.assert_not_called()

def test_code_failure_never_opens_profile(store, coordinator, engine, app):
    link(store, coordinator)
    engine.switch_to.side_effect = cd.ClaudeDesktopError('Login needs repair')
    with pytest.raises(DesktopSelectionError, match='repair') as error: coordinator.select('1')
    assert error.value.code_result is None
    app.open.assert_not_called()

def test_launch_failure_reports_partial_without_rollback(store, coordinator, engine, app):
    link(store, coordinator)
    app.open.side_effect = cd.ClaudeDesktopError('App exited')
    with pytest.raises(DesktopSelectionError, match='Code selection completed') as error: coordinator.select('1')
    assert error.value.code_result['switched']
    assert engine.live_identity() == ('same@example.test', 'personal')
    assert engine.switch_to.call_count == 1 and 'No automatic rollback' in str(error.value)

def test_external_code_change_during_launch_reported(store, coordinator, engine, app):
    link(store, coordinator)
    def open_profile(*args):
        engine.live_identity.side_effect = lambda: ('other@example.test', 'other')
        return {'pid': 42, 'state': 'launched'}
    app.open.side_effect = open_profile
    with pytest.raises(DesktopSelectionError, match='changed again') as error: coordinator.select('1')
    assert error.value.desktop_result['pid'] == 42 and engine.switch_to.call_count == 1

def test_second_selection_refused_while_busy(store, coordinator, engine):
    link(store, coordinator)
    with coordinator._operation:
        with pytest.raises(cd.ClaudeDesktopError, match='in progress'): coordinator.select('1')
    engine.switch_to.assert_not_called()

def test_rotation_filters_unlinked_archived_and_key_accounts(store, coordinator, engine, app):
    p, adapter = link(store, coordinator), coordinator.adapter()
    assert adapter.switchable_account_numbers() == ['1']
    assert adapter.switch_to('1', json_output=True)['switched']
    coordinator.completed.assert_called_once()
    store.update(p.id, archived=True)
    assert adapter.switchable_account_numbers() == []
    coordinator.enabled = lambda: False
    assert adapter.switchable_account_numbers() == []

@pytest.mark.parametrize('launch_fails', [False, True])
def test_auto_rotation_runs_combined_selection_and_reports_partial_failure(temp_home, store, app, launch_fails):
    from tests.test_autoswitch import EngineHarness, _usage
    from openswap.autoswitch import AutoSwitchEngine, TickOutcome
    h = EngineHarness(temp_home)
    for n in (1, 2, 3):
        h.seed(n, f'account{n}@example.test')
    h.make_live('account1@example.test', 1)
    complete = Mock()
    c = ClaudeDesktopSwitch(h.switcher, store, enabled=lambda: True, app_factory=lambda: app, completed=complete)
    p = link(store, c, '3', 'Linked work')
    if launch_fails:
        app.open.side_effect = cd.ClaudeDesktopError('App exited')
    h.engine = AutoSwitchEngine(c.adapter(), h.settings, h.events.append, clock=h.clock)
    outcome = h.tick_with_usage({'1': _usage(95), '2': _usage(5), '3': _usage(20)})
    assert outcome == (TickOutcome.ERROR if launch_fails else TickOutcome.SWITCHED)
    assert h.active_number() == 3  # the better, unlinked candidate was excluded
    app.open.assert_called_once_with(*store.paths(p))
    result, error = complete.call_args.args
    if launch_fails:
        assert result is None and error.code_result['switched']
        assert 'No automatic rollback' in str(error)
        assert h.events[-1].kind == 'error'
    else:
        assert error is None and result['desktop_result']['profile']['id'] == p.id
        assert h.events[-1].kind == 'switch'

@pytest.fixture
def controller(coordinator, store, engine):
    c = ClaudeDesktopAccounts(engine, store, prompt=Mock(), alert=Mock(return_value=1), show_error=Mock(), changed=Mock(),
        enabled=lambda: True, persisted_enabled=lambda: True, blocked=lambda: False, accounts=lambda: ['1', '2', '3'],
        name=lambda n: f'Account {n}', completed=Mock(), confirm=lambda: True)
    c.switch = coordinator
    return c

def test_cancel_link_creates_nothing(controller, store):
    controller.prompt.side_effect = [SimpleNamespace(clicked=1, choice=0), SimpleNamespace(clicked=1, text='New')]
    controller.alert.return_value = 0
    assert controller.link('1') is None
    assert not store.root.exists()

def test_link_then_select_same_account_opens_both(controller, store, coordinator, monkeypatch):
    p = store.add('Chosen')
    controller.prompt.return_value = SimpleNamespace(clicked=1, choice=1)
    assert controller.link('2').code_account == coordinator.account('2')
    original = store.registry.read_bytes()
    monkeypatch.setattr('openswap.claude_desktop_accounts.threading.Thread', lambda **kw: SimpleNamespace(start=kw['target']))
    controller.select('2')
    assert controller.pending
    controller.completed.assert_not_called()
    controller.poll()
    result, error, manual = controller.completed.call_args.args
    assert result['desktop_result']['profile']['id'] == p.id and error is None and manual
    assert store.registry.read_bytes() == original

def test_changed_link_after_consent_refused(controller, store, coordinator, engine, monkeypatch):
    p, q = link(store, coordinator), store.add('Other')
    controller.alert.side_effect = lambda **kw: (store.assign(q.id, p.code_account), 1)[1]
    monkeypatch.setattr('openswap.claude_desktop_accounts.threading.Thread', lambda **kw: SimpleNamespace(start=kw['target']))
    controller.select('1')
    controller.poll()
    assert 'changed after confirmation' in str(controller.completed.call_args.args[1])
    engine.switch_to.assert_not_called()

def test_stale_profile_menu_cannot_select_or_unlink_new_binding(controller, store, coordinator, engine):
    old = link(store, coordinator)
    store.link(old.id, None)
    current = store.assign(old.id, coordinator.account('2'))
    controller.select_profile(old)
    controller.show_error.assert_called_once()
    engine.switch_to.assert_not_called()
    with pytest.raises(cd.ClaudeDesktopError, match='link changed'):
        store.link(old.id, None, expected=old.code_account)
    assert store.get(old.id) == current

def test_link_follows_identity_when_slots_move(coordinator, store, engine, app):
    p = link(store, coordinator)
    engine.slot_identity.side_effect = lambda n: p.code_account.identity if n == '2' else ('other@example.test', 'other')
    engine.switch_to.side_effect = lambda *args, **kw: {'switched': True}
    engine.live_identity.side_effect = lambda: p.code_account.identity
    assert coordinator.adapter().switchable_account_numbers() == ['2']
    assert coordinator.select('2')['desktop_result']['profile']['id'] == p.id

@pytest.mark.parametrize('desktop', [False, True])
def test_card_selection_uses_coordinator_only_in_desktop_mode(desktop):
    from openswap import menubar
    from tests.menubar_harness import extract_class
    app = extract_class(menubar.__file__, 'MenuBarApp', {'_on_account_click'}, dict(vars(menubar)))()
    app.settings = menubar.MenuBarSettings(desktop_features_enabled=desktop)
    app._desktop_switching = False
    app._claude_selection_busy = lambda: False
    for name in ('_slot_needs_relogin', '_slot_missing_login', '_slot_needs_reconcile', '_slot_needs_restore'):
        setattr(app, name, lambda n: False)
    app._confirm_switch = lambda n: True
    app._run_switch = lambda fn: fn()
    app.switcher = Mock()
    app._claude_desktop_accounts = Mock()
    app._finish_manual_switch = Mock()
    app._name_for_num = lambda n: 'Personal'
    app._on_account_click('1', close_panel=True)
    assert app._claude_desktop_accounts.select.call_count == int(desktop)
    assert app.switcher.switch_to.call_count == int(not desktop)
    assert app._finish_manual_switch.call_count == int(not desktop)

@pytest.mark.parametrize('manual', [False, True])
def test_partial_completion_refreshes_code_without_false_success_or_auto_dialog(manual, tmp_path):
    from openswap import menubar
    from tests.menubar_harness import extract_class
    record = Mock()
    app = extract_class(menubar.__file__, 'MenuBarApp', {'_claude_selection_completed'},
                        {**vars(menubar), 'record_manual_switch': record})()
    app.switcher = Mock(backup_dir=tmp_path)
    for name in ('refresh_async', '_clear_hold_event', '_show_error', '_notify'):
        setattr(app, name, Mock())
    error = DesktopSelectionError('Code selected, Desktop failed', code_result={'switched': True})
    app._claude_selection_completed(None, error, manual)
    app.refresh_async.assert_called_once()
    record.assert_called_once_with(tmp_path)
    assert app._show_error.call_count == int(manual)
    app._notify.assert_not_called()

@pytest.mark.parametrize('change', ['identity', 'key', 'disabled'])
def test_engine_rechecks_link_target_under_credential_lock(temp_home, monkeypatch, change):
    from tests.test_engine_abi import _two_org_engine, EMAIL, ADS_ORG
    from openswap.exceptions import SwitchError
    engine = _two_org_engine(temp_home)
    before = (temp_home / '.claude.json').read_bytes()
    credentials_before = (temp_home / '.claude' / '.credentials.json').read_bytes()
    def change_before_lock():
        data = engine._get_sequence_data()
        if change == 'identity': data['accounts']['2']['organizationUuid'] = 'different-org'
        elif change == 'key': data['accounts']['2']['kind'] = 'api_key'
        else: data['accounts']['2']['disabled'] = True
        engine._write_json(engine.sequence_file, data)
        return {'live': None, 'resolved': None}
    monkeypatch.setattr(engine, '_prefetch_live_identity', change_before_lock)
    with pytest.raises(SwitchError):
        engine.switch_to('2', json_output=True, expected_identity=(EMAIL, ADS_ORG))
    assert (temp_home / '.claude.json').read_bytes() == before
    assert (temp_home / '.claude' / '.credentials.json').read_bytes() == credentials_before


def test_real_engine_linked_selection_uses_normal_credential_transaction(temp_home, store, app):
    from tests.test_engine_abi import _two_org_engine, EMAIL, ADS_ORG
    engine = _two_org_engine(temp_home)
    coordinator = ClaudeDesktopSwitch(engine, store, enabled=lambda: True, app_factory=lambda: app)
    profile = link(store, coordinator, '2', 'Team')
    result = coordinator.select('2', expected=profile.code_account, expected_profile=profile.id)
    assert result['code_result']['switched']
    assert engine.live_identity() == (EMAIL, ADS_ORG)
    app.open.assert_called_once_with(*store.paths(profile))


def test_same_active_code_account_still_opens_desktop(temp_home, store, app):
    from tests.test_engine_abi import _two_org_engine
    engine = _two_org_engine(temp_home)
    coordinator = ClaudeDesktopSwitch(engine, store, enabled=lambda: True, app_factory=lambda: app)
    profile = link(store, coordinator)
    result = coordinator.select('1', expected=profile.code_account, expected_profile=profile.id)
    assert not result['code_result']['switched']
    app.open.assert_called_once()


def test_cancel_selection_and_double_click_never_queue_extra_switches(controller, store, coordinator, monkeypatch):
    link(store, coordinator)
    thread = Mock()
    monkeypatch.setattr('openswap.claude_desktop_accounts.threading.Thread', thread)
    controller.alert.return_value = 0
    controller.select('1')
    thread.assert_not_called()
    controller.alert.return_value = 1
    controller.select('1')
    controller.select('1')
    thread.assert_called_once()
