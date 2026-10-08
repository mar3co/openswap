"""Per-job launchd containment: fake launchd/process table, plus an opt-in real run."""

from __future__ import annotations

import os
import plistlib
import signal
from dataclasses import replace
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from openswap.worker import containment as c
from openswap.worker.containment import (
    ContainmentError,
    LaunchdContainment,
    JobHandle,
    load_handle,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="launchd containment is macOS-only")

WORKER_PID = 4242
WORKER_COALITION = 7
JOB_COALITION = 900


class FakeProcs:
    """A tiny process table: pid -> [coalition, status]."""

    def __init__(self, boot="boot-a"):
        self.table = {WORKER_PID: [WORKER_COALITION, 2]}
        self.boot = boot
        self.signals: list[tuple[int, int]] = []
        self.unkillable: set[int] = set()
        self.spawn_on_scan = 0  # running members fork this many times while racing
        self.vanishing = 0  # this many scans list one pid that is gone by its query
        self.zombies: list[int] = []  # listed every scan, never queryable
        self.dead_unreadable: set[int] = set()
        self.live_unreadable: set[int] = set()  # listed, alive, coalition unreadable
        self.table_fails = 0  # pids() raises this many more times
        self.started: dict[int, float] = {}  # pid -> start time (wall clock)
        self._next = 5000

    def new(self, coalition, status=2):
        self._next += 1
        self.table[self._next] = [coalition, status]
        return self._next

    def pids(self):
        if self.table_fails:
            self.table_fails -= 1
            raise ContainmentError("process_table_unavailable")
        if self.live_unreadable:
            return [*self.table, *self.live_unreadable]
        if self.vanishing:
            # Listed, then gone before it can be queried (or a zombie).
            self._next += 1
            self.vanishing -= 1
            return [*self.table, self._next, *self.zombies]
        if self.zombies:
            return [*self.table, *self.zombies]
        if self.spawn_on_scan:
            racers = [pid for pid, (cid, st) in self.table.items() if cid == JOB_COALITION and st != 4]
            if racers:
                self.spawn_on_scan -= 1
                self.new(JOB_COALITION)
        return list(self.table)

    def coalition_of(self, pid):
        if pid in self.live_unreadable:
            return None
        entry = self.table.get(pid)
        return entry[0] if entry else None

    def status_of(self, pid):
        entry = self.table.get(pid)
        return entry[1] if entry else None

    def signal(self, pid, signum):
        self.signals.append((pid, signum))
        if pid not in self.table:
            raise ProcessLookupError(pid)
        if signum == signal.SIGSTOP:
            self.table[pid][1] = 4
        elif signum == signal.SIGCONT:
            self.table[pid][1] = 2
        elif signum in (signal.SIGKILL, signal.SIGTERM) and pid not in self.unkillable:
            del self.table[pid]

    def boot_session(self):
        return self.boot

    def is_dead(self, pid):
        if pid in self.live_unreadable:
            return False
        return pid not in self.table or pid in self.zombies or pid in self.dead_unreadable

    def zombie_identity(self, pid):
        entry = self.table.get(pid)
        return f"100.{pid:06d}" if pid in self.zombies or (entry and entry[1] == 5) else None

    def start_time(self, pid):
        if pid not in self.table and pid not in self.zombies:
            return None
        return self.started.get(pid, 0.0)  # long ago unless a test says otherwise



class FakeLaunchd:
    def __init__(self, procs: FakeProcs, *, coalition=JOB_COALITION):
        self.procs = procs
        self.coalition = coalition
        self.loaded: dict[str, int] = {}
        self.calls: list[list[str]] = []
        self.bootstrap_rc = 0
        self.print_pid_override = None
        self.write_pid = True
        self.keep_loaded = False
        self.escaped: list[int] = []
        self.run_dirs: dict[str, str] = {}
        self.plists: dict[str, str] = {}
        self.bootstraps = 0
        self.ack = True

    def __call__(self, args):
        self.calls.append(list(args))
        verb = args[0]
        if verb == "print":
            label = args[1].split("/", 2)[2]
            if label in self.loaded:
                pid = self.print_pid_override or self.loaded[label]
                plist_path = self.plists.get(label, "")
                return subprocess.CompletedProcess(
                    args, 0, f"{label} = {{\n\tpath = {plist_path}\n\tstate = running\n\tpid = {pid}\n}}\n", "")
            return subprocess.CompletedProcess(args, 113, "", "Could not find service")
        if verb == "bootstrap":
            if self.bootstrap_rc:
                return subprocess.CompletedProcess(args, self.bootstrap_rc, "", "error")
            plist = plistlib.loads(Path(args[2]).read_bytes())
            run_dir = Path(plist["ProgramArguments"][len(c.WRAPPER_PREFIX)])
            # launchd gives every bootstrapped job a new coalition.
            leader = self.procs.new(self.coalition + self.bootstraps)
            self.bootstraps += 1
            self.loaded[plist["Label"]] = leader
            self.run_dirs[plist["Label"]] = str(run_dir)
            self.plists[plist["Label"]] = args[2]
            if self.write_pid:
                (run_dir / "leader.pid").write_text(f"{leader}\n")
            if self.ack:
                # Stands in for the wrapper acknowledging ``go``.
                (run_dir / "started").write_text("")
            return subprocess.CompletedProcess(args, 0, "", "")
        if verb == "bootout":
            label = args[1].split("/", 2)[2]
            leader = self.loaded.get(label)
            if leader is not None and not self.keep_loaded:
                self.procs.table.pop(leader, None)  # launchd kills the leader's group only
                del self.loaded[label]
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(args)


def make(tmp_path, **kwargs):
    (tmp_path / "locks").mkdir(mode=0o700, parents=True, exist_ok=True)
    procs = FakeProcs()
    launchd = FakeLaunchd(procs, **kwargs)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    containment = LaunchdContainment(
        uid=501, procs=procs, launchctl=launchd, sleep=sleep,
        monotonic=lambda: clock[0], self_pid=WORKER_PID, lock_dir=tmp_path / "locks",
    )
    return containment, procs, launchd


def private_dir(tmp_path, name="runs"):
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return root


def launch(containment, root, job_id="a" * 32, **kwargs):
    return containment.launch(
        job_id=job_id, run_dir=root / job_id, argv=["/bin/echo", "hi"],
        env={"PATH": "/usr/bin:/bin"}, cwd=root, stdin_text="task", **kwargs,
    )


def test_launch_records_coalition_before_releasing_the_provider(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    handle = launch(containment, root)
    assert handle.coalition_id == JOB_COALITION
    assert handle.released is True
    assert handle.label == "com.opensoft.openswap.worker.job." + "a" * 32
    assert handle.domain == "gui/501"
    assert (root / ("a" * 32) / "go").exists()
    assert load_handle(root / ("a" * 32)) == handle
    plist = plistlib.loads(handle.plist_path.read_bytes())
    # The shell itself starts from a clean environment.
    assert plist["ProgramArguments"][:5] == ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "/bin/sh", "-c"]
    n = len(c.WRAPPER_PREFIX) + 1
    assert plist["ProgramArguments"][n:] == ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "/bin/echo", "hi"]
    assert plist["KeepAlive"] is False and plist["AbandonProcessGroup"] is False
    assert plist["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}  # the wrapper's own, fixed
    assert plist["Umask"] == 0o077
    assert (root / ("a" * 32) / "stdin.txt").read_text() == "task"
    assert oct((root / ("a" * 32) / "stdin.txt").stat().st_mode & 0o777) == "0o600"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell wrapper")
def test_wrapper_runs_the_provider_only_after_go(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "stdin.txt").write_text("")
    proc = subprocess.Popen(
        ["/bin/sh", "-c", c.WRAPPER_SCRIPT, "openswap-job", str(run_dir), "/bin/echo", "ran"],
    )
    for _ in range(200):
        if (run_dir / "leader.pid").exists():
            break
        time.sleep(0.01)
    assert int((run_dir / "leader.pid").read_text()) == proc.pid
    time.sleep(0.2)
    assert not (run_dir / "stdout.jsonl").exists() and not (run_dir / "started").exists()
    (run_dir / "go").write_text("")
    assert proc.wait(timeout=10) == 0
    assert (run_dir / "started").exists()
    assert (run_dir / "stdout.jsonl").read_text() == "ran\n"
    assert (run_dir / "exit").read_text().strip() == "0"


@pytest.mark.parametrize("problem", ["same_coalition", "bootstrap", "pid_mismatch", "no_pid", "no_boot"])
def test_unprovable_launch_is_refused_and_never_released(tmp_path, problem):
    kwargs = {"coalition": WORKER_COALITION} if problem == "same_coalition" else {}
    containment, procs, launchd = make(tmp_path, **kwargs)
    if problem == "bootstrap":
        launchd.bootstrap_rc = 5
    elif problem == "pid_mismatch":
        launchd.print_pid_override = 99999
    elif problem == "no_pid":
        launchd.write_pid = False
    elif problem == "no_boot":
        procs.boot = None
    root = private_dir(tmp_path)
    with pytest.raises(ContainmentError) as error:
        launch(containment, root)
    assert error.value.launched is False
    assert not (root / ("a" * 32) / "go").exists()
    assert launchd.loaded == {}
    assert WORKER_PID in procs.table  # never signalled the worker's coalition
    assert all(pid != WORKER_PID for pid, _ in procs.signals)


def test_label_already_loaded_is_refused(tmp_path):
    containment, procs, launchd = make(tmp_path)
    launchd.loaded[c.job_label("a" * 32)] = procs.new(JOB_COALITION)
    with pytest.raises(ContainmentError) as error:
        launch(containment, private_dir(tmp_path))
    assert error.value.code == "job_label_in_use"
    assert error.value.launched is False


def test_stop_kills_escaped_setsid_members_that_bootout_misses(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    escaped = [procs.new(JOB_COALITION) for _ in range(3)]
    unrelated = procs.new(55)
    proof = containment.stop(handle)
    assert proof.stopped is True and proof.survivors == 0 and proof.label_loaded is False
    assert not any(pid in procs.table for pid in escaped)
    assert unrelated in procs.table and WORKER_PID in procs.table
    # Every member is frozen before any is killed.
    kinds = [sig for _, sig in procs.signals]
    assert kinds.index(signal.SIGKILL) > max(i for i, s in enumerate(kinds) if s == signal.SIGSTOP)


def test_sweep_wins_a_fork_race(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    procs.new(JOB_COALITION)
    procs.spawn_on_scan = 25
    proof = containment.stop(handle)
    assert proof.stopped is True
    assert not [p for p, (cid, _) in procs.table.items() if cid == JOB_COALITION]


def test_unkillable_member_or_loaded_label_is_not_proof(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    stuck = procs.new(JOB_COALITION)
    procs.unkillable.add(stuck)
    proof = containment.stop(handle, timeout=1.0)
    assert proof.stopped is False and proof.survivors == 1

    containment, procs, launchd = make(tmp_path / "second")
    handle = launch(containment, private_dir(tmp_path / "second"))
    launchd.keep_loaded = True
    proof = containment.stop(handle, timeout=1.0)
    assert proof.stopped is False and proof.label_loaded is True


def test_zombies_are_not_survivors(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    procs.new(JOB_COALITION, status=5)
    assert containment.stop(handle).stopped is True


def test_recover_after_reboot_is_proof_without_signals(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    handle = launch(containment, root)
    procs.boot = "boot-b"
    procs.signals.clear()
    proof = containment.recover(handle.run_dir)
    assert proof.stopped is True and proof.rebooted is True
    assert procs.signals == []


def test_recover_without_handle_is_none_and_unreleased_handle_proves_by_unload(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    assert containment.recover(root / "missing") is None
    run_dir = root / ("b" * 32)
    run_dir.mkdir(mode=0o700)
    c._save_handle(JobHandle(c.job_label("b" * 32), "gui/501", run_dir, "boot-a"))
    proof = containment.recover(run_dir)
    assert proof.stopped is True and proof.never_released is True
    # Released but with no coalition recorded cannot be proven.
    c._save_handle(JobHandle(c.job_label("b" * 32), "gui/501", run_dir, "boot-a", released=True))
    assert containment.recover(run_dir).stopped is False


@pytest.mark.parametrize("payload", [
    "not json", '{"label": "x", "domain": "gui/501"}',
    '{"label": "com.opensoft.openswap.worker.job.aa", "domain": "system"}',
    '{"label": "com.opensoft.openswap.worker.job.aa", "domain": "gui/501", "coalition_id": "1"}',
    '{"label": "com.opensoft.openswap.worker.job.aa", "domain": "gui/501", "released": 1}',
])
def test_malformed_handles_are_ignored(tmp_path, payload):
    (tmp_path / "handle.json").write_text(payload)
    assert load_handle(tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_symlinked_handle_is_ignored(tmp_path):
    real = tmp_path / "real.json"
    real.write_text('{"label": "com.opensoft.openswap.worker.job.aa", "domain": "gui/501"}')
    run = tmp_path / "run"
    run.mkdir()
    (run / "handle.json").symlink_to(real)
    assert load_handle(run) is None


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("OPENSWAP_LAUNCHD_TESTS") != "1",
    reason="real launchd containment; set OPENSWAP_LAUNCHD_TESTS=1 on a Mac",
)
def test_real_launchd_contains_a_setsid_daemon(tmp_path):
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    started = tmp_path / "started"
    script = (
        "use POSIX;"
        "if (fork()==0) { setsid(); if (fork()==0) { close STDIN; close STDOUT; close STDERR;"
        " exec '/bin/sleep','297'; } exit 0; }"
        f"open(my $f, '>', '{started}'); close $f; sleep 300;"
    )
    containment = LaunchdContainment()
    handle = containment.launch(
        job_id=uuid.uuid4().hex, run_dir=root / "job", argv=["/usr/bin/perl", "-e", script],
        env={"PATH": "/usr/bin:/bin"}, cwd=tmp_path, stdin_text="",
    )
    try:
        for _ in range(200):
            if started.exists():
                break
            time.sleep(0.05)
        assert len(containment.members(handle)) >= 3
        proof = containment.stop(handle)
        assert proof.stopped is True
        assert containment.members(handle) == []
    finally:
        containment.stop(handle)


@pytest.mark.parametrize("stale", ["go", "leader.pid", "exit", "handle.json", "stdout.jsonl", "job.plist",
                                   "leader.pid.tmp", "exit.tmp", "stderr.log", "launchd.out", "anything"])
def test_a_run_directory_with_wrapper_state_is_refused(tmp_path, stale):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    run_dir = root / ("a" * 32)
    run_dir.mkdir(mode=0o700)
    (run_dir / stale).write_text("")
    with pytest.raises(ContainmentError) as error:
        launch(containment, root)
    assert error.value.code == "run_dir_not_fresh" and error.value.launched is False
    assert not any(call[0] == "bootstrap" for call in launchd.calls)


def test_inconclusive_label_check_is_refused_without_touching_launchd(tmp_path):
    containment, procs, launchd = make(tmp_path)
    original = launchd.__call__

    def flaky(args):
        if args[0] == "print":
            launchd.calls.append(list(args))
            return subprocess.CompletedProcess(args, 124, "", "timeout")
        return original(args)

    containment._launchctl = flaky
    with pytest.raises(ContainmentError) as error:
        launch(containment, private_dir(tmp_path))
    assert error.value.code == "launchd_unavailable"
    assert [call[0] for call in launchd.calls] == ["print"]


def test_failed_bootstrap_never_boots_out_a_label_it_did_not_load(tmp_path):
    containment, procs, launchd = make(tmp_path)
    launchd.bootstrap_rc = 5
    with pytest.raises(ContainmentError):
        launch(containment, private_dir(tmp_path))
    assert not any(call[0] == "bootout" for call in launchd.calls)


@pytest.mark.parametrize("saved, current", [(None, "boot-a"), ("boot-a", None)])
def test_a_saved_coalition_is_never_swept_without_a_boot_session_match(tmp_path, saved, current):
    containment, procs, launchd = make(tmp_path)
    run_dir = private_dir(tmp_path) / ("c" * 32)
    run_dir.mkdir(mode=0o700)
    bystander = procs.new(JOB_COALITION)  # whatever holds that ID now
    procs.boot = current
    proof = containment.stop(JobHandle(c.job_label("c" * 32), "gui/501", run_dir, saved, 77, JOB_COALITION, True))
    assert proof.stopped is False
    assert bystander in procs.table and procs.signals == []


def test_a_pid_that_vanishes_mid_scan_delays_the_proof_but_not_forever(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    procs.new(JOB_COALITION)
    procs.vanishing = 3
    proof = containment.stop(handle)
    assert proof.stopped is True and procs.vanishing == 0


def test_a_lingering_zombie_does_not_block_the_proof(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    procs.zombies = [88888]
    assert containment.stop(handle).stopped is True


def test_a_member_whose_status_cannot_be_read_is_never_proven_stopped(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    root_helper = procs.new(JOB_COALITION, status=None)  # e.g. a setuid-root child
    procs.unkillable.add(root_helper)
    proof = containment.stop(handle, timeout=1.0)
    assert proof.stopped is False and proof.survivors == 1


def test_no_bootout_without_a_boot_session_match(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    procs.boot = None
    launchd.calls.clear()
    proof = containment.stop(handle)
    assert proof.stopped is False
    assert not any(call[0] == "bootout" for call in launchd.calls)


class FakeLibproc:
    """proc_listallpids that fills the buffer the first ``full`` times."""

    def __init__(self, full):
        self.full = full

    def proc_listallpids(self, buf, size):
        if buf is None:
            return 10
        capacity = size // 4
        if self.full:
            self.full -= 1
            count = capacity
        else:
            count = 12
        for i in range(count):
            buf[i] = i + 2
        return count


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc")
def test_a_listing_that_fills_its_buffer_is_retried_then_refused():
    table = object.__new__(c.DarwinProcessTable)
    table._libproc = FakeLibproc(full=2)
    assert table.pids() == list(range(2, 14))
    table._libproc = FakeLibproc(full=99)
    with pytest.raises(ContainmentError):
        table.pids()


def test_concurrent_launches_of_one_label_cannot_both_proceed(tmp_path):
    from openswap.locking import FileLock

    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    holder = FileLock(tmp_path / "locks" / f"{c.job_label('a' * 32)}.lock", timeout=0)
    assert holder.acquire(timeout=0)
    try:
        with pytest.raises(ContainmentError) as error:
            launch(containment, root)
    finally:
        holder.release()
    assert error.value.code == "job_label_in_use" and error.value.launched is False
    assert launchd.calls == []


def test_pre_bootstrap_write_failures_are_unlaunched(tmp_path, monkeypatch):
    containment, procs, launchd = make(tmp_path)

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(c, "write_private", full_disk)
    with pytest.raises(ContainmentError) as error:
        launch(containment, private_dir(tmp_path))
    assert error.value.code == "run_dir_unwritable" and error.value.launched is False
    assert not any(call[0] == "bootstrap" for call in launchd.calls)


def test_a_handle_write_failure_after_bootstrap_is_unlaunched_and_unloaded(tmp_path, monkeypatch):
    containment, procs, launchd = make(tmp_path)
    original = c._save_handle
    calls = []

    def failing_after_first(handle):
        calls.append(handle)
        if len(calls) > 1:
            raise OSError(28, "No space left on device")
        original(handle)

    monkeypatch.setattr(c, "_save_handle", failing_after_first)
    with pytest.raises(ContainmentError) as error:
        launch(containment, private_dir(tmp_path))
    assert error.value.launched is False
    assert launchd.loaded == {}


def test_a_symlink_in_the_run_dir_is_refused_and_tmp_is_allowed(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    run_dir = root / ("a" * 32)
    run_dir.mkdir(mode=0o700)
    (run_dir / "tmp").mkdir(mode=0o700)
    handle = launch(containment, root)
    assert handle.released
    containment, procs, launchd = make(tmp_path)
    run_dir = root / ("b" * 32)
    run_dir.mkdir(mode=0o700)
    (run_dir / "stderr.log").symlink_to(tmp_path / "victim")
    with pytest.raises(ContainmentError) as error:
        launch(containment, root, job_id="b" * 32)
    assert error.value.code == "run_dir_not_fresh"


def test_a_stale_stop_never_boots_out_a_replacement_service(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    old = launch(containment, root)
    assert containment.stop(old).stopped is True
    # A replacement launch of the same label from another run directory.
    other_root = private_dir(tmp_path, "runs2")
    new = launch(containment, other_root)
    launchd.calls.clear()
    proof = containment.stop(old)
    assert proof.stopped is True  # the old job's coalition is empty and its service gone
    assert not any(call[0] == "bootout" for call in launchd.calls)
    assert c.job_label("a" * 32) in launchd.loaded and new.leader_pid in procs.table


def test_stop_waits_for_the_label_lock(tmp_path):
    from openswap.locking import FileLock

    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    handle = launch(containment, root)
    holder = FileLock(tmp_path / "locks" / f"{handle.label}.lock", timeout=0)
    assert holder.acquire(timeout=0)
    try:
        assert containment.stop(handle, timeout=0.2).stopped is False
    finally:
        holder.release()
    assert containment.stop(handle).stopped is True


def test_label_locks_are_shared_across_run_roots(tmp_path):
    containment, procs, launchd = make(tmp_path)
    from openswap.locking import FileLock

    holder = FileLock(tmp_path / "locks" / f"{c.job_label('a' * 32)}.lock", timeout=0)
    assert holder.acquire(timeout=0)
    try:
        for name in ("runs-a", "runs-b"):
            with pytest.raises(ContainmentError) as error:
                launch(containment, private_dir(tmp_path, name))
            assert error.value.code == "job_label_in_use"
    finally:
        holder.release()


def test_ownership_needs_the_exact_plist_path(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    nested = root / ("a" * 32)
    handle = launch(containment, root)
    assert containment._owns_label(handle) is True
    # A service whose plist lives under a path that merely starts with this one.
    launchd.plists[handle.label] = str(nested / "retry" / handle.plist_path.name)
    assert containment._owns_label(handle) is False
    # The same run directory relaunched: a different launch, a different plist.
    launchd.plists[handle.label] = str(nested / "job-0123456789abcdef.plist")
    assert containment._owns_label(handle) is False


def test_a_live_process_with_an_unreadable_coalition_blocks_the_proof(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    procs.live_unreadable = {77777}
    proof = containment.stop(handle, timeout=1.0)
    assert proof.stopped is False
    assert (77777, signal.SIGKILL) not in procs.signals and (77777, signal.SIGSTOP) not in procs.signals


def test_a_failing_process_table_still_cleans_up_and_proves_nothing(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    escaped = procs.new(JOB_COALITION)
    original = procs.pids
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:  # after one good scan that saw the members
            raise ContainmentError("process_table_unavailable")
        return original()

    procs.pids = flaky
    proof = containment.stop(handle, timeout=1.0)
    assert proof.stopped is False
    assert escaped not in procs.table  # killed from the last good scan
    assert launchd.loaded == {}  # the label was still booted out


def test_the_wrapper_uses_no_path_lookup_before_release(tmp_path):
    pre_release = c.WRAPPER_SCRIPT.split('"$@"')[0]
    for word in ("mv ", "sleep "):
        assert f"/bin/{word}" in pre_release
        assert pre_release.count(word) == pre_release.count(f"/bin/{word}")


def test_an_unknown_in_the_previous_scan_prevents_a_freeze(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    procs.live_unreadable = {66666}
    calls = {"n": 0}
    original = procs.pids

    def unknown_once():
        calls["n"] += 1
        if calls["n"] > 1:
            procs.live_unreadable = set()
            procs.dead_unreadable = {66666}
            procs.zombies = [66666]  # listed, now confirmed dead
        return original()

    procs.pids = unknown_once
    frozen, _ = containment._sweep(JOB_COALITION, deadline=1e9)
    # The scan right after the unknown one must not complete the freeze on its own.
    assert calls["n"] >= 3


def test_the_wrapper_environment_is_fixed_and_the_provider_gets_its_own(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    handle = containment.launch(
        job_id="a" * 32, run_dir=root / ("a" * 32), argv=["/bin/echo"],
        env={"PATH": "/x", "HOME": "/h"},
        cwd=root, stdin_text="",
    )
    plist = plistlib.loads(handle.plist_path.read_bytes())
    assert plist["EnvironmentVariables"] == {"PATH": "/usr/bin:/bin"}
    n = len(c.WRAPPER_PREFIX) + 1
    assert plist["ProgramArguments"][n:n + 4] == ["/usr/bin/env", "-i", "PATH=/x", "HOME=/h"]
    containment, procs, launchd = make(tmp_path / "other")
    with pytest.raises(ContainmentError) as error:
        containment.launch(job_id="b" * 32, run_dir=private_dir(tmp_path / "other") / ("b" * 32),
                           argv=["/bin/echo"], env={"BASH_FUNC_echo%%": "() { evil; }"},
                           cwd=tmp_path, stdin_text="")
    assert error.value.code == "environment_invalid" and error.value.launched is False


def test_a_wrapper_that_gave_up_before_go_is_unlaunched(tmp_path):
    containment, procs, launchd = make(tmp_path)
    launchd.ack = False
    procs.is_dead = lambda pid: True  # exited 125 before seeing go
    with pytest.raises(ContainmentError) as error:
        launch(containment, private_dir(tmp_path))
    assert error.value.code == "job_release_expired" and error.value.launched is False


def test_an_unreadable_leader_is_never_taken_for_an_exit(tmp_path):
    containment, procs, launchd = make(tmp_path)
    launchd.ack = False
    containment.leader_alive = lambda handle: False  # unreadable, not confirmed dead
    with pytest.raises(ContainmentError) as error:
        launch(containment, private_dir(tmp_path))
    assert error.value.launched is True


def test_two_launches_cannot_share_a_run_directory(tmp_path):
    from openswap.locking import FileLock

    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    holder = FileLock(tmp_path / "locks" / f"rundir-{c._run_dir_key(root / 'shared')}.lock", timeout=0)
    assert holder.acquire(timeout=0)
    try:
        with pytest.raises(ContainmentError) as error:
            containment.launch(job_id="b" * 32, run_dir=root / "shared", argv=["/bin/echo"], env={},
                               cwd=root, stdin_text="")
    finally:
        holder.release()
    assert error.value.code == "run_dir_in_use" and error.value.launched is False


def test_an_unacknowledged_release_is_uncertain(tmp_path):
    containment, procs, launchd = make(tmp_path)
    launchd.ack = False
    with pytest.raises(ContainmentError) as error:
        launch(containment, private_dir(tmp_path))
    assert error.value.code == "job_release_unacknowledged" and error.value.launched is True


def test_recovery_uses_the_mirror_and_refuses_disagreement(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    handle = launch(containment, root)
    escaped = procs.new(JOB_COALITION)
    (handle.run_dir / "handle.json").unlink()  # the run directory's copy is gone
    proof = containment.recover(handle.run_dir)
    assert proof is not None and proof.stopped is True and escaped not in procs.table

    containment, procs, launchd = make(tmp_path / "second")
    root = private_dir(tmp_path / "second")
    handle = launch(containment, root)
    tampered = replace(handle, coalition_id=12345)
    c._save_handle(tampered)
    procs.signals.clear()
    proof = containment.recover(handle.run_dir)
    assert proof.stopped is False and procs.signals == []


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_two_spellings_of_one_run_directory_share_its_lock(tmp_path):
    from openswap.locking import FileLock

    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    (root / "shared").mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(root)
    canonical = Path(os.path.realpath(root / "shared"))
    holder = FileLock(tmp_path / "locks" / f"rundir-{c._run_dir_key(canonical)}.lock", timeout=0)
    assert holder.acquire(timeout=0)
    try:
        with pytest.raises(ContainmentError) as error:
            containment.launch(job_id="b" * 32, run_dir=alias / "shared", argv=["/bin/echo"], env={},
                               cwd=root, stdin_text="")
    finally:
        holder.release()
    assert error.value.code == "run_dir_in_use"


def test_a_partial_persistence_update_is_merged_not_refused(tmp_path):
    base = JobHandle(c.job_label("a" * 32), "gui/501", tmp_path, "boot-a", launch_id="0" * 16)
    later = replace(base, leader_pid=50, coalition_id=900, released=True)
    assert c.reconcile_handles(base, later) == later
    assert c.reconcile_handles(later, base) == later
    assert c.reconcile_handles(later, replace(later, coalition_id=901)) is None
    assert c.reconcile_handles(later, replace(later, launch_id="1" * 16)) is None
    assert c.reconcile_handles(None, base) == base


def test_a_stale_stop_keeps_a_replacement_launchs_mirror(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    old = launch(containment, root)
    assert containment.stop(old).stopped is True
    for name in os.listdir(old.run_dir):
        path = old.run_dir / name
        path.unlink() if not path.is_dir() else None
    new = containment.launch(job_id="c" * 32, run_dir=old.run_dir, argv=["/bin/echo"], env={},
                             cwd=root, stdin_text="")
    containment.stop(old)
    assert containment._load_mirror(new.run_dir) is not None



def test_a_pid_seen_alive_before_and_gone_now_is_not_harmless(tmp_path):
    containment, procs, launchd = make(tmp_path)
    # An exited pid has no identity left: never harmless, even if seen before.
    assert containment._complete({5: "gone"}, {5: "gone"}) is False
    assert containment._complete({5: "gone"}, {5: "other"}) is False
    # A zombie is harmless only if the very same zombie was seen last time.
    assert containment._complete({5: "zombie@1.000001"}, {5: "zombie@1.000001"}) is True
    assert containment._complete({5: "zombie@2.000001"}, {5: "zombie@1.000001"}) is False
    assert containment._complete({5: "zombie@1.000001"}, {5: "gone"}) is False
    assert containment._complete({5: "zombie@1.000001"}, {}) is False


def test_exit_status_belongs_to_its_own_launch(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    old = launch(containment, root)
    (old.run_dir / "exit").write_text("0\n")
    assert containment.exit_status(old) == 0
    c._save_handle(replace(old, launch_id="f" * 16))  # the directory now belongs to another launch
    assert containment.exit_status(old) is None



def test_exit_status_is_unknown_while_a_launch_holds_the_run_directory(tmp_path):
    from openswap.locking import FileLock

    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    (handle.run_dir / "exit").write_text("0\n")
    held = FileLock(containment._lock_dir / f"rundir-{c._run_dir_key(handle.run_dir)}.lock", timeout=0)
    assert held.acquire(timeout=0)
    try:
        assert containment.exit_status(handle) is None
    finally:
        held.release()
    assert containment.exit_status(handle) == 0
    (handle.run_dir / "handle.json").unlink()  # cleaned away: no longer provably this launch's
    assert containment.exit_status(handle) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="kern.proc.pid")
def test_a_real_zombie_has_a_stable_identity_and_an_exited_pid_none():
    procs = c.DarwinProcessTable()
    child = subprocess.Popen(["/usr/bin/true"])  # not reaped until wait(): a zombie
    pid = child.pid
    try:
        deadline = time.monotonic() + 5
        identity = procs.zombie_identity(pid)
        while identity is None and time.monotonic() < deadline:
            time.sleep(0.01)
            identity = procs.zombie_identity(pid)
        assert identity is not None and procs.zombie_identity(pid) == identity
        assert procs.zombie_identity(os.getpid()) is None  # alive: not a zombie
    finally:
        child.wait()
    assert procs.zombie_identity(pid) is None



def test_polling_exit_status_recreates_the_lock_directory_privately(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    (handle.run_dir / "exit").write_text("0\n")
    for lock in containment._lock_dir.iterdir():
        lock.unlink()
    containment._lock_dir.rmdir()  # e.g. the cache folder was cleared
    assert containment.exit_status(handle) == 0
    if os.name == "posix":
        assert (containment._lock_dir.stat().st_mode & 0o777) == 0o700
    assert containment.stop(handle).stopped is True  # later operations still work



def test_a_stop_that_hit_a_recycled_pid_is_undone(tmp_path):
    containment, procs, launchd = make(tmp_path)
    member = procs.new(JOB_COALITION)
    original = procs.signal

    def recycle_then_signal(pid, signum):
        if pid == member and signum == signal.SIGSTOP and procs.table[pid][0] == JOB_COALITION:
            procs.table[pid] = [333, 2]  # exited; the pid now belongs to someone else
        original(pid, signum)

    procs.signal = recycle_then_signal
    containment._stop_member(JOB_COALITION, member)
    assert procs.table[member] == [333, 2]  # resumed, not left stopped
    assert (member, signal.SIGCONT) in procs.signals


def test_sigkill_only_reaches_stopped_members(tmp_path):
    containment, procs, launchd = make(tmp_path)
    running = procs.new(JOB_COALITION)
    assert containment._kill_stopped_member(JOB_COALITION, running) is False
    assert (running, signal.SIGKILL) not in procs.signals
    procs.signal(running, signal.SIGSTOP)
    assert containment._kill_stopped_member(JOB_COALITION, running) is True
    assert running not in procs.table


def test_a_run_directory_with_a_line_break_never_launches(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    with pytest.raises(ContainmentError) as error:
        containment.launch(job_id="a" * 32, run_dir=root / "bad\nname", argv=["/bin/echo"], env={}, cwd=root,
                           stdin_text="")
    assert error.value.code == "run_dir_unsafe" and error.value.launched is False
    assert launchd.loaded == {}



def test_two_spellings_of_one_directory_share_its_locks(tmp_path):
    folder = tmp_path / "Runs" / "Job"
    folder.mkdir(parents=True)
    other = tmp_path / "runs" / "job"
    if not other.exists():
        pytest.skip("case-sensitive file system")
    assert c.canonical_dir(other) == c.canonical_dir(folder)
    assert c._run_dir_key(c.canonical_dir(other)) == c._run_dir_key(c.canonical_dir(folder))
    assert c.canonical_dir(other).name == "Job"


def test_a_second_spelling_cannot_launch_into_a_running_directory(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    first = launch(containment, root)
    lower = Path(str(first.run_dir).swapcase()) if sys.platform == "darwin" else None
    if lower is None or not lower.exists():
        pytest.skip("case-sensitive file system")
    held = containment._run_dir_lock(c.canonical_dir(lower), 0)
    assert held.acquire(timeout=0) is True  # same lock file as the first launch's, now free
    held.release()
    assert c.canonical_dir(lower) == first.run_dir



def test_a_stopped_pid_whose_coalition_becomes_unreadable_is_resumed(tmp_path):
    containment, procs, launchd = make(tmp_path)
    member = procs.new(JOB_COALITION)
    original = procs.signal

    def recycle_unreadable(pid, signum):
        if pid == member and signum == signal.SIGSTOP and procs.table[pid][0] == JOB_COALITION:
            procs.table[pid] = [333, 2]
            procs.live_unreadable.add(pid)  # a live process whose coalition cannot be read
        original(pid, signum)

    procs.signal = recycle_unreadable
    containment._stop_member(JOB_COALITION, member)
    assert (member, signal.SIGCONT) in procs.signals and procs.table[member][1] == 2



def test_recover_waits_for_an_in_progress_launch_before_reading_its_handle(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    held = containment._run_dir_lock(handle.run_dir, 0)
    assert held.acquire(timeout=0)
    containment.RECOVER_LOCK_WAIT = 0
    try:
        # A launch still holds the directory: no snapshot, no proof, nothing touched.
        assert containment.recover(handle.run_dir) == c.StopProof(False, None, 0)
        assert handle.label in launchd.loaded
    finally:
        held.release()
    assert containment.recover(handle.run_dir).stopped is True



def test_an_incomplete_scan_cannot_anchor_a_freeze(tmp_path):
    containment, procs, launchd = make(tmp_path)
    scans = iter([{5001: "gone"}, {}, {}])
    containment._scan = lambda coalition_id: next(scans, {})
    seen = []
    original = containment._complete

    def complete(scan, previous):
        seen.append((dict(scan), None if previous is None else dict(previous)))
        return original(scan, previous)

    containment._complete = complete
    frozen, _ = containment._sweep(JOB_COALITION, deadline=1e9)
    assert frozen is True
    # The freeze was proven only after two complete scans, not on {} after {gone}.
    assert len(seen) >= 3


def test_locks_and_mirrors_live_outside_the_cache_folder():
    lock_dir = c.default_lock_dir()
    assert "Caches" not in lock_dir.parts and lock_dir.parts[-3:] == (
        "Application Support", "com.opensoft.openswap", "job-locks")



def test_a_member_turning_zombie_mid_scan_makes_the_scan_incomplete(tmp_path):
    containment, procs, launchd = make(tmp_path)
    zombie = procs.new(JOB_COALITION, status=5)
    first = containment._scan(JOB_COALITION)
    assert first[zombie].startswith("zombie@")
    assert containment._complete(first, {}) is False  # newly seen: it may have forked
    assert containment._complete(containment._scan(JOB_COALITION), first) is True  # the same zombie again


def test_a_failed_bootstrap_never_boots_out_someone_elses_service(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    label = c.job_label("a" * 32)
    calls = []
    real = containment._launchctl

    def launchctl(args):
        calls.append(list(args))
        if args[0] == "bootstrap":
            # A competing process loaded the label from its own plist first.
            launchd.loaded[label] = 4242
            return subprocess.CompletedProcess(args, 5, "", "")
        if args[0] == "print" and label in launchd.loaded:
            return subprocess.CompletedProcess(args, 0, "\tpath = /elsewhere/other.plist\n\tpid = 4242\n", "")
        return real(args)

    containment._launchctl = launchctl
    with pytest.raises(ContainmentError) as error:
        containment.launch(job_id="a" * 32, run_dir=root / ("a" * 32), argv=["/bin/echo"], env={}, cwd=root,
                           stdin_text="")
    assert error.value.code == "launchd_bootstrap_failed"
    assert not any(call[0] == "bootout" for call in calls)



def test_unparseable_print_output_proves_no_ownership(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    real = containment._launchctl

    def launchctl(args):
        if args[0] == "print":
            return subprocess.CompletedProcess(args, 0, "{ a new format without a path line }\n", "")
        return real(args)

    containment._launchctl = launchctl
    assert containment._owns_label(handle) == c.OWNERSHIP_UNKNOWN
    proof = containment.stop(handle)
    assert proof.stopped is False and proof.label_loaded is None


def test_a_leader_pid_recycled_before_the_coalition_lookup_is_refused(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    real = containment._launchctl
    prints = {"n": 0}

    def launchctl(args):
        result = real(args)
        if args[0] == "print" and result.returncode == 0:
            prints["n"] += 1
            if prints["n"] == 2:  # the re-check after the coalition lookup
                return subprocess.CompletedProcess(args, 0, result.stdout.replace("pid = ", "pid = 9"), "")
        return result

    containment._launchctl = launchctl
    with pytest.raises(ContainmentError) as error:
        containment.launch(job_id="a" * 32, run_dir=root / ("a" * 32), argv=["/bin/echo"], env={}, cwd=root,
                           stdin_text="")
    assert error.value.code == "job_leader_unverified" and error.value.launched is False



def test_an_unreadable_worker_coalition_refuses_the_launch(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    procs.live_unreadable.add(WORKER_PID)  # the worker's own coalition cannot be read
    procs.table[WORKER_PID] = [WORKER_COALITION, 2]
    with pytest.raises(ContainmentError) as error:
        containment.launch(job_id="a" * 32, run_dir=root / ("a" * 32), argv=["/bin/echo"], env={}, cwd=root,
                           stdin_text="")
    assert error.value.code == "job_coalition_unavailable" and error.value.launched is False



def test_a_listed_pid_now_held_by_a_newer_process_makes_the_scan_incomplete(tmp_path):
    containment, procs, launchd = make(tmp_path)
    other = procs.new(333)
    containment._wall = lambda: 1000.0
    procs.started[other] = 999.0  # started before the listing: the listed process
    assert containment._scan(JOB_COALITION)[other] == "other"
    procs.started[other] = 1000.5  # started after the listing began: the pid was reused
    scan = containment._scan(JOB_COALITION)
    assert scan[other] == "gone" and containment._complete(scan, scan) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="kern.proc.pid")
def test_real_start_times_are_wall_clock():
    procs = c.DarwinProcessTable()
    started = procs.start_time(os.getpid())
    assert started is not None and started <= time.time()



def test_the_mirror_is_found_by_another_spelling_after_the_directory_is_gone(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    handle = launch(containment, root)
    import shutil

    shutil.rmtree(handle.run_dir)  # the whole run directory was deleted
    # Another spelling of its (still existing) parent: canonicalized through the parent.
    other_spelling = Path(str(handle.run_dir.parent).swapcase()) / handle.run_dir.name
    if not other_spelling.parent.exists():
        pytest.skip("case-sensitive file system")
    assert c.canonical_dir(other_spelling) == handle.run_dir
    assert containment.recover(other_spelling) is not None


def test_directories_differing_only_in_case_never_share_a_key():
    assert c._run_dir_key(Path("/runs/Job")) != c._run_dir_key(Path("/runs/job"))



def test_a_mixed_case_run_directory_name_never_launches(tmp_path):
    containment, procs, launchd = make(tmp_path)
    root = private_dir(tmp_path)
    with pytest.raises(ContainmentError) as error:
        containment.launch(job_id="a" * 32, run_dir=root / "Job", argv=["/bin/echo"], env={}, cwd=root,
                           stdin_text="")
    assert error.value.code == "run_dir_unsafe" and launchd.loaded == {}


def test_a_deleted_run_directory_is_found_from_an_uppercase_spelling(tmp_path):
    containment, procs, launchd = make(tmp_path)
    handle = launch(containment, private_dir(tmp_path))
    import shutil

    shutil.rmtree(handle.run_dir)
    upper = handle.run_dir.parent / handle.run_dir.name.upper()
    if not c._case_insensitive(str(handle.run_dir.parent)):
        pytest.skip("case-sensitive file system")
    assert c.canonical_dir(upper) == handle.run_dir
    assert containment.recover(upper) is not None
