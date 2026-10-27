"""Resident Dock launchers attach to exact sessions and never relaunch on refresh."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import plistlib
import pytest

from openswap import claude_desktop as cd
from openswap import claude_desktop_presence as presence
from openswap import claude_desktop_shortcut as sc
from tests.test_claude_desktop_monitor import store, existing
from tests.test_claude_desktop_shortcut import builder
from tests.test_claude_desktop import app


def test_attach_only_never_launches_a_closed_profile(store, app):
    p=store.add('Work')
    result=store.open(p.id, app=app, existing_only=True)
    assert result['state']=='not_running' and result['pid'] is None
    app.popen.assert_not_called()


def test_missing_shortcut_does_not_create_unexpected_dock_app(store, tmp_path):
    p=store.add('Work'); creator=Mock(); opener=Mock()
    assert not presence.attach_presence(store,p.id,directory=tmp_path,creator=creator,opener=opener)
    creator.assert_not_called(); opener.assert_not_called()


def test_default_keeps_using_original_claude_icon(store, builder):
    existing(store); p=store.ensure_default(); builder.build(p)
    opener=Mock()
    assert not presence.attach_presence(store,p.id,directory=builder.directory,opener=opener)
    opener.assert_not_called()


def test_attach_uses_existing_app_without_rebuilding_or_launching_claude(store, builder):
    p=store.add('Work'); path=Path(builder.build(p)['path'])
    creator=Mock(); opener=Mock()
    assert presence.attach_presence(store,p.id,directory=builder.directory,creator=creator,opener=opener)
    creator.assert_not_called()
    opener.assert_called_once_with(path,f'com.opensoft.openswap.claude-profile.{p.id}')
    builder.app.open.assert_not_called()


def test_legacy_shortcut_upgrades_in_place_preserving_badge(store, builder):
    p=store.add('Work'); store.update(p.id,badge='7',color='blue')
    path=Path(builder.build(store.get(p.id))['path'])
    info=path/'Contents/Info.plist'; data=plistlib.loads(info.read_bytes()); data.pop('OpenSwapProfileLauncherVersion');info.write_bytes(plistlib.dumps(data))
    creator=Mock(side_effect=lambda s,i,**kw:builder.build(s.get(i)))
    opener=Mock()
    assert presence.attach_presence(store,p.id,directory=builder.directory,creator=creator,opener=opener)
    creator.assert_called_once_with(store,p.id,directory=builder.directory)
    assert (store.get(p.id).badge,store.get(p.id).color)==('7','blue')
    assert plistlib.loads(info.read_bytes())['OpenSwapProfileLauncherVersion']==2
    assert opener.call_args.args[0]==path


def test_archive_and_foreign_shortcuts_never_attach(store,builder):
    p=store.add('Work'); path=Path(builder.build(p)['path']); opener=Mock()
    info=path/'Contents/Info.plist'; data=plistlib.loads(info.read_bytes());data['OpenSwapProfile']['root']='/another/store';info.write_bytes(plistlib.dumps(data))
    assert not presence.attach_presence(store,p.id,directory=builder.directory,opener=opener)
    store.update(p.id,archived=True)
    with pytest.raises(cd.ClaudeDesktopError):presence.attach_presence(store,p.id,directory=builder.directory,opener=opener)
    opener.assert_not_called()


def test_attach_failure_does_not_open_profile_or_change_registry(store,builder):
    p=store.add('Work');builder.build(p);before=store.registry.read_bytes()
    with pytest.raises(cd.ClaudeDesktopError):
        presence.attach_presence(store,p.id,directory=builder.directory,opener=Mock(side_effect=cd.ClaudeDesktopError('Failed')))
    assert before==store.registry.read_bytes()
    builder.app.open.assert_not_called()


def test_resident_script_targets_process_object_and_normal_quit():
    source=sc.launcher_script(['printf','open'],attach_command=['printf','attach'])
    assert 'on reopen' in source and 'on idle' in source and 'on quit' in source
    assert 'set targetApplication to candidate' in source
    assert "targetApplication's isTerminated()" in source
    assert "targetApplication's terminate()" in source
    assert 'forceTerminate' not in source and 'killall' not in source
    assert 'bundleIdentifier()' in source and 'bundleURL()' in source
    assert '--openswap-attach' in source


def test_existing_only_reuses_exact_profile_without_launch(store,app):
    p=store.add('Work'); desktop,_=store.paths(p)
    app.run.return_value.stdout=f'441 {app.binary} --user-data-dir={desktop}\n'
    result=store.open(p.id,app=app,existing_only=True)
    assert result['pid']==441 and result['state']=='already_running'
    app.popen.assert_not_called()


def test_cli_attachment_cannot_relaunch_or_recurse(store,app,monkeypatch,capsys):
    p=store.add('Work'); attach=Mock()
    monkeypatch.setattr(cd,'ClaudeDesktopApp',lambda:app)
    monkeypatch.setattr(presence,'attach_presence',attach)
    assert cd.main(['open',p.id,'--profile-root',str(store.root),'--existing','--from-launcher','--json'])==0
    import json
    assert json.loads(capsys.readouterr().out)['state']=='not_running'
    attach.assert_not_called();app.popen.assert_not_called()


def test_cli_open_attaches_indicator_without_changing_launch_result(store,app,monkeypatch,capsys):
    p=store.add('Work');desktop,_=store.paths(p)
    app.run.return_value.stdout=f'441 {app.binary} --user-data-dir={desktop}\n'
    attach=Mock()
    monkeypatch.setattr(cd,'ClaudeDesktopApp',lambda:app)
    monkeypatch.setattr(presence,'attach_presence',attach)
    assert cd.main(['open',p.id,'--profile-root',str(store.root),'--json'])==0
    import json
    assert json.loads(capsys.readouterr().out)['pid']==441
    attach.assert_called_once()
    assert attach.call_args.args[1]==p.id
    app.popen.assert_not_called()


def test_passive_attach_does_not_reopen_running_helper(monkeypatch,tmp_path):
    import sys
    path=tmp_path/'Claude Work.app'
    helper=Mock();helper.isTerminated.return_value=False;helper.bundleURL.return_value.path.return_value=str(path)
    native=Mock();native.runningApplicationsWithBundleIdentifier_.return_value=[helper]
    monkeypatch.setitem(sys.modules,'AppKit',SimpleNamespace(NSRunningApplication=native))
    run=Mock();monkeypatch.setattr(presence.subprocess,'run',run)
    presence.open_presence(path,'example.helper')
    run.assert_not_called();helper.activateWithOptions_.assert_not_called()


def test_passive_launch_requests_attach_only_in_background(monkeypatch,tmp_path):
    import sys
    native=Mock();native.runningApplicationsWithBundleIdentifier_.return_value=[]
    monkeypatch.setitem(sys.modules,'AppKit',SimpleNamespace(NSRunningApplication=native))
    run=Mock(return_value=SimpleNamespace(returncode=0));monkeypatch.setattr(presence.subprocess,'run',run)
    path=tmp_path/'Claude Work.app'
    presence.open_presence(path,'example.helper')
    assert run.call_args.args[0]==['/usr/bin/open','-g',str(path),'--args','--openswap-attach']


@pytest.mark.parametrize('interrupted', ['disabled', 'stale', 'invalid-app'])
def test_status_refresh_cannot_attach_after_disabling_or_failed_validation(store,monkeypatch,interrupted):
    from openswap.claude_desktop_menu import ClaudeDesktopMenu
    p=store.add('Work'); native=Mock(); native.validate.return_value={'version':'test'}
    controller=ClaudeDesktopMenu(store=store,prompt=Mock(),show_error=Mock(),changed=Mock(),notify=Mock(),app_factory=lambda:native)
    def states(s):
        if interrupted=='disabled':controller.enabled=lambda:False
        if interrupted=='stale':controller._check_generation+=1
        return {p.id:'Running'}
    native.profile_states.side_effect=states
    if interrupted=='invalid-app':native.validate.side_effect=cd.ClaudeDesktopError('Invalid app')
    attach=Mock();monkeypatch.setattr(presence,'attach_presence',attach)
    monkeypatch.setattr('openswap.claude_desktop_menu.threading.Thread',lambda **kw:SimpleNamespace(start=kw['target']))
    controller.refresh_status()
    attach.assert_not_called()


@pytest.mark.parametrize('same_store',[True,False])
def test_renamed_running_helper_is_reused_only_for_same_profile_store(store,builder,monkeypatch,same_store):
    import sys
    p=store.add('Work');old=Path(builder.build(p)['path'])
    store.update(p.id,name='Renamed');new=Path(builder.build(store.get(p.id))['path'])
    if not same_store:
        info=old/'Contents/Info.plist';data=plistlib.loads(info.read_bytes());data['OpenSwapProfile']['root']='/different/store';info.write_bytes(plistlib.dumps(data))
    helper=Mock();helper.isTerminated.return_value=False;helper.bundleURL.return_value.path.return_value=str(old)
    native=Mock();native.runningApplicationsWithBundleIdentifier_.return_value=[helper]
    monkeypatch.setitem(sys.modules,'AppKit',SimpleNamespace(NSRunningApplication=native))
    run=Mock();monkeypatch.setattr(presence.subprocess,'run',run)
    if same_store:presence.open_presence(new,'example.helper')
    else:
        with pytest.raises(cd.ClaudeDesktopError):presence.open_presence(new,'example.helper')
    run.assert_not_called();helper.activateWithOptions_.assert_not_called()
