"""Dock setup preserves the user's layout and never touches running Claude."""
import copy
import plistlib
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from openswap import claude_desktop_dock as dock
from openswap import claude_desktop_shortcut as sc
from openswap.claude_desktop import ClaudeDesktopError
from tests.test_claude_desktop_monitor import store, existing
from tests.test_claude_desktop_shortcut import builder

pytestmark = pytest.mark.skipif(sys.platform != 'darwin', reason='Dock shortcuts are macOS-only')


def tile(path, guid=1):
    return {'GUID': guid, 'tile-type': 'file-tile', 'tile-data': {
        'file-data': {'_CFURLString': str(Path(path).as_uri()) + '/', '_CFURLStringType': 15},
        'preserve-me': 'user metadata'}}


def shortcuts(tmp_path):
    return [dock.DockShortcut(tmp_path/'Claude Default.app', 'a'*32, tmp_path, True),
            dock.DockShortcut(tmp_path/'Claude Work.app', 'b'*32, tmp_path)]


def test_preserves_original_and_inserts_profile_shortcuts_beside_it(tmp_path):
    before = [tile('/Applications/Other.app', 1), tile(dock.APP_PATH, 2), tile('/Applications/Last.app', 3)]
    saved = copy.deepcopy(before)
    items = shortcuts(tmp_path)
    after = dock.plan_pins(before, items, guid_factory=iter([4, 5]).__next__)
    assert before == saved
    assert after[0] == before[0] and after[-1] == before[-1]
    assert after[1] == before[1]
    assert [dock.tile_path(p) for p in after[2:4]] == [s.path for s in items]
    assert dock.plan_pins(after, items) == after


def test_no_default_leaves_original_and_unrelated_pins_alone(tmp_path):
    before = [tile(dock.APP_PATH), tile('/Applications/Other.app', 2)]
    after = dock.plan_pins(before, shortcuts(tmp_path)[1:])
    assert after[0] == before[0] and after[-1] == before[1] and len(after) == 3


def test_existing_profile_positions_and_duplicates_are_preserved(tmp_path):
    items = shortcuts(tmp_path)
    before = [tile(items[1].path, 1), tile('/Applications/Other.app', 2),
              tile(items[0].path, 3), tile(items[1].path, 4)]
    assert dock.plan_pins(before, items) == before


def test_renamed_shortcut_updates_owned_alias_at_same_position(tmp_path):
    s = shortcuts(tmp_path)[1]
    alias = tmp_path/'Claude Old Name.app'
    s = dock.DockShortcut(s.path, s.profile_id, s.root, aliases=(alias,))
    before = [tile('/Applications/Other.app'), tile(alias, 2)]
    after = dock.plan_pins(before, [s])
    assert after[0] == before[0] and after[1]['GUID'] == 2
    assert dock.tile_path(after[1]) == s.path


def test_current_pin_wins_over_alias_without_duplicate(tmp_path):
    s = shortcuts(tmp_path)[1]
    alias = tmp_path/'Claude Old Name.app'
    s = dock.DockShortcut(s.path, s.profile_id, s.root, aliases=(alias,))
    before = [tile(dock.APP_PATH, 1), tile(alias, 2), tile(s.path, 3)]
    assert dock.plan_pins(before, [s]) == before


def test_new_pins_follow_existing_profile_pins_in_creation_order(tmp_path):
    work = shortcuts(tmp_path)[1]
    personal = dock.DockShortcut(tmp_path/'Claude Personal.app', 'c'*32, tmp_path)
    before = [tile(dock.APP_PATH, 1), tile('/Applications/Other.app', 2)]
    after = dock.plan_pins(before, [work], guid_factory=iter([3]).__next__)
    after = dock.plan_pins(after, [personal], guid_factory=iter([4]).__next__)
    assert [dock.tile_path(row) for row in after] == [dock.APP_PATH.resolve(), work.path, personal.path, Path('/Applications/Other.app')]


@pytest.mark.parametrize('before', [None, {}, [None]])
def test_unknown_dock_format_fails_without_guessing(tmp_path, before):
    with pytest.raises(ClaudeDesktopError): dock.plan_pins(before, shortcuts(tmp_path))


@pytest.mark.parametrize('raw', ['https://example.test/App.app', 'file://remote/Claude.app', 7])
def test_nonlocal_or_invalid_urls_are_not_treated_as_apps(raw):
    assert dock.tile_path({'tile-data': {'file-data': {'_CFURLString': raw}}}) is None


class Backend:
    def __init__(self, apps):
        self.apps = apps
        self.managed = Mock(return_value=False)
        self.refresh = Mock()
        self.write = Mock(side_effect=self._write)
    def snapshot(self):
        return copy.deepcopy(self.apps)
    def _write(self, apps):
        self.apps = copy.deepcopy(apps)


def built(store, builder):
    profile = store.add('Work')
    path = Path(builder.build(profile)['path'])
    return dock.DockShortcut(path, profile.id, store.root)


def test_installer_keeps_private_backup_and_retry_is_idempotent(store, builder):
    s = built(store, builder)
    before = [tile('/Applications/Other.app')]
    backend = Backend(before)
    assert dock.install_shortcuts(store, [s], backend=backend)['state'] == 'updated'
    backup, = (store.root/'dock-backups').glob('*.plist')
    assert backup.stat().st_mode & 0o777 == 0o600
    assert plistlib.loads(backup.read_bytes()) == {'persistent-apps': before}
    assert backend.apps[0] == before[0]
    assert dock.install_shortcuts(store, [s], backend=backend)['state'] == 'unchanged'
    backend.write.assert_called_once()
    backend.refresh.assert_called_once()
    dock.install_shortcuts(store, [s], backend=backend, refresh_icons=True)
    assert backend.refresh.call_count == 2 and backend.write.call_count == 1


@pytest.mark.parametrize('reason', ['managed', 'archived', 'foreign', 'symlink', 'concurrent'])
def test_unsafe_or_managed_setup_keeps_dock_untouched(store, builder, reason):
    s = built(store, builder)
    backend = Backend([tile('/Applications/Other.app')])
    if reason == 'managed': backend.managed.return_value = True
    if reason == 'archived': store.update(s.profile_id, archived=True)
    if reason == 'foreign':
        info = s.path/'Contents/Info.plist'
        info.write_bytes(plistlib.dumps({'OpenSwapProfile': s.marker, 'CFBundleIdentifier': 'unrelated.app'}))
    if reason == 'symlink':
        alias = s.path.with_name('Alias.app'); alias.symlink_to(s.path)
        s = dock.DockShortcut(alias, s.profile_id, s.root)
    if reason == 'concurrent':
        backend.snapshot = Mock(side_effect=[backend.apps, [tile('/Applications/User-added.app')]])
    with pytest.raises(ClaudeDesktopError): dock.install_shortcuts(store, [s], backend=backend)
    backend.write.assert_not_called(); backend.refresh.assert_not_called()


def test_installer_checks_ownership_against_store_root_not_caller_root(store, builder):
    s = built(store, builder)
    other = dock.DockShortcut(s.path, s.profile_id, Path('/other/store'))
    assert dock.install_shortcuts(store, [other], backend=Backend([]))['state'] == 'updated'
    info = s.path/'Contents/Info.plist'
    data = plistlib.loads(info.read_bytes()); data['OpenSwapProfile']['root'] = '/other/store'
    info.write_bytes(plistlib.dumps(data))
    backend = Backend([])
    with pytest.raises(ClaudeDesktopError): dock.install_shortcuts(store, [other], backend=backend)
    backend.write.assert_not_called()


def test_backups_keep_only_the_newest_five(store, builder):
    s = built(store, builder)
    backend = Backend([])
    for i in range(7):
        backend.apps = [tile(f'/Applications/App{i}.app', 100 + i)]
        dock.install_shortcuts(store, [s], backend=backend)
    backups = list((store.root/'dock-backups').glob('*.plist'))
    assert sorted(plistlib.loads(b.read_bytes())['persistent-apps'][0]['GUID'] for b in backups) == [102, 103, 104, 105, 106]


def test_installer_finds_renamed_owned_shortcut_but_not_other_store(store, builder):
    s = built(store, builder)
    old = s.path
    before = [tile(old)]
    store.update(s.profile_id, name='Renamed')
    path = Path(builder.build(store.get(s.profile_id))['path'])
    new = dock.DockShortcut(path, s.profile_id, store.root)
    backend = Backend(before)
    dock.install_shortcuts(store, [new], backend=backend)
    assert len(backend.apps) == 1 and dock.tile_path(backend.apps[0]) == path
    # Same ID in a different store does not prove ownership.
    info = old/'Contents/Info.plist'
    data = plistlib.loads(info.read_bytes()); data['OpenSwapProfile']['root'] = '/other/store'
    info.write_bytes(plistlib.dumps(data))
    backend = Backend(before)
    dock.install_shortcuts(store, [new], backend=backend)
    assert len(backend.apps) == 2 and backend.apps[0] == before[0]


@pytest.mark.parametrize('pin', [False, True])
def test_add_setup_creates_default_and_numbered_shortcuts_without_launching(store, builder, pin):
    existing(store)
    p = store.add('Work')
    creator = Mock(side_effect=lambda s, i, **kw: builder.build(s.get(i), **kw))
    installer = Mock()
    sc.prepare_shortcuts(store, p.id, pin=pin, creator=creator, installer=installer)
    assert [c.args[1] for c in creator.call_args_list] == [store.ensure_default().id, p.id]
    assert (builder.directory/'Claude Default.app').is_dir()
    assert (builder.directory/'Claude Work.app').is_dir()
    assert store.get(p.id).badge == '1'
    assert list(store.paths(p)[0].iterdir()) == []
    if pin:
        installer.assert_called_once()
        items = installer.call_args.args[1]
        assert len(items) == 1 and items[0].profile_id == p.id and not items[0].default
        assert installer.call_args.kwargs['refresh_icons'] is True
    else:
        installer.assert_not_called()


def test_add_keeps_current_launchers_and_restarts_dock_only_after_rebuild(store, builder):
    existing(store)
    creator = lambda s, i, **kw: builder.build(s.get(i), **kw)
    installer = Mock()
    work = store.add('Work')
    sc.prepare_shortcuts(store, work.id, pin=True, creator=creator, installer=installer)
    runs = builder.run.call_count
    personal = store.add('Personal')
    sc.prepare_shortcuts(store, personal.id, pin=True, creator=creator, installer=installer)
    assert builder.run.call_count == runs + 3  # Personal compiled and signed; Default kept
    assert installer.call_args.kwargs['refresh_icons'] is True
    sc.prepare_shortcuts(store, personal.id, pin=True, creator=creator, installer=installer)
    assert builder.run.call_count == runs + 3
    assert installer.call_args.kwargs['refresh_icons'] is False


@pytest.mark.parametrize('stale', ['version', 'command', 'target', 'badge'])
def test_add_rebuilds_default_launcher_that_no_longer_matches(store, builder, stale):
    existing(store)
    creator = lambda s, i, **kw: builder.build(s.get(i), **kw)
    default = store.ensure_default(); builder.build(default)
    info = builder.directory/'Claude Default.app'/'Contents/Info.plist'
    data = plistlib.loads(info.read_bytes())
    if stale == 'version': data['OpenSwapProfileLauncherVersion'] = 1
    if stale == 'command': data['OpenSwapProfileLauncher']['command'][-1] = '--other-flag'
    if stale == 'target': data['OpenSwapProfileLauncher']['target'] = '/Applications/Old Claude.app'
    if stale == 'badge': data['OpenSwapProfileLauncher']['badge'] = '3'
    info.write_bytes(plistlib.dumps(data))
    runs = builder.run.call_count
    sc.prepare_shortcuts(store, store.add('Work').id, pin=False, creator=creator, installer=Mock())
    assert builder.run.call_count == runs + 6
    assert plistlib.loads(info.read_bytes())['OpenSwapProfileLauncherVersion'] == sc.LAUNCHER_VERSION


def test_archived_default_is_not_restored_or_pinned_by_add(store, builder):
    existing(store); default = store.ensure_default(); store.update(default.id, archived=True)
    p = store.add('Work'); installer = Mock()
    sc.prepare_shortcuts(store, p.id, pin=True,
                         creator=lambda s, i, **kw: builder.build(s.get(i), **kw), installer=installer)
    assert len(installer.call_args.args[1]) == 1
    assert store.get(default.id, include_archived=True).archived


def test_creation_failure_keeps_profile_and_completed_default_without_changing_dock(store, builder):
    existing(store); p = store.add('Work'); installer = Mock()
    def create(s, identifier, **kw):
        if identifier == p.id: raise OSError('disk full')
        return builder.build(s.get(identifier), **kw)
    with pytest.raises(OSError): sc.prepare_shortcuts(store, p.id, pin=True, creator=create, installer=installer)
    installer.assert_not_called()
    assert store.get(p.id) == p and (builder.directory/'Claude Default.app').is_dir()
