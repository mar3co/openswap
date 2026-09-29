#!/usr/bin/env python3
"""Credential-free feasibility harness for plan 017's Codex host spike.

This script intentionally has no authenticated Codex execution path. It can
inspect ``codex --version`` and ``codex exec --help`` in a disposable home, and
it can exercise process supervision with a caller-supplied fake executable.
Neither proves provider login, account isolation, web research, refresh, or
Codex cancellation semantics.

Process-tree cleanup is best effort. While the fake leader is alive the
supervisor periodically snapshots ``ps`` and records every descendant by
parent pid, regardless of process group, so a helper that calls ``setsid()``
is still found and terminated after the leader exits. A descendant that forks
between the final snapshot and the leader's exit is reparented before it can
be attributed to the run and can still escape, so the harness never claims
complete process-tree cleanup; it only refuses to report ``succeeded`` or
``cancelled`` when it did observe an escaped descendant. Signalling an escaped
descendant is identity-safe only where the OS offers a non-reusable process
handle (Linux ``pidfd``); on macOS the harness re-checks a start-time,
group and command identity before each signal and records the cleanup as
uncertain in evidence.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import json
import math
import os
import re
import select
import selectors
import shlex
import shutil
import signal
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping, Sequence

DISCOVERED_CODEX_VERSION = "codex-cli 0.158.0-alpha.2.1"
LIVE_CODEX_ENABLED = False
MAX_RETAINED_EVENT_NAMES = 256
MAX_EVENT_LINE_CHARS = 64 * 1024
EVENT_NAMES_TRUNCATED = "event-names-truncated"
PROBE_TERMINATION_GRACE_S = 0.15
PROBE_CLEANUP_WAIT_S = 0.5
MAX_PROBE_OUTPUT_BYTES = 256 * 1024
PROBE_READ_CHUNK_BYTES = 16 * 1024
DESCENDANT_SNAPSHOT_INTERVAL_S = 0.05
DESCENDANT_KILL_WAIT_S = 0.5
GROUP_KILL_WAIT_S = 0.5
NON_TERMINAL_STATES = frozenset({"starting", "running", "cancel_requested"})
# Lowercase dotted identifiers such as Codex's ``thread.started``; anything
# else (including anything that could carry a token) becomes ``unknown-event``.
_EVENT_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}(\.[a-z][a-z0-9_]{0,31}){0,3}")
# A version is digits and dots with an optional pre-release suffix; a bare
# token of any other shape (which could be a secret) is never persisted.
_VERSION_OUTPUT = re.compile(r"codex-cli \d{1,4}(?:\.\d{1,4}){1,3}(?:-[0-9A-Za-z][0-9A-Za-z.]{0,23})?")


class SpikeError(RuntimeError):
    """A fail-closed harness error suitable for display to an operator."""


def _run_probe(command: Sequence[str], *, env: dict[str, str], cwd: str,
               timeout_s: float) -> subprocess.CompletedProcess:
    """Run a probe with bounded output capture and owned-group cleanup."""
    if os.name != "posix":
        raise SpikeError("Bounded Codex probes require POSIX process supervision.")
    process = subprocess.Popen(
        list(command), env=env, cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    stdout_fd = process.stdout.fileno()
    stderr_fd = process.stderr.fileno()
    buffers = {stdout_fd: bytearray(), stderr_fd: bytearray()}
    selector = None
    timed_out = False
    overflowed = False
    cleanup_uncertain = False
    leftovers = False
    detached = False
    tracking_incomplete = False
    tracked: dict[int, tuple[int, str, str, int | None, str, bool]] = {}
    last_snapshot = 0.0
    try:
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            fd = stream.fileno()
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_s
        # The leader is observed, not reaped, until group cleanup is done so
        # its pid (and therefore our pgid) cannot be reused meanwhile.
        exited = False
        while not exited or selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            now = time.monotonic()
            if (
                now - last_snapshot >= DESCENDANT_SNAPSHOT_INTERVAL_S
                and deadline - now >= SCAN_WINDOW_S
            ):
                # Best effort: descendants are attributed by parent pid while
                # the leader is alive, so one that calls setsid() is still ours.
                last_snapshot = now
                # A missed scan could miss a helper that forks, detaches and
                # is reparented before the next one, so it refuses the result.
                if not _track_descendants(process.pid, tracked, deadline=deadline):
                    tracking_incomplete = True
            # Observe exit BEFORE selecting: anything the group wrote before
            # it went quiet is then already readable, so an empty select after
            # that observation really means no output is left.
            exited = _leader_exited(process, deadline=deadline)
            quiet = exited and not _probe_group_running(process.pid)
            events = selector.select(min(remaining, 0.05)) if selector.get_map() else ()
            if not selector.get_map() and not exited:
                time.sleep(min(remaining, 0.02))
            if not events and quiet:
                # The leader is gone and no group member can still write: with
                # nothing readable there is no output left, so stop waiting.
                break
            for key, _ in events:
                fd = key.fd
                try:
                    chunk = os.read(fd, PROBE_READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(fd)
                    continue
                buffer = buffers[fd]
                available = MAX_PROBE_OUTPUT_BYTES - len(buffer)
                if len(chunk) > available:
                    if available > 0:
                        buffer.extend(chunk[:available])
                    overflowed = True
                    break
                buffer.extend(chunk)
            if overflowed:
                break
        if timed_out or overflowed:
            cleanup_uncertain = _cleanup_probe_process(process)
        else:
            leftovers = _probe_group_running(process.pid)
            if leftovers:
                cleanup_uncertain = _cleanup_probe_process(process)
        # Group signals miss a descendant that left the group, so hunt the
        # tracked ones before the leader is reaped. Any found refuses the result.
        detached, detached_certain = _sweep_probe_descendants(
            process, tracked, tracking_incomplete=tracking_incomplete
        )
        cleanup_uncertain = cleanup_uncertain or not detached_certain or tracking_incomplete
        _reap_leader(process, timeout_s=PROBE_CLEANUP_WAIT_S)
    except BaseException:
        # Snapshot first, while the leader is still the parent of anything
        # that detached since the last periodic scan; killing the group
        # reparents such a helper and the sweep could no longer find it.
        try:
            _track_descendants(
                process.pid, tracked, deadline=time.monotonic() + CLEANUP_SCAN_BUDGET_S
            )
        except Exception:
            pass
        _cleanup_probe_process(process)
        try:
            _sweep_probe_descendants(process, tracked)
        except Exception:
            pass
        raise
    finally:
        _close_handles(tracked)
        if selector is not None:
            selector.close()
        process.stdout.close()
        process.stderr.close()
        if process.returncode is None and _leader_exited(process):
            process.wait()
    if overflowed:
        raise SpikeError("Probe output exceeded the bounded capture limit; result was refused.")
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout_s) from None
    if detached:
        raise SpikeError("Probe left a detached descendant; result was refused.")
    if cleanup_uncertain or leftovers:
        raise SpikeError("Probe cleanup was uncertain; result was refused.")
    try:
        decoded_stdout = bytes(buffers[stdout_fd]).decode("utf-8")
        decoded_stderr = bytes(buffers[stderr_fd]).decode("utf-8")
    except UnicodeDecodeError:
        raise
    return subprocess.CompletedProcess(command, process.returncode, decoded_stdout, decoded_stderr)


def _leader_exited(
    process: subprocess.Popen,
    table: list[tuple[int, int, int, str, str, str]] | None = None,
    deadline: float | None = None,
) -> bool:
    """Whether the group leader has exited, observed WITHOUT reaping it.

    A reaped leader frees its pid, and with it the process-group id we signal,
    for reuse by an unrelated process. Keeping the leader as a zombie until
    group cleanup is finished reserves that id. ``waitid(WNOWAIT)`` is used
    where available; otherwise the process table's state column (``Z``)
    serves, then a single-pid ``ps`` state lookup. It never reaps: when no
    observation works the leader is reported as not known to have exited,
    so callers time out into cleanup and an uncertain (``interrupted``)
    result rather than free the pid while cleanup still depends on it.
    With ``deadline`` the ``ps`` fallback is bounded by the time left.
    """
    if process.returncode is not None:
        return True
    waitid = getattr(os, "waitid", None)
    if waitid is not None and hasattr(os, "WNOWAIT"):
        try:
            return waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        except ChildProcessError:
            return True
        except OSError:
            pass
    if table is not None:
        for pid, _ppid, _pgid, stat, _birth, _identity_ in table:
            if pid == process.pid:
                return stat.startswith(("Z", "X"))
        # An unreaped child is always listed; missing means it is gone.
        return True
    state = _pid_state(process.pid, timeout_s=None if deadline is None else _ps_budget(deadline))
    if state is not None:
        return state.startswith(("Z", "X"))
    return False


def _pid_state(pid: int, timeout_s: float | None = None) -> str | None:
    """The ``ps`` state column for ``pid`` (``Z`` for an unreaped exit), or None."""
    ps = shutil.which("ps") or "/bin/ps"
    try:
        listing = subprocess.run(
            [ps, "-o", "stat=", "-p", str(pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            timeout=PS_TIMEOUT_S if timeout_s is None else timeout_s, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    state = listing.stdout.strip()
    return state or None


def _probe_group_running(pgid: int, deadline: float | None = None) -> bool:
    return _group_alive(pgid, deadline)


def _cleanup_probe_process(process: subprocess.Popen) -> bool:
    """Bounded cleanup for this probe's session without reading its pipes.

    The leader is observed, not reaped: its pid (and so our pgid) stays
    reserved until the caller has finished hunting detached descendants.
    Returns whether cleanup is uncertain.
    """
    _signal_group(process, signal.SIGTERM)
    deadline = time.monotonic() + PROBE_TERMINATION_GRACE_S
    while _probe_group_running(process.pid, deadline) and time.monotonic() < deadline:
        time.sleep(0.02)
    # A scan that cannot finish by the grace deadline errs toward "running".
    if _probe_group_running(process.pid, deadline):
        _signal_group(process, signal.SIGKILL)
    if not _wait_leader_exited(process, PROBE_CLEANUP_WAIT_S):
        _signal_group(process, signal.SIGKILL)
        if not _wait_leader_exited(process, PROBE_CLEANUP_WAIT_S):
            return True
    # SIGKILL takes effect only when a member leaves the kernel (for example a
    # slow disk write), so wait, bounded, for the group to actually be gone.
    deadline = time.monotonic() + PROBE_CLEANUP_WAIT_S
    while _probe_group_running(process.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return _probe_group_running(process.pid)


def _sweep_probe_descendants(
    process: subprocess.Popen,
    tracked: dict[int, tuple[int, str, str, int | None, str, bool]],
    *,
    tracking_incomplete: bool = False,
) -> tuple[bool, bool]:
    """Terminate tracked probe descendants outside its group: (any found, cleanup certain).

    A failed final scan still sweeps the descendants earlier scans recorded
    (group cleanup cannot reach one that called setsid()); the failure only
    makes the result uncertain.
    """
    scanned = _track_descendants(
        process.pid, tracked, deadline=time.monotonic() + CLEANUP_SCAN_BUDGET_S
    )
    escaped = _escaped_descendants(
        process.pid, tracked, include_grouped_pidfds=tracking_incomplete or not scanned
    )
    if not escaped:
        return False, scanned
    all_gone, _mode = _terminate_pids(escaped, grace_s=PROBE_TERMINATION_GRACE_S)
    return True, all_gone and scanned


# The only symlinks trusted in an output path: macOS's root-owned top-level
# system links, each with its exact expected target.
_TRUSTED_SYSTEM_LINKS = {
    "darwin": {"/tmp": "private/tmp", "/var": "private/var", "/etc": "private/etc"},
}


def _trusted_system_link(current: Path, status: os.stat_result) -> bool:
    expected = _TRUSTED_SYSTEM_LINKS.get(sys.platform, {}).get(str(current))
    if expected is None or os.name != "posix" or status.st_uid != 0:
        return False
    try:
        return os.readlink(current) in (expected, "/" + expected)
    except OSError:
        return False


def _refuse_unsafe_directory(current: Path, status: os.stat_result) -> None:
    # Another user who can modify an ancestor could rename our directory and
    # put a symlink in its place after validation. A sticky directory (such
    # as /tmp) lets only an entry's owner rename it, so it stays safe.
    if os.name != "posix":
        return
    if status.st_uid not in (0, os.getuid()):
        raise SpikeError("Private output path must not sit under another user's directory.")
    if status.st_mode & 0o022 and not status.st_mode & stat_module.S_ISVTX:
        raise SpikeError("Private output path must not sit under a directory others can modify.")


def _refuse_symlinked_components(path: Path) -> None:
    """Refuse a path that another user could redirect, before or after validation.

    A symlinked component (not just the leaf) would redirect every directory
    and file we create into its target, so only an exactly listed system link
    (macOS's ``/tmp``, ``/var`` and ``/etc`` -> ``/private/...``) is trusted;
    ownership or location alone is not, because a harness run as root would
    own every link it could be tricked into following. Every existing
    directory must also be owned by us or root and not be modifiable by
    others unless sticky, so nobody can swap a component for a symlink after
    this check. On platforms without POSIX ownership any symlink is refused.
    """
    anchor = Path(path.absolute().anchor)
    current = anchor
    for part in path.absolute().parts[1:]:
        current = current / part
        try:
            status = os.lstat(current)
        except OSError:
            # Missing (we create the rest below) or not a directory: the
            # creation step raises the real error, reported with details withheld.
            return
        if stat_module.S_ISLNK(status.st_mode):
            if not _trusted_system_link(current, status):
                raise SpikeError("Private output path must not traverse a symlink.")
            try:
                status = os.stat(current)
            except OSError:
                return
        if stat_module.S_ISDIR(status.st_mode) and current.parent != current:
            _refuse_unsafe_directory(current, status)


def _private_dir(path: Path) -> None:
    _refuse_symlinked_components(path)
    # Create missing ancestors one by one so every level we own is 0o700,
    # rather than trusting mkdir(parents=True) to apply the mode above the leaf.
    missing = []
    current = path
    # lexists, not exists: a component raced in as a symlink must count as
    # existing (and be validated below), never be walked through as missing.
    while not os.path.lexists(current) and current.parent != current:
        missing.append(current)
        current = current.parent
    # Re-validate the existing prefix right before creating anything under it.
    _refuse_symlinked_components(current)
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            # Someone created it between our check and mkdir: it must be a
            # real directory, never a symlink we would then create through.
            try:
                raced = os.lstat(directory)
            except OSError:
                raise SpikeError("Private output path changed while it was created.") from None
            if not stat_module.S_ISDIR(raced.st_mode):
                raise SpikeError("Private output path must not traverse a symlink.")
            # Validate before creating beneath it: another user's raced-in
            # directory could be swapped for a symlink before the next mkdir.
            _refuse_unsafe_directory(directory, raced)
        else:
            # A new directory entry is only durable once its parent is synced.
            _fsync_dir(directory.parent)
    # Re-check the whole path now that every component exists.
    _refuse_symlinked_components(path)
    # lstat, not stat: a symlinked directory would redirect every file we
    # create into its target, and O_NOFOLLOW on file names does not cover it.
    try:
        status = os.lstat(path)
    except OSError:
        raise SpikeError("Private output path must be a directory.") from None
    if stat_module.S_ISLNK(status.st_mode):
        raise SpikeError("Private output directory must not be a symlink.")
    if not stat_module.S_ISDIR(status.st_mode):
        raise SpikeError("Private output path must be a directory.")
    if os.name == "posix":
        if status.st_mode & 0o077:
            raise SpikeError("Existing output directory must not grant group or world access.")
        if status.st_uid != os.getuid():
            raise SpikeError("Existing output directory must be owned by the current user.")


def _fsync_dir(directory: Path) -> None:
    """Persist a directory's entries (new files or subdirectories) to disk."""
    if os.name != "posix":
        # Windows cannot open a directory handle this way and has no
        # equivalent directory fsync; the harness's durability claims are
        # POSIX-only, matching the rest of the process supervision.
        return
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    """Open without following a symlink planted at the final path component."""
    try:
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise SpikeError("refusing to follow a symlink") from None
        raise


def _append_jsonl(path: Path, record: dict) -> None:
    _private_dir(path.parent)
    fd = _open_nofollow(path, os.O_RDWR | os.O_CREAT | os.O_APPEND)
    try:
        end = os.lseek(fd, 0, os.SEEK_END)
        separator = b""
        if end:
            os.lseek(fd, end - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                separator = b"\n"
        os.lseek(fd, 0, os.SEEK_END)
        payload = separator + (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        while payload:
            written = os.write(fd, payload)
            if written <= 0:
                raise OSError("JSONL append made no progress")
            payload = payload[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    # fsync on the file does not persist a newly created journal's directory
    # entry; without this a crash after launch could lose the launch intent and
    # let the same job ID be accepted again.
    _fsync_dir(path.parent)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with os.fdopen(_open_nofollow(path, os.O_RDONLY), "rb") as stream:
        raw = stream.read()
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            # JSONDecodeError is a ValueError; so is the int-digit limit.
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _safe_event_name(value: object) -> str:
    name = str(value or "unknown")
    return name if _EVENT_NAME.fullmatch(name) else "unknown-event"


def _latest(records: list[dict], job_id: str) -> dict | None:
    return next((row for row in reversed(records) if row.get("job_id") == job_id), None)


@contextmanager
def _locked_state_directory(state_dir: Path):
    """Exclusively protect this POSIX fake-harness state directory."""
    if os.name != "posix":
        raise SpikeError("Atomic fake-harness state locking requires POSIX.")
    import fcntl

    lock_fd = _open_nofollow(state_dir / "supervisor.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SpikeError("Another fake-harness supervisor or recovery is active.") from None
        yield
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


def recover_uncertain_runs(state_dir: Path) -> list[str]:
    """Mark idle intents without terminal outcomes interrupted; never relaunch."""
    state_dir = state_dir.expanduser().absolute()
    _private_dir(state_dir)
    with _locked_state_directory(state_dir):
        return _recover_uncertain_runs_locked(state_dir)


def _recover_uncertain_runs_locked(state_dir: Path) -> list[str]:
    journal = state_dir / "journal.jsonl"
    latest: dict[str, dict] = {}
    latest_running: dict[str, dict] = {}
    for row in _read_jsonl(journal):
        job_id = row.get("job_id")
        if isinstance(job_id, str):
            latest[job_id] = row
            if row.get("state") == "running":
                latest_running[job_id] = row
    interrupted = []
    for job_id, row in latest.items():
        if row.get("state") in NON_TERMINAL_STATES:
            record = {"job_id": job_id, "state": "interrupted", "reason": "uncertain_after_restart"}
            pgid = latest_running.get(job_id, {}).get("pgid")
            if isinstance(pgid, int) and not isinstance(pgid, bool) and pgid > 1:
                # Observe only: the pgid may already belong to an unrelated
                # process after a restart, so recovery never signals it.
                record["group_still_alive"] = _group_alive(pgid)
            _append_jsonl(journal, record)
            _append_jsonl(state_dir / "evidence.jsonl", {"kind": "recovery", **record})
            interrupted.append(job_id)
    return interrupted


def _group_alive(pgid: int, deadline: float | None = None) -> bool:
    """Whether any process in ``pgid`` can still execute (zombies excluded)."""
    observed = _group_has_running_members(pgid, deadline)
    if observed is not None:
        return observed
    # Without enumeration, killpg(0) also succeeds for a zombie-only group, so
    # this fallback errs toward "alive".
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _group_has_running_members(pgid: int, deadline: float | None = None) -> bool | None:
    """Return whether the group has non-zombie processes; None if unavailable.

    On macOS the ``ps`` scan is bounded by ``deadline`` (one second at most),
    so a stalled ``ps`` cannot postpone a signal past its grace deadline.
    """
    if sys.platform.startswith("linux"):
        proc_root = Path("/proc")
        try:
            for entry in proc_root.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    stat = (entry / "stat").read_text(encoding="utf-8")
                    fields = stat[stat.rfind(")") + 2 :].split()
                    # After comm: state, ppid, pgrp, ...
                    if len(fields) > 2 and int(fields[2]) == pgid and fields[0] not in {"Z", "X"}:
                        return True
                except (OSError, ValueError):
                    continue
            return False
        except OSError:
            pass
    if sys.platform == "darwin":
        try:
            listing = subprocess.run(
                ["/bin/ps", "-axo", "pid=,pgid=,stat="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=min(GROUP_SCAN_TIMEOUT_S, _ps_budget(deadline)),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if listing.returncode != 0:
            return None
        for line in listing.stdout.splitlines():
            columns = line.split()
            if len(columns) >= 3:
                try:
                    member_group = int(columns[1])
                except ValueError:
                    continue
                if member_group == pgid and not columns[2].startswith(("Z", "X")):
                    return True
        return False
    return None


def _group_running(
    pgid: int, reader: threading.Thread, deadline: float | None = None
) -> bool:
    observed = _group_has_running_members(pgid, deadline)
    if observed is not None:
        return observed
    # Without enumeration, EOF alone cannot prove that all descendants exited
    # because a child may have closed stdout. Treat any remaining group as
    # uncertain; killpg(0) may include zombies (including the deliberately
    # unreaped leader), which errs toward "interrupted" rather than success.
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return reader.is_alive()
    except PermissionError:
        return True
    return True


def _terminate_group(
    process: subprocess.Popen,
    reader: threading.Thread,
    *,
    grace_s: float,
) -> bool:
    """TERM, then KILL only our process group; return if cleanup is uncertain."""
    _signal_group(process, signal.SIGTERM)
    grace_deadline = time.monotonic() + grace_s
    while _group_running(process.pid, reader, grace_deadline) and time.monotonic() < grace_deadline:
        time.sleep(0.03)
    # A scan that cannot finish by the grace deadline errs toward "running".
    if _group_running(process.pid, reader, grace_deadline):
        _signal_group(process, signal.SIGKILL)
    # Observe the leader's exit without reaping it: the caller reaps only after
    # descendant discovery and cleanup, so the leader's pid (our pgid, and the
    # ancestry root for tracking) cannot be reused in the meantime.
    wait_uncertain = not _wait_leader_exited(process, 1.0)
    if wait_uncertain:
        _signal_group(process, signal.SIGKILL)
        wait_uncertain = not _wait_leader_exited(process, 1.0)
    if reader.ident is None:
        if process.stdout is not None:
            process.stdout.close()
    else:
        reader.join(timeout=1)
    # Give the kernel a moment to reap KILLed members so a slow reap does not
    # turn a clean cancellation into "interrupted". Once the leader is reaped
    # its pid (and therefore this pgid) may be reused by an unrelated process,
    # so this wait is bounded and only ever observes, never re-signals.
    kill_deadline = time.monotonic() + GROUP_KILL_WAIT_S
    while time.monotonic() < kill_deadline and _group_running(process.pid, reader):
        time.sleep(0.02)
    return (
        wait_uncertain
        or not _leader_exited(process, deadline=time.monotonic() + SCAN_WINDOW_S)
        or _group_running(process.pid, reader)
    )


def _wait_leader_exited(process: subprocess.Popen, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while not _leader_exited(process, deadline=deadline):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _reap_leader(process: subprocess.Popen, timeout_s: float = 1.0) -> None:
    """Reap the leader once cleanup is finished; bounded so a survivor cannot hang us."""
    if process.returncode is not None:
        return
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        pass


def _abort_supervision(
    process: subprocess.Popen,
    reader: threading.Thread,
    tracked: dict[int, tuple[int, str, str, int | None, str, bool]],
    *,
    grace_s: float,
) -> None:
    """Best-effort cleanup after a supervision failure, then reap the leader.

    A failure after a descendant detached (for example a full disk on a
    journal append) must not leave that descendant running, so descendants
    are snapshotted while the leader is still their parent, the group is
    terminated, and tracked descendants outside the group are swept.
    """
    try:
        _track_descendants(process.pid, tracked, deadline=time.monotonic() + CLEANUP_SCAN_BUDGET_S)
    except Exception:
        pass
    try:
        _terminate_group(process, reader, grace_s=grace_s)
        scanned = _track_descendants(
            process.pid, tracked, deadline=time.monotonic() + CLEANUP_SCAN_BUDGET_S
        )
        leftover = _escaped_descendants(
            process.pid, tracked, include_grouped_pidfds=not scanned
        )
        if leftover:
            _terminate_pids(leftover, grace_s=grace_s)
    except Exception:
        pass
    finally:
        _close_handles(tracked)
        _reap_leader(process)


def _ps_budget(deadline: float | None) -> float:
    """``ps`` timeout that cannot carry a scan past a supervision deadline."""
    if deadline is None:
        return PS_TIMEOUT_S
    return min(PS_TIMEOUT_S, max(deadline - time.monotonic(), MIN_PS_TIMEOUT_S))


def _snapshot(deadline: float | None) -> list[tuple[int, int, int, str, str, str]] | None:
    return _process_table() if deadline is None else _process_table(timeout_s=_ps_budget(deadline))


def _process_table(timeout_s: float | None = None) -> list[tuple[int, int, int, str, str, str]] | None:
    """Best-effort ``(pid, ppid, pgid, stat, birth, identity)`` snapshot; None when unavailable.

    ``birth`` is the immutable start time (``lstart``, second resolution) used
    to detect a reused pid without ever dropping a live descendant whose
    mutable fields changed. ``identity`` adds the process group and command
    line and is compared immediately before a signal is sent. Where the OS
    offers a non-reusable handle (``pidfd`` on Linux) the harness prefers it;
    see :func:`_open_pidfd`.
    """
    ps = shutil.which("ps") or "/bin/ps"
    try:
        listing = subprocess.run(
            [ps, "-axo", "pid=,ppid=,pgid=,stat=,lstart=,command="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PS_TIMEOUT_S if timeout_s is None else timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if listing.returncode != 0:
        return None
    table = []
    for line in listing.stdout.splitlines():
        columns = line.split()
        if len(columns) < 3:
            continue
        try:
            pid, ppid, pgid = int(columns[0]), int(columns[1]), int(columns[2])
        except ValueError:
            continue
        stat = columns[3] if len(columns) > 3 else ""
        # lstart is five tokens ("Tue Sep 22 19:30:46 2026"); the rest is the
        # command line.
        birth = " ".join(columns[4:9])
        identity = _identity(birth, pgid, " ".join(columns[9:]))
        table.append((pid, ppid, pgid, stat, birth, identity))
    return table


def _identity(lstart: str, pgid: int, command: str) -> str:
    return f"{lstart}|pgid={pgid}|{command}"


def _command_of(identity: str) -> str:
    return identity.split("|", 2)[-1]


def _pid_identity(pid: int) -> str | None:
    """Current identity of ``pid`` (see :func:`_process_table`), or None when gone.

    Deliberately built from the same listing and parser as the snapshots, so
    an identity recorded from a snapshot compares equal to one read back here
    on every ``ps`` implementation. A zombie has exited and cannot execute, so
    it is reported as gone.
    """
    table = _process_table()
    if table is None:
        return None
    for entry_pid, _ppid, _pgid, stat, _birth, identity in table:
        if entry_pid == pid:
            return None if stat.startswith(("Z", "X")) else identity
    return None


def _pid_row(pid: int, timeout_s: float | None = None) -> tuple[int, int, str, str] | None:
    """``(ppid, pgid, birth, identity)`` for a live ``pid``, or None when gone.

    Used to validate a freshly opened pidfd: a pid reused within the same
    second keeps the same second-resolution start time, so the parent, group
    and command line must all still match the snapshot as well.
    """
    table = _process_table() if timeout_s is None else _process_table(timeout_s=timeout_s)
    if table is None:
        return None
    for entry_pid, ppid, pgid, stat, birth, identity in table:
        if entry_pid == pid:
            return None if stat.startswith(("Z", "X")) else (ppid, pgid, birth, identity)
    return None


def _open_pidfd(pid: int) -> int | None:
    """Non-reusable process handle where the OS provides one (Linux pidfd)."""
    opener = getattr(os, "pidfd_open", None)
    if opener is None or not hasattr(signal, "pidfd_send_signal"):
        return None
    try:
        return opener(pid)
    except OSError:
        return None


def _fd_readable(fd: int, timeout_s: float) -> bool | None:
    """Whether ``fd`` is readable within ``timeout_s``; None when polling failed.

    ``poll`` has no FD_SETSIZE limit, unlike ``select``, which cannot watch a
    descriptor numbered 1024 or higher.
    """
    poller_factory = getattr(select, "poll", None)
    try:
        if poller_factory is None:
            readable, _, _ = select.select([fd], [], [], timeout_s)
            return bool(readable)
        poller = poller_factory()
        poller.register(fd, select.POLLIN)
        return bool(poller.poll(max(0, int(timeout_s * 1000))))
    except (OSError, ValueError):
        return None


def _pidfd_exited(handle: int) -> bool:
    """A pidfd becomes readable once its process has exited, reaped or not.

    Orphaned descendants may linger as zombies where PID 1 does not reap
    promptly (containers); they can no longer execute, so they count as gone.
    A polling failure is uncertainty, never exit: the entry stays tracked.
    """
    return _fd_readable(handle, 0) is True


def _close_handles(tracked: Mapping[int, tuple[int, str, str, int | None, str, bool]]) -> None:
    for _pgid, _stat, _birth, handle, _identity_, _verified in tracked.values():
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass


def _track_descendants(
    leader_pid: int,
    tracked: dict[int, tuple[int, str, str, int | None, str, bool]],
    table: list[tuple[int, int, int, str, str, str]] | None = None,
    deadline: float | None = None,
) -> bool:
    """Record every descendant of the leader by parent pid, whatever its pgid.

    ``tracked`` maps pid -> (last observed pgid, stat, birth, pidfd or None,
    latest identity, verified) and is kept for the life of the run so a descendant that
    later calls setsid(), exec()s, or is reparented after the leader exits is
    still attributed to the run. Only the immutable birth (start time) decides
    whether a pid was reused; a change of process group or command line
    merely updates the entry. An entry whose original process is known to be
    gone (its start time changed, or its pidfd reads as exited) is evicted
    before attribution, so a reused numeric pid never seeds descendant
    discovery for an unrelated process's children.

    ``verified`` records whether an entry was proven to belong to this run:
    its parent was the leader or a verified descendant (re-read after opening
    a pidfd, when one is used). Group and command changes (setsid, exec) do
    not affect that proof. An unverified entry is tracked and reported but
    never signalled, and it still forces an ``interrupted`` result if alive.
    Its children are tracked too, but inherit its unverified status: a
    possibly reused pid must never vouch for an unrelated process's children.

    Without a pidfd, a pid reused within the same second keeps the same start
    time, so a trusted handle-less entry loses trust if its command line
    changes or its parent becomes a process outside the run (other than
    init). A group change alone (setsid) keeps trust.

    With ``deadline``, every ``ps`` scan is bounded by the time left, and a
    pidfd that cannot be re-validated in time is kept unverified.
    """
    if table is None:
        table = _snapshot(deadline)
    if table is None:
        return False
    births = {pid: birth for pid, _ppid, _pgid, _stat, birth, _identity_ in table}
    for pid in list(tracked):
        _pgid, _stat, birth, handle, _identity_, _verified = tracked[pid]
        reused = pid in births and births[pid] != birth
        exited = handle is not None and _pidfd_exited(handle)
        if reused or exited:
            del tracked[pid]
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
    known = {leader_pid, *tracked}
    trusted = {leader_pid, *(pid for pid, entry in tracked.items() if entry[5])}
    # Entries present before this snapshot; only they may be upgraded to
    # verified by it. A pid added in this same pass was validated against a
    # fresher row than this table, so this table cannot vouch for it.
    existing = set(tracked)
    changed = True
    while changed:
        changed = False
        for pid, ppid, pgid, stat, birth, identity in table:
            if pid == leader_pid:
                continue
            if pid in tracked and pid not in existing:
                # Added earlier in this call from a fresher re-read than this
                # table; later passes must not overwrite it with stale fields.
                continue
            if pid in tracked or ppid in known:
                if pid in tracked:
                    handle = tracked[pid][3]
                    # A later snapshot showing our leader (or a verified
                    # descendant) as the parent proves the entry is ours.
                    verified = tracked[pid][5] or (pid in existing and ppid in trusted)
                    if (
                        handle is None
                        and verified
                        and pid in existing
                        and (
                            _command_of(identity) != _command_of(tracked[pid][4])
                            or (ppid not in known and ppid != 1)
                        )
                    ):
                        verified = False
                else:
                    handle = _open_pidfd(pid)
                    verified = ppid in trusted
                    if handle is not None:
                        # Opening the handle is not atomic with the snapshot:
                        # if the pid was reused in between, the handle binds
                        # to a stranger. Re-read the row: a changed start time
                        # means reuse (refuse); an unchanged start time whose
                        # parent is still in this run proves ownership even if
                        # the group or command changed meanwhile; otherwise
                        # keep the handle but never signal through it.
                        # If the re-read is out of time or fails while the
                        # handle is still live, the child may be ours: keep it
                        # tracked but unverified rather than dropping it.
                        row = None
                        if deadline is None or time.monotonic() < deadline:
                            row = _pid_row(pid) if deadline is None else _pid_row(
                                pid, timeout_s=_ps_budget(deadline)
                            )
                        if (row is None and _pidfd_exited(handle)) or (
                            row is not None and row[2] != birth
                        ):
                            try:
                                os.close(handle)
                            except OSError:
                                pass
                            continue
                        verified = row is not None and row[0] in trusted
                        if verified:
                            # The verifying re-read is newer than the snapshot:
                            # keep its group and identity, so a child that called
                            # setsid() meanwhile is recognised as outside our group.
                            pgid, identity = row[1], row[3]
                tracked[pid] = (pgid, stat, birth, handle, identity, verified)
                if pid not in known:
                    known.add(pid)
                    changed = True
                if verified and pid not in trusted:
                    trusted.add(pid)
                    changed = True
    return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _escaped_descendants(
    leader_pid: int,
    tracked: dict[int, tuple[int, str, str, int | None, str, bool]],
    *,
    include_grouped_pidfds: bool = False,
) -> dict[int, tuple[str, int | None, bool]]:
    """Tracked pids outside the leader's group that are still alive: identity, handle, verified.

    Reads ``tracked`` as the caller's latest :func:`_track_descendants` scan
    left it; it never scans itself, so a failed scan cannot go unreported.
    With ``include_grouped_pidfds`` (tracking was incomplete) a live pidfd
    counts even if its cached group is the leader's: the child may have
    called setsid() after its last successful scan, and the pidfd still
    identifies it safely.
    """
    escaped = {}
    for pid, (pgid, stat, _birth, handle, identity, verified) in sorted(tracked.items()):
        if stat.startswith(("Z", "X")):
            continue
        if pgid != leader_pid:
            if _pid_alive(pid):
                escaped[pid] = (identity, handle, verified)
        elif include_grouped_pidfds and handle is not None and not _pidfd_exited(handle):
            escaped[pid] = (identity, handle, verified)
    return escaped


PS_TIMEOUT_S = 5.0
GROUP_SCAN_TIMEOUT_S = 1.0
# A deadline-bounded scan never runs past the deadline (this floor only keeps
# the timeout positive). No periodic scan starts with less than
# SCAN_WINDOW_S left, so a loaded host cannot fail one near the deadline and
# turn a genuine cancellation into "interrupted"; the cleanup scan after the
# timeout still attributes the leader's children.
MIN_PS_TIMEOUT_S = 0.01
SCAN_WINDOW_S = 0.25
# A cleanup-time scan (after a timeout or failure) is bounded too, so a slow
# `ps` cannot let a detached helper run on; a failed scan forces "interrupted".
CLEANUP_SCAN_BUDGET_S = 1.0
SIGNALLING_PIDFD = "pidfd"
SIGNALLING_IDENTITY_CHECK = "identity_check"


def _terminate_pids(
    escaped: Mapping[int, tuple[str, int | None, bool]], *, grace_s: float
) -> tuple[bool, str]:
    """TERM, then KILL, escaped descendants; return (all gone, signalling mode).

    An unverified entry (its pidfd could not be proven to belong to this run)
    is never signalled; while it stays alive the result is "not all gone".

    With a pidfd the signal is bound to the original process, so a reused pid
    can never be hit. Without one (macOS) the identity is re-read from one
    process-table scan per round, taken immediately before that round's
    signals and bounded by the cleanup deadline so a stalled ``ps`` cannot
    postpone termination; check and signal are still two operations and the
    start time has second resolution, so this path is best effort only; the
    caller records the mode so evidence never claims identity-safe cleanup.
    A process counts as gone only when that is known: its pid is free, it is a
    zombie, or its start time changed (reuse). A failed snapshot, or the same
    start time with a different group or command (possibly our own exec), is
    uncertain: never signalled, and never reported as gone.
    """
    mode = SIGNALLING_PIDFD if all(h is not None for _b, h, _v in escaped.values()) else SIGNALLING_IDENTITY_CHECK
    cleanup_deadline = time.monotonic() + grace_s + DESCENDANT_KILL_WAIT_S + CLEANUP_SCAN_BUDGET_S
    needs_table = mode == SIGNALLING_IDENTITY_CHECK

    def round_table():
        return _snapshot(cleanup_deadline) if needs_table else None

    def status(pid: int, table) -> str:
        """``ours`` (signalable), ``gone``, or ``uncertain``."""
        identity, handle, _verified = escaped[pid]
        if handle is not None:
            if _pidfd_exited(handle):
                return "gone"
            try:
                signal.pidfd_send_signal(handle, 0)
            except ProcessLookupError:
                return "gone"
            except OSError:
                return "ours"
            return "ours"
        if not _pid_alive(pid):
            return "gone"
        if table is None:
            return "uncertain"
        for entry_pid, _ppid, _pgid, stat, birth, current in table:
            if entry_pid == pid:
                if stat.startswith(("Z", "X")):
                    return "gone"
                if current == identity:
                    return "ours"
                return "gone" if birth != identity.split("|", 1)[0] else "uncertain"
        return "gone"

    def signal_all(sig: int) -> None:
        table = round_table()
        for pid, (_identity, handle, verified) in escaped.items():
            if not verified or status(pid, table) != "ours":
                continue
            try:
                if handle is not None:
                    signal.pidfd_send_signal(handle, sig)
                else:
                    os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def wait_gone(deadline: float) -> bool:
        while True:
            table = round_table()
            if all(status(pid, table) == "gone" for pid in escaped):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    signal_all(signal.SIGTERM)
    if wait_gone(time.monotonic() + grace_s):
        return True, mode
    signal_all(signal.SIGKILL)
    return wait_gone(time.monotonic() + DESCENDANT_KILL_WAIT_S), mode


def _collect_event_names(
    stream, collected: list[str], stop: threading.Event | None = None
) -> None:
    # Drain while running so even a noisy fake cannot fill the pipe and block
    # the leader before the timeout. Keep each line and the event list bounded.
    # With ``stop`` the pipe is polled, so the caller can end the reader even
    # while an escaped descendant still holds the write end open.
    truncated = False

    def record(name: str) -> None:
        nonlocal truncated
        if len(collected) < MAX_RETAINED_EVENT_NAMES:
            collected.append(name)
        elif not truncated:
            collected.append(EVENT_NAMES_TRUNCATED)
            truncated = True

    if stop is not None:
        _collect_polled(stream.fileno(), record, stop)
        return

    while True:
        line = stream.readline(MAX_EVENT_LINE_CHARS + 1)
        if not line:
            return
        if len(line) > MAX_EVENT_LINE_CHARS:
            # Discard the rest of an oversized JSONL record in bounded chunks.
            while not line.endswith("\n"):
                line = stream.readline(MAX_EVENT_LINE_CHARS + 1)
                if not line:
                    break
            record("unstructured-output")
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError, UnicodeDecodeError):
            record("unstructured-output")
            continue
        if isinstance(item, dict):
            record(_safe_event_name(item.get("type")))
        else:
            record("non-object-event")


def _record_event_line(text: str, record) -> None:
    try:
        item = json.loads(text)
    except (ValueError, RecursionError):
        record("unstructured-output")
        return
    if isinstance(item, dict):
        record(_safe_event_name(item.get("type")))
    else:
        record("non-object-event")


def _collect_polled(fd: int, record, stop: threading.Event) -> None:
    """Bounded line reader over a raw pipe that returns promptly once ``stop`` is set."""
    pending = bytearray()
    discarding = False
    while not stop.is_set():
        readable = _fd_readable(fd, 0.05)
        if readable is None:
            return
        if not readable:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            return
        if not chunk:
            if pending and not discarding:
                line = bytes(pending)
                if len(line) > MAX_EVENT_LINE_CHARS:
                    record("unstructured-output")
                else:
                    _record_event_line(line.decode("utf-8", errors="replace"), record)
            return
        pending.extend(chunk)
        while True:
            newline = pending.find(b"\n")
            if discarding:
                # Drop the rest of an oversized record in bounded chunks.
                if newline < 0:
                    pending.clear()
                    break
                del pending[: newline + 1]
                discarding = False
                continue
            if newline < 0:
                if len(pending) > MAX_EVENT_LINE_CHARS:
                    record("unstructured-output")
                    discarding = True
                    pending.clear()
                break
            line = bytes(pending[:newline])
            del pending[: newline + 1]
            if len(line) > MAX_EVENT_LINE_CHARS:
                record("unstructured-output")
                continue
            _record_event_line(line.decode("utf-8", errors="replace"), record)


def supervise_fake_command(
    argv: Sequence[str], *, state_dir: Path, timeout_s: float, grace_s: float = 0.5,
    job_id: str | None = None,
) -> dict:
    """Run one explicit fake command under an exclusive state-directory lock."""
    if (
        not argv
        or not math.isfinite(timeout_s)
        or timeout_s <= 0
        or not math.isfinite(grace_s)
        or grace_s < 0
    ):
        raise SpikeError(
            "A command, finite positive timeout, and finite non-negative grace period are required."
        )
    if not Path(argv[0]).is_absolute():
        raise SpikeError("Fake executable must be an absolute path.")
    state_dir = state_dir.expanduser().absolute()
    _private_dir(state_dir)
    with _locked_state_directory(state_dir):
        return _supervise_fake_command_locked(
            argv, state_dir=state_dir, timeout_s=timeout_s, grace_s=grace_s, job_id=job_id
        )


def _supervise_fake_command_locked(
    argv: Sequence[str], *, state_dir: Path, timeout_s: float, grace_s: float,
    job_id: str | None,
) -> dict:
    """Run after atomically reserving the harness state directory."""
    job_id = job_id or str(uuid.uuid4())
    journal = state_dir / "journal.jsonl"
    records = _read_jsonl(journal)
    if _latest(records, job_id) is not None:
        raise SpikeError("This job ID already exists; uncertain or completed work is never replayed.")
    latest_states: dict[str, object] = {}
    for row in records:
        if isinstance(row.get("job_id"), str):
            latest_states[row["job_id"]] = row.get("state")
    if any(state in NON_TERMINAL_STATES for state in latest_states.values()):
        raise SpikeError("Journal has unrecovered non-terminal jobs; run recover first.")

    # The durable intent precedes launch. A restart from here becomes uncertain.
    _append_jsonl(journal, {"job_id": job_id, "state": "starting"})
    _append_jsonl(
        state_dir / "evidence.jsonl",
        {"kind": "launch_intent", "job_id": job_id, "state": "starting"},
    )
    # Allowlisted environment only: the fake never inherits the operator's
    # credentials or provider settings. SPIKE_* is the test-marker channel.
    child_env = {"PATH": os.defpath, "HOME": str(state_dir)}
    child_env.update({k: v for k, v in os.environ.items() if k.startswith("SPIKE_")})
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        _append_jsonl(journal, {"job_id": job_id, "state": "failed", "reason": "fake_start_error"})
        raise SpikeError(f"Fake executable could not be started ({type(exc).__name__}).") from None
    # The runtime budget starts at launch, so a slow journal append below
    # cannot extend how long the child runs.
    deadline = time.monotonic() + timeout_s

    event_names: list[str] = []
    reader_stop = threading.Event()
    reader = threading.Thread(
        target=_collect_event_names, args=(process.stdout, event_names, reader_stop), daemon=True
    )
    tracked: dict[int, tuple[int, str, str, int | None, str, bool]] = {}
    try:
        _append_jsonl(journal, {"job_id": job_id, "state": "running", "pgid": process.pid})
        reader.start()
    except BaseException:
        _abort_supervision(process, reader, tracked, grace_s=grace_s)
        reader_stop.set()
        raise
    timed_out = False
    cleanup_uncertain = False
    leftovers = False
    unexpected_leftovers = False
    escaped: dict[int, tuple[str, int | None, bool]] = {}
    escaped_terminated = True
    escaped_signalling = SIGNALLING_PIDFD
    tracking_complete = True
    try:
        # The leader is observed, never reaped, until group cleanup is done:
        # a zombie keeps its pid, so the pgid we signal cannot be reused. One
        # process-table snapshot per iteration serves both the leader check and
        # descendant attribution, keeping `ps` pressure low on loaded runners.
        while True:
            # Scans are bounded by the job deadline so a slow `ps` cannot
            # postpone cancellation.
            table = None
            if deadline - time.monotonic() >= SCAN_WINDOW_S:
                table = _snapshot(deadline)
                if table is None:
                    tracking_complete = False
                else:
                    # Best effort: descendants are attributed by parent pid
                    # while the leader is alive, whatever group they moved to.
                    _track_descendants(process.pid, tracked, table, deadline)
            if _leader_exited(process, table, deadline=deadline):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(DESCENDANT_SNAPSHOT_INTERVAL_S, remaining))
        timed_out = not _leader_exited(process, deadline=time.monotonic() + SCAN_WINDOW_S)
        # One short bounded scan while the leader is still unreaped, before any
        # group signal: a run too short for a periodic scan still attributes a
        # helper that already detached, before killing the leader reparents it.
        # A stalled scan delays termination by at most SCAN_WINDOW_S and forces
        # "interrupted".
        if not _track_descendants(
            process.pid, tracked, deadline=time.monotonic() + SCAN_WINDOW_S
        ):
            tracking_complete = False
        # A provider parent may exit while a child keeps the pipe or continues
        # work. Clean the group after timeout and after every leader exit, and
        # only report success when enumeration and the output reader are quiet.
        # Terminate before journaling the cancellation: an fsync on stalled
        # storage must not keep a timed-out provider running. If the append
        # then fails, the journal still holds the non-terminal "running" row,
        # so recovery reports the job as interrupted.
        if timed_out:
            cleanup_uncertain = _terminate_group(process, reader, grace_s=grace_s)
            _append_jsonl(journal, {"job_id": job_id, "state": "cancel_requested"})
        else:
            reader.join(timeout=0.05)
            leftovers = _group_running(process.pid, reader)
            if leftovers:
                cleanup_uncertain = _terminate_group(process, reader, grace_s=grace_s)
                _append_jsonl(journal, {"job_id": job_id, "state": "cancel_requested"})
        unexpected_leftovers = bool(not timed_out and leftovers)
        # Re-snapshot once more, then hunt tracked descendants that left the
        # group (setsid). Anything found forces "interrupted" below.
        if not _track_descendants(
            process.pid, tracked, deadline=time.monotonic() + CLEANUP_SCAN_BUDGET_S
        ):
            tracking_complete = False
        escaped = _escaped_descendants(
            process.pid, tracked, include_grouped_pidfds=not tracking_complete
        )
        if escaped:
            escaped_terminated, escaped_signalling = _terminate_pids(escaped, grace_s=grace_s)
        # Only now is the leader reaped: discovery and cleanup are complete.
        _reap_leader(process, timeout_s=5.0)
    except BaseException:
        _abort_supervision(process, reader, tracked, grace_s=grace_s)
        reader_stop.set()
        raise
    _close_handles(tracked)

    reader.join(timeout=1)
    reader_stuck = reader.is_alive()
    if reader_stuck:
        # Something (an escaped descendant) still holds the pipe open: stop
        # the reader so it cannot outlive this run or change recorded events.
        reader_stop.set()
        reader.join(timeout=1)
    if not reader.is_alive() and process.stdout is not None:
        process.stdout.close()
    event_names = list(event_names)
    # A failed snapshot may have missed a helper that detached meanwhile, so
    # success or cancellation cannot be claimed without complete tracking.
    if (
        reader_stuck or cleanup_uncertain or unexpected_leftovers or escaped
        or not tracking_complete
    ):
        final_state = "interrupted"
    elif timed_out:
        final_state = "cancelled"
    else:
        final_state = "succeeded" if process.returncode == 0 else "failed"
    _append_jsonl(journal, {"job_id": job_id, "state": final_state})
    _append_jsonl(
        state_dir / "evidence.jsonl",
        {
            "kind": "supervision_result", "job_id": job_id, "state": final_state,
            "returncode": process.returncode, "event_names": event_names,
            "escaped_descendants": sorted(escaped),
            "escaped_descendants_terminated": escaped_terminated,
            # Tracked but never signalled: their pidfd could not be proven to
            # belong to this run (parent changed between snapshot and open).
            "escaped_descendants_unverified": sorted(
                pid for pid, (_i, _h, verified) in escaped.items() if not verified
            ),
            # Only a pidfd binds the signal to the original process; the
            # identity-check path cannot rule out a pid reused between check
            # and signal, so cleanup is recorded as uncertain there.
            "escaped_descendant_signalling": escaped_signalling,
            # False when any process-table snapshot failed during the run, so
            # an escaped descendant could have been missed.
            "descendant_tracking_complete": tracking_complete,
            "escaped_cleanup_certain": bool(escaped)
            and escaped_terminated
            and escaped_signalling == SIGNALLING_PIDFD,
        },
    )
    return {"job_id": job_id, "state": final_state, "returncode": process.returncode,
            "event_names": event_names}


def inspect_codex(codex_bin: str | None, evidence_dir: Path) -> dict:
    """Read version and help only, with both HOME and CODEX_HOME disposable."""
    executable = codex_bin or shutil.which("codex")
    if not executable:
        raise SpikeError("Codex CLI not found. Pass --codex-bin with its executable path.")
    binary = Path(executable).expanduser().absolute()
    if not binary.is_file():
        raise SpikeError("Codex executable does not exist.")
    _private_dir(evidence_dir)
    try:
        with tempfile.TemporaryDirectory(prefix="openswap-codex-spike-") as temp:
            home = Path(temp)
            codex_home = home / "codex"
            codex_home.mkdir(mode=0o700)
            env = {"PATH": os.defpath, "HOME": str(home), "CODEX_HOME": str(codex_home)}
            version = _run_probe([str(binary), "--version"], env=env, cwd=temp,
                                 timeout_s=10)
            help_result = _run_probe([str(binary), "exec", "--help"], env=env, cwd=temp,
                                     timeout_s=15)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        raise SpikeError("Codex version/help probe could not complete.") from None
    version_text = version.stdout.strip()
    help_text = help_result.stdout + help_result.stderr
    if version.returncode != 0 or help_result.returncode != 0 or not version_text:
        raise SpikeError("Codex version/help probe failed; details were not retained.")
    # Only a bare "codex-cli <token>" line is ever written to evidence.
    if not _VERSION_OUTPUT.fullmatch(version_text):
        raise SpikeError("Unrecognised version output")
    observed = {
        "version": version_text,
        "expected_version": DISCOVERED_CODEX_VERSION,
        "matches_discovered_pin": version_text == DISCOVERED_CODEX_VERSION,
        "pre_release": "alpha" in version_text.casefold() or "beta" in version_text.casefold(),
        "exec_json": "--json" in help_text,
        "skip_git_repo_check": "--skip-git-repo-check" in help_text,
        "ignore_user_config": "--ignore-user-config" in help_text,
        "sandbox_option": "--sandbox" in help_text,
        "no_auth_performed": True,
    }
    _append_jsonl(evidence_dir / "evidence.jsonl", {"kind": "codex_help_probe", **observed})
    return observed


def _sandbox_operation_status(output: str, operation: str) -> int | None:
    lines = output.splitlines()
    if lines.count(f"probe-start:{operation}") != 1:
        return None
    if lines.count(f"probe-complete:{operation}") != 1:
        return None
    status_lines = [line for line in lines if line.startswith(f"probe-status:{operation}:")]
    if len(status_lines) != 1:
        return None
    try:
        return int(status_lines[0].rsplit(":", 1)[1])
    except ValueError:
        return None


def probe_low_level_sandbox(codex_bin: str | None, evidence_dir: Path) -> dict:
    """Reproduce the synthetic path-boundary test without a model or login."""
    executable = codex_bin or shutil.which("codex")
    if not executable or not Path(executable).expanduser().is_file():
        raise SpikeError("Codex CLI not found. Pass --codex-bin with its executable path.")
    binary = str(Path(executable).expanduser().absolute())
    _private_dir(evidence_dir)
    with tempfile.TemporaryDirectory(prefix="openswap-codex-sandbox-probe-") as temp:
        root = Path(temp)
        codex_home = root / "codex-home"
        workspace = root / "workspace"
        outside = root / "outside"
        for path in (codex_home, workspace, outside):
            path.mkdir(mode=0o700)
        (workspace / "inside.txt").write_text("workspace-sentinel", encoding="utf-8")
        (outside / "outside.txt").write_text("outside-sentinel", encoding="utf-8")
        (codex_home / "auth.json").write_text("synthetic-auth-sentinel", encoding="utf-8")
        (codex_home / "config.toml").write_text(
            'default_permissions = "research-test"\n\n'
            "[permissions.research-test]\n"
            'extends = ":workspace"\n\n'
            "[permissions.research-test.filesystem]\n"
            '":root" = "deny"\n'
            '":minimal" = "read"\n'
            '":tmpdir" = "deny"\n'
            '":slash_tmp" = "deny"\n\n'
            '[permissions.research-test.filesystem.":workspace_roots"]\n'
            '"." = "write"\n',
            encoding="utf-8",
        )
        with (codex_home / "config.toml").open("a", encoding="utf-8") as stream:
            stream.write("# synthetic-config-sentinel\n")
        env = {"PATH": os.defpath, "HOME": str(root), "CODEX_HOME": str(codex_home)}
        def operation(name: str, command: str) -> str:
            error_file = shlex.quote(str(workspace / f"{name}.stderr"))
            return (
                f"printf 'probe-start:{name}\\n'; "
                f"{command} 2>{error_file}; status=$?; "
                f"printf '\\nprobe-complete:{name}\\n'; "
                f"printf 'probe-status:{name}:%s\\n' \"$status\"; "
                f"if grep -q 'Operation not permitted' {error_file}; "
                f"then printf 'probe-denied:{name}\\n'; fi"
            )

        command = "; ".join((
            operation("inside_read", f"cat {shlex.quote(str(workspace / 'inside.txt'))}"),
            operation("outside_read", f"cat {shlex.quote(str(outside / 'outside.txt'))}"),
            operation("inside_write", f"/bin/sh -c {shlex.quote('printf inside-write > ' + shlex.quote(str(workspace / 'write-test.txt')))}"),
            operation("outside_write", f"/bin/sh -c {shlex.quote('printf outside-write > ' + shlex.quote(str(outside / 'write-test.txt')))}"),
        ))
        auth_command = "; ".join((
            operation("auth_read", 'cat "$CODEX_HOME/auth.json"'),
            operation("config_read", 'cat "$CODEX_HOME/config.toml"'),
        ))
        try:
            result = _run_probe(
                [binary, "sandbox", "--permission-profile", "research-test", "--log-denials",
                 "--cd", str(workspace), "/bin/sh", "-c", command],
                env=env, cwd=temp, timeout_s=20,
            )
            auth_read = _run_probe(
                [binary, "sandbox", "--permission-profile", "research-test", "--log-denials",
                 "--cd", str(workspace), "/bin/sh", "-c", auth_command],
                env=env, cwd=temp, timeout_s=20,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
            raise SpikeError("Codex sandbox probe could not complete.") from None
        combined = result.stdout + result.stderr
        auth_combined = auth_read.stdout + auth_read.stderr
        first_ops = {
            name: _sandbox_operation_status(combined, name)
            for name in ("inside_read", "outside_read", "inside_write", "outside_write")
        }
        auth_ops = {
            name: _sandbox_operation_status(auth_combined, name)
            for name in ("auth_read", "config_read")
        }
        first_denials = set(line.removeprefix("probe-denied:") for line in combined.splitlines()
                            if line.startswith("probe-denied:"))
        auth_denials = set(line.removeprefix("probe-denied:") for line in auth_combined.splitlines()
                           if line.startswith("probe-denied:"))
        observed = {
            "kind": "low_level_sandbox_probe",
            "exec_or_model_run": False,
            "inside_read_allowed": (first_ops["inside_read"] == 0
                                    and "workspace-sentinel" in combined),
            "outside_read_denied": (first_ops["outside_read"] not in (None, 0)
                                    and "outside-sentinel" not in combined
                                    and "outside_read" in first_denials),
            "inside_write_allowed": (first_ops["inside_write"] == 0
                                     and (workspace / "write-test.txt").is_file()
                                     and (workspace / "write-test.txt").read_text(encoding="utf-8")
                                     == "inside-write"),
            "outside_write_denied": (first_ops["outside_write"] not in (None, 0)
                                     and not (outside / "write-test.txt").exists()
                                     and "outside_write" in first_denials),
            "codex_home_auth_and_config_denied": (
                all(status not in (None, 0) for status in auth_ops.values())
                and {"auth_read", "config_read"}.issubset(auth_denials)
                and "synthetic-auth-sentinel" not in auth_combined
                and "synthetic-config-sentinel" not in auth_combined
            ),
        }
        required_statuses = all(status is not None for status in (*first_ops.values(), *auth_ops.values()))
        if not required_statuses:
            raise SpikeError(
                "Sandbox commands did not all run to completion; details were not retained."
            )
        expected = (
            observed["inside_read_allowed"], observed["outside_read_denied"],
            observed["inside_write_allowed"], observed["outside_write_denied"],
            observed["codex_home_auth_and_config_denied"],
        )
        if not all(expected):
            failed = [
                name for name, ok in zip(
                    ("inside_read_allowed", "outside_read_denied", "inside_write_allowed",
                     "outside_write_denied", "codex_home_auth_and_config_denied"), expected
                ) if not ok
            ]
            raise SpikeError(
                "Sandbox probe did not confirm: " + ", ".join(failed) + ". Raw output was not retained."
            )
    _append_jsonl(evidence_dir / "evidence.jsonl", observed)
    return observed


def run_live_codex(*_args, **_kwargs):
    """Refuse authenticated runs until the account/isolation gate is cleared."""
    if not LIVE_CODEX_ENABLED:
        raise SpikeError(
            "Live Codex runs are disabled in this spike: the owner has not selected an account, "
            "and account ownership, restrictions, refresh behavior, and exclusive use are unproven."
        )
    raise SpikeError("No live Codex runner is implemented by this feasibility harness.")


def _demo(state_dir: Path, timeout_s: float) -> dict:
    """Exercise cancellation with this script's inert child process."""
    return supervise_fake_command(
        [sys.executable, str(Path(__file__).absolute()), "_fake-child"],
        state_dir=state_dir,
        timeout_s=timeout_s,
        job_id=str(uuid.uuid4()),
    )


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="read version/help in a disposable Codex home")
    inspect.add_argument("--codex-bin")
    inspect.add_argument("--evidence-dir", type=Path, required=True)
    sandbox = sub.add_parser("sandbox-probe", help="test synthetic filesystem rules without a model/login")
    sandbox.add_argument("--codex-bin")
    sandbox.add_argument("--evidence-dir", type=Path, required=True)
    recover = sub.add_parser("recover", help="mark uncertain journal entries interrupted")
    recover.add_argument("--state-dir", type=Path, required=True)
    demo = sub.add_parser("demo", help="run an inert child and exercise process-group cancellation")
    demo.add_argument("--state-dir", type=Path, required=True)
    demo.add_argument("--timeout", type=float, default=0.5)
    live = sub.add_parser("live", help="authenticated run (intentionally disabled)")
    live.add_argument("--task", required=True)
    live.add_argument("--codex-home", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_codex(args.codex_bin, args.evidence_dir.expanduser().absolute())
        elif args.command == "sandbox-probe":
            result = probe_low_level_sandbox(args.codex_bin, args.evidence_dir.expanduser().absolute())
        elif args.command == "recover":
            result = {"interrupted": recover_uncertain_runs(args.state_dir.expanduser().absolute())}
        elif args.command == "demo":
            result = _demo(args.state_dir.expanduser().absolute(), args.timeout)
        else:
            run_live_codex(task=args.task, codex_home=args.codex_home)
            result = {}
    except SpikeError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError):
        # Never echo an OS error: it can carry paths or other operator details.
        print("refused: harness failure (details withheld)", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["_fake-child"]:
        print(json.dumps({"type": "thread.started"}), flush=True)
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            pass
        raise SystemExit(0)
    raise SystemExit(_main())
