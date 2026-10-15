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

:func:`migrate_from_uv_install` moves a Mac that ran the uv install over to
this app: the login item, the old widget host, and a note about the stale
``~/.local/bin/openswap``.
"""

from __future__ import annotations

import logging
import os
import plistlib
import sys
from collections.abc import Callable
from pathlib import Path

_logger = logging.getLogger("openswap")

EXECUTABLE_NAME = "OpenSwap"
RELOAD_HELPER_NAME = "openswap-widget-reload"
APPEX_NAME = "OpenSwapWidgetExtension.appex"
APPEX_ID = "com.opensoft.openswap.widget.extension"

UV_INSTALL_NOTICE = "An older uv install is still on PATH: run uv tool uninstall openswap"

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


def migrate_from_uv_install(
    home: Path | None = None,
    uid: int | None = None,
    notify: Callable[[str], object] | None = None,
) -> list[str]:
    """Move a Mac that ran the uv install over to this app. Never raises.

    Run by ``openswap setup`` and by the frozen extra at start-up, so it is
    idempotent: each step checks the state it would change and does nothing
    once it is right. A step that fails is logged (and passed to ``notify``)
    and the rest still run. Returns what changed; empty when nothing needed
    doing, which is every run after the first.

    1. The menu bar LaunchAgent is pointed at this app's executable.
    2. The Swift widget host a uv install built into ``~/Applications`` is
       retired, so one appex id has one provider.
    3. A leftover ``~/.local/bin/openswap`` is reported, not removed: it is
       the user's uv tool, and ``~/.openswap`` stays theirs too.
    """
    exe = executable()
    if exe is None or sys.platform != "darwin":
        return []
    home = home if home is not None else Path.home()
    say = notify or _logger.info
    changed: list[str] = []
    steps = (
        ("menu bar login item", lambda: _repoint_menubar_agent(exe, home, uid)),
        ("widget host", lambda: _retire_widget_host(home, uid)),
        ("uv install check", lambda: _report_uv_install(exe, home, say)),
    )
    for name, step in steps:
        try:
            result = step()
        except Exception as e:
            message = f"Could not migrate the {name} to OpenSwap.app: {e}"
            _logger.warning(message, exc_info=True)
            if notify is not None:
                notify(message)
            continue
        if result:
            changed.append(result)
    return changed


def _repoint_menubar_agent(exe: Path, home: Path, uid: int | None) -> str | None:
    from openswap import launch_agent

    plist = launch_agent.plist_path(home=home)
    if not plist.is_file():
        return None  # never installed: `openswap setup` is how it starts
    try:
        program = plistlib.loads(plist.read_bytes()).get("ProgramArguments")
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        program = None
    if isinstance(program, list) and program and program[0] == str(exe):
        return None
    if os.environ.get("XPC_SERVICE_NAME") == launch_agent.LABEL:
        # This process is that agent: re-bootstrapping would boot it out
        # mid-start. The rewritten plist takes effect at the next login.
        plist.write_bytes(launch_agent.build_plist([str(exe)], home=home))
        return "menubar plist"
    launch_agent.install(home=home, uid=uid, program=[str(exe)])
    return "menubar agent"


def _retire_widget_host(home: Path, uid: int | None) -> str | None:
    from openswap import widget_install

    return None if widget_install.retire_legacy_host(home=home, uid=uid) is None else "widget host"


def _report_uv_install(exe: Path, home: Path, say: Callable[[str], object]) -> None:
    shim = home / ".local" / "bin" / "openswap"
    if not (shim.exists() or shim.is_symlink()):
        return None
    try:
        if shim.resolve() == exe.resolve():
            return None  # linked to this app, like the cask's own symlink
    except (OSError, RuntimeError):
        pass
    say(UV_INSTALL_NOTICE)
    return None
