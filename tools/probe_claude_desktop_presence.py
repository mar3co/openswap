"""Opt-in macOS Dock lifecycle probe using disposable apps, never Claude data.

PYTHONPATH=src python tools/probe_claude_desktop_presence.py --launch
Requires the menu-bar Python dependencies. Briefly activates fixture apps.
"""
import argparse
import json
import plistlib
import subprocess
import tempfile
import uuid
from pathlib import Path

from openswap.claude_desktop_shortcut import launcher_script


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--launch', action='store_true', required=True)
    parser.parse_args()
    import AppKit as A
    import Foundation as F

    def pump(count=1):
        for _ in range(count):
            F.NSRunLoop.currentRunLoop().runUntilDate_(F.NSDate.dateWithTimeIntervalSinceNow_(.1))

    def running(bundle):
        return [app for app in A.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) if not app.isTerminated()]

    def wait(predicate):
        for _ in range(100):
            result = predicate()
            if result:
                return result
            pump()
        raise RuntimeError('Timed out waiting for the fixture lifecycle.')

    with tempfile.TemporaryDirectory(prefix='openswap-dock-probe-') as raw:
        root = Path(raw).resolve()
        bundles = {}

        def build(name, script):
            bundle = 'com.opensoft.openswap.probe.' + uuid.uuid4().hex
            path = root / (name + '.app')
            subprocess.run(['/usr/bin/osacompile', '-o', str(path), '-s', '-'],
                           input=script, text=True, capture_output=True, check=True)
            info = path / 'Contents/Info.plist'
            data = plistlib.loads(info.read_bytes())
            data.update(CFBundleIdentifier=bundle, LSUIElement=False)
            info.write_bytes(plistlib.dumps(data))
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(path)], capture_output=True, check=True)
            bundles[bundle] = path
            return path, bundle

        def launch(path, *, attach=False):
            args = ['/usr/bin/open', '-g', str(path)]
            if attach:
                args += ['--args', '--openswap-attach']
            subprocess.run(args, capture_output=True, check=True)

        fixture = 'on run\nend run\non idle\nreturn 1\nend idle\n'
        try:
            other_path, other_id = build('Unrelated fixture', fixture)
            launch(other_path)
            other = wait(lambda: running(other_id))[0]
            for mode in ('external', 'dock', 'crash', 'canceled-quit', 'attach'):
                script = fixture + ('on quit\nreturn\nend quit\n' if mode == 'canceled-quit' else '')
                target_path, target_id = build('Session ' + mode, script)
                launch(target_path)
                target = wait(lambda: running(target_id))[0]
                command = ['/usr/bin/printf', '%s', json.dumps({'ok': True, 'pid': target.processIdentifier()})]
                helper_path, helper_id = build('Profile ' + mode, launcher_script(command, app_path=target_path, bundle_id=target_id))
                frontmost = A.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier()
                launch(helper_path, attach=mode == 'attach')
                helper = wait(lambda: running(helper_id))[0]
                pump(20)
                assert not helper.isTerminated() and helper.activationPolicy() == 0
                if mode == 'attach':
                    assert A.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier() == frontmost
                # Reopen (Dock click) twice: exact target, no duplicate helpers.
                for _ in range(2):
                    subprocess.run(['/usr/bin/open', str(helper_path)], capture_output=True, check=True)
                    pump(10)
                    wait(lambda: A.NSWorkspace.sharedWorkspace().frontmostApplication().processIdentifier() == target.processIdentifier())
                assert len(running(helper_id)) == 1
                if mode in ('external', 'attach'):
                    assert target.terminate()
                elif mode == 'crash':
                    assert target.forceTerminate()
                else:
                    assert helper.terminate()
                    if mode == 'canceled-quit':
                        pump(20)
                        assert not target.isTerminated() and not helper.isTerminated()
                        assert target.forceTerminate()
                wait(lambda: target.isTerminated())
                wait(lambda: helper.isTerminated())
                assert not other.isTerminated()
                subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(helper_path)], capture_output=True, check=True)
                print(mode + ': Dock presence, exact focus, lifecycle and signature passed', flush=True)

            # A profile that closed during passive attachment must stay closed.
            helper_path, helper_id = build('Closed profile', launcher_script(
                ['/usr/bin/printf', '%s', json.dumps({'ok': True, 'pid': None})],
                app_path=other_path, bundle_id=other_id))
            launch(helper_path, attach=True)
            pump(25)
            assert not running(helper_id) and not other.isTerminated()
            print('Closed profile attachment exited silently.', flush=True)
        finally:
            # Only fixture bundles created in this unique temporary directory.
            for bundle, path in bundles.items():
                for app in running(bundle):
                    if app.bundleURL() and Path(app.bundleURL().path()) == path:
                        app.forceTerminate()
            pump(10)


if __name__ == '__main__':
    main()
