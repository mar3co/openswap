"""Where the frozen ``OpenSwap.app`` lives, for code that must act on it.

The cask installs one app that holds the frozen extra, the WidgetKit appex,
and a widget reload helper. The launch agent, the widget, and upgrade each
need to know "am I that app, and where is it?"; they ask here instead of
each poking at ``sys.frozen``.

Everything is derived from ``sys.executable``, resolved. From a terminal the
process runs through the cask's ``binary`` symlink (``/opt/homebrew/bin/openswap``),
so ``sys.argv[0]`` names the symlink; the interpreter path PyInstaller sets
is the real file inside the bundle, and resolving it maps any symlink back to
the bundle. A source or uv install is not frozen and gets ``None`` throughout.
"""

from __future__ import annotations

import sys
from pathlib import Path

EXECUTABLE_NAME = "OpenSwap"
RELOAD_HELPER_NAME = "openswap-widget-reload"
APPEX_NAME = "OpenSwapWidgetExtension.appex"
APPEX_ID = "com.opensoft.openswap.widget.extension"

# Homebrew's Caskroom on Apple silicon and on Intel. Checked on disk rather
# than asking `brew --prefix`, so nothing here shells out.
CASKROOMS = (
    Path("/opt/homebrew/Caskroom/openswap"),
    Path("/usr/local/Caskroom/openswap"),
)


def is_frozen() -> bool:
    """True inside the PyInstaller-built app."""
    return bool(getattr(sys, "frozen", False))


def app_path() -> Path | None:
    """The ``.app`` that contains this process, or None when not frozen."""
    if not is_frozen():
        return None
    try:
        exe = Path(sys.executable).resolve()
    except (OSError, RuntimeError):
        return None
    for parent in exe.parents:
        if parent.name.endswith(".app"):
            return parent
    return None


def executable() -> Path | None:
    """``OpenSwap.app/Contents/MacOS/OpenSwap``, the binary launchd should run."""
    app = app_path()
    return None if app is None else app / "Contents" / "MacOS" / EXECUTABLE_NAME


def reload_helper() -> Path | None:
    """The in-bundle tool that reloads this app's widget timelines."""
    app = app_path()
    return None if app is None else app / "Contents" / "MacOS" / RELOAD_HELPER_NAME


def appex() -> Path | None:
    """The bundled WidgetKit extension."""
    app = app_path()
    return None if app is None else app / "Contents" / "PlugIns" / APPEX_NAME


def installed_by_homebrew(home: Path | None = None) -> bool:
    """Whether this app sits in a cask ``appdir`` with an openswap Caskroom.

    The cask installs to ``/Applications`` (or ``~/Applications`` with
    ``--appdir``). A copy dragged somewhere else, or a Caskroom that is gone,
    is not something ``brew upgrade`` will replace.
    """
    app = app_path()
    if app is None:
        return False
    appdirs = {Path("/Applications"), (home or Path.home()) / "Applications"}
    resolved: set[Path] = set()
    for appdir in appdirs:
        try:
            resolved.add(appdir.resolve())
        except (OSError, RuntimeError):
            resolved.add(appdir)
    if app.parent not in resolved:
        return False
    return any(room.is_dir() for room in CASKROOMS)
