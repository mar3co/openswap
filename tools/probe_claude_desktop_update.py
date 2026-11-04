"""Opt-in coordinated-update probe with disposable apps, never real Claude.

PYTHONPATH=src .venv/bin/python tools/probe_claude_desktop_update.py --launch
The installer is simulated; quit, process replacement and Dock helpers are native.
"""
import argparse
import json
import plistlib
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openswap import claude_desktop as cd
from openswap.claude_desktop_dock import DockShortcut
from openswap.claude_desktop_presence import open_presence
from openswap.claude_desktop_shortcut import launcher_script
from openswap.claude_desktop_update import DesktopUpdate, MacUpdateBackend


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", required=True)
    parser.parse_args()
    import AppKit as A
    import Foundation as F

    def pump(seconds):
        F.NSRunLoop.currentRunLoop().runUntilDate_(F.NSDate.dateWithTimeIntervalSinceNow_(seconds))

    def running(bundle):
        return [a for a in A.NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle) if not a.isTerminated()]

    def wait(predicate):
        for _ in range(150):
            result = predicate()
            if result:
                return result
            pump(.1)
        raise RuntimeError("Fixture lifecycle timed out")

    with tempfile.TemporaryDirectory(prefix="openswap-update-probe-") as raw:
        root = Path(raw).resolve()
        original_root = cd.default_root
        cd.default_root = lambda: root / "coordination"
        bundles = []

        def build(name, script, bundle):
            app = root / (name + ".app")
            subprocess.run(["/usr/bin/osacompile", "-o", str(app), "-s", "-"],
                           input=script, text=True, capture_output=True, check=True)
            info = app / "Contents/Info.plist"
            data = plistlib.loads(info.read_bytes())
            data.update(CFBundleIdentifier=bundle, LSUIElement=False)
            info.write_bytes(plistlib.dumps(data))
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(app)], capture_output=True, check=True)
            bundles.append(bundle)
            return app

        def launch(path, bundle):
            subprocess.run(["/usr/bin/open", "-g", str(path)], capture_output=True, check=True)
            return wait(lambda: running(bundle))[0]

        try:
            store = cd.DesktopProfiles(root / "profiles", default_desktop=root / "no-default")
            profiles = [store.add("Fixture one"), store.add("Fixture two")]
            store.add("Closed fixture")
            fixtures, helpers, replies, targets = {}, {}, {}, {}
            script = "on run\nend run\non idle\nreturn 1\nend idle\n"
            for profile in profiles:
                bundle = "com.opensoft.openswap.update-probe." + uuid.uuid4().hex
                app = build(profile.id, script, bundle)
                native = launch(app, bundle)
                fixtures[profile.id] = (app, bundle)
                targets[profile.id] = native
                reply = root / (profile.id + ".json")
                reply.write_text(json.dumps({"pid": native.processIdentifier()}))
                replies[profile.id] = reply
                helper_bundle = DockShortcut(Path("unused.app"), profile.id, store.root).bundle_id
                helper = build("Dock " + profile.id,
                               launcher_script(["/bin/cat", str(reply)], app_path=app, bundle_id=bundle), helper_bundle)
                helpers[profile.id] = (helper, helper_bundle)
                open_presence(helper, helper_bundle)
                wait(lambda: running(helper_bundle))
            unrelated_id = "com.opensoft.openswap.update-probe." + uuid.uuid4().hex
            unrelated = launch(build("Unrelated", script, unrelated_id), unrelated_id)
            pump(2)
            original_targets = dict(targets)
            old_helpers = {p: running(bundle)[0] for p, (_, bundle) in helpers.items()}

            class FixtureBackend(MacUpdateBackend):
                current = "1.0.0"

                def installed(self):
                    if all(n.isTerminated() for n in original_targets.values()):
                        self.current = "2.0.0"
                    return self.current, (self.current,)

                def staged(self):
                    return "2.0.0"

                def installer_running(self):
                    return True

                def snapshot(self, store):
                    return {p: n for p, n in targets.items() if not n.isTerminated()}

                def restore(self, store, identifier):
                    app, bundle = fixtures[identifier]
                    native = launch(app, bundle)
                    targets[identifier] = native
                    replies[identifier].write_text(json.dumps({"pid": native.processIdentifier()}))

                def attach(self, store, identifier):
                    app, bundle = helpers[identifier]
                    open_presence(app, bundle)
                    wait(lambda: running(bundle))

            backend = FixtureBackend()
            coordinator = DesktopUpdate(store, backend=backend)
            # Match the menu: native operations run on a worker while the main
            # thread keeps processing AppKit notifications.
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: coordinator.execute(coordinator.plan()))
                wait(future.done)
                result = future.result()
            pump(2)
            assert result["version"] == "2.0.0" and not coordinator.pending()
            assert all(n.isTerminated() for n in original_targets.values())
            assert all(n.isTerminated() for n in old_helpers.values())
            assert all(not n.isTerminated() for n in targets.values())
            assert all(len(running(bundle)) == 1 for _, bundle in helpers.values())
            assert not unrelated.isTerminated()
            print(json.dumps({"ok": True, "restored_profiles": len(targets), "native_dock_helpers": len(helpers),
                              "unrelated_app_preserved": True, "real_claude_touched": False}))
        finally:
            # Only the randomly named apps this probe created are cleaned up.
            for bundle in bundles:
                for native in running(bundle):
                    native.terminate()
            pump(2)
            cd.default_root = original_root


if __name__ == "__main__":
    main()
