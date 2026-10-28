"""Locally generated macOS launchers; Claude's signed app is never modified."""

from __future__ import annotations

import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, replace
from pathlib import Path
from xml.parsers.expat import ExpatError

from openswap.claude_desktop import (
    APP_PATH, BUNDLE_ID, BADGE_COLORS, ClaudeDesktopApp, ClaudeDesktopError, DesktopProfiles, _plain, validate_badge,
)
from openswap.claude_desktop_dock import DockShortcut, _owned_bundle, install_shortcuts
from openswap.locking import FileLock

LAUNCHER_VERSION = 2
ICON_NOTICE = (
    "The numbered icon shows a running dot while this profile is open. "
    "Click it to return to that session. Claude’s original icon stays available."
)


def shortcut_directory() -> Path:
    return Path.home() / "Applications" / "OpenSwap Profiles"


def shortcut_name(name: str) -> str:
    # Profile names remain labels, never paths or source code.
    safe = re.sub(r'[/\\:]', "-", name).strip(" .") or "Profile"
    return f"Claude {safe}.app"


def launcher_command(store: DesktopProfiles, identifier: str) -> list[str]:
    from openswap import bundle

    executable = bundle.executable()
    if executable is not None:
        if bundle.runs_from_temporary_location():
            raise ClaudeDesktopError("Move OpenSwap to Applications before creating shortcuts.")
        command = [str(executable)]
    else:
        # Preserve the venv's executable path, not its resolved base interpreter.
        # Explicit source path also supports an editable development installation.
        source = Path(__file__).resolve().parent.parent
        command = ["/usr/bin/env", f"PYTHONPATH={source}", sys.executable, "-m", "openswap.cli"]
    return [*command, "claude-desktop", "open", identifier, "--profile-root", str(store.root), "--focus"]


def _apple_string(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def launcher_script(command: list[str], *, attach_command=None, app_path=APP_PATH, bundle_id=BUNDLE_ID) -> str:
    # Compile a regular, stay-open applet. AppKit retains the exact process
    # object (including after exit), so PID reuse cannot select another app.
    source = (Path(__file__).parent / "assets/profile-launcher.applescript").read_text()
    for token, value in {
        '"OPEN_COMMAND"': shlex.join(command),
        '"ATTACH_COMMAND"': shlex.join(attach_command or command),
        '"TARGET_BUNDLE"': bundle_id,
        '"TARGET_PATH"': str(Path(app_path).resolve()),
    }.items():
        source = source.replace(token, _apple_string(value))
    return source


def original_icon(app_path: Path) -> Path:
    """Use Claude's original artwork, before macOS adds its surrounding plate."""
    try:
        info = plistlib.loads((app_path / "Contents/Info.plist").read_bytes())
        name = info["CFBundleIconFile"]
        if not isinstance(name, str) or not name or Path(name).name != name:
            raise ValueError
        name = name if name.endswith(".icns") else name + ".icns"
        resources = (app_path / "Contents/Resources").resolve()
        path = resources / name
        if path.is_symlink() or path.resolve().parent != resources or not path.is_file():
            raise ValueError
        return path
    except (OSError, KeyError, ValueError, TypeError, ExpatError) as exc:
        raise ClaudeDesktopError("Could not read Claude's original app icon. Reinstall Claude and try again.") from exc


def _icon_bitmap(base, mark, badge: str, color: str, px: int):
    """One renderer for shortcut files and the onboarding preview."""
    import AppKit as A
    import Foundation as F

    rep = A.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, px, px, 8, 4, True, False, A.NSDeviceRGBColorSpace, 0, 0)
    ctx = A.NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    A.NSGraphicsContext.saveGraphicsState()
    try:
        A.NSGraphicsContext.setCurrentContext_(ctx)
        base.drawInRect_fromRect_operation_fraction_(A.NSMakeRect(0, 0, px, px), A.NSZeroRect, A.NSCompositingOperationCopy, 1.0)
        # Keep overlays inside Claude's rounded outline. Tahoe
        # encloses icons with protruding badges in a gray plate,
        # even when the generated ICNS itself has no frame.
        brand = A.NSMakeRect(px * .13, px * .13, px * .24, px * .24)
        A.NSColor.colorWithSRGBRed_green_blue_alpha_(.98, .97, .94, 1).setFill()
        A.NSBezierPath.bezierPathWithOvalInRect_(brand).fill()
        mark.drawInRect_fromRect_operation_fraction_(
            A.NSMakeRect(brand.origin.x + px * .024, brand.origin.y + px * .024,
                         brand.size.width - px * .048, brand.size.height - px * .048),
            A.NSZeroRect, A.NSCompositingOperationSourceOver, 1.0)
        if badge:
            diameter = px * (0.34 if len(badge) == 3 else 0.30)
            box = A.NSMakeRect(px - diameter - px * .12, px - diameter - px * .12, diameter, diameter)
            A.NSColor.whiteColor().setFill()
            A.NSBezierPath.bezierPathWithOvalInRect_(box).fill()
            inset = px * .018
            inner = A.NSMakeRect(box.origin.x + inset, box.origin.y + inset, diameter - 2 * inset, diameter - 2 * inset)
            A.NSColor.colorWithSRGBRed_green_blue_alpha_(*BADGE_COLORS[color], 1.0).setFill()
            A.NSBezierPath.bezierPathWithOvalInRect_(inner).fill()
            font = A.NSFont.boldSystemFontOfSize_(diameter * (0.44 if len(badge) == 3 else .59))
            attrs = {A.NSFontAttributeName: font, A.NSForegroundColorAttributeName: A.NSColor.whiteColor()}
            text = F.NSString.stringWithString_(badge)
            bounds = text.sizeWithAttributes_(attrs)
            text.drawAtPoint_withAttributes_((box.origin.x + (diameter-bounds.width)/2, box.origin.y + (diameter-bounds.height)/2), attrs)
    finally:
        A.NSGraphicsContext.restoreGraphicsState()
    return rep


def render_icon(app_path: Path, target: Path, badge: str, color: str, *, run=subprocess.run) -> None:
    """Original Claude artwork, a small OpenSwap mark and an optional number."""
    validate_badge(badge, color)
    try:
        import AppKit as A
        import Foundation as F
    except ImportError as exc:
        raise ClaudeDesktopError("Install OpenSwap with menu bar support to create shortcut icons.") from exc
    pool = F.NSAutoreleasePool.alloc().init()
    try:
        base = A.NSImage.alloc().initWithContentsOfFile_(str(original_icon(app_path)))
        if base is None or not base.isValid():
            raise ClaudeDesktopError("Could not read Claude's app icon. Reinstall Claude and try again.")
        mark = A.NSImage.alloc().initWithContentsOfFile_(str(Path(__file__).parent / "assets/opensoft-symbol.svg"))
        if mark is None or not mark.isValid():
            raise ClaudeDesktopError("OpenSwap's icon is missing. Reinstall OpenSwap and try again.")
        with tempfile.TemporaryDirectory(prefix="openswap-icon-") as raw:
            iconset = Path(raw) / "Profile.iconset"
            iconset.mkdir()
            for size in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    px = size * scale
                    rep = _icon_bitmap(base, mark, badge, color, px)
                    data = rep.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})
                    suffix = "@2x" if scale == 2 else ""
                    if not data.writeToFile_atomically_(str(iconset / f"icon_{size}x{size}{suffix}.png"), True):
                        raise ClaudeDesktopError("Could not write the shortcut icon.")
            _run(run, ["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(target)])
    finally:
        del pool


def _run(run, argv, **kwargs):
    try:
        result = run(argv, capture_output=True, text=True, timeout=45, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClaudeDesktopError("macOS could not create the shortcut. Try again after checking disk space and permissions.") from exc
    if result.returncode:
        raise ClaudeDesktopError("macOS could not create the shortcut. Check available disk space and try again.")
    return result


def _destination_lock(directory: Path):
    path = directory / ".openswap-shortcuts.lock"
    if not _plain(path):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            _plain(path)
    return FileLock(path, timeout=1)


def _check_destination(path: Path, shortcut: DockShortcut) -> None:
    if path.is_symlink():
        raise ClaudeDesktopError("The shortcut destination is a symbolic link. Choose another folder.")
    if path.exists() and not _owned_bundle(path, shortcut):
        raise ClaudeDesktopError("Another app or file already uses this shortcut name. Rename the profile or choose another folder; nothing was replaced.")


def _current_launcher(path: Path, shortcut: DockShortcut, record: dict) -> bool:
    """An owned bundle built from the same command, Claude app and badge needs no rebuild."""
    if not _owned_bundle(path, shortcut):
        return False
    try:
        info = plistlib.loads((path / "Contents/Info.plist").read_bytes())
    except (OSError, ValueError, ExpatError):
        return False
    return info.get("OpenSwapProfileLauncherVersion") == LAUNCHER_VERSION and info.get("OpenSwapProfileLauncher") == record


def create_shortcut(store: DesktopProfiles, identifier: str, *, badge=None, color=None,
                    directory: Path | None = None, app=None, run=None, icon_renderer=None,
                    reuse_current=False) -> dict:
    app, run = app or ClaudeDesktopApp(), run or subprocess.run
    info = app.validate()
    directory = (directory if directory is not None else shortcut_directory()).absolute()
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ClaudeDesktopError("Choose a regular folder for your shortcuts.")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    # The registry lock guards metadata reads and the final write only, never
    # the build: osacompile, iconutil and codesign take seconds.
    with store._lock():
        profile = store.get(identifier)  # archived shortcuts cannot be recreated
        store._directories(profile)
    badge = profile.badge if badge is None else badge
    color = profile.color if color is None else color
    validate_badge(badge, color)
    path = directory / shortcut_name(profile.name)
    shortcut = DockShortcut(path, profile.id, store.root, profile.kind == "default")
    command = launcher_command(store, profile.id)
    record = {"command": command, "target": str(Path(app.path).resolve()), "badge": badge, "color": color}
    result = {"path": str(path), "app_version": info["version"], "notice": ICON_NOTICE}
    with _destination_lock(directory):
        if reuse_current and (badge, color) == (profile.badge, profile.color) and _current_launcher(path, shortcut, record):
            return {**result, "profile": asdict(profile), "state": "shortcut_kept"}
        _check_destination(path, shortcut)
        with tempfile.TemporaryDirectory(prefix=".openswap-shortcut-", dir=directory) as raw:
            stage = Path(raw) / path.name
            _run(run, ["/usr/bin/osacompile", "-o", str(stage), "-s", "-"], input=launcher_script(
                [*command, "--json", "--from-launcher"],
                attach_command=[*(arg for arg in command if arg != "--focus"), "--json", "--from-launcher", "--existing"],
                app_path=app.path))
            contents = stage / "Contents"
            plist_path = contents / "Info.plist"
            try:
                plist = plistlib.loads(plist_path.read_bytes())
                if not isinstance(plist, dict):
                    raise ValueError
            except (OSError, ValueError, ExpatError, plistlib.InvalidFileException) as exc:
                raise ClaudeDesktopError("macOS produced an incomplete shortcut. Try creating it again.") from exc
            plist.update(CFBundleIdentifier=shortcut.bundle_id,
                         CFBundleName=f"Claude {profile.name}", CFBundleDisplayName=f"Claude {profile.name}",
                         CFBundleIconFile="Profile.icns", LSUIElement=False, OpenSwapProfile=shortcut.marker,
                         OpenSwapProfileLauncherVersion=LAUNCHER_VERSION, OpenSwapProfileLauncher=record)
            plist.pop("CFBundleIconName", None)
            plist_path.write_bytes(plistlib.dumps(plist))
            (icon_renderer or render_icon)(app.path, contents / "Resources" / "Profile.icns", badge, color)
            # Sign only our generated launcher after writing its icon and plist.
            _run(run, ["/usr/bin/codesign", "--force", "--sign", "-", str(stage)])
            _run(run, ["/usr/bin/codesign", "--verify", "--strict", str(stage)])
            _check_destination(path, shortcut)
            previous = Path(raw) / "previous.app"
            if path.exists():
                os.replace(path, previous)
            installed = False
            try:
                os.replace(stage, path)
                installed = True
                with store._lock():
                    # Re-read under the lock: an archive or rename during the
                    # build must leave neither a launcher nor a saved badge.
                    current = store.get(profile.id, include_archived=True)
                    if current.archived or current.name != profile.name:
                        raise ClaudeDesktopError("The profile changed while its shortcut was being created. Nothing was saved; try again.")
                    updated = replace(current, badge=badge, color=color)
                    rows = store.list(include_archived=True)
                    store._save([updated if p.id == profile.id else p for p in rows])
            except Exception:
                # Roll back only the launcher we just built, never profile data.
                if installed:
                    shutil.rmtree(path)
                if previous.exists():
                    os.replace(previous, path)
                raise
    return {**result, "profile": asdict(updated), "state": "shortcut_created"}


def prepare_shortcuts(store: DesktopProfiles, identifier: str, *, pin: bool, creator=None, installer=None) -> dict:
    """Complete Add's shortcut setup, or retry it without adding another profile.

    Partial failures keep the profile and any completed shortcuts. Dock changes
    happen only after all required launchers exist; the caller can still open
    Claude when setup needs attention. Launchers already built from the current
    command, Claude app and badge are kept, so the Dock restarts only when the
    pinned launcher was rebuilt or its pin is new.
    """
    creator, installer = creator or create_shortcut, installer or install_shortcuts
    profile = store.get(identifier)
    default = store.ensure_default()
    profiles = ([default] if default and not default.archived and default.id != profile.id else []) + [profile]
    shortcuts, rebuilt = [], False
    for item in profiles:
        result = creator(store, item.id, reuse_current=True)
        shortcuts.append(DockShortcut(Path(result["path"]), item.id, store.root, item.kind == "default"))
        if item.id == profile.id:
            rebuilt = result["state"] == "shortcut_created"
    if pin:
        # Add only the requested profile to the Dock. Default's shortcut is
        # available in Finder, but never replaces Claude's ordinary launcher.
        installer(store, [s for s in shortcuts if s.profile_id == profile.id], refresh_icons=rebuilt)
    return {"state": "dock_ready" if pin else "shortcuts_ready", "paths": [str(s.path) for s in shortcuts]}
