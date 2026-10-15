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

# Homebrew's prefix on Apple silicon and on Intel. Checked on disk rather
# than asking `brew --prefix`, so nothing here shells out.
BREW_PREFIXES = (Path("/opt/homebrew"), Path("/usr/local"))
CASKROOMS = tuple(prefix / "Caskroom" / "openswap" for prefix in BREW_PREFIXES)
# The cask's `binary` stanza links this name in `<prefix>/bin` to the app.
BREW_BINARIES = tuple(prefix / "bin" / "openswap" for prefix in BREW_PREFIXES)


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
    if not any(room.is_dir() for room in CASKROOMS):
        return False
    if app.parent in resolved:
        return True
    # Any other --appdir: the cask's binary symlink still points into this app.
    return any(_links_to(link, executable()) for link in BREW_BINARIES)


def _links_to(link: Path, target: Path | None) -> bool:
    if target is None:
        return False
    try:
        return link.is_symlink() and link.resolve() == target.resolve()
    except (OSError, RuntimeError):
        return False


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
    # Never bootstrap here. `openswap setup` installs the agent before it
    # migrates, so only a running extra reaches this line: the agent itself
    # (re-bootstrapping would boot it out mid-start) or one opened from Finder
    # or a terminal (bootstrapping would start a second extra beside it).
    # Stop the uv extra unless it is this process, rewrite the plist, and let
    # launchd start the app at the next login.
    if os.environ.get("XPC_SERVICE_NAME") != launch_agent.LABEL:
        for old in launch_agent.LEGACY_LABELS:
            launch_agent.uninstall(old, home=home, uid=uid)
        if launch_agent.is_loaded(launch_agent.LABEL, uid):
            launch_agent._launchctl("bootout", launch_agent.service_target(launch_agent.LABEL, uid))
    plist.write_bytes(launch_agent.build_plist([str(exe)], home=home))
    return "menubar plist"


def _retire_widget_host(home: Path, uid: int | None) -> str | None:
    from openswap import widget_install

    return None if widget_install.retire_legacy_host(home=home, uid=uid) is None else "widget host"


def _uv_bin_dirs(home: Path) -> list[Path]:
    """Where a uv install's ``openswap`` shim can be, most specific first.

    uv's tool bin directory (``UV_TOOL_BIN_DIR``, ``XDG_BIN_HOME``,
    ``XDG_DATA_HOME/../bin``, ``~/.local/bin``), the ``UV_INSTALL_DIR`` the
    installer honours, then every PATH entry, since a shim anywhere on PATH
    can shadow the cask's command.
    """
    env = os.environ
    dirs = [Path(env[name]) for name in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "UV_INSTALL_DIR") if env.get(name)]
    if env.get("XDG_DATA_HOME"):
        dirs.append(Path(env["XDG_DATA_HOME"]).parent / "bin")
    dirs.append(home / ".local" / "bin")
    dirs.extend(Path(entry) for entry in env.get("PATH", "").split(os.pathsep) if entry)
    return dirs


def _report_uv_install(exe: Path, home: Path, say: Callable[[str], object]) -> None:
    seen: set[Path] = set()
    for directory in _uv_bin_dirs(home):
        shim = directory / "openswap"
        if shim in seen or not (shim.exists() or shim.is_symlink()):
            continue
        seen.add(shim)
        try:
            if shim.resolve() == exe.resolve():
                continue  # linked to this app, like the cask's own symlink
        except (OSError, RuntimeError):
            pass
        say(UV_INSTALL_NOTICE)
        return None
    return None
