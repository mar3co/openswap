#!/usr/bin/env python3
"""Credential-free feasibility harness for plan 017's Codex host spike.

This script intentionally has no authenticated Codex execution path. It can
inspect ``codex --version`` and ``codex exec --help`` in a disposable home, and
it can exercise process supervision with a caller-supplied fake executable.
Neither proves provider login, account isolation, web research, refresh, or
Codex cancellation semantics.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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
from typing import Sequence

DISCOVERED_CODEX_VERSION = "codex-cli 0.158.0-alpha.2.1"
LIVE_CODEX_ENABLED = False
MAX_RETAINED_EVENT_NAMES = 256
MAX_EVENT_LINE_CHARS = 64 * 1024
EVENT_NAMES_TRUNCATED = "event-names-truncated"
_EVENT_NAME = re.compile(r"^[a-zA-Z0-9_.-]{1,80}$")


class SpikeError(RuntimeError):
    """A fail-closed harness error suitable for display to an operator."""


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _append_jsonl(path: Path, record: dict) -> None:
    _private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _safe_event_name(value: object) -> str:
    name = str(value or "unknown")
    return name if _EVENT_NAME.fullmatch(name) else "redacted-event"


def _latest(records: list[dict], job_id: str) -> dict | None:
    return next((row for row in reversed(records) if row.get("job_id") == job_id), None)


def recover_uncertain_runs(state_dir: Path) -> list[str]:
    """Mark intents without terminal outcomes interrupted; never relaunch."""
    journal = state_dir / "journal.jsonl"
    latest: dict[str, dict] = {}
    for row in _read_jsonl(journal):
        job_id = row.get("job_id")
        if isinstance(job_id, str):
            latest[job_id] = row
    interrupted = []
    for job_id, row in latest.items():
        if row.get("state") in {"starting", "running", "cancel_requested"}:
            _append_jsonl(
                journal,
                {"job_id": job_id, "state": "interrupted", "reason": "uncertain_after_restart"},
            )
            _append_jsonl(
                state_dir / "evidence.jsonl",
                {"kind": "recovery", "state": "interrupted", "reason": "uncertain_after_restart"},
            )
            interrupted.append(job_id)
    return interrupted


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
    return wait_uncertain or process.poll() is None or _group_running(process.pid, reader)


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
    """Run only an explicit fake command in its own cancellable process group."""
    if not argv or timeout_s <= 0 or grace_s < 0:
        raise SpikeError("A command, positive timeout, and non-negative grace period are required.")
    if not Path(argv[0]).is_absolute():
        raise SpikeError("Fake executable must be an absolute path.")
    state_dir = state_dir.expanduser().absolute()
    _private_dir(state_dir)
    job_id = job_id or str(uuid.uuid4())
    journal = state_dir / "journal.jsonl"
    if _latest(_read_jsonl(journal), job_id) is not None:
        raise SpikeError("This job ID already exists; uncertain or completed work is never replayed.")

    # The durable intent precedes launch. A restart from here becomes uncertain.
    _append_jsonl(journal, {"job_id": job_id, "state": "starting"})
    _append_jsonl(state_dir / "evidence.jsonl", {"kind": "launch_intent", "state": "starting"})
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
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
        _append_jsonl(journal, {"job_id": job_id, "state": "running"})
        reader.start()
    except BaseException:
        _terminate_group(process, reader, grace_s=grace_s)
        raise
    deadline = time.monotonic() + timeout_s
    timed_out = False
    cleanup_uncertain = False
    leftovers = False
    unexpected_leftovers = False
    try:
        while process.poll() is None and time.monotonic() < deadline:
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
    except BaseException:
        _terminate_group(process, reader, grace_s=grace_s)
        raise

    reader.join(timeout=1)
    if reader.is_alive() or cleanup_uncertain or unexpected_leftovers:
        final_state = "interrupted"
    elif timed_out:
        final_state = "cancelled"
    else:
        final_state = "succeeded" if process.returncode == 0 else "failed"
    _append_jsonl(journal, {"job_id": job_id, "state": final_state})
    _append_jsonl(
        state_dir / "evidence.jsonl",
        {"kind": "supervision_result", "state": final_state, "returncode": process.returncode,
         "event_names": event_names},
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
            version = subprocess.run([str(binary), "--version"], env=env, cwd=temp,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, timeout=10, check=False)
            help_result = subprocess.run([str(binary), "exec", "--help"], env=env, cwd=temp,
                                         stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        raise SpikeError("Codex version/help probe could not complete.") from None
    version_text = version.stdout.strip()
    help_text = help_result.stdout + help_result.stderr
    if version.returncode != 0 or help_result.returncode != 0 or not version_text:
        raise SpikeError("Codex version/help probe failed; details were not retained.")
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
        command = (
            f"cat {shlex.quote(str(workspace / 'inside.txt'))}; "
            f"cat {shlex.quote(str(outside / 'outside.txt'))}; "
            f"printf inside-write > {shlex.quote(str(workspace / 'write-test.txt'))}; "
            f"printf outside-write > {shlex.quote(str(outside / 'write-test.txt'))}"
        )
        try:
            result = subprocess.run(
                [binary, "sandbox", "--permission-profile", "research-test", "--log-denials",
                 "--cd", str(workspace), "/bin/sh", "-c", command],
                env=env, cwd=temp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=20, check=False,
            )
            auth_read = subprocess.run(
                [binary, "sandbox", "--permission-profile", "research-test", "--log-denials",
                 "--cd", str(workspace), "/bin/sh", "-c",
                 'cat "$CODEX_HOME/auth.json"; cat "$CODEX_HOME/config.toml"'],
                env=env, cwd=temp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=20, check=False,
            )
        except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
            raise SpikeError("Codex sandbox probe could not complete.") from None
        combined = result.stdout + result.stderr
        auth_combined = auth_read.stdout + auth_read.stderr
        observed = {
            "kind": "low_level_sandbox_probe",
            "exec_or_model_run": False,
            "inside_read_allowed": "workspace-sentinel" in combined,
            "outside_read_denied": "outside-sentinel" not in combined and "Operation not permitted" in combined,
            "inside_write_allowed": (workspace / "write-test.txt").exists(),
            "outside_write_denied": not (outside / "write-test.txt").exists(),
            "codex_home_auth_and_config_denied": (
                auth_read.returncode != 0
                and "Operation not permitted" in auth_combined
                and "synthetic-auth-sentinel" not in auth_combined
                and "synthetic-config-sentinel" not in auth_combined
            ),
        }
        if result.returncode == 0 or auth_read.returncode == 0:
            raise SpikeError("Sandbox probe produced an unexpected success; details were not retained.")
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
