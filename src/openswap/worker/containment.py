"""Per-job launchd containment with a resource-coalition stop proof (macOS).

Plan 017 needs Stop to end a provider's whole process tree, including
descendants that call ``setsid()`` and close their pipes. A process group
cannot do that (cancellation-boundary.md), and neither can ``launchctl
bootout`` alone: measured on macOS 27 (Darwin 27.0, 2026-10-07), ``bootout``
of a per-job label killed the job's leader and its same-group child but left a
``setsid()``-daemonised grandchild running.

What does hold is the job's **resource coalition**. launchd starts every job it
bootstraps in a new resource coalition, membership is inherited across
``fork``/``exec`` and cannot be changed by ``setsid()``, reparenting or closing
file descriptors (adoption into another coalition needs the privileged spawn
attribute launchd itself uses). In the same measurement every escaped
descendant was still listed in the job's coalition, and a sweep that first
``SIGSTOP``s every member until a full scan finds no running member, then
``SIGKILL``s them, emptied a coalition of 60 continuously forking ``setsid()``
processes in 0.07 s. A stopped process cannot fork, so once a scan sees only
stopped members no new member can appear: that closes the fork/reparent race
the process-group tracker left open.

The stop proof is therefore: the job's label is no longer loaded **and** a scan
of every process finds no live member of the job's coalition. Anything short of
that is reported as unproven, never as stopped.

Residual limit (documented, not hidden): work a member asks *launchd* (or
another system service) to start on its behalf runs in that service's
coalition, not this one. The research sandbox is expected to deny it; the live
check probes ``launchctl submit`` from inside the sandbox to measure that.

Nothing here reads credentials or provider output. Tests inject a fake process
table and ``launchctl`` runner; the real backend is macOS-only.
"""

from __future__ import annotations

import ctypes
import json
import os
import plistlib
import re
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

LABEL_PREFIX = "com.opensoft.openswap.worker.job."
_LABEL_RE = re.compile(r"^com\.opensoft\.openswap\.worker\.job\.[a-z0-9-]{1,64}$")
HANDLE_FILE = "handle.json"
LEADER_PID_FILE = "leader.pid"
GO_FILE = "go"
EXIT_FILE = "exit"
STDOUT_FILE = "stdout.jsonl"
STDERR_FILE = "stderr.log"
STDIN_FILE = "stdin.txt"
LAUNCHCTL_TIMEOUT_SECONDS = 20.0
# launchd sends SIGKILL this long after the SIGTERM of a bootout.
EXIT_TIMEOUT_SECONDS = 5

# The job's main program. It records its own pid, then waits (bounded) for the
# worker to record the coalition before it runs the provider, so a provider
# never runs unless the worker can sweep it. Its exit file is written only by
# a normal return of the provider.
WRAPPER_SCRIPT = (
    'd="$1"; shift\n'
    'echo $$ > "$d/leader.pid.tmp" && mv "$d/leader.pid.tmp" "$d/leader.pid" || exit 124\n'
    'i=0\n'
    'while [ ! -e "$d/go" ]; do\n'
    '  i=$((i+1)); [ "$i" -gt 600 ] && exit 125\n'
    '  sleep 0.05\n'
    'done\n'
    '"$@" < "$d/stdin.txt" > "$d/stdout.jsonl" 2> "$d/stderr.log"\n'
    'rc=$?\n'
    'echo "$rc" > "$d/exit.tmp" && mv "$d/exit.tmp" "$d/exit"\n'
)

# Files only a launch creates; any of them means the directory is not fresh.
_WRAPPER_STATE = (GO_FILE, LEADER_PID_FILE, EXIT_FILE, HANDLE_FILE, STDOUT_FILE, "job.plist")

_SZOMB = 5
_SSTOP = 4


class ContainmentError(RuntimeError):
    """A launch could not be contained. ``launched`` is False when the
    provider program provably never ran (it is gated on the ``go`` file)."""

    def __init__(self, code: str, *, launched: bool = False):
        super().__init__(code)
        self.code = code
        self.launched = launched


class ProcessTable(Protocol):
    def pids(self) -> list[int]: ...

    def coalition_of(self, pid: int) -> int | None: ...

    def status_of(self, pid: int) -> int | None: ...

    def signal(self, pid: int, signum: int) -> None: ...

    def boot_session(self) -> str | None: ...


class DarwinProcessTable:
    """libproc-backed process table; same-user queries need no privilege."""

    PROC_PIDTBSDINFO = 3
    PROC_PIDCOALITIONINFO = 20
    _BSDINFO_SIZE = 136

    def __init__(self):
        if sys.platform != "darwin":
            raise ContainmentError("containment_unsupported_platform")
        try:
            self._libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            self._libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        except OSError:
            raise ContainmentError("containment_unavailable") from None

    def pids(self) -> list[int]:
        count = self._libproc.proc_listallpids(None, 0)
        if count <= 0:
            raise ContainmentError("process_table_unavailable")
        buf = (ctypes.c_int * (count + 1024))()
        count = self._libproc.proc_listallpids(buf, ctypes.sizeof(buf))
        if count <= 0:
            raise ContainmentError("process_table_unavailable")
        return [buf[i] for i in range(count) if buf[i] > 0]

    def coalition_of(self, pid: int) -> int | None:
        buf = ctypes.create_string_buffer(64)
        size = self._libproc.proc_pidinfo(pid, self.PROC_PIDCOALITIONINFO, ctypes.c_uint64(0), buf, 64)
        if size < 8:
            return None
        return struct.unpack_from("<Q", buf.raw, 0)[0]

    def status_of(self, pid: int) -> int | None:
        buf = ctypes.create_string_buffer(self._BSDINFO_SIZE)
        size = self._libproc.proc_pidinfo(pid, self.PROC_PIDTBSDINFO, ctypes.c_uint64(0), buf, self._BSDINFO_SIZE)
        if size < self._BSDINFO_SIZE:
            return None
        return struct.unpack_from("<I", buf.raw, 4)[0]

    def signal(self, pid: int, signum: int) -> None:
        os.kill(pid, signum)

    def boot_session(self) -> str | None:
        size = ctypes.c_size_t(128)
        buf = ctypes.create_string_buffer(128)
        if self._libc.sysctlbyname(b"kern.bootsessionuuid", buf, ctypes.byref(size), None, 0) != 0:
            return None
        value = buf.value.decode("ascii", "replace").strip()
        return value or None


@dataclass(frozen=True)
class JobHandle:
    """Everything needed to find and stop a job again, even from a new worker."""

    label: str
    domain: str
    run_dir: Path
    boot_session: str | None
    leader_pid: int | None = None
    coalition_id: int | None = None
    released: bool = False  # the ``go`` file was created: the provider may run

    def to_dict(self) -> dict:
        return {
            "label": self.label, "domain": self.domain, "boot_session": self.boot_session,
            "leader_pid": self.leader_pid, "coalition_id": self.coalition_id,
            "released": self.released,
        }


@dataclass(frozen=True)
class StopProof:
    stopped: bool
    label_loaded: bool | None
    survivors: int
    killed: int = 0
    rebooted: bool = False
    never_released: bool = False

    def to_dict(self) -> dict:
        return {
            "stopped": self.stopped, "label_loaded": self.label_loaded,
            "survivors": self.survivors, "killed": self.killed,
            "rebooted": self.rebooted, "never_released": self.never_released,
        }


def job_label(job_id: str) -> str:
    label = f"{LABEL_PREFIX}{job_id}"
    if not _LABEL_RE.fullmatch(label):
        raise ContainmentError("invalid_job_label")
    return label


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (0700) or verify an existing one is a private real dir."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ContainmentError("run_dir_unsafe")
    if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ContainmentError("run_dir_unsafe")


def write_private(path: Path, data: bytes) -> None:
    """Atomically write ``data`` with mode 0600, never following a symlink."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_private_text(path: Path, limit: int = 64 * 1024) -> str | None:
    """Read a small regular file without following a symlink; None if absent."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        return os.read(fd, limit).decode("utf-8", "replace")
    finally:
        os.close(fd)


def load_handle(run_dir: Path) -> JobHandle | None:
    text = read_private_text(Path(run_dir) / HANDLE_FILE)
    if text is None:
        return None
    try:
        raw = json.loads(text)
        label = raw["label"]
        domain = raw["domain"]
        if not isinstance(label, str) or not _LABEL_RE.fullmatch(label):
            return None
        if not isinstance(domain, str) or not re.fullmatch(r"^(gui|user)/\d+$", domain):
            return None
        boot = raw.get("boot_session")
        pid = raw.get("leader_pid")
        cid = raw.get("coalition_id")
        released = raw.get("released", False)
        if boot is not None and not isinstance(boot, str):
            return None
        if pid is not None and (type(pid) is not int or pid <= 1):
            return None
        if cid is not None and (type(cid) is not int or cid <= 0):
            return None
        if type(released) is not bool:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return JobHandle(label, domain, Path(run_dir), boot, pid, cid, released)


def _save_handle(handle: JobHandle) -> None:
    write_private(handle.run_dir / HANDLE_FILE, json.dumps(handle.to_dict()).encode())


def _default_launchctl(args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["/bin/launchctl", *args], capture_output=True, text=True, check=False,
            timeout=LAUNCHCTL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", "timeout")
    except OSError:
        return subprocess.CompletedProcess(args, 127, "", "unavailable")


def _printed_pid(stdout: str) -> int | None:
    for line in stdout.splitlines():
        if line.startswith("\t") and not line.startswith("\t\t"):
            stripped = line.strip()
            if stripped.startswith("pid = "):
                raw = stripped.removeprefix("pid = ").strip()
                return int(raw) if raw.isdigit() else None
    return None


class LaunchdContainment:
    """Run each job as its own launchd job and prove its coalition is empty."""

    def __init__(
        self,
        *,
        uid: int | None = None,
        procs: ProcessTable | None = None,
        launchctl: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        self_pid: int | None = None,
    ):
        self.domain = f"gui/{os.getuid() if uid is None else uid}"
        self._procs = procs
        self._launchctl = launchctl or _default_launchctl
        self._sleep = sleep
        self._monotonic = monotonic
        self._self_pid = os.getpid() if self_pid is None else self_pid

    @property
    def procs(self) -> ProcessTable:
        if self._procs is None:
            self._procs = DarwinProcessTable()
        return self._procs

    # -- launch -------------------------------------------------------------

    def build_plist(self, label: str, run_dir: Path, argv: list[str], env: dict[str, str], cwd: Path) -> bytes:
        return plistlib.dumps({
            "Label": label,
            "ProgramArguments": ["/bin/sh", "-c", WRAPPER_SCRIPT, "openswap-job", str(run_dir), *argv],
            "WorkingDirectory": str(cwd),
            "EnvironmentVariables": dict(env),
            "RunAtLoad": True,
            "KeepAlive": False,
            "AbandonProcessGroup": False,
            "ExitTimeOut": EXIT_TIMEOUT_SECONDS,
            "ProcessType": "Standard",
            "Umask": 0o077,
            "StandardInPath": "/dev/null",
            "StandardOutPath": str(run_dir / "launchd.out"),
            "StandardErrorPath": str(run_dir / "launchd.err"),
        })

    def launch(
        self, *, job_id: str, run_dir: Path, argv: list[str], env: dict[str, str], cwd: Path,
        stdin_text: str, ready_timeout: float = 15.0,
    ) -> JobHandle:
        """Bootstrap the job and release it only once its coalition is recorded.

        Raises :class:`ContainmentError` with ``launched=False`` whenever the
        provider program cannot have run: it is gated on the ``go`` file, which
        is created only after the full handle is durable.
        """
        run_dir = Path(run_dir)
        label = job_label(job_id)
        ensure_private_dir(run_dir)
        if any(os.path.lexists(run_dir / name) for name in _WRAPPER_STATE):
            # A stale ``go`` would let the new wrapper start the provider
            # before its coalition is recorded: only a fresh directory runs.
            raise ContainmentError("run_dir_not_fresh")
        procs = self.procs
        boot = procs.boot_session()
        if boot is None:
            # Without the boot session a saved coalition ID could later name
            # another boot's coalition, so nothing could ever be swept safely.
            raise ContainmentError("boot_session_unavailable")
        loaded = self.label_loaded(JobHandle(label, self.domain, run_dir, boot))
        if loaded is not False:
            # In use, or launchd could not say: never bootstrap over (or later
            # boot out) a service this launch did not create.
            raise ContainmentError("job_label_in_use" if loaded else "launchd_unavailable")
        write_private(run_dir / STDIN_FILE, stdin_text.encode("utf-8"))
        plist_path = run_dir / "job.plist"
        write_private(plist_path, self.build_plist(label, run_dir, argv, env, cwd))
        handle = JobHandle(label, self.domain, run_dir, boot)
        _save_handle(handle)
        booted = self._launchctl(["bootstrap", self.domain, str(plist_path)])
        if booted.returncode != 0:
            # The label was confirmed absent above, so a loaded one now is ours.
            if self.label_loaded(handle):
                self._abort_unreleased(handle)
            raise ContainmentError("launchd_bootstrap_failed")
        try:
            pid = self._wait_for_leader(run_dir, ready_timeout)
            printed = self._launchctl(["print", f"{self.domain}/{label}"])
            if printed.returncode != 0 or _printed_pid(printed.stdout) != pid:
                raise ContainmentError("job_leader_unverified")
            coalition = procs.coalition_of(pid)
            own = procs.coalition_of(self._self_pid)
            if coalition is None or coalition == own:
                # Without a coalition of its own the job cannot be swept.
                raise ContainmentError("job_coalition_unavailable")
            handle = replace(handle, leader_pid=pid, coalition_id=coalition)
            _save_handle(handle)
            handle = replace(handle, released=True)
            _save_handle(handle)
        except BaseException:
            self._abort_unreleased(handle)
            raise
        try:
            write_private(run_dir / GO_FILE, b"")
        except BaseException:
            # The handle already says released, so recovery sweeps it either way.
            self.stop(handle)
            raise ContainmentError("job_release_failed", launched=True) from None
        return handle

    def _wait_for_leader(self, run_dir: Path, timeout: float) -> int:
        deadline = self._monotonic() + timeout
        while True:
            text = read_private_text(run_dir / LEADER_PID_FILE, limit=64)
            if text is not None and text.strip().isdigit():
                return int(text.strip())
            if self._monotonic() >= deadline:
                raise ContainmentError("job_leader_timeout")
            self._sleep(0.02)

    def _abort_unreleased(self, handle: JobHandle) -> None:
        """Unload a job whose provider never ran; its wrapper is still waiting."""
        self._launchctl(["bootout", f"{handle.domain}/{handle.label}"])
        if handle.coalition_id is not None:
            self._sweep(handle.coalition_id, deadline=self._monotonic() + 5.0)

    # -- observation ----------------------------------------------------------

    def exit_status(self, handle: JobHandle) -> int | None:
        text = read_private_text(handle.run_dir / EXIT_FILE, limit=32)
        if text is None or not text.strip().lstrip("-").isdigit():
            return None
        return int(text.strip())

    def members(self, handle: JobHandle) -> list[int]:
        if handle.coalition_id is None:
            return []
        return self._live_members(handle.coalition_id)

    def leader_alive(self, handle: JobHandle) -> bool:
        if handle.leader_pid is None or handle.coalition_id is None:
            return False
        procs = self.procs
        return (procs.coalition_of(handle.leader_pid) == handle.coalition_id
                and procs.status_of(handle.leader_pid) not in (None, _SZOMB))

    def label_loaded(self, handle: JobHandle) -> bool | None:
        result = self._launchctl(["print", f"{handle.domain}/{handle.label}"])
        if result.returncode == 0:
            return True
        if result.returncode in (113, 3):  # "Could not find service" / no such process
            return False
        return False if "could not find" in (result.stderr or "").lower() else None

    # -- stop -----------------------------------------------------------------

    def stop(self, handle: JobHandle, *, timeout: float = 15.0) -> StopProof:
        """Stop every member of the job and report proof, never a guess."""
        procs = self.procs
        current_boot = procs.boot_session()
        if handle.boot_session is not None and current_boot is not None and current_boot != handle.boot_session:
            # Every process of an earlier boot is gone, its label with it.
            return StopProof(True, False, 0, rebooted=True)
        # A saved coalition ID is meaningful only in the boot that recorded it.
        same_boot = handle.boot_session is not None and current_boot == handle.boot_session
        deadline = self._monotonic() + timeout
        killed = 0
        if handle.coalition_id is not None and same_boot:
            killed += self._sweep(handle.coalition_id, deadline=deadline)
        self._launchctl(["bootout", f"{handle.domain}/{handle.label}"])
        loaded = self.label_loaded(handle)
        while loaded and self._monotonic() < deadline:
            self._sleep(0.1)
            loaded = self.label_loaded(handle)
        survivors = 0
        if handle.coalition_id is not None:
            if not same_boot:
                return StopProof(False, loaded, 0, killed)
            killed += self._sweep(handle.coalition_id, deadline=deadline)
            survivors = len(self._live_members(handle.coalition_id))
            stopped = survivors == 0 and loaded is False
            return StopProof(stopped, loaded, survivors, killed)
        # No coalition was ever recorded, so the provider was never released:
        # the wrapper only waits for ``go``. An unloaded label is then proof.
        never = not handle.released and not (handle.run_dir / GO_FILE).exists()
        return StopProof(never and loaded is False, loaded, 0, killed, never_released=never)

    def recover(self, run_dir: Path) -> StopProof | None:
        """Stop whatever a lost worker left behind; None when nothing was launched."""
        handle = load_handle(Path(run_dir))
        if handle is None:
            return None
        return self.stop(handle)

    def _live_members(self, coalition_id: int) -> list[int]:
        procs = self.procs
        members = []
        for pid in procs.pids():
            if pid == self._self_pid or pid <= 1:
                continue
            if procs.coalition_of(pid) == coalition_id and procs.status_of(pid) not in (None, _SZOMB):
                members.append(pid)
        return members

    def _sweep(self, coalition_id: int, *, deadline: float) -> int:
        """Freeze every member, then kill them all; returns how many were killed.

        A stopped process cannot fork, so once a full scan finds every member
        already stopped the membership is final. Each pid's coalition is
        re-read immediately before it is signalled.
        """
        procs = self.procs
        while True:
            members = self._live_members(coalition_id)
            if not members:
                return 0
            running = [pid for pid in members if procs.status_of(pid) != _SSTOP]
            if not running:
                break
            for pid in running:
                if procs.coalition_of(pid) == coalition_id:
                    try:
                        procs.signal(pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        pass
            if self._monotonic() >= deadline:
                break
            self._sleep(0.002)
        killed: set[int] = set()
        while True:
            members = self._live_members(coalition_id)
            if not members:
                return len(killed)
            for pid in members:
                if procs.coalition_of(pid) == coalition_id:
                    try:
                        procs.signal(pid, signal.SIGKILL)
                        killed.add(pid)
                    except ProcessLookupError:
                        pass
            if self._monotonic() >= deadline:
                return len(killed)
            self._sleep(0.02)
