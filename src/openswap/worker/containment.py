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
import hashlib
import json
import os
import plistlib
import re
import secrets
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace

from openswap.locking import FileLock
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
STARTED_FILE = "started"
RELEASE_ACK_SECONDS = 10.0
# The wrapper itself runs with only this; the provider's environment is
# applied by ``env -i`` after the gate, so nothing in it (an exported shell
# function, say) can run before the coalition is recorded.
WRAPPER_ENV = {"PATH": "/usr/bin:/bin"}
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LAUNCHCTL_TIMEOUT_SECONDS = 20.0
# launchd sends SIGKILL this long after the SIGTERM of a bootout.
EXIT_TIMEOUT_SECONDS = 5

# The job's main program. It records its own pid, then waits (bounded) for the
# worker to record the coalition before it runs the provider, so a provider
# never runs unless the worker can sweep it. Its exit file is written only by
# a normal return of the provider. Only shell builtins and absolute system
# utilities run before the provider, whatever PATH the job's environment sets.
WRAPPER_SCRIPT = (
    'd="$1"; shift\n'
    'echo $$ > "$d/leader.pid.tmp" && /bin/mv "$d/leader.pid.tmp" "$d/leader.pid" || exit 124\n'
    'i=0\n'
    'while [ ! -e "$d/go" ]; do\n'
    '  i=$((i+1)); [ "$i" -gt 600 ] && exit 125\n'
    '  /bin/sleep 0.05\n'
    'done\n'
    ': > "$d/started" || exit 126\n'
    '"$@" < "$d/stdin.txt" > "$d/stdout.jsonl" 2> "$d/stderr.log"\n'
    'rc=$?\n'
    'echo "$rc" > "$d/exit.tmp" && /bin/mv "$d/exit.tmp" "$d/exit"\n'
)
WRAPPER_PREFIX = ("/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "/bin/sh", "-c", WRAPPER_SCRIPT, "openswap-job")

def _fresh(run_dir: Path) -> bool:
    """An empty run directory, apart from a private real ``tmp`` directory."""
    try:
        names = os.listdir(run_dir)
    except OSError:
        return False
    for name in names:
        if name != "tmp":
            return False
        info = (run_dir / name).lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
    return True


def _run_dir_key(run_dir: Path) -> str:
    return hashlib.sha256(str(Path(run_dir)).encode()).hexdigest()[:32]


def default_lock_dir() -> Path:
    """Per-user directory for label locks, shared by every run root of this user.

    launchd labels are domain-wide (``gui/<uid>``), so their locks must not
    depend on where a caller keeps its run directories.
    """
    try:
        import pwd

        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError):
        home = Path.home()
    # From the account database, not $HOME or $TMPDIR, so every process of
    # this user agrees whatever its environment.
    return home / "Library" / "Caches" / "com.opensoft.openswap" / "job-locks"

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

    def is_dead(self, pid: int) -> bool: ...


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
        """Every pid, from a listing that provably fit its buffer.

        The kernel clamps the listing to the buffer and stops walking when it
        is full, so a result that fills the buffer may be truncated: retry
        with more room, and give up (no proof) rather than return it.
        """
        for _ in range(5):
            estimate = self._libproc.proc_listallpids(None, 0)
            if estimate <= 0:
                raise ContainmentError("process_table_unavailable")
            capacity = estimate * 2 + 1024
            buf = (ctypes.c_int * capacity)()
            count = self._libproc.proc_listallpids(buf, ctypes.sizeof(buf))
            if count <= 0:
                raise ContainmentError("process_table_unavailable")
            if count < capacity:
                return [buf[i] for i in range(count) if buf[i] > 0]
        raise ContainmentError("process_table_unavailable")

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

    def is_dead(self, pid: int) -> bool:
        """Confirmed exited or a zombie; anything unclear is not dead."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if self.status_of(pid) == _SZOMB:
            return True
        try:
            result = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)], capture_output=True,
                                    text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0 and not result.stdout.strip():
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
            return False
        return result.stdout.strip().startswith("Z")

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
    # Unique per launch: names this launch's plist, which launchd reports as
    # the service path, so a later launch at the same run directory differs.
    launch_id: str | None = None

    @property
    def plist_path(self) -> Path:
        return self.run_dir / (f"job-{self.launch_id}.plist" if self.launch_id else "job.plist")

    def to_dict(self) -> dict:
        return {
            "label": self.label, "domain": self.domain, "boot_session": self.boot_session,
            "leader_pid": self.leader_pid, "coalition_id": self.coalition_id,
            "released": self.released, "launch_id": self.launch_id,
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
    except ValueError:
        return None
    return _handle_from(raw, Path(run_dir))


def reconcile_handles(primary: JobHandle | None, mirror: JobHandle | None) -> JobHandle | None:
    """One handle from the run directory's copy and its mirror, or ``None`` to refuse.

    A launch writes both copies at each step, and a crash between the two
    writes leaves one a step behind: the same launch, with a field still
    unset that the other has. That is merged (taking what either recorded,
    and "released" if either says so). Copies that name different launches, or
    record different values for the same field, are not a partial update and
    are refused.
    """
    if primary is None or mirror is None:
        return primary or mirror
    if primary == mirror:
        return primary
    fixed = ("label", "domain", "run_dir", "boot_session", "launch_id")
    if any(getattr(primary, name) != getattr(mirror, name) for name in fixed):
        return None
    merged = {}
    for name in ("leader_pid", "coalition_id"):
        a, b = getattr(primary, name), getattr(mirror, name)
        if a is not None and b is not None and a != b:
            return None
        merged[name] = a if a is not None else b
    return replace(primary, released=primary.released or mirror.released, **merged)


def _handle_from(raw, run_dir: Path) -> JobHandle | None:
    try:
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
        launch_id = raw.get("launch_id")
        if launch_id is not None and (not isinstance(launch_id, str) or not re.fullmatch(r"[0-9a-f]{16}", launch_id)):
            return None
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
    return JobHandle(label, domain, Path(run_dir), boot, pid, cid, released, launch_id)


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
        lock_dir: Path | None = None,
    ):
        self._lock_dir = Path(lock_dir) if lock_dir is not None else default_lock_dir()
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
        for key, value in env.items():
            if not _ENV_KEY_RE.fullmatch(key) or "\0" in value:
                raise ContainmentError("environment_invalid")
        if not argv or "=" in argv[0]:
            raise ContainmentError("program_invalid")
        provider = ["/usr/bin/env", "-i", *(f"{key}={value}" for key, value in env.items()), *argv]
        return plistlib.dumps({
            "Label": label,
            # launchd adds its own domain environment (``launchctl setenv``
            # included), so the shell itself starts from ``env -i``: nothing
            # inherited can run before the coalition is recorded.
            "ProgramArguments": [*WRAPPER_PREFIX, str(run_dir), *provider],
            "WorkingDirectory": str(cwd),
            "EnvironmentVariables": dict(WRAPPER_ENV),
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
        label = job_label(job_id)
        try:
            ensure_private_dir(Path(run_dir))
        except OSError:
            raise ContainmentError("run_dir_unavailable") from None
        # One spelling per physical directory, for its locks, handle and mirror.
        run_dir = Path(os.path.realpath(run_dir))
        # One launch or stop per label at a time: the label check, bootstrap
        # and any cleanup run under this lock, so a label loaded after a failed
        # bootstrap can only be this launch's own.
        try:
            lock = self._label_lock(label)
            acquired = lock.acquire(timeout=0)
        except (OSError, ContainmentError):
            raise ContainmentError("label_lock_unavailable") from None
        if not acquired:
            raise ContainmentError("job_label_in_use")
        try:
            # The run directory is claimed too, whatever the label: two
            # launches into one directory would share its wrapper files.
            try:
                dir_lock = FileLock(self._lock_dir / f"rundir-{_run_dir_key(run_dir)}.lock", timeout=0)
                dir_acquired = dir_lock.acquire(timeout=0)
            except OSError:
                raise ContainmentError("label_lock_unavailable") from None
            if not dir_acquired:
                raise ContainmentError("run_dir_in_use")
            try:
                return self._launch_locked(label, run_dir, argv, env, cwd, stdin_text, ready_timeout)
            finally:
                dir_lock.release()
        finally:
            lock.release()

    def _launch_locked(self, label, run_dir, argv, env, cwd, stdin_text, ready_timeout) -> JobHandle:
        if not _fresh(run_dir):
            # A stale ``go`` would start the provider before its coalition is
            # recorded, and any pre-existing name (a symlink, say) would be
            # opened by the wrapper's redirections: only an empty directory
            # (apart from a private ``tmp``) runs.
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
        handle = JobHandle(label, self.domain, run_dir, boot, launch_id=secrets.token_hex(8))
        plist_path = handle.plist_path
        try:
            # Claim the directory atomically with the plist, then write the
            # rest. Nothing has been handed to launchd yet, so a failure here
            # (a full disk, say) provably launched nothing.
            fd = os.open(plist_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(self.build_plist(label, run_dir, argv, env, cwd))
            write_private(run_dir / STDIN_FILE, stdin_text.encode("utf-8"))
            self._persist(handle)
        except FileExistsError:
            raise ContainmentError("run_dir_not_fresh") from None
        except OSError:
            raise ContainmentError("run_dir_unwritable") from None
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
            self._persist(handle)
            handle = replace(handle, released=True)
            self._persist(handle)
        except BaseException as error:
            # ``go`` was never created, so the provider never ran.
            self._abort_unreleased(handle)
            if isinstance(error, ContainmentError) or not isinstance(error, Exception):
                raise
            raise ContainmentError("job_handle_unwritable") from None
        try:
            write_private(run_dir / GO_FILE, b"")
        except BaseException:
            # The handle already says released, so recovery sweeps it either way.
            self._stop_locked(handle, timeout=15.0)
            raise ContainmentError("job_release_failed", launched=True) from None
        # The wrapper acknowledges ``go`` before it runs the provider. If it
        # gave up waiting first (its bounded wait expired), it has exited
        # without the acknowledgement and the provider never ran.
        deadline = self._monotonic() + RELEASE_ACK_SECONDS
        while not os.path.lexists(run_dir / STARTED_FILE):
            # Only a confirmed exit (not an unreadable process) means the
            # wrapper gave up before it could start the provider.
            if handle.leader_pid is not None and self.procs.is_dead(handle.leader_pid):
                if os.path.lexists(run_dir / STARTED_FILE):
                    break
                self._stop_locked(handle, timeout=15.0)
                raise ContainmentError("job_release_expired", launched=False)
            if self._monotonic() >= deadline:
                self._stop_locked(handle, timeout=15.0)
                raise ContainmentError("job_release_unacknowledged", launched=True)
            self._sleep(0.02)
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
        # The directory may since hold another launch: its exit is not ours.
        current = load_handle(handle.run_dir)
        if current is not None and current.launch_id != handle.launch_id:
            return None
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

    def _mirror_path(self, run_dir: Path) -> Path:
        digest = _run_dir_key(run_dir)
        return self._lock_dir.parent / "job-handles" / f"{digest}.json"

    def _persist(self, handle: JobHandle) -> None:
        """Write the handle in the run directory and a mirror outside it.

        Recovery uses the mirror when the run directory's copy is gone, and
        refuses to act when the two disagree.
        """
        _save_handle(handle)
        mirror = self._mirror_path(handle.run_dir)
        mirror.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        ensure_private_dir(mirror.parent)
        write_private(mirror, json.dumps({"run_dir": str(handle.run_dir), **handle.to_dict()}).encode())

    def _load_mirror(self, run_dir: Path) -> JobHandle | None:
        text = read_private_text(self._mirror_path(run_dir))
        if text is None:
            return None
        try:
            raw = json.loads(text)
        except ValueError:
            return None
        if not isinstance(raw, dict) or raw.get("run_dir") != str(Path(run_dir)):
            return None
        return _handle_from(raw, Path(run_dir))

    def _forget_mirror(self, run_dir: Path) -> None:
        try:
            self._mirror_path(run_dir).unlink()
        except OSError:
            pass

    def _label_lock(self, label: str) -> FileLock:
        self._lock_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        ensure_private_dir(self._lock_dir)
        return FileLock(self._lock_dir / f"{label}.lock", timeout=0)

    def _owns_label(self, handle: JobHandle) -> bool | None:
        """Whether the loaded label is this handle's job: ``None`` when nothing is loaded.

        launchd prints the plist a service was bootstrapped from as its
        top-level ``path``; each launch writes its own plist in its own run
        directory, so an exact match identifies the service.
        """
        printed = self._launchctl(["print", f"{handle.domain}/{handle.label}"])
        if printed.returncode != 0:
            return None
        expected = f"path = {handle.plist_path}"
        return any(
            line.startswith("\t") and not line.startswith("\t\t") and line.strip() == expected
            for line in (printed.stdout or "").splitlines()
        )

    def stop(self, handle: JobHandle, *, timeout: float = 15.0) -> StopProof:
        """Stop every member of the job and report proof, never a guess.

        Takes the label's lock (shared with :meth:`launch`) so it can never
        interleave with a launch of the same label.
        """
        try:
            lock = self._label_lock(handle.label)
            acquired = lock.acquire(timeout=timeout)
        except (OSError, ContainmentError):
            acquired = False
        if not acquired:
            return StopProof(False, None, 0)
        try:
            proof = self._stop_locked(handle, timeout=timeout)
        finally:
            lock.release()
        if proof.stopped:
            # Check and delete under the run directory's lock, which every
            # launch into it holds, so a replacement's mirror is never removed.
            try:
                dir_lock = FileLock(self._lock_dir / f"rundir-{_run_dir_key(handle.run_dir)}.lock", timeout=5)
                held = dir_lock.acquire()
            except OSError:
                held = False
            if held:
                try:
                    mirror = self._load_mirror(handle.run_dir)
                    if mirror is not None and mirror.launch_id == handle.launch_id and mirror.label == handle.label:
                        self._forget_mirror(handle.run_dir)
                finally:
                    dir_lock.release()
        return proof

    def _stop_locked(self, handle: JobHandle, *, timeout: float) -> StopProof:
        procs = self.procs
        current_boot = procs.boot_session()
        if handle.boot_session is not None and current_boot is not None and current_boot != handle.boot_session:
            # Every process of an earlier boot is gone, its label with it.
            return StopProof(True, False, 0, rebooted=True)
        if handle.boot_session is None or current_boot != handle.boot_session:
            # Neither the saved coalition ID nor the label can be trusted to be
            # this job's without a boot-session match: touch nothing.
            return StopProof(False, None, 0)
        deadline = self._monotonic() + timeout
        frozen, killed = True, 0
        if handle.coalition_id is not None:
            frozen, killed = self._sweep(handle.coalition_id, deadline=deadline)
        owned = self._owns_label(handle)
        if owned:
            self._launchctl(["bootout", f"{handle.domain}/{handle.label}"])
            loaded = self.label_loaded(handle)
            while loaded and self._monotonic() < deadline:
                self._sleep(0.1)
                loaded = self.label_loaded(handle)
        else:
            # Not loaded, or loaded by another launch: this job's service is
            # gone either way, and someone else's is never booted out.
            loaded = False if owned is False or self.label_loaded(handle) is False else None
        if handle.coalition_id is not None:
            # Sweep again: anything the bootout left behind is still a member.
            again_frozen, again_killed = self._sweep(handle.coalition_id, deadline=deadline)
            killed += again_killed
            try:
                survivors = len(self._live_members(handle.coalition_id))
            except ContainmentError:
                return StopProof(False, loaded, 1, killed)
            stopped = frozen and again_frozen and survivors == 0 and loaded is False
            return StopProof(stopped, loaded, survivors, killed)
        # No coalition was ever recorded, so the provider was never released:
        # the wrapper only waits for ``go``. An unloaded label is then proof.
        never = not handle.released and not (handle.run_dir / GO_FILE).exists()
        return StopProof(never and loaded is False, loaded, 0, killed, never_released=never)

    def recover(self, run_dir: Path) -> StopProof | None:
        """Stop whatever a lost worker left behind; None when nothing was launched.

        Uses the run directory's handle or, if that is gone, its mirror; when
        both exist and disagree, nothing is trusted and nothing is touched.
        """
        run_dir = Path(os.path.realpath(run_dir))
        primary = load_handle(run_dir)
        mirror = self._load_mirror(run_dir)
        if primary is None and mirror is None:
            return None
        handle = reconcile_handles(primary, mirror)
        if handle is None:
            return StopProof(False, None, 0)
        return self.stop(handle)

    def _scan(self, coalition_id: int) -> dict[int, str]:
        """Classify every listed pid: ``stopped``/``running`` member, ``other``, or ``gone``.

        ``gone`` is a pid whose coalition could not be read: on macOS that is
        a zombie or a process that exited after the listing (measured: live
        processes of every user are readable). A member whose status cannot be
        read counts as ``running``, so it can never be mistaken for frozen.
        """
        procs = self.procs
        kinds: dict[int, str] = {}
        for pid in procs.pids():
            if pid == self._self_pid or pid <= 1:
                continue
            coalition = procs.coalition_of(pid)
            if coalition is None:
                # Unreadable: only a confirmed exit or zombie is harmless; a
                # live process we cannot place blocks any proof.
                kinds[pid] = "gone" if procs.is_dead(pid) else "unknown"
            elif coalition != coalition_id:
                kinds[pid] = "other"
            else:
                status = procs.status_of(pid)
                kinds[pid] = "other" if status == _SZOMB else "stopped" if status == _SSTOP else "running"
        return kinds

    @staticmethod
    def _complete(scan: dict[int, str], previous: dict[int, str] | None) -> bool:
        """Whether every live process at this scan's listing was seen.

        A pid that was listed but is ``gone`` by its query could have forked
        just before dying, so the scan is complete only if every such pid was
        already listed by the previous scan (a lingering zombie, say).
        """
        if previous is None:
            return False
        # Only a pid already unqueryable last time (a lingering zombie) is
        # harmless: a pid seen alive before may have been reused since.
        return all(previous.get(pid) == "gone" for pid, kind in scan.items() if kind == "gone")

    def _live_members(self, coalition_id: int) -> list[int]:
        """Members, plus live processes whose coalition cannot be read (they may be members)."""
        return [pid for pid, kind in self._scan(coalition_id).items() if kind in {"running", "stopped", "unknown"}]

    def _kill_known(self, coalition_id: int, pids) -> int:
        killed = 0
        for pid in pids:
            try:
                if self.procs.coalition_of(pid) == coalition_id:
                    self.procs.signal(pid, signal.SIGKILL)
                    killed += 1
            except (ProcessLookupError, PermissionError, ContainmentError):
                pass
        return killed

    def _sweep(self, coalition_id: int, *, deadline: float) -> tuple[bool, int]:
        """Freeze every member, then kill them all; returns ``(proven, killed)``.

        ``SIGSTOP`` is delivered asynchronously and a process listing is not
        atomic, so a freeze counts only when two consecutive scans list the
        same members, all observed stopped, and the second is complete (see
        :meth:`_complete`). Every member alive at the second listing was
        already stopped before it, and a stopped process cannot fork, so no
        member can appear afterwards. Only then is the empty scan after the
        kills proof; otherwise the sweep still kills what it finds but proves
        nothing. Each pid's coalition is re-read right before it is signalled.
        """
        procs = self.procs
        frozen = False
        previous: dict[int, str] | None = None
        known: set[int] = set()
        while True:
            try:
                scan = self._scan(coalition_id)
            except ContainmentError:
                # No trustworthy listing: kill what is known, prove nothing.
                return False, self._kill_known(coalition_id, known)
            known |= {pid for pid, kind in scan.items() if kind in {"running", "stopped"}}
            running = [pid for pid, kind in scan.items() if kind == "running"]
            stopped = {pid for pid, kind in scan.items() if kind == "stopped"}
            if (
                not running and "unknown" not in scan.values()
                and previous is not None and self._complete(scan, previous)
                and "running" not in previous.values() and "unknown" not in previous.values()
                and stopped == {pid for pid, kind in previous.items() if kind == "stopped"}
            ):
                frozen = True
                break
            for pid in running:
                if procs.coalition_of(pid) == coalition_id:
                    try:
                        procs.signal(pid, signal.SIGSTOP)
                    except (ProcessLookupError, PermissionError):
                        pass
            previous = scan
            if self._monotonic() >= deadline:
                break
            self._sleep(0.002)
        killed: set[int] = set()
        previous = None
        while True:
            try:
                scan = self._scan(coalition_id)
            except ContainmentError:
                return False, len(killed) + self._kill_known(coalition_id, known - killed)
            known |= {pid for pid, kind in scan.items() if kind in {"running", "stopped"}}
            members = [pid for pid, kind in scan.items() if kind in {"running", "stopped"}]
            if not members and "unknown" not in scan.values() and (
                    previous is None or self._complete(scan, previous)):
                return frozen, len(killed)
            for pid in members:
                if procs.coalition_of(pid) == coalition_id:
                    try:
                        procs.signal(pid, signal.SIGKILL)
                        killed.add(pid)
                    except (ProcessLookupError, PermissionError):
                        pass
            previous = scan
            if self._monotonic() >= deadline:
                return False, len(killed)
            self._sleep(0.02)
