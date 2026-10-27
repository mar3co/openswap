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
    BADGE_COLORS, ClaudeDesktopApp, ClaudeDesktopError, DesktopProfiles, _plain, validate_badge,
)
from openswap.locking import FileLock

ICON_NOTICE = (
    "The badge appears on the shortcut in Finder and the Dock. "
    "Claude's running icon stays unchanged."
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


def launcher_script(command: list[str]) -> str:
    # Shell escaping and AppleScript escaping are different layers. The command
    # is a fixed argv vector; no name or path is interpolated as executable text.
    shell = shlex.join(command)
    return f'''on run
    try
        with timeout of 120 seconds
            do shell script {_apple_string(shell)}
        end timeout
    on error messageText number errorNumber
        display alert "Could not open Claude profile" message (messageText & return & return & "If OpenSwap was moved or removed, open OpenSwap and create this shortcut again.") as warning
    end try
end run
'''


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


def _check_destination(path: Path, marker: dict) -> None:
    if path.is_symlink():
        raise ClaudeDesktopError("The shortcut destination is a symbolic link. Choose another folder.")
    if not path.exists():
        return
    try:
        info = path / "Contents" / "Info.plist"
        if (not path.is_dir() or (path / "Contents").is_symlink() or info.is_symlink()
                or info.stat().st_size > 65536):
            raise ValueError
        if plistlib.loads(info.read_bytes()).get("OpenSwapProfile") != marker:
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException) as exc:
        raise ClaudeDesktopError("Another app or file already uses this shortcut name. Rename the profile or choose another folder; nothing was replaced.") from exc


def create_shortcut(store: DesktopProfiles, identifier: str, *, badge=None, color=None,
                    directory: Path | None = None, app=None, run=None, icon_renderer=None) -> dict:
    app, run = app or ClaudeDesktopApp(), run or subprocess.run
    info = app.validate()
    directory = (directory if directory is not None else shortcut_directory()).absolute()
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ClaudeDesktopError("Choose a regular folder for your shortcuts.")
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    with store._lock(), _destination_lock(directory):
        profile = store.get(identifier)  # archived shortcuts cannot be recreated
        store._directories(profile)
        badge = profile.badge if badge is None else badge
        color = profile.color if color is None else color
        validate_badge(badge, color)
        path = directory / shortcut_name(profile.name)
        marker = {"version": 1, "profile": profile.id, "root": str(store.root)}
        _check_destination(path, marker)
        command = launcher_command(store, profile.id)
        with tempfile.TemporaryDirectory(prefix=".openswap-shortcut-", dir=directory) as raw:
            stage = Path(raw) / path.name
            _run(run, ["/usr/bin/osacompile", "-o", str(stage), "-"], input=launcher_script(command))
            contents = stage / "Contents"
            plist_path = contents / "Info.plist"
            try:
                plist = plistlib.loads(plist_path.read_bytes())
                if not isinstance(plist, dict):
                    raise ValueError
            except (OSError, ValueError, ExpatError, plistlib.InvalidFileException) as exc:
                raise ClaudeDesktopError("macOS produced an incomplete shortcut. Try creating it again.") from exc
            plist.update(CFBundleIdentifier=f"com.opensoft.openswap.claude-profile.{profile.id}",
                         CFBundleName=f"Claude {profile.name}", CFBundleDisplayName=f"Claude {profile.name}",
                         CFBundleIconFile="Profile.icns", LSUIElement=True, OpenSwapProfile=marker)
            plist.pop("CFBundleIconName", None)
            plist_path.write_bytes(plistlib.dumps(plist))
            (icon_renderer or render_icon)(app.path, contents / "Resources" / "Profile.icns", badge, color)
            # Sign only our generated launcher after writing its icon and plist.
            _run(run, ["/usr/bin/codesign", "--force", "--sign", "-", str(stage)])
            _run(run, ["/usr/bin/codesign", "--verify", "--strict", str(stage)])
            _check_destination(path, marker)
            previous = Path(raw) / "previous.app"
            if path.exists():
                os.replace(path, previous)
            installed = False
            try:
                os.replace(stage, path)
                installed = True
                updated = replace(profile, badge=badge, color=color)
                rows = store.list(include_archived=True)
                store._save([updated if p.id == profile.id else p for p in rows])
            except Exception:
                # Roll back only the launcher we just built, never profile data.
                if installed:
                    shutil.rmtree(path)
                if previous.exists():
                    os.replace(previous, path)
                raise
    return {"path": str(path), "profile": asdict(updated), "state": "shortcut_created",
            "app_version": info["version"], "notice": ICON_NOTICE}


def prepare_shortcuts(store: DesktopProfiles, identifier: str, *, pin: bool, creator=None, installer=None) -> dict:
    """Complete Add's shortcut setup, or retry it without adding another profile.

    Partial failures keep the profile and any completed shortcuts. Dock changes
    happen only after all required launchers exist; the caller can still open
    Claude when setup needs attention.
    """
    from openswap.claude_desktop_dock import DockShortcut, install_shortcuts

    creator, installer = creator or create_shortcut, installer or install_shortcuts
    profile = store.get(identifier)
    default = store.ensure_default()
    profiles = ([default] if default and not default.archived and default.id != profile.id else []) + [profile]
    shortcuts = []
    for item in profiles:
        result = creator(store, item.id)
        shortcuts.append(DockShortcut(Path(result["path"]), item.id, store.root, item.kind == "default"))
    if pin:
        # Add only the requested profile to the Dock. Default's shortcut is
        # available in Finder, but never replaces Claude's ordinary launcher.
        installer(store, [s for s in shortcuts if s.profile_id == profile.id], refresh_icons=True)
    return {"state": "dock_ready" if pin else "shortcuts_ready", "paths": [str(s.path) for s in shortcuts]}
