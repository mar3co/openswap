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


def runs_from_temporary_location(app: Path | None = None) -> bool:
    """Whether this copy is one macOS or the user is only passing through.

    A quarantined app opened from Downloads runs from a randomized
    ``/AppTranslocation/`` path, and a disk image mounts under ``/Volumes/``.
    Either path is gone soon, so nothing may be pinned to it.
    """
    app = app if app is not None else app_path()
    if app is None:
        return False
    text = app.as_posix()
    return "/AppTranslocation/" in text or text.startswith("/Volumes/")


def is_app_executable(program: str) -> bool:
    """Whether ``program`` is ``…/<Name>.app/Contents/MacOS/OpenSwap``: some
    copy of this app, as opposed to a uv console script or interpreter."""
    parts = Path(program).parts
    return (
        len(parts) >= 4
        and parts[-1] == EXECUTABLE_NAME
        and parts[-2] == "MacOS"
        and parts[-3] == "Contents"
        and parts[-4].endswith(".app")
    )


def _runs_uv(program: object, exe: Path) -> bool:
    """Whether a LaunchAgent's ``ProgramArguments`` should move to ``exe``.

    Yes for the uv install (a console script, or ``python -m openswap``), an
    unreadable value, or a copy of the app that no longer exists. No for this
    app, and for any other copy of the app that is still there: a cask agent
    must not be taken over by a copy run from Downloads or a dev build.
    """
    if not (isinstance(program, list) and program and isinstance(program[0], str)):
        return True
    first = program[0]
    if first == str(exe):
        return False
    return not (is_app_executable(first) and Path(first).exists())


def _plist_program(plist: Path) -> object:
    try:
        return plistlib.loads(plist.read_bytes()).get("ProgramArguments")
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return None


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

    Nothing happens from a translocated or mounted copy
    (:func:`runs_from_temporary_location`): it would pin paths that vanish.

    1. ``com.cswap.menubar`` (an older label) is booted out and its plist
       removed.
    2. A menu bar LaunchAgent that runs the uv install is pointed at this
       app's executable.
    3. The Remote tasks worker LaunchAgent, when it runs the uv install, is
       pointed at this app with its own arguments kept.
    4. A Claude Code status-line hook that names another openswap is pointed
       at this app.
    5. The Swift widget host a uv install built into ``~/Applications`` is
       retired, so one appex id has one provider.
    6. A leftover ``openswap`` on PATH is reported, not removed: it is the
       user's uv tool, and ``~/.openswap`` stays theirs too. While anything
       above still runs it (a step failed), the report says to keep it.
    """
    exe = executable()
    if exe is None or sys.platform != "darwin":
        return []
    if runs_from_temporary_location():
        _logger.info("OpenSwap.app runs from %s; not migrating from it", app_path())
        return []
    home = home if home is not None else Path.home()
    say = notify or _logger.info
    changed: list[str] = []
    steps = (
        ("old menu bar login item", lambda: _retire_legacy_menubar_labels(home, uid)),
        ("menu bar login item", lambda: _repoint_menubar_agent(exe, home, uid)),
        ("Remote tasks worker", lambda: _repoint_worker_agent(exe, home, uid)),
        ("Claude Code status line", lambda: _repoint_status_line(exe, home)),
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


def _retire_legacy_menubar_labels(home: Path, uid: int | None) -> str | None:
    from openswap import launch_agent

    retired = False
    for old in launch_agent.LEGACY_LABELS:
        if os.environ.get("XPC_SERVICE_NAME") == old:
            continue  # this process is that job; its next start is not ours
        if launch_agent.plist_path(old, home).exists() or launch_agent.is_loaded(old, uid):
            launch_agent.uninstall(old, home=home, uid=uid)
            retired = True
    return "legacy menubar" if retired else None


def _repoint_menubar_agent(exe: Path, home: Path, uid: int | None) -> str | None:
    from openswap import launch_agent

    plist = launch_agent.plist_path(home=home)
    if not plist.is_file():
        return None  # never installed: `openswap setup` is how it starts
    if not _runs_uv(_plist_program(plist), exe):
        return None
    # Never bootstrap here. `openswap setup` bootstraps the agent itself;
    # the other caller is a running extra: the agent itself
    # (re-bootstrapping would boot it out mid-start) or one opened from Finder
    # or a terminal (bootstrapping would start a second extra beside it).
    # Stop the uv extra unless it is this process, rewrite the plist, and let
    # launchd start the app at the next login.
    if os.environ.get("XPC_SERVICE_NAME") != launch_agent.LABEL:
        if launch_agent.is_loaded(launch_agent.LABEL, uid):
            launch_agent._launchctl("bootout", launch_agent.service_target(launch_agent.LABEL, uid))
    plist.write_bytes(launch_agent.build_plist([str(exe)], home=home))
    return "menubar plist"


def _repoint_worker_agent(exe: Path, home: Path, uid: int | None) -> str | None:
    """Rewrite the worker plist the way ``worker.launch_agent.install`` writes
    it, keeping the plist's own worker arguments and other keys."""
    from openswap import launch_agent
    from openswap.exceptions import ClaudeSwitchError
    from openswap.worker import launch_agent as worker_agent

    plist = worker_agent.plist_path(home)
    if not plist.is_file():
        return None  # the worker is opt-in: `openswap worker enable` installs it
    program = _plist_program(plist)
    if not _runs_uv(program, exe):
        return None
    try:
        data = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(program, list) and "worker" in program:
        data["ProgramArguments"] = [str(exe), *program[program.index("worker"):]]
        env = data.get("EnvironmentVariables")
        env = dict(env) if isinstance(env, dict) else {}
        env["PATH"] = launch_agent._path_env([str(exe)])
        data["EnvironmentVariables"] = env
        plist.write_bytes(plistlib.dumps(data))
    else:
        plist.write_bytes(worker_agent.build_plist([str(exe)], home))
    if os.environ.get("XPC_SERVICE_NAME") == worker_agent.LABEL or not worker_agent._is_loaded(uid):
        return "worker plist"  # not running, or this is it: next start uses the app
    worker_agent._launchctl("bootout", worker_agent._service_target(uid))
    if not worker_agent._wait_until_unloaded(uid):
        raise ClaudeSwitchError("the uv worker did not stop; the app runs it from the next login")
    booted = worker_agent._launchctl("bootstrap", worker_agent._domain_target(uid), str(plist))
    if booted.returncode != 0:
        raise ClaudeSwitchError(
            f"launchctl bootstrap failed (exit {booted.returncode}); run: openswap worker enable"
        )
    return "worker agent"


def _claude_config_home(home: Path) -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else home / ".claude"


def _repoint_status_line(exe: Path, home: Path) -> str | None:
    from openswap import statusline

    changed = statusline.repoint_command(_claude_config_home(home), program=exe)
    return "status line" if changed else None


def _still_on_uv(exe: Path, home: Path) -> list[str]:
    """What still runs another openswap after the steps above (one failed)."""
    from openswap import launch_agent, statusline
    from openswap.worker import launch_agent as worker_agent

    pinned = []
    for name, plist in (
        ("menu bar login item", launch_agent.plist_path(home=home)),
        ("Remote tasks worker", worker_agent.plist_path(home)),
    ):
        if plist.is_file() and _runs_uv(_plist_program(plist), exe):
            pinned.append(name)
    try:
        if statusline.runs_other_program(_claude_config_home(home), exe):
            pinned.append("Claude Code status line")
    except Exception:
        pinned.append("Claude Code status line")
    return pinned


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
        pinned = _still_on_uv(exe, home)
        if pinned:
            say(
                f"An older uv install is still on PATH, and the {' and the '.join(pinned)} "
                "still run it. Keep it until they point at OpenSwap.app "
                "(run openswap setup from the app again), then run uv tool uninstall openswap"
            )
        else:
            say(UV_INSTALL_NOTICE)
        return None
    return None
