"""Profile-specific Dock pins, preserving unrelated apps and their order."""
from __future__ import annotations

import copy
import os
import plistlib
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.parsers.expat import ExpatError

from openswap.claude_desktop import APP_PATH, ClaudeDesktopError, _plain

BUNDLE_PREFIX = "com.opensoft.openswap.claude-profile."
BACKUPS_KEPT = 5


@dataclass(frozen=True)
class DockShortcut:
    path: Path
    profile_id: str
    root: Path
    default: bool = False
    aliases: tuple[Path, ...] = ()

    @property
    def bundle_id(self):
        return BUNDLE_PREFIX + self.profile_id

    @property
    def marker(self):
        return {"version": 1, "profile": self.profile_id, "root": str(self.root)}


def tile_path(tile):
    try:
        raw = tile.get("tile-data", {}).get("file-data", {}).get("_CFURLString", "")
        url = urlsplit(raw)
        if url.scheme == "file" and url.netloc in ("", "localhost"):
            return Path(unquote(url.path)).resolve()
    except (ValueError, TypeError, AttributeError, OSError):
        pass
    return None


def _profile_tile(row):
    data = row.get("tile-data")
    bundle = data.get("bundle-identifier") if isinstance(data, dict) else None
    return isinstance(bundle, str) and bundle.startswith(BUNDLE_PREFIX)


def _tile(shortcut, guid):
    return {"GUID": guid, "tile-type": "file-tile", "tile-data": {
        "dock-extra": False, "file-type": 41, "bundle-identifier": shortcut.bundle_id,
        "file-label": shortcut.path.stem,
        "file-data": {"_CFURLString": shortcut.path.as_uri() + "/", "_CFURLStringType": 15}}}


def plan_pins(existing, shortcuts, *, claude_app=APP_PATH, guid_factory=lambda: uuid.uuid4().int & 0xffffffff):
    """Pure plan: add profile pins; update only validated profile aliases.

    A pin at the current path wins over aliases, which are rewritten only when
    no current pin exists. New pins follow the profile launchers already next
    to Claude, so they appear in creation order. User-created duplicates and
    other apps are not removed. Repeating setup never adds another pin.
    """
    if not isinstance(existing, list) or any(not isinstance(row, dict) for row in existing):
        raise ClaudeDesktopError("Could not read Dock apps. The shortcuts were kept; try Dock setup again.")
    result = copy.deepcopy(existing)
    used = {row.get("GUID") for row in result}
    # Keep Claude's own pin and running indicator. Use it only as an anchor
    # for additional profile launchers, never as a replacement target.
    anchor = next((i for i, row in enumerate(result) if tile_path(row) == claude_app.resolve()), None)
    for shortcut in sorted(shortcuts, key=lambda s: not s.default):
        paths = [tile_path(row) for row in result]
        if shortcut.path.resolve() in paths:
            continue
        aliases = {p.resolve() for p in shortcut.aliases}
        alias = next((i for i, p in enumerate(paths) if p in aliases), None)
        if alias is not None:
            result[alias] = _tile(shortcut, result[alias].get("GUID"))
            continue
        guid = guid_factory()
        while guid in used:
            guid = guid_factory()
        used.add(guid)
        index = len(result) if anchor is None else anchor + 1
        while index < len(result) and _profile_tile(result[index]):
            index += 1
        result.insert(index, _tile(shortcut, guid))
    return result


def _owned_bundle(path, shortcut):
    try:
        info = path / "Contents/Info.plist"
        if (path.is_symlink() or (path / "Contents").is_symlink() or info.is_symlink()
                or info.stat().st_size > 65536):
            return False
        data = plistlib.loads(info.read_bytes())
        return data.get("OpenSwapProfile") == shortcut.marker and data.get("CFBundleIdentifier") == shortcut.bundle_id
    except (OSError, ValueError, TypeError, AttributeError, ExpatError):
        return False


class NativeDock:
    def __init__(self):
        from Foundation import NSUserDefaults
        self.preferences = NSUserDefaults.alloc().initWithSuiteName_("com.apple.dock")

    def snapshot(self):
        from Foundation import NSPropertyListSerialization, NSPropertyListBinaryFormat_v1_0
        self.preferences.synchronize()
        domain = self.preferences.persistentDomainForName_("com.apple.dock")
        if domain is None:
            raise ClaudeDesktopError("Dock settings are unavailable. The shortcuts were kept; try Dock setup again.")
        data, error = NSPropertyListSerialization.dataWithPropertyList_format_options_error_(
            domain, NSPropertyListBinaryFormat_v1_0, 0, None)
        if error is not None:
            raise ClaudeDesktopError("Could not read Dock settings. No pins were changed.")
        return plistlib.loads(bytes(data)).get("persistent-apps")

    def managed(self):
        return self.preferences.objectIsForcedForKey_("persistent-apps")

    def write(self, apps):
        # Change only this key, never replace the entire preferences domain.
        self.preferences.setObject_forKey_(apps, "persistent-apps")
        if not self.preferences.synchronize():
            raise ClaudeDesktopError("Could not save Dock shortcuts. Try Dock setup again.")

    def refresh(self):
        from AppKit import NSRunningApplication
        for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.dock"):
            url = app.bundleURL()
            if url is not None and url.path() == "/System/Library/CoreServices/Dock.app":
                if not app.forceTerminate():
                    raise ClaudeDesktopError("Shortcuts were saved. Reopen the Dock to refresh their icons.")


def _prune_backups(directory):
    backups = sorted(directory.glob("*.plist"), key=lambda p: p.lstat().st_mtime_ns)
    for stale in backups[:-BACKUPS_KEPT]:
        stale.unlink()


def install_shortcuts(store, shortcuts, *, backend=None, refresh_icons=False):
    """Called only for the checked Add-to-Dock option or explicit setup action.

    Ownership is checked against this store's root, never a caller-supplied one.
    """
    backend = backend or NativeDock()
    shortcuts = [DockShortcut(s.path, s.profile_id, store.root, s.default) for s in shortcuts]
    with store._lock():
        # Store-level lock also serializes setup from this app's CLI/UI.
        for shortcut in shortcuts:
            store.get(shortcut.profile_id)  # no archived/stale profile pins
            if not _owned_bundle(shortcut.path, shortcut):
                raise ClaudeDesktopError("A profile shortcut changed. Try Dock setup again.")
        if backend.managed():
            raise ClaudeDesktopError("Your organization manages the Dock. Profile shortcuts are ready in Applications → OpenSwap Profiles.")
        before = backend.snapshot()
        if not isinstance(before, list):
            raise ClaudeDesktopError("Could not read Dock apps. No pins were changed.")
        resolved = []
        for shortcut in shortcuts:
            aliases = tuple(p for row in before if (p := tile_path(row)) is not None
                            and p != shortcut.path.resolve() and _owned_bundle(p, shortcut))
            resolved.append(replace(shortcut, aliases=aliases))
        after = plan_pins(before, resolved)
        changed = after != before
        if changed:
            backup_dir = store.root / "dock-backups"
            if not _plain(backup_dir, directory=True):
                backup_dir.mkdir(mode=0o700)
            backup = backup_dir / (uuid.uuid4().hex + ".plist")
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                plistlib.dump({"persistent-apps": before}, stream, fmt=plistlib.FMT_BINARY)
            _prune_backups(backup_dir)
            if backend.snapshot() != before:
                raise ClaudeDesktopError("The Dock changed during setup. No pins were changed; try Dock setup again.")
            backend.write(after)
            if backend.snapshot() != after:
                raise ClaudeDesktopError("Could not confirm the Dock update. The shortcuts and a backup were kept.")
    if changed or refresh_icons:
        # Dock only; never terminate Claude or other apps. Callers pass
        # refresh_icons only when a pinned launcher was rebuilt.
        backend.refresh()
    return {"state": "updated" if changed else "unchanged"}
