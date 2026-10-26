"""Take OpenSwap off a Mac: ``openswap uninstall`` and the cask's hook.

Outside its data folder OpenSwap sets up the menu bar login item, the Remote
tasks worker's LaunchAgent, the Claude Code status line, and the widget's
registration. :func:`remove_integrations` undoes those and leaves saved
accounts and settings alone. :func:`delete_saved_logins` is the part of a zap
that Homebrew's ``zap`` stanza cannot reach: the Keychain.

Homebrew runs a cask's whole ``uninstall`` stanza on ``brew upgrade`` and
``brew reinstall`` too, before it swaps the app, so the cask cannot just
remove the login item: every upgrade would drop it. Its hook runs
``OpenSwap uninstall --homebrew-hook`` instead, and :func:`homebrew_command`
reads which brew command started it. Only an uninstall removes anything.

:func:`run` is ``openswap uninstall`` after its checks and prompt. The menu
bar's **Uninstall OpenSwap…** starts the same command in a detached child
(:func:`start_from_app`) and quits: removing the login item boots the extra
out, so none of the work may run inside it.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openswap import bundle, paths
from openswap.exceptions import ClaudeSwitchError

# `brew rm` and `brew remove` are aliases Homebrew keeps for uninstall.
UNINSTALL_COMMANDS = frozenset({"uninstall", "remove", "rm"})

_BREW_RB = "/Library/Homebrew/brew.rb"
# Homebrew's Ruby is the hook's parent. A few more levels allow for a wrapper
# (env, sudo) without walking the whole process tree.
_MAX_ANCESTORS = 5

# The hidden flag on the child the menu bar starts, and where its output goes.
FROM_APP_FLAG = "--from-app"
APP_LOG_NAME = "com.opensoft.openswap.uninstall.log"
# How long that child waits for the menu bar app to quit before going on.
APP_QUIT_TIMEOUT = 30.0

# ``say(line)`` reports progress; ``say(line, error=True)`` a failure.
Say = Callable[..., None]


@dataclass
class Removal:
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    # The worker was booted out (``force``) without proof its task stopped.
    job_stop_unconfirmed: bool = False


def homebrew_command(pid: int | None = None) -> tuple[str, list[str]] | None:
    """The brew command that started this process, and its other arguments.

    ``("uninstall", ["--zap", "--cask", "openswap"])`` under ``brew uninstall
    --zap --cask openswap``. None when no near ancestor runs Homebrew's
    ``brew.rb``, or ``ps`` cannot say.
    """
    pid = os.getppid() if pid is None else pid
    for _ in range(_MAX_ANCESTORS):
        if pid <= 1:
            return None
        found = _process(pid)
        if found is None:
            return None
        parent, words = found
        for i, word in enumerate(words):
            if word.endswith(_BREW_RB):
                rest = words[i + 1:]
                for j, arg in enumerate(rest):
                    if not arg.startswith("-"):
                        return arg, rest[:j] + rest[j + 1:]
                return None
        pid = parent
    return None


def _process(pid: int) -> tuple[int, list[str]] | None:
    """``pid``'s parent and command line, split on whitespace."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "ppid=,args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = result.stdout.split()
    if result.returncode != 0 or not fields or not fields[0].isdigit():
        return None
    return int(fields[0]), fields[1:]


def remove_integrations(
    home: Path | None = None,
    uid: int | None = None,
    force: bool = False,
) -> Removal:
    """Remove the login item, the worker, the status line, and the widget.

    Also, quietly: the menu bar's lock, show and hand-over requests, and the
    login item's last start failure, and in the app the finished welcome
    page, so a reinstall offers Open at login again. Each step finds nothing
    to do when its part is already gone, so a second run succeeds.

    Never raises: a part that fails is reported in ``failed`` and the rest
    still run. The app only removes what runs a copy of OpenSwap.app, so
    uninstalling the cask leaves a uv install's login item alone. A uv or
    source install removes its own (it refuses to run beside the cask).

    ``force`` is for when the app is about to be deleted: a worker that will
    not stop cleanly is booted out anyway rather than left pointing at a
    missing app.
    """
    home = home if home is not None else Path.home()
    removal = Removal()
    steps = (
        ("menu bar login item", lambda: _remove_menubar_agent(home, uid)),
        ("menu bar's runtime files", _remove_menubar_state),
        ("setup wizard state", _reset_welcome),
        ("Remote tasks worker", lambda: _remove_worker(home, uid, force, removal)),
        ("Claude Code status line", _restore_status_line),
        ("widget", lambda: _remove_widget(home, uid)),
    )
    for name, step in steps:
        try:
            if step():
                removal.removed.append(name)
        except Exception as e:
            removal.failed.append(f"{name}: {e}")
    return removal


def _ours(plist: Path) -> bool:
    """Whether this install is the one ``plist`` runs.

    In the app, only this copy of it (or a program that is gone, which no
    install can still use): an agent on a uv install, a dev build or another
    copy of the app that still exists outlives this one.
    """
    if not bundle.is_frozen():
        return True
    try:
        program = plistlib.loads(plist.read_bytes()).get("ProgramArguments")
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return True  # unreadable: no other install can claim it either
    if not (isinstance(program, list) and program and isinstance(program[0], str)):
        return True
    return _runs_this_app(program[0])


def _remove_menubar_agent(home: Path, uid: int | None) -> bool:
    from openswap import launch_agent

    removed = False
    for label in (launch_agent.LABEL, *launch_agent.LEGACY_LABELS):
        plist = launch_agent.plist_path(label, home)
        if plist.is_file() and not _ours(plist):
            continue
        result = launch_agent.uninstall(label, home=home, uid=uid)
        removed = removed or result["was_loaded"] or result["removed_plist"]
    return removed


def _remove_menubar_state() -> bool:
    """Delete the menu bar's transient files from the backup root: its lock,
    show and hand-over requests, and the login item's last start failure.

    Only while no extra holds the lock: one that still runs (a uv install's,
    beside the cask) owns them, and unlinking a held lock would let a second
    extra lock a new file at the same path. Returns False: nothing the user
    set up, so nothing to report.
    """
    from openswap import launch_agent, menubar_instance

    backup = paths.get_backup_root()
    lock_file = menubar_instance.lock_path(backup)
    lock = None
    if lock_file.exists():
        try:
            lock = menubar_instance.menubar_lock(backup)
        except OSError:
            return False  # no flock here, or the file cannot be opened
        if lock is None:
            return False
    try:
        for path in (
            menubar_instance.show_request_path(backup),
            menubar_instance.handoff_request_path(backup),
            menubar_instance.move_request_path(backup),
            launch_agent.login_item_status_path(backup),
            *([lock_file] if lock is not None else []),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    finally:
        if lock is not None:
            lock.release()
    return False


def _reset_welcome() -> bool:
    """Open the app's setup wizard again after a reinstall.

    ``welcome_done`` says the login item was decided, and the uninstall has
    just removed it: kept, a reinstalled app opened from Finder would skip the
    wizard and never offer Open at login. Only the app shows the wizard.
    Returns False, as for the runtime files.
    """
    if not bundle.is_frozen():
        return False
    from openswap.menubar_display import MenuBarSettings, menubar_settings_path

    path = menubar_settings_path(paths.get_backup_root())
    if not path.is_file():
        return False
    settings = MenuBarSettings.load(path)
    if settings.welcome_done:
        settings.welcome_done = False
        settings.save(path)
    return False


def _remove_worker(home: Path, uid: int | None, force: bool, removal: Removal | None = None) -> bool:
    from openswap.worker import launch_agent as worker_agent
    from openswap.worker.cli import disable_worker

    plist = worker_agent.plist_path(home)
    if plist.is_file():
        if not _ours(plist):
            return False
    elif not worker_agent._is_loaded(uid):
        return False
    # disable_worker stops a running task first and turns Remote tasks off, so
    # a reinstall does not find the worker enabled with no LaunchAgent.
    try:
        stopped, _, diagnostic = disable_worker(paths.get_backup_root())
    except Exception as e:
        # A failed migration or lifecycle lock: the same as not stopping.
        if not force:
            raise
        stopped, diagnostic = False, str(e)
    if stopped:
        return True
    if not force:
        raise ClaudeSwitchError(
            f"it did not stop ({diagnostic}); see openswap worker status"
        )
    # Before the boot-out: it can succeed and then fail to unlink the plist,
    # and the task may outlive it either way.
    if removal is not None:
        removal.job_stop_unconfirmed = True
    try:
        _mark_stop_unconfirmed(home, diagnostic)
    except OSError as e:
        # Without the marker only the loaded worker shows a later zap that a
        # task may still run, so it stays: a plist on a missing app costs one
        # failed start per login.
        raise ClaudeSwitchError(
            f"it did not stop ({diagnostic}) and that could not be recorded ({e}), "
            "so it stays loaded; see openswap worker status"
        ) from e
    worker_agent.uninstall(home=home, uid=uid)
    return True


def _restore_status_line() -> bool:
    """Put back the user's own Claude Code status line, if this install's
    hook replaced it. A hook that runs another install's openswap stays.

    The app always writes its own executable into the hook, so in the app a
    bare ``openswap statusline`` is not its own: it came from a uv or source
    install, which may outlive the cask.
    """
    from openswap import statusline

    config_home = paths.get_claude_config_home()
    if statusline.installed_command(config_home) is None:
        return False
    if bundle.is_frozen():
        named = statusline.hook_program(config_home)
        if named is None or not _runs_this_app(named):
            return False
    return bool(statusline.uninstall(config_home, paths.get_backup_root())["restored"])


def _runs_this_app(program: str) -> bool:
    """Whether ``program`` is this app or a link to it (the cask's
    ``bin/openswap``), or is already gone."""
    app = bundle.app_path()
    try:
        resolved = Path(program).resolve(strict=True)
    except FileNotFoundError:
        return True
    except (OSError, RuntimeError):
        return False
    return app is not None and resolved.is_relative_to(app.resolve())


def _remove_widget(home: Path, uid: int | None) -> bool:
    from openswap import widget_install

    if bundle.is_frozen():
        # The app's appex goes with the app. Unregistering it only takes it
        # out of the widget gallery before macOS notices, so a pluginkit
        # failure is no reason to stop the uninstall.
        try:
            widget_install.uninstall_widget(home=home, uid=uid)
        except ClaudeSwitchError:
            pass
        return False
    result = widget_install.uninstall_widget(home=home, uid=uid)
    return bool(result.get("removed_app") or result.get("removed_plist") or result.get("was_loaded"))


def delete_saved_logins() -> int:
    """Delete every Keychain item OpenSwap saved: account backups (also under
    the pre-rename service), the worker's pairing, and each session profile's
    current login. Never Claude Code's own items, which hold the login in use
    now. Returns how many service items were deleted.

    Run before the data folder goes, as ``purge`` does: a session profile's
    item is named from its directory's path, so once that is deleted the
    item can no longer be found.
    """
    from openswap import macos_keychain
    from openswap.credentials import LEGACY_BACKUP_SECURITY_SERVICE, SECURITY_SERVICE

    delete_session_logins()
    deleted = 0
    for service in (SECURITY_SERVICE, LEGACY_BACKUP_SECURITY_SERVICE):
        try:
            deleted += macos_keychain.delete_all(service)
        except macos_keychain.KEYCHAIN_ERRORS as e:
            raise ClaudeSwitchError(f"Could not delete the saved logins from the Keychain: {e}") from e
    return deleted


def paired_servers() -> list[str]:
    """The servers this Mac's Remote tasks worker is paired with, oldest
    root last, without repeats: usually none or one, but a pre-XDG folder the
    zap also deletes can name another.

    A zap deletes the pairing's key here, but the server still lists the Mac
    (offline) until its owner removes it there: unpairing is local, and the
    worker protocol has no request for a worker to remove itself. Read them
    before the zap, which deletes the settings that name them, from every root
    the zap deletes: opening the data migrates a pre-XDG folder only later.
    """
    from openswap.settings import load_worker_settings

    found: list[str] = []
    for root in _data_roots():
        try:
            url = load_worker_settings(root).control_service_url
        except Exception:
            continue  # unreadable settings: nothing to point the user at
        if url is not None and url not in found:
            found.append(url)
    return found


def job_state_notice(busy: str) -> str:
    """What a zap says when it kept the worker's job state."""
    kept = " and ".join(str(path) for path in job_state_paths() if path.exists()) or str(job_state_paths()[0])
    return f"Kept {kept}: {busy}. Delete it once no Remote task runs."


def server_name(url: str) -> str:
    """``OpenTag`` for the OpenTag portal, else the server's address."""
    from openswap.worker.connect import DEFAULT_SERVER

    return "OpenTag" if url.rstrip("/") == DEFAULT_SERVER else url


def server_notice(url: str) -> str:
    """What a zap says when it forgot a pairing the server still lists."""
    return f"{server_name(url)} still lists this Mac for Remote tasks. To remove it, open {url} and remove it there."


def delete_session_logins(roots: list[Path] | None = None) -> None:
    """Delete the Keychain item of each idle session profile under ``roots``
    (default: every root a zap deletes).

    A Claude still running on a profile keeps its login; only `brew
    uninstall --zap` gets here past refuse_live_sessions.
    """
    from openswap.session import delete_macos_keychain_entry, profile_is_quiescent

    for profile in _session_profiles(roots):
        if profile_is_quiescent(profile):
            delete_macos_keychain_entry(profile)


def legacy_roots() -> list[Path]:
    """The pre-XDG backup root, when it is a separate folder."""
    return _data_roots()[1:]


def _data_roots() -> list[Path]:
    """The backup root and, when it is a separate folder, the pre-XDG one:
    both go in a zap, as in purge."""
    backup = paths.get_backup_root()
    legacy = paths.get_legacy_backup_root()
    if legacy.is_relative_to(backup) or backup.is_relative_to(legacy):
        return [backup]
    return [backup, legacy]


def _session_profiles(roots: list[Path] | None = None) -> list[Path]:
    """Session profiles under ``roots``, by default every root a zap deletes."""
    found = []
    for root in _data_roots() if roots is None else roots:
        sessions = root / "sessions"
        if sessions.is_dir():
            found.extend(sorted(p for p in sessions.iterdir() if p.is_dir()))
    return found


def refuse_live_sessions() -> None:
    """Raise while a session-mode Claude runs on a profile a zap would delete.

    As ``purge`` does: deleting a profile and its Keychain item from under a
    live Claude is unsafe, and a record that cannot be read is no proof that
    nothing runs.
    """
    from openswap.session import scan_live_sessions

    live, unreadable = [], []
    for profile in _session_profiles():
        found, bad = scan_live_sessions(profile)
        if found:
            live.append(f"{profile.name} (PID {', '.join(str(s.pid) for s in found)})")
        elif bad:
            unreadable.append(profile.name)
    if live:
        several = len(live) > 1
        raise ClaudeSwitchError(
            f"Claude Code is running on session profile{'s' if several else ''} "
            f"{'; '.join(live)}. Quit {'them' if several else 'it'}, then try again."
        )
    if unreadable:
        raise ClaudeSwitchError(
            f"Could not read the session records of {', '.join(unreadable)}, so whether "
            "Claude Code is running there is unknown. Repair or remove them, then try again."
        )


def job_state_paths(home: Path | None = None) -> list[Path]:
    """The worker's job locks, job handles and pinned Claude Code copies
    (``containment.default_lock_dir``'s parent), and the Caches folder an
    older worker, perhaps another install's still running, keeps them in."""
    library = (home if home is not None else Path.home()) / "Library"
    return [library / "Application Support" / bundle.APP_ID, library / "Caches" / bundle.APP_ID]


# In the job state, after a forced boot-out: a task may have outlived the
# worker and every label and lock that would show it, and a later zap (after
# a reinstall) must still keep the handles that can stop it.
STOP_UNCONFIRMED_MARKER = "stop-unconfirmed"


def _mark_stop_unconfirmed(home: Path, diagnostic: object) -> None:
    """Write :data:`STOP_UNCONFIRMED_MARKER`; raises OSError when it cannot."""
    folder = job_state_paths(home)[0]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / STOP_UNCONFIRMED_MARKER).write_text(
        f"{datetime.now().isoformat(timespec='seconds')} {diagnostic}\n", encoding="utf-8",
    )


def job_state_busy(removal: Removal, uid: int | None = None) -> str | None:
    """Why the worker's job state must stay, or None when a zap may delete it.

    Its job handles are what recovery needs to stop a task, and its locks
    must not be recreated on new inodes while a task still holds them, so it
    goes only once no task can be running: not after a forced boot-out, nor
    while any worker or job label is loaded (another install's worker shares
    this per-user folder), nor while a worker process holds its instance lock,
    nor once a forced boot-out left :data:`STOP_UNCONFIRMED_MARKER` in it,
    however many uninstalls ago: only the user, once the task is gone,
    deletes that. Not on the cask's ``zap trash:`` list for that
    reason.
    """
    if removal.job_stop_unconfirmed:
        return "the Remote tasks worker was removed before its task was confirmed stopped"
    if (job_state_paths()[0] / STOP_UNCONFIRMED_MARKER).exists():
        return "an earlier uninstall removed the Remote tasks worker before its task was confirmed stopped"
    labels = _loaded_labels(uid)
    if labels is None:
        return "launchd could not say whether a Remote task is still running"
    from openswap.worker.containment import LABEL_PREFIX
    from openswap.worker.launch_agent import LABEL

    if LABEL in labels:
        return "a Remote tasks worker is still loaded"
    if any(label.startswith(LABEL_PREFIX) for label in labels):
        return "a Remote task is still running"
    # A worker started by hand (`openswap worker run`) has no label, and an
    # idle one runs no job, but it can admit a task at any moment.
    if any((root / "worker").is_dir() and _worker_process_may_run(root) for root in _data_roots()):
        return "a Remote tasks worker process is still running"
    return None


def _worker_process_may_run(root: Path) -> bool:
    """Whether a worker process may hold ``root``'s process-lifetime lock.
    Only meaningful under :func:`worker_starts_held`, as in purge. A failure
    counts as running."""
    from openswap.worker.cli import _worker_instance_lock_is_free

    try:
        return not _worker_instance_lock_is_free(root)
    except Exception:
        return True


@contextmanager
def worker_starts_held() -> Iterator[bool]:
    """Hold the lifecycle lock of each data root, which every worker start
    and ``worker enable`` takes, across :func:`job_state_busy` and the
    deletion it allows: a worker started between the two would use the
    locks and handles being deleted. Every root, worker state or not, since
    an enable could create it meanwhile; taking the lock creates the folder,
    which the zap deletes. Yields False when one cannot be taken, and the
    caller keeps the job state. A held lock whose file the deletion removes
    is still released by its descriptor."""
    from openswap.worker.cli import lifecycle_lock

    with ExitStack() as stack:
        held = True
        for root in _data_roots():
            try:
                stack.enter_context(lifecycle_lock(root))
            except Exception:
                held = False
                break
        yield held


# What a zap says when it could not hold the worker's lifecycle locks.
WORKER_LIFECYCLE_BUSY = "another OpenSwap command is changing Remote tasks"


def _loaded_labels(uid: int | None = None) -> set[str] | None:
    """The labels loaded in this user's GUI launchd domain, where the agents
    and their jobs live, or None when it cannot be read.

    Named explicitly: a plain ``launchctl list`` from SSH reads the user or
    session domain instead, which never shows the GUI domain's services.
    No GUI domain (nobody logged in) or no services block is also None, so
    the caller keeps what it would delete.
    """
    target = f"gui/{os.getuid() if uid is None else uid}"
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", target], capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    labels: set[str] = set()
    inside = False
    for line in result.stdout.splitlines():
        if not inside:
            inside = line == "\tservices = {"
        elif line == "\t}":
            return labels
        elif line.strip():
            # "\t\t<pid> <status> \t<label>"
            labels.add(line.rsplit("\t", 1)[-1].strip())
    return None


def data_paths(
    home: Path | None = None, *, homebrew_zap: bool = False, keep_job_state: bool = False,
) -> list[Path]:
    """The files a zap deletes: the cask's ``zap trash:`` list plus the
    pre-XDG backup root, which purge also removes, and the worker's job
    state unless ``keep_job_state`` (see :func:`job_state_busy`). With
    ``homebrew_zap``, only what Homebrew's zap leaves behind."""
    home = home if home is not None else Path.home()
    library = home / "Library"
    logs = library / "Logs"
    backup, *legacy = _data_roots()
    # Not the uninstall's own log: an uninstall the app started is writing
    # it, and it is the only record of a step that fails after this one.
    # The job state first: it goes while the roots' lifecycle.lock files,
    # which worker_starts_held holds, still exist, so no worker enable can
    # recreate one and lock a new inode until it is gone.
    found = [] if keep_job_state else list(job_state_paths(home))
    found += [] if homebrew_zap else [
        backup,
        library / "Containers" / bundle.APPEX_ID,
        # The app's defaults (the status item's position), and a source
        # install's under the bundle id menubar_display gives its Python.
        library / "Preferences" / f"{bundle.APP_ID}.plist",
        library / "Preferences" / f"{bundle.APP_ID}.menubar.plist",
        *sorted(p for p in logs.glob("com.opensoft.openswap.*.log") if p.name != APP_LOG_NAME),
        *sorted(logs.glob("com.opensoft.openswap.*.err")),
    ]
    found.extend(legacy)
    return [path for path in found if path.exists() or path.is_symlink()]


def delete_data(
    home: Path | None = None, *, homebrew_zap: bool = False, keep_job_state: bool = False,
) -> list[Path]:
    """Delete :func:`data_paths`. Returns what was deleted; raises on the
    first path that will not go."""
    deleted = []
    for path in data_paths(home, homebrew_zap=homebrew_zap, keep_job_state=keep_job_state):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as e:
            raise ClaudeSwitchError(f"Could not delete {path}: {e}") from e
        deleted.append(path)
    return deleted


def homebrew_brew() -> Path | None:
    """The ``brew`` that installed this app, when Homebrew did."""
    if not bundle.installed_by_homebrew():
        return None
    prefix = bundle.cask_prefix()
    brew = None if prefix is None else prefix / "bin" / "brew"
    return brew if brew is not None and os.access(brew, os.X_OK) else None


def run(zap: bool, say: Say, *, from_app: bool = False) -> int:
    """Remove OpenSwap: ``openswap uninstall [--zap]`` after its checks and
    prompt.

    Removes the integrations, then with ``zap`` the saved accounts, settings
    and logs, then the app: it execs ``brew uninstall --cask`` when Homebrew
    installed it (whose hook finds nothing left) and does not return.
    Otherwise it names what is left for the user to remove.

    ``from_app`` marks the child that the menu bar's Uninstall OpenSwap…
    starts. Nobody reads that child's output, so a copy not installed by
    Homebrew moves itself to the Trash as the last step, and a failed
    ``brew uninstall`` opens the log.
    """
    from openswap.claude_desktop import default_root as desktop_profiles_root

    if desktop_profiles_root().exists():
        say(f"Claude Desktop profiles, including sign-ins and local work, are kept at {desktop_profiles_root()}.")
    still_installed = "OpenSwap is still installed. " + (
        "Fix the error above, then open OpenSwap and choose Uninstall OpenSwap… again."
        if from_app else f"Fix that, then run: openswap uninstall{' --zap' if zap else ''}"
    )
    brew = homebrew_brew()
    if from_app and brew is None and bundle.installed_by_homebrew():
        # Trashing the app would leave the cask registered with no app, and
        # its uninstall hook would never run: stop before removing anything.
        prefix = bundle.cask_prefix()
        where = prefix / "bin" / "brew" if prefix is not None else "brew"
        say(f"Error: Homebrew installed OpenSwap, but {where} cannot run, so nothing was removed.", error=True)
        say(
            "Fix Homebrew, then open OpenSwap and choose Uninstall OpenSwap… again, "
            "or run: brew uninstall --cask openswap",
            error=True,
        )
        return 1
    removal = remove_integrations()
    for name in removal.removed:
        say(f"Removed the {name}.")
    if removal.failed:
        for failure in removal.failed:
            say(f"Could not remove the {failure}", error=True)
        say(still_installed, error=True)
        return 1

    servers: list[str] = []
    if zap:
        from openswap.engine import Engine
        from openswap.exceptions import MigrationError

        servers = paired_servers()
        try:
            # Opening the data below can move the pre-XDG folder, and its
            # profiles' Keychain items are named from their old paths.
            delete_session_logins(legacy_roots())
            try:
                engine = Engine()
            except MigrationError:
                # Both folders hold data, so no openswap command (session
                # setup included) can open them: delete both without purge.
                engine = None
            if engine is not None:
                # purge rechecks live sessions, leases and the worker under
                # the locks session setup takes, then deletes saved accounts
                # and the backup folders: a session that went live during
                # the prompt is refused here, not deleted from under Claude.
                engine.purge(assume_yes=True)
            else:
                refuse_live_sessions()
            # What purge does not track (the worker's pairing, strays).
            deleted = delete_saved_logins()
            # Homebrew's zap deletes the cask's files; the rest go here.
            with worker_starts_held() as held:
                busy = job_state_busy(removal) if held else WORKER_LIFECYCLE_BUSY
                removed_files = delete_data(homebrew_zap=brew is not None, keep_job_state=busy is not None)
        except (ClaudeSwitchError, OSError) as e:
            say(f"Error: {e}", error=True)
            remaining = paired_servers() if servers else []
            for url in servers:
                if url not in remaining:
                    # purge got as far as its settings: a retry cannot name it.
                    say(server_notice(url))
            say(still_installed, error=True)
            return 1
        if deleted:
            say(f"Deleted {deleted} saved login{'s' if deleted != 1 else ''} from the Keychain.")
        for path in removed_files:
            say(f"Deleted {path}")
        if busy is not None:
            say(job_state_notice(busy))
        for url in servers:
            say(server_notice(url))

    if brew is not None:
        command = [str(brew), "uninstall", "--cask", *(["--zap"] if zap else []), "openswap"]
        say(f"Removing the app: brew {' '.join(command[1:])}")
        sys.stdout.flush()
        sys.stderr.flush()
        if from_app:
            # brew deletes the bundle this process runs from, so a shell (not
            # this process) waits for it and opens the log if it fails.
            script = f'"$0" "$@" || exec /usr/bin/open {shlex.quote(str(app_log_path()))}'
            command = ["/bin/sh", "-c", script, *command]
        os.execv(command[0], command)
    app = bundle.app_path()
    if app is None:
        say("To finish, run: uv tool uninstall openswap")
    elif from_app:
        _trash_app(app, say)
    else:
        say(f"To finish, move {app} to the Trash.")
    return 0


def _trash_app(app: Path, say: Say) -> None:
    """Move this app to the Trash, or reveal it in Finder when that fails so
    the user can finish by hand."""
    from openswap.widget_install import _move_to_trash

    try:
        _move_to_trash(app)
    except ClaudeSwitchError as e:
        say(str(e), error=True)
        say(f"To finish, move {app} to the Trash.")
        _open("-R", str(app))
        return
    say(f"Moved {app} to the Trash.")


def _open(*args: str) -> None:
    try:
        subprocess.run(["/usr/bin/open", *args], check=False, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass


def app_dialog() -> dict[str, str]:
    """The menu bar's Uninstall OpenSwap… confirmation, for this install:
    keyword arguments for its dialog."""
    lines = [
        "OpenSwap quits, then removes what it set up:",
        "• the menu bar login item",
        "• the Remote tasks worker",
        "• the Claude Code status line (your own comes back)",
        "• the widget",
    ]
    app = bundle.app_path()
    if app is None:
        after = "\n\nTo remove the openswap command too, run “uv tool uninstall openswap” afterwards."
    else:
        lines.append(f"• {app.name}, {'through Homebrew' if bundle.installed_by_homebrew() else 'moved to the Trash'}")
        after = ""
    names = [server_name(url) for url in paired_servers()]
    from openswap.claude_desktop import default_root as desktop_profiles_root

    if desktop_profiles_root().exists():
        after += "\n\nClaude Desktop profiles, including their sign-ins and local work, stay on this Mac even when deleting saved accounts."
    if names:
        # Nobody reads the uninstall's log, so say it here, before the zap.
        if len(names) == 1:
            still = f"{names[0]} still lists the Mac"
        else:
            still = "Each still lists the Mac"
        after = (
            f"\n\nDeleting saved accounts also forgets this Mac's pairing with {' and '.join(names)}. "
            f"{still} until you remove it there."
        ) + after
    return {
        "title": "Uninstall OpenSwap?",
        "message": "\n".join(lines) + (
            "\n\nClaude Code and Codex stay signed in, each to the account it uses now. "
            "Remote tasks' results stay in their folders. Your saved accounts stay unless you "
            "select the checkbox below. Deleting them cannot be undone."
        ) + after,
        "checkbox": "Also delete saved accounts, settings and logs",
        "ok": "Uninstall",
    }


def app_log_path(home: Path | None = None) -> Path:
    """Where the uninstall the menu bar starts writes its output."""
    return (home if home is not None else Path.home()) / "Library" / "Logs" / APP_LOG_NAME


def show_app_log() -> None:
    """Open the uninstall's log (in Console), for a failure nobody saw."""
    _open(str(app_log_path()))


def app_command(zap: bool) -> list[str]:
    """The child the menu bar starts: this app's own executable, or the
    console script a source install's login item would run."""
    from openswap import launch_agent

    return [
        *launch_agent.resolve_program(), "uninstall", "--yes",
        *(["--zap"] if zap else []), FROM_APP_FLAG,
    ]


def start_from_app(zap: bool, *, home: Path | None = None) -> list[str]:
    """Start :func:`app_command` detached from the menu bar app, which quits
    next; returns the command. Raises OSError when it cannot start.

    The child gets a new session, so it is not in the launchd job's process
    group and outlives the extra, and no ``XPC_SERVICE_NAME``: it is not the
    login item, whatever started the extra.
    """
    home = home if home is not None else Path.home()
    command = app_command(zap)
    log = app_log_path(home)
    log.parent.mkdir(parents=True, exist_ok=True)
    env = {key: value for key, value in os.environ.items() if key != "XPC_SERVICE_NAME"}
    if bundle.is_frozen():
        # A fresh bootloader, not one that reuses this process's PyInstaller state.
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    with open(log, "a", encoding="utf-8") as out:
        out.write(f"\n{datetime.now().isoformat(timespec='seconds')} {' '.join(command)}\n")
        out.flush()
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=home,
            env=env,
            start_new_session=True,
        )
    return command


def wait_for_app_to_quit(timeout: float = APP_QUIT_TIMEOUT) -> bool:
    """Wait for the menu bar app that started this process to quit.

    Removing the login item while it runs would boot it out mid-quit, and a
    zap could race its last writes. Its exit reparents this process, which
    changes the parent pid. False when it is still running after ``timeout``.
    """
    parent = os.getppid()
    deadline = time.monotonic() + timeout
    while parent > 1 and os.getppid() == parent:
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True
