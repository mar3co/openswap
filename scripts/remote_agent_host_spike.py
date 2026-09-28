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
_VERSION_OUTPUT = re.compile(r"codex-cli \S{1,40}")


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
    try:
        selector = selectors.DefaultSelector()
        for stream in (process.stdout, process.stderr):
            fd = stream.fileno()
            os.set_blocking(fd, False)
            selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_s
        while process.poll() is None or selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            events = selector.select(min(remaining, 0.05)) if selector.get_map() else ()
            if not selector.get_map() and process.poll() is None:
                time.sleep(min(remaining, 0.02))
            if (
                not events
                and process.poll() is not None
                and not _probe_group_running(process.pid)
            ):
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
            process.wait()
            leftovers = _probe_group_running(process.pid)
            if leftovers:
                cleanup_uncertain = _cleanup_probe_process(process)
    except BaseException:
        _cleanup_probe_process(process)
        raise
    finally:
        if selector is not None:
            selector.close()
        process.stdout.close()
        process.stderr.close()
    if overflowed:
        raise SpikeError("Probe output exceeded the bounded capture limit; result was refused.")
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout_s) from None
    if cleanup_uncertain or leftovers:
        raise SpikeError("Probe cleanup was uncertain; result was refused.")
    try:
        decoded_stdout = bytes(buffers[stdout_fd]).decode("utf-8")
        decoded_stderr = bytes(buffers[stderr_fd]).decode("utf-8")
    except UnicodeDecodeError:
        raise
    return subprocess.CompletedProcess(command, process.returncode, decoded_stdout, decoded_stderr)


def _probe_group_running(pgid: int) -> bool:
    observed = _group_has_running_members(pgid)
    if observed is not None:
        return observed
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cleanup_probe_process(process: subprocess.Popen) -> bool:
    """Bounded cleanup for this probe's session without reading its pipes."""
    if os.name == "posix":
        _signal_group(process, signal.SIGTERM)
        deadline = time.monotonic() + PROBE_TERMINATION_GRACE_S
        while _probe_group_running(process.pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        if _probe_group_running(process.pid):
            _signal_group(process, signal.SIGKILL)
    elif process.poll() is None:
        process.terminate()

    try:
        process.wait(timeout=PROBE_CLEANUP_WAIT_S)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            _signal_group(process, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=PROBE_CLEANUP_WAIT_S)
        except subprocess.TimeoutExpired:
            return True
    return process.poll() is None or (os.name == "posix" and _probe_group_running(process.pid))


def _private_dir(path: Path) -> None:
    # Create missing ancestors one by one so every level we own is 0o700,
    # rather than trusting mkdir(parents=True) to apply the mode above the leaf.
    missing = []
    current = path
    while not current.exists() and current.parent != current:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            # A new directory entry is only durable once its parent is synced.
            _fsync_dir(directory.parent)
    if not path.is_dir():
        raise SpikeError("Private output path must be a directory.")
    if os.name == "posix":
        status = path.stat()
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


def _group_alive(pgid: int) -> bool:
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


def _group_has_running_members(pgid: int) -> bool | None:
    """Return whether the group has non-zombie processes; None if unavailable."""
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
                timeout=1,
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


def _group_running(pgid: int, reader: threading.Thread) -> bool:
    observed = _group_has_running_members(pgid)
    if observed is not None:
        return observed
    # Without enumeration, EOF alone cannot prove that all descendants exited
    # because a child may have closed stdout. Treat any remaining group as
    # uncertain; killpg(0) may include zombies, which is safer than success.
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
    while _group_running(process.pid, reader) and time.monotonic() < grace_deadline:
        time.sleep(0.03)
    if _group_running(process.pid, reader):
        _signal_group(process, signal.SIGKILL)
    wait_uncertain = False
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            wait_uncertain = True
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
    return wait_uncertain or process.poll() is None or _group_running(process.pid, reader)


def _process_table() -> list[tuple[int, int, int, str, str]] | None:
    """Best-effort ``(pid, ppid, pgid, stat, birth)`` snapshot; None when unavailable.

    ``birth`` is an identity string built from the start time (``lstart``,
    second resolution), the process group and the command line, so a pid
    reused by an unrelated process is very unlikely to match a tracked one.
    Where the OS offers a non-reusable handle (``pidfd`` on Linux) the harness
    prefers it; see :func:`_open_pidfd`.
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
            timeout=1,
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
        # command line. Fold pgid in so the identity survives reparenting.
        birth = _identity(" ".join(columns[4:9]), pgid, " ".join(columns[9:]))
        table.append((pid, ppid, pgid, stat, birth))
    return table


def _identity(lstart: str, pgid: int, command: str) -> str:
    return f"{lstart}|pgid={pgid}|{command}"


def _pid_identity(pid: int) -> str | None:
    """Current identity of ``pid`` (see :func:`_process_table`), or None when gone."""
    ps = shutil.which("ps") or "/bin/ps"
    try:
        listing = subprocess.run(
            [ps, "-o", "stat=,lstart=,pgid=,command=", "-p", str(pid)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", timeout=1, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    columns = listing.stdout.split()
    if len(columns) < 7:
        return None
    # A zombie has exited and cannot execute; report it as gone.
    if columns[0].startswith(("Z", "X")):
        return None
    try:
        pgid = int(columns[6])
    except ValueError:
        return None
    return _identity(" ".join(columns[1:6]), pgid, " ".join(columns[7:]))


def _open_pidfd(pid: int) -> int | None:
    """Non-reusable process handle where the OS provides one (Linux pidfd)."""
    opener = getattr(os, "pidfd_open", None)
    if opener is None or not hasattr(signal, "pidfd_send_signal"):
        return None
    try:
        return opener(pid)
    except OSError:
        return None


def _pidfd_exited(handle: int) -> bool:
    """A pidfd becomes readable once its process has exited, reaped or not.

    Orphaned descendants may linger as zombies where PID 1 does not reap
    promptly (containers); they can no longer execute, so they count as gone.
    """
    try:
        readable, _, _ = select.select([handle], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)


def _close_handles(tracked: Mapping[int, tuple[int, str, str, int | None]]) -> None:
    for _pgid, _stat, _birth, handle in tracked.values():
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass


def _track_descendants(leader_pid: int, tracked: dict[int, tuple[int, str, str, int | None]]) -> None:
    """Record every descendant of the leader by parent pid, whatever its pgid.

    ``tracked`` maps pid -> (last observed pgid, stat, identity, pidfd or
    None) and is
    kept for the life of the run so a descendant that later calls setsid()
    or is reparented after the leader exits is still attributed to the run.
    A pid that reappears with a different birth identity belonged to a
    process that already exited and was reused; it is dropped and only
    re-attributed if its new parent is part of this run.
    """
    table = _process_table()
    if table is None:
        return
    for pid, _ppid, _pgid, _stat, birth in table:
        if pid in tracked and tracked[pid][2] != birth:
            handle = tracked.pop(pid)[3]
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
    known = {leader_pid, *tracked}
    changed = True
    while changed:
        changed = False
        for pid, ppid, pgid, stat, birth in table:
            if pid == leader_pid:
                continue
            if pid in tracked or ppid in known:
                handle = tracked[pid][3] if pid in tracked else _open_pidfd(pid)
                tracked[pid] = (pgid, stat, birth, handle)
                if pid not in known:
                    known.add(pid)
                    changed = True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _escaped_descendants(
    leader_pid: int, tracked: dict[int, tuple[int, str, str, int | None]]
) -> dict[int, tuple[str, int | None]]:
    """Tracked pids outside the leader's group that are still alive, with identity and handle."""
    _track_descendants(leader_pid, tracked)
    return {
        pid: (birth, handle) for pid, (pgid, stat, birth, handle) in sorted(tracked.items())
        if pgid != leader_pid and not stat.startswith(("Z", "X")) and _pid_alive(pid)
    }


SIGNALLING_PIDFD = "pidfd"
SIGNALLING_IDENTITY_CHECK = "identity_check"


def _terminate_pids(
    escaped: Mapping[int, tuple[str, int | None]], *, grace_s: float
) -> tuple[bool, str]:
    """TERM, then KILL, escaped descendants; return (all gone, signalling mode).

    With a pidfd the signal is bound to the original process, so a reused pid
    can never be hit. Without one (macOS) the identity is re-read immediately
    before each signal, but check and signal are still two operations and the
    start time has second resolution, so this path is best effort only; the
    caller records the mode so evidence never claims identity-safe cleanup.
    """
    mode = SIGNALLING_PIDFD if all(h is not None for _b, h in escaped.values()) else SIGNALLING_IDENTITY_CHECK

    def still_ours(pid: int) -> bool:
        birth, handle = escaped[pid]
        if handle is not None:
            if _pidfd_exited(handle):
                return False
            try:
                signal.pidfd_send_signal(handle, 0)
            except ProcessLookupError:
                return False
            except OSError:
                return True
            return True
        return _pid_alive(pid) and _pid_identity(pid) == birth

    def signal_all(sig: int) -> None:
        for pid, (_birth, handle) in escaped.items():
            if not still_ours(pid):
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
            if not any(still_ours(pid) for pid in escaped):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    signal_all(signal.SIGTERM)
    if wait_gone(time.monotonic() + grace_s):
        return True, mode
    signal_all(signal.SIGKILL)
    return wait_gone(time.monotonic() + DESCENDANT_KILL_WAIT_S), mode


def _collect_event_names(stream, collected: list[str]) -> None:
    # Drain while running so even a noisy fake cannot fill the pipe and block
    # the leader before the timeout. Keep each line and the event list bounded.
    truncated = False

    def record(name: str) -> None:
        nonlocal truncated
        if len(collected) < MAX_RETAINED_EVENT_NAMES:
            collected.append(name)
        elif not truncated:
            collected.append(EVENT_NAMES_TRUNCATED)
            truncated = True

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
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        _append_jsonl(journal, {"job_id": job_id, "state": "failed", "reason": "fake_start_error"})
        raise SpikeError(f"Fake executable could not be started ({type(exc).__name__}).") from None

    event_names: list[str] = []
    reader = threading.Thread(
        target=_collect_event_names, args=(process.stdout, event_names), daemon=True
    )
    try:
        _append_jsonl(journal, {"job_id": job_id, "state": "running", "pgid": process.pid})
        reader.start()
    except BaseException:
        _terminate_group(process, reader, grace_s=grace_s)
        raise
    deadline = time.monotonic() + timeout_s
    timed_out = False
    cleanup_uncertain = False
    leftovers = False
    unexpected_leftovers = False
    tracked: dict[int, tuple[int, str, str, int | None]] = {}
    escaped: dict[int, tuple[str, int | None]] = {}
    escaped_terminated = True
    escaped_signalling = SIGNALLING_PIDFD
    try:
        next_snapshot = 0.0
        while process.poll() is None and time.monotonic() < deadline:
            if time.monotonic() >= next_snapshot:
                # Best effort: descendants are attributed by parent pid while
                # the leader is alive, whatever group they moved to.
                _track_descendants(process.pid, tracked)
                next_snapshot = time.monotonic() + DESCENDANT_SNAPSHOT_INTERVAL_S
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
        timed_out = process.poll() is None
        if timed_out:
            _append_jsonl(journal, {"job_id": job_id, "state": "cancel_requested"})
        # A provider parent may exit while a child keeps the pipe or continues
        # work. Clean the group after timeout and after every leader exit, and
        # only report success when enumeration and the output reader are quiet.
        if timed_out:
            cleanup_uncertain = _terminate_group(process, reader, grace_s=grace_s)
        else:
            process.wait()
            reader.join(timeout=0.05)
            leftovers = _group_running(process.pid, reader)
            if leftovers:
                _append_jsonl(journal, {"job_id": job_id, "state": "cancel_requested"})
                cleanup_uncertain = _terminate_group(process, reader, grace_s=grace_s)
        unexpected_leftovers = bool(not timed_out and leftovers)
        # Re-snapshot once more, then hunt tracked descendants that left the
        # group (setsid). Anything found forces "interrupted" below.
        escaped = _escaped_descendants(process.pid, tracked)
        if escaped:
            escaped_terminated, escaped_signalling = _terminate_pids(escaped, grace_s=grace_s)
    except BaseException:
        _terminate_group(process, reader, grace_s=grace_s)
        # A failure after a descendant detached (for example a full disk on the
        # cancel_requested append) must not leave that descendant running.
        try:
            leftover = _escaped_descendants(process.pid, tracked)
            if leftover:
                _terminate_pids(leftover, grace_s=grace_s)
        except Exception:
            pass
        finally:
            _close_handles(tracked)
        raise
    _close_handles(tracked)

    reader.join(timeout=1)
    if reader.is_alive() or cleanup_uncertain or unexpected_leftovers or escaped:
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
            # Only a pidfd binds the signal to the original process; the
            # identity-check path cannot rule out a pid reused between check
            # and signal, so cleanup is recorded as uncertain there.
            "escaped_descendant_signalling": escaped_signalling,
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
