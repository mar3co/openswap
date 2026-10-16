"""Run ``openswap menubar`` as a launchd LaunchAgent instead of a foreground process.

``openswap menubar`` blocks the terminal that started it, so the status item dies
with that terminal — and never comes back after a logout or reboot. launchd is
the native macOS answer: a per-user LaunchAgent starts the menu bar at login,
restarts it if it crashes, and needs no ``.app`` bundle.

Two decisions here are worth stating, because both differ from the obvious
approach:

*The plist pins a path that survives upgrades, not ``sys.executable``.* A
LaunchAgent outlives upgrades. In the cask's ``OpenSwap.app`` that path is
the bundle's own ``Contents/MacOS/OpenSwap``, not the ``openswap`` symlink
Homebrew puts on PATH: ``brew upgrade`` replaces the app at the same path, and
launchd starting the bundle binary keeps the LaunchServices identity the
notification guard expects. In a uv install it is the console script:
``uv tool upgrade`` (and ``openswap upgrade``) rebuilds the tool's virtualenv —
``sys.executable`` points inside that virtualenv and can be replaced — while
the console script keeps its path across upgrades. Either way an upgraded
openswap needs a ``launchctl kickstart``, not a reinstalled service.
``sys.executable -m openswap`` stays as the fallback for installs that expose
no console script.

*Logs go to ``~/Library/Logs``, not ``/tmp``.* ``/tmp`` is world-writable and
periodically purged, so a crash log can vanish before anyone reads it, and a
predictable world-writable path is a poor place to point a long-lived writer.

Everything here is macOS-only; callers guard on ``sys.platform`` and the public
functions refuse rather than half-work elsewhere.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from openswap import bundle
from openswap.exceptions import ClaudeSwitchError

LABEL = "com.opensoft.openswap.menubar"
LEGACY_LABELS = ("com.cswap.menubar",)

# launchd's default PATH is /usr/bin:/bin:/usr/sbin:/sbin, which covers
# `security` (Keychain reads) but not a Homebrew or ~/.local/bin `claude`. The
# menu bar shells out to detect running sessions, so seed a PATH that finds it.
_EXTRA_PATH_DIRS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin")
_BASE_PATH_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")

# `launchctl bootout` can return before launchd has finished tearing the
# job down, and a `bootstrap` inside that window fails with "Operation
# already in progress". Poll until the job is really gone instead of
# assuming bootout was synchronous (Homebrew's services code does the same).
_UNLOAD_TIMEOUT_SECONDS = 5.0
_UNLOAD_POLL_SECONDS = 0.1

# How long a copy handing over to the login item waits for launchd to spawn
# it. bootstrap returns before the process exists.
_HANDOFF_PID_TIMEOUT_SECONDS = 5.0

MOVE_TO_APPLICATIONS = "Move OpenSwap to Applications first."


class LoginItemNotStarted(ClaudeSwitchError):
    """launchd did not start the login item that :func:`set_login_item` wrote.

    Its own type so the app can point at System Settings › Login Items, where
    a user may have switched OpenSwap off in the background.
    """


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ClaudeSwitchError(
            "The menu bar service is only available on macOS."
        )


def plist_path(label: str = LABEL, home: Path | None = None) -> Path:
    """Absolute path of the LaunchAgent plist for ``label``."""
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{label}.plist"


def log_paths(label: str = LABEL, home: Path | None = None) -> tuple[Path, Path]:
    """``(stdout, stderr)`` log destinations for ``label``."""
    logs = (home or Path.home()) / "Library" / "Logs"
    return logs / f"{label}.log", logs / f"{label}.err"


def service_target(label: str = LABEL, uid: int | None = None) -> str:
    """launchd service target, e.g. ``gui/501/com.opensoft.openswap.menubar``."""
    return f"gui/{os.getuid() if uid is None else uid}/{label}"


def domain_target(uid: int | None = None) -> str:
    """launchd domain target, e.g. ``gui/501``."""
    return f"gui/{os.getuid() if uid is None else uid}"


def resolve_program() -> list[str]:
    """Argv prefix that launchd should run, minus the subcommand.

    The frozen app pins its own in-bundle executable. Otherwise this prefers
    the installed console script (stable across upgrades, see the module
    docstring) and falls back to running the package through the interpreter
    that is executing right now.

    The path is made absolute but deliberately NOT resolved: a `uv tool
    install` puts a symlink at ``~/.local/bin/openswap`` pointing into the tool's
    virtualenv, and resolving it would write that virtualenv-internal path
    into the plist — the very path this module avoids pinning, since a
    reinstall recreates the virtualenv while the symlink keeps its name.
    """
    exe = bundle.executable()
    if exe is not None and exe.is_file():
        return [str(exe)]

    names = ("openswap",)
    candidate = sys.argv[0] if sys.argv and sys.argv[0] else None
    if candidate is not None:
        absolute = Path(os.path.abspath(candidate))
        if absolute.name in names and absolute.is_file():
            return [str(absolute)]

    for name in names:
        which = shutil.which(name)
        if which:
            return [str(Path(os.path.abspath(which)))]

    return [sys.executable, "-m", "openswap"]


def _path_env(program: list[str]) -> str:
    """PATH for the agent, with the program's own directory first."""
    dirs: list[str] = []
    first = Path(program[0]).parent
    if str(first) not in ("", "."):
        dirs.append(str(first))
    for extra in (*_EXTRA_PATH_DIRS, *_BASE_PATH_DIRS):
        expanded = os.path.expanduser(extra)
        if expanded not in dirs:
            dirs.append(expanded)
    return ":".join(dirs)


def build_plist(
    program: list[str] | None = None,
    label: str = LABEL,
    home: Path | None = None,
) -> bytes:
    """Serialize the LaunchAgent plist.

    Built with :mod:`plistlib` rather than a formatted XML string so paths
    containing ``&`` or ``<`` cannot produce a plist launchd refuses to parse.
    """
    program = program or resolve_program()
    out_log, err_log = log_paths(label, home)
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [*program, "menubar"],
            "RunAtLoad": True,
            # Restart a crash, but respect a deliberate Quit. The menu bar's
            # quit handler calls rumps.quit_application(), a clean exit(0);
            # under a bare `KeepAlive: True` launchd would relaunch it at once
            # and the Quit item would do nothing the user can see.
            "KeepAlive": {"SuccessfulExit": False},
            # A menu bar owner is a UI process; Background would have launchd
            # apply throttled I/O and CPU bands to it.
            "ProcessType": "Interactive",
            "EnvironmentVariables": {"PATH": _path_env(program)},
            "StandardOutPath": str(out_log),
            "StandardErrorPath": str(err_log),
        }
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as e:  # pragma: no cover - launchctl is in the base OS
        raise ClaudeSwitchError("launchctl not found; is this macOS?") from e


def _wait_until_unloaded(
    label: str = LABEL,
    uid: int | None = None,
    timeout: float = _UNLOAD_TIMEOUT_SECONDS,
) -> bool:
    """Block until launchd has dropped the job. True if it went away in time."""
    deadline = time.monotonic() + timeout
    while is_loaded(label, uid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_UNLOAD_POLL_SECONDS)
    return True


def _wait_for_pid(
    label: str = LABEL,
    uid: int | None = None,
    timeout: float = 3.0,
) -> int | None:
    """The job's pid once launchd has spawned it, or None if it has not by
    ``timeout``. bootstrap returns before the process exists, so a plain
    status read right after it would report a healthy job as absent."""
    deadline = time.monotonic() + timeout
    while True:
        pid = status(label, uid)["pid"]
        if pid or time.monotonic() >= deadline:
            return pid
        time.sleep(0.2)


def is_loaded(label: str = LABEL, uid: int | None = None) -> bool:
    """Whether launchd currently knows about the service."""
    return _launchctl("print", service_target(label, uid)).returncode == 0


def status(label: str = LABEL, uid: int | None = None, home: Path | None = None) -> dict:
    """Installed / loaded / running state, plus the pid when there is one."""
    _require_macos()
    printed = _launchctl("print", service_target(label, uid))
    loaded = printed.returncode == 0
    state: str | None = None
    pid: int | None = None
    if loaded:
        for line in printed.stdout.splitlines():
            # `launchctl print` nests sub-dictionaries — pid-local endpoints,
            # inherited environment — and those repeat keys the job itself
            # uses, `state` among them. The job's own fields are the ones at a
            # single tab, so anything deeper is a different object's field.
            if not line.startswith("\t") or line.startswith("\t\t"):
                continue
            stripped = line.strip()
            if state is None and stripped.startswith("state = "):
                state = stripped.removeprefix("state = ").strip()
            elif pid is None and stripped.startswith("pid = "):
                raw = stripped.removeprefix("pid = ").strip()
                if raw.isdigit():
                    pid = int(raw)
    return {
        "label": label,
        "installed": plist_path(label, home).exists(),
        "loaded": loaded,
        "state": state,
        "pid": pid,
        "plist": str(plist_path(label, home)),
    }


def install(
    label: str = LABEL,
    home: Path | None = None,
    program: list[str] | None = None,
    uid: int | None = None,
) -> dict:
    """Write the plist and hand the service to launchd.

    Idempotent: an already-loaded service is booted out first, so running this
    after an upgrade re-reads the plist instead of failing with launchd's
    "service already loaded" (EEXIST, code 5).
    """
    _require_macos()
    if label == LABEL:
        for old in LEGACY_LABELS:
            uninstall(old, home=home, uid=uid)
    program = program or resolve_program()
    target_plist = plist_path(label, home)
    out_log, err_log = log_paths(label, home)

    try:
        target_plist.parent.mkdir(parents=True, exist_ok=True)
        out_log.parent.mkdir(parents=True, exist_ok=True)
        target_plist.write_bytes(build_plist(program, label, home))
    except OSError as e:
        raise ClaudeSwitchError(f"Could not write the launch agent: {e}") from e

    settled = True
    if is_loaded(label, uid):
        _launchctl("bootout", service_target(label, uid))
        settled = _wait_until_unloaded(label, uid)

    booted = _launchctl("bootstrap", domain_target(uid), str(target_plist))
    if booted.returncode != 0:
        detail = (booted.stderr or booted.stdout or "").strip()
        if not settled:
            detail = f"{detail}; the previous instance was still shutting down".lstrip("; ")
        raise ClaudeSwitchError(
            f"launchctl bootstrap failed (exit {booted.returncode})"
            + (f": {detail}" if detail else "")
        )

    return {
        "label": label,
        "plist": str(target_plist),
        "program": [*program, "menubar"],
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
    }


def uninstall(
    label: str = LABEL,
    home: Path | None = None,
    uid: int | None = None,
) -> dict:
    """Stop the service and delete its plist.

    Tolerates every partial state — loaded without a plist, a plist that was
    never bootstrapped, neither — because the point of an uninstall is to
    arrive at "gone", not to insist on the path taken to get there.
    """
    _require_macos()
    target_plist = plist_path(label, home)
    was_loaded = is_loaded(label, uid)
    if was_loaded:
        booted_out = _launchctl("bootout", service_target(label, uid))
        if booted_out.returncode != 0 and is_loaded(label, uid):
            detail = (booted_out.stderr or booted_out.stdout or "").strip()
            raise ClaudeSwitchError(
                f"launchctl bootout failed (exit {booted_out.returncode})"
                + (f": {detail}" if detail else "")
            )

    existed = target_plist.exists()
    try:
        target_plist.unlink(missing_ok=True)
    except OSError as e:
        raise ClaudeSwitchError(f"Could not remove the launch agent: {e}") from e

    return {"label": label, "was_loaded": was_loaded, "removed_plist": existed}


def runs_as_login_item() -> bool:
    """Whether this process is the menu bar LaunchAgent itself.

    launchd puts the job's label in ``XPC_SERVICE_NAME``; a copy opened from
    Finder, Spotlight or a terminal has none (or another one).
    """
    return os.environ.get("XPC_SERVICE_NAME") == LABEL


def login_item_enabled(home: Path | None = None) -> bool:
    """Whether OpenSwap opens at login.

    The plist on disk is the setting (plan 020, Decision 1), so ``setup``,
    ``uninstall``, ``brew uninstall`` and the app's toggle always agree.
    """
    return plist_path(LABEL, home).exists()


def login_item_runs_this(home: Path | None = None) -> bool:
    """Whether the login item runs this copy (``resolve_program()``).

    Only then may a copy hand over to it: a plist for another copy of the app
    (the cask's, while a dev build runs) must not be started from this one.
    """
    return bundle._plist_program(plist_path(LABEL, home)) == [*resolve_program(), "menubar"]


def start_login_item(*, home: Path | None = None, uid: int | None = None) -> int | None:
    """Start the login item and return launchd's pid, or None if none appeared.

    A running job is left alone, a loaded one is kickstarted, and otherwise
    the plist is bootstrapped. A job that does not come up in time is booted
    out again, so a late start cannot wait behind this copy's menu bar lock
    and be relaunched by ``KeepAlive`` every time it gives up; the plist stays
    for the next login. Never call it from the job itself. Raises
    LoginItemNotStarted when launchctl fails.
    """
    _require_macos()
    state = status(LABEL, uid, home)
    if state["pid"]:
        return state["pid"]
    if state["loaded"]:
        started = _launchctl("kickstart", service_target(LABEL, uid))
        verb = "kickstart"
    else:
        started = _launchctl("bootstrap", domain_target(uid), str(plist_path(LABEL, home)))
        verb = "bootstrap"
    if started.returncode != 0:
        detail = (started.stderr or started.stdout or "").strip()
        raise LoginItemNotStarted(
            f"launchctl {verb} failed (exit {started.returncode})"
            + (f": {detail}" if detail else "")
        )
    pid = _wait_for_pid(LABEL, uid, timeout=_HANDOFF_PID_TIMEOUT_SECONDS)
    if pid is None:
        _launchctl("bootout", service_target(LABEL, uid))
    return pid


def unload_own_job(*, home: Path | None = None, uid: int | None = None) -> bool:
    """From the launchd job on its way out, after Open at login was turned off.

    Turning login off from the job only deletes the plist, so the job stays
    loaded, and the cask's ``kickstart -k`` would revive it behind another
    copy. This starts ``launchctl bootout`` for the job as a detached process
    (a new session, so launchd stopping the job's process group does not take
    it along) and returns at once; the caller exits right after. It does
    nothing (False) unless this process is the job and its plist is gone, so a
    plain Quit with login on keeps the job loaded for the next login.
    """
    if sys.platform != "darwin" or not runs_as_login_item():
        return False
    if plist_path(LABEL, home).exists():
        return False
    try:
        subprocess.Popen(
            ["launchctl", "bootout", service_target(LABEL, uid)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    return True


def set_login_item(
    enabled: bool,
    *,
    home: Path | None = None,
    uid: int | None = None,
) -> str:
    """Turn Open at login on or off from a running extra.

    Returns what happened, for the app to say:

    - ``"unchanged"``: it already was that way.
    - ``"enabled"``: the login item now runs this program. Only from the
      launchd job itself, which is already the copy that should run.
    - ``"handoff"``: launchd's copy is up; the caller should quit so it can
      take the menu bar lock.
    - ``"disabled"``: the job is gone and does not start at the next login.
    - ``"disabled_after_quit"``: from the job itself; this copy keeps running
      until Quit (which calls :func:`unload_own_job`) or logout, and does not
      come back at the next login.

    It never boots out the calling process: the job only rewrites or deletes
    its plist, and only another copy talks to launchctl. ``install`` and
    ``uninstall`` stay the CLI's, which never runs inside the extra.

    Raises ClaudeSwitchError from a translocated or mounted copy, from a uv
    install beside the cask, and when launchctl fails. ``LoginItemNotStarted``
    means launchd did not start the job this wrote.
    """
    _require_macos()
    if bundle.runs_from_temporary_location():
        raise ClaudeSwitchError(MOVE_TO_APPLICATIONS)
    target_plist = plist_path(LABEL, home)
    is_job = runs_as_login_item()

    if not enabled:
        if is_job:
            if not target_plist.exists():
                return "unchanged"
            try:
                target_plist.unlink(missing_ok=True)
            except OSError as e:
                raise ClaudeSwitchError(f"Could not remove the launch agent: {e}") from e
            return "disabled_after_quit"
        if not target_plist.exists() and not is_loaded(LABEL, uid):
            return "unchanged"
        uninstall(LABEL, home=home, uid=uid)
        return "disabled"

    bundle.refuse_uv_beside_cask("open at login")
    program = resolve_program()
    stale = bundle._plist_program(target_plist) != [*program, "menubar"]
    if stale:
        out_log, _err_log = log_paths(LABEL, home)
        try:
            target_plist.parent.mkdir(parents=True, exist_ok=True)
            out_log.parent.mkdir(parents=True, exist_ok=True)
            target_plist.write_bytes(build_plist(program, LABEL, home))
        except OSError as e:
            raise ClaudeSwitchError(f"Could not write the launch agent: {e}") from e
    if is_job:
        # launchd already runs this copy; the plist is read again at login.
        return "enabled" if stale else "unchanged"

    # Another copy: launchd's copy is the one that should run, so start it
    # and hand over. The job waits for this copy's menu bar lock. A job
    # still loaded from the old plist would kickstart the old program, so it
    # is booted out (it is not this process) and bootstrapped from the new one.
    if stale and is_loaded(LABEL, uid):
        _launchctl("bootout", service_target(LABEL, uid))
        if not _wait_until_unloaded(LABEL, uid):
            raise LoginItemNotStarted("the previous login item was still shutting down")
    if start_login_item(home=home, uid=uid) is None:
        raise LoginItemNotStarted("launchd did not start OpenSwap")
    return "handoff"
