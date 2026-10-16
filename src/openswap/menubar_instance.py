"""One menu bar extra at a time.

The launchd job is the copy that should run: it restarts after a crash and is
what the cask's upgrade step restarts. Any other copy (Finder, Spotlight,
``openswap menubar`` in a terminal) that finds an extra already running asks
it to open its popover and exits, so opening the app again shows it instead of
adding a second status item.

The guard is an exclusive ``flock`` on ``<backup_root>/menubar.lock``, taken
before the status item exists. flock belongs to the open file description, so
the lock lasts exactly as long as the process that holds it, and a crash can
never leave it stale. The holder writes its pid into the file, so the CLI can
tell whether launchd's copy owns it (:func:`lock_holder_pid`).

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

``openswap setup`` and ``menubar --install-service`` start the job from
outside any extra. When an extra launchd did not start holds the lock (one
opened from Finder while Open at login was off), they leave a hand-over
request (``menubar.handoff-request`` plus ``com.opensoft.openswap.menubar.handoff``,
read like a show request). That extra checks again and, finding launchd's
copy up, leaves a show request and quits, so the job does not wait behind
the lock and loop under ``KeepAlive``.

The job waits a while for the lock, because during a hand-over it starts
while the old copy is still quitting. If it still cannot get the lock it
exits 1 and launchd's ``KeepAlive`` retries it, unless Open at login was
turned off: then it unloads its own job and exits 0.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO

from openswap import launch_agent

LOCK_FILENAME = "menubar.lock"
SHOW_REQUEST_FILENAME = "menubar.show-request"
HANDOFF_REQUEST_FILENAME = "menubar.handoff-request"
SHOW_NOTIFICATION = "com.opensoft.openswap.menubar.show"
HANDOFF_NOTIFICATION = "com.opensoft.openswap.menubar.handoff"
ALREADY_RUNNING = "OpenSwap is already running in the menu bar."
LOGIN_ITEM_WAIT_SECONDS = 10.0
SHOW_REQUEST_MAX_AGE_SECONDS = 30.0
# How long setup waits for launchd's copy to hold the lock after a hand-over
# request: the job's own lock wait is 10 s, plus its start-up.
CLI_HANDOFF_WAIT_SECONDS = 15.0
# How long a copy that lost the lock watches for the holder to take its show
# request, in case the holder is quitting and the lock is about to come free.
HOLDER_ANSWER_SECONDS = 2.0
_LOCK_POLL_SECONDS = 0.1


class AlreadyRunning(Exception):
    """Another extra holds the lock; ``exit_code`` is what this copy exits with."""

    def __init__(self, exit_code: int):
        super().__init__(ALREADY_RUNNING)
        self.exit_code = exit_code


def lock_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / LOCK_FILENAME


def show_request_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / SHOW_REQUEST_FILENAME


def handoff_request_path(backup_dir: Path) -> Path:
    return Path(backup_dir) / HANDOFF_REQUEST_FILENAME


class MenubarLock:
    """The held menu bar lock. Released when the process exits, or by
    :meth:`release`."""

    def __init__(self, file: IO[str]):
        self._file = file

    def release(self) -> None:
        import fcntl

        if self._file is None:
            return
        try:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


def menubar_lock(backup_dir: Path, wait: float = 0.0) -> MenubarLock | None:
    """Hold the menu bar lock and write this process's pid into it, or None
    when another extra still holds it after ``wait`` seconds. Raises OSError
    when the lock file cannot be opened or locked.

    The file is opened without truncating it: a copy that loses the race must
    not erase the holder's pid.
    """
    import fcntl

    path = lock_path(backup_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    file = open(path, "a+", encoding="utf-8")
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                file.close()
                return None
            time.sleep(_LOCK_POLL_SECONDS)
        except OSError:
            file.close()
            raise
    file.seek(0)
    file.truncate()
    file.write(f"{os.getpid()}\n")
    file.flush()
    return MenubarLock(file)


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists, owned by someone else
    return True


def lock_holder_pid(backup_dir: Path) -> int | None:
    """The pid the last holder of the menu bar lock wrote, or None."""
    try:
        text = lock_path(backup_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _post(name: str) -> None:
    from Foundation import NSDistributedNotificationCenter

    # deliverImmediately: an accessory app is never active, and AppKit holds
    # back distributed notifications for inactive apps.
    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        name, None, None, True
    )


def post_show_notification() -> None:
    """Tell the lock holder a show request is waiting."""
    _post(SHOW_NOTIFICATION)


def post_handoff_notification() -> None:
    """Tell the lock holder a hand-over request is waiting."""
    _post(HANDOFF_NOTIFICATION)


def claim(
    backup_dir: Path,
    *,
    as_login_item: bool,
    post: Callable[[], None] = post_show_notification,
    say: Callable[[str], object] = print,
    unload: Callable[[], bool] | None = None,
    logger=None,
) -> MenubarLock | None:
    """Take the menu bar lock for this process, or raise AlreadyRunning.

    ``as_login_item`` is whether this process is the launchd job
    (``launch_agent.runs_as_login_item()``): it waits up to
    ``LOGIN_ITEM_WAIT_SECONDS``, then exits 1 for ``KeepAlive`` to retry, or 0
    when ``unload`` (``launch_agent.unload_own_job``) unloaded its job because
    Open at login is off. Any other copy does not wait for the lock: it leaves
    a show request (``post`` is the notification half) and exits 0 once the
    holder takes it. The holder may be on its way out (between Quit and exit),
    and then nobody would answer and nothing would be left running, so for up
    to ``HOLDER_ANSWER_SECONDS`` it also tries the lock again, and runs if the
    lock comes free (its own show request then opens its popover).

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
    deadline = time.monotonic() + HOLDER_ANSWER_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_LOCK_POLL_SECONDS)
        if not show_request_path(backup_dir).exists():
            break  # the holder took it: it is alive and showing its popover
        try:
            lock = menubar_lock(backup_dir)
        except OSError:
            break
        if lock is not None:
            return lock  # the holder quit meanwhile: run here instead
    say(ALREADY_RUNNING)
    raise AlreadyRunning(0)


def _leave_request(path: Path, what: str, post: Callable[[], None], logger) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{time.time()}\n", encoding="utf-8")
    except OSError:
        if logger is not None:
            logger.warning("could not write the %s request", what, exc_info=True)
    try:
        post()
    except Exception:
        if logger is not None:
            logger.warning("could not post the %s request", what, exc_info=True)


def leave_show_request(
    backup_dir: Path,
    *,
    post: Callable[[], None] = post_show_notification,
    logger=None,
) -> None:
    """Ask the lock holder, now or once it starts, to open its popover."""
    _leave_request(show_request_path(backup_dir), "show", post, logger)


def leave_handoff_request(
    backup_dir: Path,
    *,
    post: Callable[[], None] = post_handoff_notification,
    logger=None,
) -> None:
    """Ask an extra launchd did not start to step aside for the login item."""
    _leave_request(handoff_request_path(backup_dir), "hand-over", post, logger)


def consume_show_request(backup_dir: Path, now: float | None = None) -> bool:
    """Remove the show request; True when it is under 30 s old. One request
    opens the popover once, whichever of the notification and start-up gets
    to it first."""
    return _consume_request(show_request_path(backup_dir), now)


def consume_handoff_request(backup_dir: Path, now: float | None = None) -> bool:
    """Remove the hand-over request; True when it is under 30 s old."""
    return _consume_request(handoff_request_path(backup_dir), now)


def _consume_request(path: Path, now: float | None) -> bool:
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
        pid = launch_agent.running_login_item_pid() or launch_agent.start_login_item(backup_dir=backup_dir)
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


def login_item_waiting(*, logger=None) -> int | None:
    """From an extra launchd did not start, on a hand-over request: the login
    item's pid when it runs this program and launchd's copy is up (waiting
    for this copy's lock), else None. The step 1 check, run again."""
    if launch_agent.runs_as_login_item():
        return None
    try:
        if not launch_agent.login_item_runs_this():
            return None
        return launch_agent.running_login_item_pid()
    except Exception:
        if logger is not None:
            logger.warning("could not check the login item", exc_info=True)
        return None


def hand_over_from_cli(
    backup_dir: Path,
    pid: int,
    *,
    post: Callable[[], None] = post_handoff_notification,
    timeout: float = CLI_HANDOFF_WAIT_SECONDS,
    logger=None,
) -> int | None:
    """After ``install()`` started the login item (launchd's ``pid``): ask an
    extra launchd did not start to step aside, then wait until launchd's copy
    holds the menu bar lock. Returns the holder's pid when it stopped waiting,
    which is ``pid`` on success."""
    leave_handoff_request(backup_dir, post=post, logger=logger)
    deadline = time.monotonic() + timeout
    while True:
        holder = lock_holder_pid(backup_dir)
        if holder == pid or time.monotonic() >= deadline:
            return holder
        time.sleep(0.2)
