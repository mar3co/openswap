"""Attach a resident Dock launcher to an existing profile without reopening it."""
from __future__ import annotations

import subprocess
from pathlib import Path

from openswap.claude_desktop import ClaudeDesktopError
from openswap.claude_desktop_dock import DockShortcut, owned_bundle, launcher_info
from openswap.claude_desktop_shortcut import LAUNCHER_VERSION, create_shortcut, shortcut_directory, shortcut_name


def open_presence(path: Path, bundle_id: str) -> None:
    try:
        from AppKit import NSRunningApplication
    except ImportError as exc:
        raise ClaudeDesktopError("Open the OpenSwap app to show this profile's running indicator.") from exc

    # Do not send a reopen event during a passive status refresh. That would
    # steal focus from whatever the user is working on.
    for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id):
        if app.isTerminated():
            continue
        actual = Path(app.bundleURL().path()).resolve() if app.bundleURL() is not None else None
        if actual == path.resolve():
            return
        # Rename keeps old launchers usable. Recognize the same profile there
        # instead of creating another app or sending it a focus-stealing reopen.
        expected = owned_bundle(path)
        if actual is not None and expected is not None and owned_bundle(actual, expected):
            return
        raise ClaudeDesktopError("Could not match the running profile icon. Close the other profile launcher and try again.")
    try:
        result = subprocess.run(["/usr/bin/open", "-g", str(path), "--args", "--openswap-attach"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClaudeDesktopError("Could not show this profile's running indicator. Choose Set up Dock shortcuts to retry.") from exc
    if result.returncode:
        raise ClaudeDesktopError("Could not show this profile's running indicator. Choose Set up Dock shortcuts to retry.")


def attach_presence(store, identifier: str, *, directory=None, creator=None, opener=None) -> bool:
    profile = store.get(identifier)
    # The ordinary Claude app already represents Default. A user can still
    # explicitly open Default's separate profile app from Finder if desired.
    if profile.kind == "default":
        return False
    path = (directory or shortcut_directory()) / shortcut_name(profile.name)
    shortcut = DockShortcut(path, profile.id, store.root)
    info = launcher_info(path)
    if info is None or not owned_bundle(path, shortcut, info=info):
        return False  # never create an unexpected Dock app or open an unrelated file
    if info.get("OpenSwapProfileLauncherVersion") != LAUNCHER_VERSION:
        result = (creator or create_shortcut)(store, profile.id, directory=path.parent)
        path = Path(result["path"])
        if path != shortcut.path or not owned_bundle(path, shortcut):
            raise ClaudeDesktopError("The profile shortcut changed during setup. Choose Set up Dock shortcuts to retry.")
    (opener or open_presence)(path, shortcut.bundle_id)
    return True
