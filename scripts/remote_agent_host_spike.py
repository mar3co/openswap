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


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _collect_event_names(stream, collected: list[str]) -> None:
    # Drain while running so even a noisy fake cannot fill the pipe and block
    # the leader before the timeout. Only event names survive this thread.
    for line in stream:
        try:
            item = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            collected.append("unstructured-output")
            continue
        if isinstance(item, dict):
            collected.append(_safe_event_name(item.get("type")))
        else:
            collected.append("non-object-event")


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
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        _append_jsonl(journal, {"job_id": job_id, "state": "failed", "reason": "fake_start_error"})
        raise SpikeError(f"Fake executable could not be started ({type(exc).__name__}).") from None

    _append_jsonl(journal, {"job_id": job_id, "state": "running"})
    event_names: list[str] = []
    reader = threading.Thread(
        target=_collect_event_names, args=(process.stdout, event_names), daemon=True
    )
    reader.start()
    deadline = time.monotonic() + timeout_s
    timed_out = False
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    if process.poll() is None:
        timed_out = True
        _append_jsonl(journal, {"job_id": job_id, "state": "cancel_requested"})
        _signal_group(process, signal.SIGTERM)
        grace_deadline = time.monotonic() + grace_s
        while _group_exists(process.pid) and time.monotonic() < grace_deadline:
            time.sleep(0.01)
        # The group leader can exit while descendants ignore TERM. Escalate
        # against the original process group even if Popen.wait() has returned.
        if _group_exists(process.pid):
            _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        process.wait()
    if timed_out and _group_exists(process.pid):
        _signal_group(process, signal.SIGKILL)
    reader.join(timeout=2)
    if timed_out and reader.is_alive():
        final_state = "interrupted"
    else:
        final_state = "cancelled" if timed_out else ("succeeded" if process.returncode == 0 else "failed")
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
