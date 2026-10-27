"""Independent Desktop launchers, badge metadata and safe shortcut publication."""
import json
import os
import plistlib
import shlex
import subprocess
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openswap import claude_desktop as cd
from openswap import claude_desktop_shortcut as sc
from openswap import menubar
from tests.test_claude_desktop import store, menu
from tests.menubar_harness import extract_class


@pytest.fixture
def builder(store, tmp_path):
    app = Mock(path=tmp_path / 'Claude.app')
    app.validate.return_value = {'version': 'test'}
    def run(argv, **kwargs):
        if argv[0] == '/usr/bin/osacompile':
            path = Path(argv[2]) / 'Contents'
            (path / 'Resources').mkdir(parents=True)
            (path / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'applet'}))
            (path / 'Resources' / 'source.txt').write_text(kwargs['input'])
        return SimpleNamespace(returncode=0)
    calls = Mock(side_effect=run)
    icon = Mock(side_effect=lambda app, target, badge, color: target.write_bytes(f'{badge}:{color}'.encode()))
    def build(profile, **kwargs):
        return sc.create_shortcut(store, profile.id, app=app, run=calls, icon_renderer=icon,
                                  directory=tmp_path / 'Shortcuts', **kwargs)
    return SimpleNamespace(build=build, app=app, run=calls, icon=icon, directory=tmp_path/'Shortcuts')


def test_badges_default_to_distinct_numbers_and_are_remembered(store):
    a, b = store.add('Work'), store.add('Personal')
    assert (a.badge,b.badge)==('1','2')
    store.update(a.id, badge='42', color='purple')
    store.update(a.id, archived=True)
    c = store.add('Client')
    assert c.badge == '1'
    assert store.get(a.id, include_archived=True).badge == '42'


@pytest.mark.parametrize('badge,color', [('0','blue'),('1000','blue'),('-1','blue'),('1\n','blue'),('01','blue'),('2','red'),('☃','green'),('1',[])])
def test_invalid_badges_cannot_change_profiles(store,badge,color):
    p=store.add('Work');before=store.registry.read_bytes()
    with pytest.raises(cd.ClaudeDesktopError): store.update(p.id,badge=badge,color=color)
    assert store.registry.read_bytes()==before


@pytest.mark.parametrize('version',[1,2])
def test_legacy_profiles_migrate_without_account_links_or_data_changes(store,version):
    p=store.add('Work');data=store.paths(p)[0]/'work';data.write_text('kept')
    row={'id':p.id,'name':p.name,'archived':False}
    if version==2: row['code_account']={'store':'a'*64,'email':'same@example.test','organization':'team'}
    store.registry.write_text(json.dumps({'version':version,'profiles':[row]}))
    assert store.get(p.id).badge=='1'
    store.update(p.id,color='green')
    raw=json.loads(store.registry.read_text())
    assert raw['version']==6 and 'code_account' not in raw['profiles'][0]
    assert data.read_text()=='kept'


def test_shortcut_uses_stable_profile_and_does_not_launch_or_change_code(store,builder):
    p=store.add('Work')
    result=builder.build(p,badge='2',color='blue')
    path=Path(result['path']);info=plistlib.loads((path/'Contents/Info.plist').read_bytes())
    assert path.name=='Claude Work.app'
    assert info['CFBundleIconFile']=='Profile.icns'
    assert info['CFBundleIdentifier'].endswith(p.id)
    script=(path/'Contents/Resources/source.txt').read_text()
    assert f'claude-desktop open {p.id}' in script and '--focus' in script
    assert 'switch ' not in script
    assert store.get(p.id).badge=='2'
    assert not list(store.paths(p)[1].iterdir())
    builder.app.open.assert_not_called()
    assert [c.args[0][0] for c in builder.run.call_args_list] == ['/usr/bin/osacompile','/usr/bin/codesign','/usr/bin/codesign']


def test_update_reuses_path_and_keeps_signin_files(store,builder):
    p=store.add('Work');sign_in=store.paths(p)[0]/'local-data';sign_in.write_text('kept')
    first=builder.build(p)
    second=builder.build(p,badge='99',color='purple')
    assert first['path']==second['path']
    assert (Path(second['path'])/'Contents/Resources/Profile.icns').read_bytes()==b'99:purple'
    assert sign_in.read_text()=='kept'


@pytest.mark.parametrize('failure',['compile','compiled_metadata','icon','sign','metadata','publish'])
def test_failed_update_keeps_old_shortcut_and_profile(store,builder,monkeypatch,failure):
    p=store.add('Work');path=Path(builder.build(p)['path'])
    before=store.registry.read_bytes()
    if failure=='compile': builder.run.side_effect=subprocess.TimeoutExpired('compile',45)
    if failure=='compiled_metadata':
        original=builder.run.side_effect
        def malformed(argv,**kwargs):
            result=original(argv,**kwargs)
            if argv[0].endswith('osacompile'):
                (Path(argv[2])/'Contents/Info.plist').write_bytes(b'incomplete')
            return result
        builder.run.side_effect=malformed
    if failure=='icon': builder.icon.side_effect=OSError('disk full')
    if failure=='sign':
        original=builder.run.side_effect
        builder.run.side_effect=lambda argv,**kw: SimpleNamespace(returncode=1) if argv[0].endswith('codesign') else original(argv,**kw)
    if failure=='metadata': monkeypatch.setattr(store,'_save',Mock(side_effect=OSError('disk full')))
    if failure=='publish':
        original=sc.os.replace
        def replace(source,target):
            if Path(source).name==path.name and Path(target)==path: raise OSError('busy')
            return original(source,target)
        monkeypatch.setattr(sc.os,'replace',replace)
    with pytest.raises((cd.ClaudeDesktopError,OSError)): builder.build(p,badge='9')
    assert store.registry.read_bytes()==before
    assert (path/'Contents/Resources/Profile.icns').read_bytes()==b'1:orange'
    assert not list(builder.directory.glob('.openswap-shortcut-*'))


def test_unrelated_app_collision_is_preserved(store,builder):
    p=store.add('Work');target=builder.directory/'Claude Work.app';target.mkdir(parents=True)
    keep=target/'user-file';keep.write_text('keep')
    with pytest.raises(cd.ClaudeDesktopError,match='Another app'): builder.build(p)
    assert keep.read_text()=='keep'
    builder.run.assert_not_called()


@pytest.mark.skipif(os.name=='nt',reason='symlink privilege')
def test_symlink_destination_cannot_replace_other_app(store,builder,tmp_path):
    p=store.add('Work');target=tmp_path/'Other.app';target.mkdir()
    builder.directory.mkdir();(builder.directory/'Claude Work.app').symlink_to(target,target_is_directory=True)
    with pytest.raises(cd.ClaudeDesktopError,match='symbolic'): builder.build(p)
    assert target.is_dir()


def test_archived_profile_cannot_create_or_open_shortcut(store,builder):
    p=store.add('Work');path=Path(builder.build(p)['path']);store.update(p.id,archived=True)
    with pytest.raises(cd.ClaudeDesktopError): builder.build(p)
    app=Mock();app.validate.return_value={'version':'test'}
    with pytest.raises(cd.ClaudeDesktopError): store.open(p.id,app=app)
    app.open.assert_not_called()
    assert path.exists()
    store.update(p.id,archived=False)
    assert store.get(p.id).badge=='1'


def test_names_cannot_escape_shortcut_folder(store,builder):
    p=store.add('../../Work: $(touch nope)')
    path=Path(builder.build(p)['path'])
    assert path.parent==builder.directory and not (builder.directory/'nope').exists()


@pytest.mark.skipif(os.name=='nt',reason='macOS launcher uses POSIX shell')
def test_script_quotes_shell_and_applescript_independently(tmp_path):
    marker=tmp_path/'should-not-exist'
    value=f'path with spaces "quotes" \\ $(touch {marker}) `touch {marker}`'
    command=['printf','%s',value]
    source=sc.launcher_script(command)
    literal=source.split('do shell script ',1)[1].split('\n',1)[0]
    # AppleScript's emitted string uses the same two escapes as JSON here.
    shell=json.loads(literal)
    assert shlex.split(shell)==command
    result=subprocess.run(['/bin/sh','-c',shell],text=True,capture_output=True,check=True)
    assert result.stdout==value and not marker.exists()


def test_bundle_launch_command_and_translocation_refusal(store,monkeypatch):
    from openswap import bundle
    monkeypatch.setattr(bundle,'executable',lambda:Path('/Applications/OpenSwap.app/Contents/MacOS/OpenSwap'))
    monkeypatch.setattr(bundle,'runs_from_temporary_location',lambda:False)
    p=store.add('Work')
    assert sc.launcher_command(store,p.id)[0]==str(Path('/Applications/OpenSwap.app/Contents/MacOS/OpenSwap'))
    monkeypatch.setattr(bundle,'runs_from_temporary_location',lambda:True)
    with pytest.raises(cd.ClaudeDesktopError,match='Move OpenSwap'): sc.launcher_command(store,p.id)


def test_menu_shortcut_runs_off_ui_and_reports_completion_on_poll(menu,store,monkeypatch):
    p=store.add('Work')
    menu.prompt.side_effect=[SimpleNamespace(clicked=1,text='7'),SimpleNamespace(clicked=1,choice=1)]
    menu.shortcut_factory=Mock(return_value={'state':'shortcut_created','path':'/tmp/Claude Work.app','notice':sc.ICON_NOTICE})
    menu.reveal=Mock();menu.show_status=Mock()
    monkeypatch.setattr('openswap.claude_desktop_menu.threading.Thread',lambda **kw:SimpleNamespace(start=kw['target']))
    menu.shortcut(p)
    assert menu.busy and menu.operation=='Creating shortcut…'
    menu.show_status.assert_not_called();menu.reveal.assert_not_called()
    menu.poll()
    assert not menu.busy
    menu.shortcut_factory.assert_called_once_with(store,p.id,badge='7',color='blue')
    menu.reveal.assert_called_once();menu.show_status.assert_called_once()


@pytest.mark.parametrize('step',[1,2])
def test_cancel_shortcut_leaves_files_and_badge_unchanged(menu,store,step):
    p=store.add('Work');before=store.registry.read_bytes();menu.shortcut_factory=Mock()
    menu.prompt.side_effect=[SimpleNamespace(clicked=0 if step==1 else 1,text='8'),SimpleNamespace(clicked=0,choice=2)]
    menu.shortcut(p)
    assert store.registry.read_bytes()==before and not menu.busy
    menu.shortcut_factory.assert_not_called()


@pytest.mark.parametrize('desktop',[False,True])
def test_code_card_remains_code_only_when_desktop_enabled(desktop):
    app=extract_class(menubar.__file__,'MenuBarApp',{'_on_account_click'},dict(vars(menubar)))()
    app.settings=menubar.MenuBarSettings(desktop_features_enabled=desktop);app._desktop_switching=False
    for name in ('_slot_needs_relogin','_slot_missing_login','_slot_needs_reconcile','_slot_needs_restore'):
        setattr(app,name,lambda n:False)
    app._confirm_switch=lambda n:True;app._run_switch=lambda fn:fn();app.switcher=Mock()
    app._finish_manual_switch=Mock();app._name_for_num=lambda n:'Work';app._claude_desktop_profiles=Mock()
    app._on_account_click('1',close_panel=True)
    app.switcher.switch_to.assert_called_once_with('1',json_output=True)
    app._claude_desktop_profiles.open.assert_not_called()


@pytest.mark.parametrize('desktop',[False,True])
def test_auto_rotation_always_uses_code_engine(desktop,tmp_path):
    import threading
    factory=Mock(return_value=SimpleNamespace())
    thread=Mock()
    scope={**vars(menubar),'AutoSwitchEngine':factory,'load_settings':lambda root:object(),
           'threading':SimpleNamespace(Thread=thread)}
    app=extract_class(menubar.__file__,'MenuBarApp',{'_start_engine'},scope)()
    app._engine=None;app._event_lock=threading.Lock();app.switcher=Mock(backup_dir=tmp_path)
    app.settings=menubar.MenuBarSettings(desktop_features_enabled=desktop)
    app._ensure_codex_engine=Mock();app._on_engine_event=Mock();app._run_engine=Mock()
    app._start_engine()
    assert factory.call_args.args[0] is app.switcher
    thread.return_value.start.assert_called_once()


def test_shortcut_cli_dispatch_without_login_changes(temp_home,monkeypatch,capsys):
    from tests.test_codex_cli import run_cli
    store=cd.DesktopProfiles();p=store.add('Work')
    create=Mock(return_value={'state':'shortcut_created','path':'/tmp/Claude Work.app','profile':asdict(p)})
    monkeypatch.setattr(sc,'create_shortcut',create)
    assert run_cli(['claude-desktop','shortcut',p.id,'--badge','8','--color','green','--json'])==0
    assert json.loads(capsys.readouterr().out)['ok']
    assert create.call_args.args[1]==p.id
    assert create.call_args.kwargs['badge']=='8'
    assert create.call_args.kwargs['color']=='green'


def test_shortcut_open_resolves_renamed_profile_and_focuses_correct_process(store,monkeypatch,capsys):
    p=store.add('Work');store.update(p.id,name='New name')
    app=Mock();app.validate.return_value={'version':'test'};app.open.return_value={'pid':42,'state':'already_running'}
    monkeypatch.setattr(cd,'ClaudeDesktopApp',lambda:app)
    focus=Mock();monkeypatch.setattr('openswap.claude_desktop_menu.focus_profile',focus)
    assert cd.main(['open',p.id,'--profile-root',str(store.root),'--focus','--json'])==0
    assert json.loads(capsys.readouterr().out)['profile']['name']=='New name'
    app.open.assert_called_once_with(*store.paths(p));focus.assert_called_once_with(42)
