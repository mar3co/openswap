"""Opt-in macOS smoke check; opens two empty profiles and closes only its children.

Run with PYTHONPATH=src python tools/probe_claude_desktop_profiles.py --launch.
Do not sign in or start work in the temporary windows. This does not establish
authenticated isolation, OAuth callback routing, Code or Cowork behavior.
"""

import argparse
import json
import subprocess
import tempfile
import time
from pathlib import Path

from openswap.claude_desktop import ClaudeDesktopApp, DesktopProfiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", required=True)
    parser.parse_args()
    children = []

    def spawn(*args, **kwargs):
        proc = subprocess.Popen(*args, **kwargs)
        children.append(proc)
        return proc

    app = ClaudeDesktopApp(popen=spawn)
    info = app.validate()
    with tempfile.TemporaryDirectory(prefix="openswap-claude-probe-") as raw:
        store = DesktopProfiles(Path(raw) / "profiles")
        profiles = [store.add("Probe A"), store.add("Probe B")]
        results = []
        try:
            for p in profiles:
                results.append(store.open(p.id, app=app))
            time.sleep(5)
            if any(p.poll() is not None for p in children):
                raise RuntimeError("A temporary Claude process exited before the observation.")
            for p, result in zip(profiles, results):
                repeated = store.open(p.id, app=app)
                if repeated["state"] != "already_running" or repeated["pid"] != result["pid"]:
                    raise RuntimeError("Repeated open did not reuse the intended profile.")
            observations = []
            for p in profiles:
                desktop, code = store.paths(p)
                # Only names and booleans; never log cookies, tokens, or config contents.
                observations.append({"profile": p.name, "desktop_files_created": any(desktop.iterdir()),
                                     "code_config_created": (code / ".claude.json").exists(),
                                     "desktop_entries": sorted(f.name for f in desktop.iterdir())})
            print(json.dumps({"app_version": info["version"], "publisher_verified": True,
                              "two_processes_survived": True, "repeat_open_reused_process": True,
                              "identity_verified": False, "profiles": observations}, indent=2))
        finally:
            # These are Popen objects we just created, never a bundle-name kill.
            # A user profile is never opened by this script.
            for proc in children:
                if proc.poll() is None:
                    proc.terminate()
            for proc in children:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    main()
