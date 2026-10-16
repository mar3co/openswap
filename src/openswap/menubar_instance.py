"""One menu bar extra at a time.

The launchd job is the copy that should run: it restarts after a crash and is
what the cask's upgrade step restarts. Any other copy (Finder, Spotlight,
``openswap menubar`` in a terminal) that finds an extra already running asks
it to open its popover and exits, so opening the app again shows it instead of
adding a second status item.

The guard is an exclusive ``flock`` on ``<backup_root>/menubar.lock``, taken
before the status item exists. flock belongs to the open file description, so
the lock lasts exactly as long as the process that holds it, and a crash can
never leave it stale.

A show request is ``<backup_root>/menubar.show-request`` plus the distributed
notification ``com.opensoft.openswap.menubar.show``. The lock holder consumes
the file on the notification and again once its popover and observer are
attached at start-up, and opens the popover when the file is under 30 s old.
The file carries the request across the moments a notification alone would
miss: a holder that has the lock but no observer yet, or a launchd copy that
has not started yet.

A copy that gets the lock while the login item runs this program but is not
running (after Quit: ``KeepAlive`` does not restart a clean exit) hands over
to it: it leaves a show request, starts the job and exits once launchd has a
pid. Otherwise it would keep the lock, and the cask's upgrade kickstart would
start a launchd copy that waits behind the old build.

The job waits a while for the lock, because during a hand-over it starts
while the old copy is still quitting. If it still cannot get the lock it
exits 1 and launchd's ``KeepAlive`` retries it, unless Open at login was
turned off: then it unloads its own job and exits 0.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from pathlib import Path

from openswap import launch_agent
from openswap.locking import FileLock

LOCK_FILENAME = "menubar.lock"
SHOW_REQUEST_FILENAME = "menubar.show-request"
SHOW_NOTIFICATION = "com.opensoft.openswap.menubar.show"
ALREADY_RUNNING = "OpenSwap is already running in the menu bar."
LOGIN_ITEM_WAIT_SECONDS = 10.0
SHOW_REQUEST_MAX_AGE_SECONDS = 30.0


class AlreadyRunning(Exception):
    """Another extra holds the lock; ``exit_code`` is what this copy exits with."""

    def __init__(self, exit_code: int):
        super().__init__(ALREADY_RUNNING)
        self.exit_code = exit_code


def lock_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / LOCK_FILENAME


def show_request_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / SHOW_REQUEST_FILENAME


def menubar_lock(backup_dir: Path, wait: float = 0.0) -> FileLock | None:
    """Hold the menu bar lock, or None when another extra still holds it after
    ``wait`` seconds. Raises OSError when the lock file cannot be opened."""
    lock = FileLock(lock_path(backup_dir))
    return lock if lock.acquire(timeout=wait) else None


def post_show_notification() -> None:
    """Tell the lock holder a show request is waiting."""
    from Foundation import NSDistributedNotificationCenter

    # deliverImmediately: an accessory app is never active, and AppKit holds
    # back distributed notifications for inactive apps.
    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        SHOW_NOTIFICATION, None, None, True
    )


def claim(
    backup_dir: Path,
    *,
    as_login_item: bool,
    post: Callable[[], None] = post_show_notification,
    say: Callable[[str], object] = print,
    unload: Callable[[], bool] | None = None,
    logger=None,
) -> FileLock | None:
    """Take the menu bar lock for this process, or raise AlreadyRunning.

    ``as_login_item`` is whether this process is the launchd job
    (``launch_agent.runs_as_login_item()``): it waits up to
    ``LOGIN_ITEM_WAIT_SECONDS``, then exits 1 for ``KeepAlive`` to retry, or 0
    when ``unload`` (``launch_agent.unload_own_job``) unloaded its job because
    Open at login is off. Any other copy does not wait: it leaves a show
    request (``post`` is the notification half) and exits 0.

    Returns None, and the extra runs unguarded, only when the lock file
    itself cannot be opened: a broken data folder must not cost the user
    their menu bar.
    """
    try:
        lock = menubar_lock(backup_dir, wait=LOGIN_ITEM_WAIT_SECONDS if as_login_item else 0.0)
    except OSError:
        if logger is not None:
            logger.warning("menu bar lock unavailable; running without it", exc_info=True)
        return None
    if lock is not None:
        return lock
    if as_login_item:
        if (unload or launch_agent.unload_own_job)():
            print(
                "Another OpenSwap holds the menu bar and Open at login is off; unloading.",
                file=sys.stderr,
            )
            raise AlreadyRunning(0)
        print(
            f"Another OpenSwap holds {lock_path(backup_dir)}; launchd will retry.",
            file=sys.stderr,
        )
        raise AlreadyRunning(1)
    leave_show_request(backup_dir, post=post, logger=logger)
    say(ALREADY_RUNNING)
    raise AlreadyRunning(0)


def leave_show_request(
    backup_dir: Path,
    *,
    post: Callable[[], None] = post_show_notification,
    logger=None,
) -> None:
    """Ask the lock holder, now or once it starts, to open its popover."""
    path = show_request_path(backup_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time()}\n", encoding="utf-8")
    except OSError:
        if logger is not None:
            logger.warning("could not write the show request", exc_info=True)
    try:
        post()
    except Exception:
        if logger is not None:
            logger.warning("could not post the show request", exc_info=True)


def consume_show_request(backup_dir: Path, now: float | None = None) -> bool:
    """Remove the show request; True when it is under 30 s old. One request
    opens the popover once, whichever of the notification and start-up gets
    to it first."""
    path = show_request_path(backup_dir)
    try:
        age = (time.time() if now is None else now) - path.stat().st_mtime
    except OSError:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return False  # the other path took it between the stat and here
    except OSError:
        pass
    return -SHOW_REQUEST_MAX_AGE_SECONDS < age < SHOW_REQUEST_MAX_AGE_SECONDS


def hand_over_to_login_item(
    backup_dir: Path,
    *,
    post: Callable[[], None] = post_show_notification,
    say: Callable[[str], object] = print,
    logger=None,
) -> bool:
    """From a copy launchd did not start that holds the menu bar lock: when
    the login item runs this program, leave a show request, start the job
    and return True once launchd has a pid. The caller then exits 0 and the
    job takes the lock. A job that is already running (starting up, or
    waiting for this lock) is not started again.

    False, and this copy runs as before, when the login item is off, runs
    another program, or did not start (logged, and recorded by
    ``start_login_item`` for Settings).
    """
    try:
        if not launch_agent.login_item_runs_this():
            return False
        leave_show_request(backup_dir, post=post, logger=logger)
        pid = launch_agent.status()["pid"] or launch_agent.start_login_item(backup_dir=backup_dir)
    except Exception:
        if logger is not None:
            logger.warning("could not hand over to the login item; running here", exc_info=True)
        return False
    if pid is None:
        if logger is not None:
            logger.warning("the login item did not start; running here")
        return False
    _out_log, err_log = launch_agent.log_paths()
    say(f"Started OpenSwap from its login item (pid {pid}).\n  log: {err_log}")
    return True
