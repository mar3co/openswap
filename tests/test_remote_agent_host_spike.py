from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

import scripts.remote_agent_host_spike as spike
from scripts.remote_agent_host_spike import (
    SpikeError,
    inspect_codex,
    recover_uncertain_runs,
    run_live_codex,
    supervise_fake_command,
)

def _executable(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.mark.skipif(os.name != "posix", reason="fake executable shebang test is POSIX-only")
def test_inspect_codex_uses_only_disposable_home_and_records_help(tmp_path):
    fake = _executable(
        tmp_path / "fake-codex",
        "import os, sys\n"
        "assert os.environ['CODEX_HOME'] == os.path.join(os.environ['HOME'], 'codex')\n"
        "if sys.argv[1:] == ['--version']:\n"
        " print('codex-cli 99.0-test')\n"
        "elif sys.argv[1:] == ['exec', '--help']:\n"
        " print('--json --sandbox --skip-git-repo-check --ignore-user-config')\n"
        " print('  --api-key secret-token-value   (example value in help output)')\n"
        "else: raise SystemExit(4)\n",
    )

    result = inspect_codex(str(fake), tmp_path / "evidence")

    assert result == {
        "version": "codex-cli 99.0-test",
        "expected_version": "codex-cli 0.158.0-alpha.2.1",
        "matches_discovered_pin": False,
        "pre_release": False,
        "exec_json": True,
        "skip_git_repo_check": True,
        "ignore_user_config": True,
        "sandbox_option": True,
        "no_auth_performed": True,
    }
    stored = (tmp_path / "evidence" / "evidence.jsonl").read_text()
    assert "codex-cli 99.0-test" in stored
    assert "secret-token-value" not in stored
    assert "secret" not in stored.lower()
    assert not (tmp_path / "evidence" / "journal.jsonl").exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group supervision is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_timeout_terminates_fake_process_group_including_child(tmp_path, monkeypatch):
    fake = _executable(
        tmp_path / "fake-provider",
        "import os, pathlib, signal, subprocess, sys, time\n"
        "root = pathlib.Path(os.environ['SPIKE_MARKERS'])\n"
        "def stop(_sig, _frame):\n"
        " (root / f'{os.getpid()}.stopped').touch()\n"
        " raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "code = \"import os,pathlib,signal,time; p=pathlib.Path(os.environ['SPIKE_MARKERS']); alive=p/f'{os.getpid()}.alive'; stopped=p/f'{os.getpid()}.stopped'; alive.touch(); signal.signal(signal.SIGTERM, lambda s,f: (stopped.touch(), alive.unlink(), (_ for _ in ()).throw(SystemExit(0)))); (p/f'{os.getpid()}.pid').write_text(str(os.getpid())); time.sleep(60)\"\n"
        "child = subprocess.Popen([sys.executable, '-c', code])\n"
        "(root / f'{os.getpid()}.pid').write_text(str(os.getpid()))\n"
        "(root / f'{os.getpid()}.alive').touch()\n"
        "(root / 'child.pid').write_text(str(child.pid))\n"
        "time.sleep(60)\n",
    )
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("SPIKE_MARKERS", str(markers))
    result = supervise_fake_command(
        [str(fake)], state_dir=tmp_path / "state", timeout_s=2.0, grace_s=1.0,
        job_id="kill-group",
    )

    child_pid = (markers / "child.pid").read_text()
    assert result["state"] == "cancelled"
    deadline = time.monotonic() + 2
    while not (markers / f"{child_pid}.stopped").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (markers / f"{child_pid}.stopped").exists()
    assert not (markers / f"{child_pid}.alive").exists()
    journal = [json.loads(line) for line in (tmp_path / "state" / "journal.jsonl").read_text().splitlines()]
    assert [row["state"] for row in journal] == ["starting", "running", "cancel_requested", "cancelled"]
    evidence = [
        json.loads(line)
        for line in (tmp_path / "state" / "evidence.jsonl").read_text().splitlines()
    ]
    assert [(row["kind"], row["job_id"]) for row in evidence] == [
        ("launch_intent", "kill-group"),
        ("supervision_result", "kill-group"),
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_s", float("nan")),
        ("timeout_s", float("inf")),
        ("grace_s", float("nan")),
        ("grace_s", float("inf")),
    ],
)
def test_non_finite_supervision_durations_refuse_before_state_or_launch(tmp_path, field, value):
    launches = tmp_path / "launched"
    fake = _executable(
        tmp_path / "fake-provider",
        f"import pathlib; pathlib.Path({str(launches)!r}).touch()\n",
    )
    state = tmp_path / "state"
    options = {"timeout_s": 1.0, "grace_s": 0.5}
    options[field] = value

    with pytest.raises(SpikeError, match="finite"):
        supervise_fake_command([str(fake)], state_dir=state, **options)

    assert not state.exists()
    assert not launches.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions are required")
def test_unsafe_existing_state_directory_permissions_are_preserved_and_refused(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o750)
    state.chmod(0o750)
    launches = tmp_path / "launch-count"
    fake = _executable(
        tmp_path / "must-not-run",
        f"import pathlib; pathlib.Path({str(launches)!r}).touch()\n",
    )

    with pytest.raises(SpikeError, match="must not grant group or world access"):
        supervise_fake_command([str(fake)], state_dir=state, timeout_s=1)

    assert state.stat().st_mode & 0o777 == 0o750
    assert not (state / "journal.jsonl").exists()
    assert not launches.exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group supervision is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_timeout_kills_child_that_ignores_term_before_reporting_cancelled(tmp_path, monkeypatch):
    child_script = tmp_path / "ignore-term-child.py"
    child_script.write_text(
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "root = pathlib.Path(os.environ['SPIKE_MARKERS'])\n"
        "(root / 'child.pid').write_text(str(os.getpid()))\n"
        "beat = root / 'heartbeat'\n"
        "while True:\n"
        " beat.write_text(str(time.monotonic_ns()))\n"
        " time.sleep(0.03)\n",
        encoding="utf-8",
    )
    fake = _executable(
        tmp_path / "fake-provider-ignoring-term",
        "import os, pathlib, signal, subprocess, sys, time\n"
        "root = pathlib.Path(os.environ['SPIKE_MARKERS'])\n"
        "def stop(_sig, _frame):\n"
        " (root / 'parent.stopped').touch()\n"
        " raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"child = subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        "(root / 'parent.pid').write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
    )
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("SPIKE_MARKERS", str(markers))
    result = supervise_fake_command(
        [str(fake)], state_dir=tmp_path / "state", timeout_s=2.0, grace_s=0.2,
        job_id="kill-ignoring-child",
    )

    beat = markers / "heartbeat"
    first_value = beat.read_text()
    time.sleep(0.15)
    assert beat.read_text() == first_value
    assert (markers / "parent.stopped").exists()
    assert result["state"] == "cancelled"


@pytest.mark.skipif(os.name != "posix", reason="process-group supervision is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_successful_parent_with_live_child_is_interrupted_and_child_is_killed(tmp_path, monkeypatch):
    child_script = tmp_path / "orphan-child.py"
    child_script.write_text(
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "root = pathlib.Path(os.environ['SPIKE_MARKERS'])\n"
        "beat = root / 'heartbeat'\n"
        "while True:\n"
        " beat.write_text(str(time.monotonic_ns()))\n"
        " time.sleep(0.03)\n",
        encoding="utf-8",
    )
    fake = _executable(
        tmp_path / "fake-parent-exits-successfully",
        "import os, pathlib, subprocess, sys, time\n"
        "root = pathlib.Path(os.environ['SPIKE_MARKERS'])\n"
        f"subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        "deadline = time.monotonic() + 2\n"
        "while not (root / 'heartbeat').exists() and time.monotonic() < deadline:\n"
        " time.sleep(0.01)\n"
        "raise SystemExit(0)\n",
    )
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("SPIKE_MARKERS", str(markers))
    result = supervise_fake_command(
        [str(fake)], state_dir=tmp_path / "state", timeout_s=3, grace_s=0.15,
        job_id="leader-exited",
    )

    beat = markers / "heartbeat"
    assert result["returncode"] == 0
    assert result["state"] == "interrupted"
    first_value = beat.read_text()
    time.sleep(0.15)
    assert beat.read_text() == first_value


@pytest.mark.skipif(os.name != "posix", reason="fake executable shebang test is POSIX-only")
def test_invalid_utf8_output_is_safely_recorded_as_unstructured(tmp_path):
    fake = _executable(
        tmp_path / "fake-invalid-output",
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\n')\n"
        "sys.stdout.flush()\n",
    )

    result = supervise_fake_command(
        [str(fake)], state_dir=tmp_path / "state", timeout_s=1, job_id="bad-bytes"
    )

    assert result["state"] == "succeeded"
    assert result["event_names"] == ["unstructured-output"]


@pytest.mark.skipif(os.name != "posix", reason="fake executable shebang test is POSIX-only")
def test_json_parser_failures_are_recorded_and_reader_continues(tmp_path, monkeypatch):
    original_loads = spike.json.loads

    def raise_for_deep_input(value, *args, **kwargs):
        if isinstance(value, str) and value.startswith("[" * 100):
            raise RecursionError("synthetic deep-input parser failure")
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(spike.json, "loads", raise_for_deep_input)
    fake = _executable(
        tmp_path / "fake-malformed-json",
        "import json, sys\n"
        "sys.stdout.write('9' * 5000 + '\\n')\n"
        "sys.stdout.write('[' * 6000 + '0' + ']' * 6000 + '\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'after'}) + '\\n')\n"
        "sys.stdout.flush()\n",
    )

    result = supervise_fake_command(
        [str(fake)], state_dir=tmp_path / "state", timeout_s=1, job_id="bad-json"
    )

    assert result["state"] == "succeeded"
    assert result["event_names"] == [
        "unstructured-output",
        "unstructured-output",
        "after",
    ]


@pytest.mark.skipif(os.name != "posix", reason="process-group supervision is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_noisy_fake_output_is_bounded_and_cancellation_still_completes(tmp_path):
    fake = _executable(
        tmp_path / "fake-noisy-provider",
        "import json, sys, time\n"
        f"sys.stdout.write('x' * ({spike.MAX_EVENT_LINE_CHARS} + 100_000) + '\\n')\n"
        "for i in range(10000):\n"
        " sys.stdout.write(json.dumps({'type': f'event_{i}'}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n",
    )

    result = supervise_fake_command(
        [str(fake)], state_dir=tmp_path / "state", timeout_s=2, grace_s=0.2,
        job_id="noisy-output",
    )

    assert result["state"] == "cancelled"
    assert result["returncode"] < 0
    assert result["event_names"][0] == "unstructured-output"
    assert result["event_names"].count(spike.EVENT_NAMES_TRUNCATED) == 1
    assert len(result["event_names"]) <= spike.MAX_RETAINED_EVENT_NAMES + 1
    journal = [
        json.loads(line)
        for line in (tmp_path / "state" / "journal.jsonl").read_text().splitlines()
    ]
    assert journal[-1]["state"] == "cancelled"


@pytest.mark.skipif(os.name != "posix", reason="killpg process groups are POSIX-only")
def test_unavailable_group_enumeration_keeps_existing_group_uncertain(monkeypatch):
    signaled = []
    monkeypatch.setattr(spike, "_group_has_running_members", lambda _pgid: None)
    monkeypatch.setattr(spike.os, "killpg", lambda pgid, sig: signaled.append((pgid, sig)))

    assert spike._group_running(4321, threading.Thread()) is True
    assert signaled == [(4321, 0)]


@pytest.mark.parametrize("stage", ["version", "exec_help"])
@pytest.mark.parametrize("failure", ["os_error", "timeout", "unicode_error"])
def test_inspect_cli_sanitizes_probe_launch_and_timeout_errors(
    tmp_path, monkeypatch, capsys, stage, failure
):
    fake = tmp_path / "fake-codex"
    fake.touch()
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        current_stage = "version" if command[-1] == "--version" else "exec_help"
        if current_stage == stage:
            _raise_probe_failure(failure, command)
        return subprocess.CompletedProcess(command, 0, "codex-cli 1.0-test\n", "")

    monkeypatch.setattr(spike, "_run_probe", fake_run)

    code = spike._main([
        "inspect", "--codex-bin", str(fake), "--evidence-dir", str(tmp_path / "evidence")
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "refused: Codex version/help probe could not complete.\n"
    assert "raw-secret-marker" not in captured.err
    assert str(fake) not in captured.err
    assert "Traceback" not in captured.err
    assert len(calls) == (1 if stage == "version" else 2)


def _raise_probe_failure(failure, command):
    if failure == "os_error":
        raise OSError("raw-secret-marker /private/tmp/raw-secret-path")
    if failure == "timeout":
        raise subprocess.TimeoutExpired(command, timeout=0.1, output="raw-secret-marker")
    raise UnicodeDecodeError("utf-8", b"\\xffsecret", 0, 1, "raw-secret-marker")


@pytest.mark.parametrize("stage", ["first", "second"])
@pytest.mark.parametrize("failure", ["os_error", "timeout", "unicode_error"])
def test_sandbox_probe_sanitizes_subprocess_failures(
    tmp_path, monkeypatch, capsys, stage, failure
):
    fake = tmp_path / "fake-codex"
    fake.touch()
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        current_stage = "first" if len(calls) == 1 else "second"
        if current_stage == stage:
            _raise_probe_failure(failure, command)
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr(spike, "_run_probe", fake_run)

    code = spike._main([
        "sandbox-probe", "--codex-bin", str(fake), "--evidence-dir", str(tmp_path / "evidence")
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "refused: Codex sandbox probe could not complete.\n"
    assert "raw-secret-marker" not in captured.err
    assert "/private/tmp/raw-secret-path" not in captured.err
    assert str(fake) not in captured.err
    assert "Traceback" not in captured.err
    assert len(calls) == (1 if stage == "first" else 2)


def _sandbox_markers(
    operations: dict[str, int], denials: set[str] | frozenset[str] = frozenset()
) -> str:
    lines = []
    for name, status in operations.items():
        lines.extend((f"probe-start:{name}", f"probe-complete:{name}",
                      f"probe-status:{name}:{status}"))
        if name in denials:
            lines.append(f"probe-denied:{name}")
    return "\n".join(lines) + "\n"


def test_sandbox_probe_requires_each_operation_and_records_real_denial_markers(
    tmp_path, monkeypatch
):
    fake = tmp_path / "fake-codex"
    fake.touch()
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        workspace = Path(command[command.index("--cd") + 1])
        if len(calls) == 1:
            (workspace / "write-test.txt").write_text("inside-write", encoding="utf-8")
            output = "workspace-sentinel\n" + _sandbox_markers(
                {"inside_read": 0, "outside_read": 1, "inside_write": 0,
                 "outside_write": 2},
                {"outside_read", "outside_write"},
            )
        else:
            output = _sandbox_markers(
                {"auth_read": 1, "config_read": 1}, {"auth_read", "config_read"}
            )
        return subprocess.CompletedProcess(command, 1, output, "")

    monkeypatch.setattr(spike, "_run_probe", fake_run)

    result = spike.probe_low_level_sandbox(str(fake), tmp_path / "evidence")

    assert calls and len(calls) == 2
    assert result["inside_read_allowed"]
    assert result["outside_read_denied"]
    assert result["inside_write_allowed"]
    assert result["outside_write_denied"]
    assert result["codex_home_auth_and_config_denied"]


@pytest.mark.parametrize(
    ("auth_output", "auth_error"),
    [
        ("", "sandbox_apply: Operation not permitted\n"),
        (_sandbox_markers({"auth_read": 1}, {"auth_read"}), ""),
    ],
    ids=["auth-sandbox-setup-failure", "missing-config-operation"],
)
def test_sandbox_probe_refuses_when_auth_config_commands_did_not_all_run(
    tmp_path, monkeypatch, capsys, auth_output, auth_error
):
    fake = tmp_path / "fake-codex"
    fake.touch()
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        workspace = Path(command[command.index("--cd") + 1])
        if len(calls) == 1:
            (workspace / "write-test.txt").write_text("inside-write", encoding="utf-8")
            output = "workspace-sentinel\n" + _sandbox_markers(
                {"inside_read": 0, "outside_read": 1, "inside_write": 0,
                 "outside_write": 2},
                {"outside_read", "outside_write"},
            )
            return subprocess.CompletedProcess(command, 1, output, "")
        return subprocess.CompletedProcess(command, 1, auth_output, auth_error)

    monkeypatch.setattr(spike, "_run_probe", fake_run)

    code = spike._main([
        "sandbox-probe", "--codex-bin", str(fake), "--evidence-dir",
        str(tmp_path / "evidence"),
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == (
        "refused: Sandbox commands did not all run to completion; details were not retained.\n"
    )
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        ("sandbox_apply: Operation not permitted\n", ""),
        ("workspace-sentinel\n", "sandbox_apply: Operation not permitted\n"),
    ],
    ids=["setup-failure", "partial-misleading-output"],
)
def test_sandbox_probe_refuses_setup_failure_without_operation_markers(
    tmp_path, monkeypatch, capsys, stdout, stderr
):
    fake = tmp_path / "fake-codex"
    fake.touch()
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, stdout, stderr)

    monkeypatch.setattr(spike, "_run_probe", fake_run)

    code = spike._main([
        "sandbox-probe", "--codex-bin", str(fake), "--evidence-dir",
        str(tmp_path / "evidence"),
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == (
        "refused: Sandbox commands did not all run to completion; details were not retained.\n"
    )
    assert len(calls) == 2
    assert "workspace-sentinel" not in captured.out


@pytest.mark.skipif(os.name != "posix", reason="owned process-group cleanup is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
@pytest.mark.parametrize("probe", ["inspect", "sandbox"])
def test_probe_timeout_kills_term_ignoring_helper_group(tmp_path, monkeypatch, capsys, probe):
    marker = tmp_path / "helper"
    helper = _executable(
        tmp_path / "helper.py",
        "import pathlib, signal, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "i = 0\n"
        "while True:\n"
        " i += 1\n"
        " marker.write_text(str(i))\n"
        " time.sleep(0.03)\n",
    )
    code = (
        "import pathlib,subprocess,sys,time\n"
        f"marker=pathlib.Path({str(marker)!r})\n"
        f"helper=subprocess.Popen([sys.executable,{str(helper)!r}])\n"
        "(marker.with_suffix('.pid')).write_text(str(helper.pid))\n"
        "time.sleep(60)\n"
    )
    fake = _executable(tmp_path / "fake-codex", code)
    real_run_probe = spike._run_probe

    def fast_probe(command, *, env, cwd, timeout_s):
        return real_run_probe(command, env=env, cwd=cwd, timeout_s=1.0)

    monkeypatch.setattr(spike, "_run_probe", fast_probe)
    args = (["inspect", "--codex-bin", str(fake), "--evidence-dir", str(tmp_path / "evidence")]
            if probe == "inspect" else
            ["sandbox-probe", "--codex-bin", str(fake), "--evidence-dir", str(tmp_path / "evidence")])

    code = spike._main(args)
    captured = capsys.readouterr()

    assert code == 2
    assert captured.out == ""
    assert "refused:" in captured.err
    assert "Traceback" not in captured.err
    assert (marker.with_suffix(".pid")).exists()
    assert marker.exists()
    before = marker.read_text()
    time.sleep(0.15)
    assert marker.read_text() == before


@pytest.mark.skipif(os.name != "posix", reason="owned process-group cleanup is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_probe_normal_leader_exit_refuses_and_cleans_live_helper(tmp_path):
    marker = tmp_path / "helper"
    helper = _executable(
        tmp_path / "helper.py",
        "import pathlib, signal, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "i = 0\n"
        "while True:\n"
        " i += 1\n"
        " marker.write_text(str(i))\n"
        " time.sleep(0.03)\n",
    )
    fake = _executable(
        tmp_path / "fake-codex",
        (
            "import pathlib,subprocess,sys\n"
            f"marker=pathlib.Path({str(marker)!r})\n"
            f"helper=subprocess.Popen([sys.executable,{str(helper)!r}], "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "marker.with_suffix('.pid').write_text(str(helper.pid))\n"
            "deadline=__import__('time').monotonic()+1\n"
            "while not marker.exists() and __import__('time').monotonic()<deadline: __import__('time').sleep(0.01)\n"
        ),
    )

    with pytest.raises(SpikeError, match="result was refused"):
        spike._run_probe(
            [str(fake), "--version"], env={"PATH": os.defpath}, cwd=str(tmp_path), timeout_s=2
        )

    assert marker.with_suffix(".pid").exists()
    assert marker.exists()
    before = marker.read_text()
    time.sleep(0.15)
    assert marker.read_text() == before


@pytest.mark.skipif(os.name != "posix", reason="bounded probe pipes need POSIX selectors")
@pytest.mark.xdist_group("spike_procs")
@pytest.mark.parametrize("flood", ["stdout", "stderr", "both"])
def test_probe_bounds_both_output_streams_and_cleans_owned_group(
    tmp_path, monkeypatch, flood
):
    markers = tmp_path / "markers"
    markers.mkdir()
    heartbeat = markers / "heartbeat"
    helper = _executable(
        tmp_path / "flood-helper.py",
        "import pathlib,signal,time\n"
        f"marker=pathlib.Path({str(heartbeat)!r})\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "i=0\n"
        "while True:\n"
        " i+=1; marker.write_text(str(i)); time.sleep(0.02)\n",
    )
    fake = _executable(
        tmp_path / "noisy-codex",
        "import os,pathlib,subprocess,sys,time\n"
        f"markers=pathlib.Path({str(markers)!r})\n"
        f"helper=subprocess.Popen([sys.executable,{str(helper)!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "(markers/'helper.pid').write_text(str(helper.pid))\n"
        "deadline=time.monotonic()+1\n"
        "while not (markers/'heartbeat').exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
        "chunk=b'x'*8192\n"
        "while True:\n"
        f" os.write(1,chunk) if {flood!r} in ('stdout','both') else None\n"
        f" os.write(2,chunk) if {flood!r} in ('stderr','both') else None\n",
    )
    monkeypatch.setattr(spike, "MAX_PROBE_OUTPUT_BYTES", 32 * 1024)

    with pytest.raises(SpikeError, match="bounded capture limit") as error:
        spike._run_probe(
            [str(fake), "--version"], env={"PATH": os.defpath}, cwd=str(tmp_path), timeout_s=5
        )

    assert "raw" not in str(error.value)
    assert (markers / "helper.pid").exists()
    assert heartbeat.exists()
    before = heartbeat.read_text()
    time.sleep(0.15)
    assert heartbeat.read_text() == before


@pytest.mark.skipif(os.name != "posix", reason="fake-harness state locking is POSIX-only")
def test_uncertain_recovery_is_interrupted_and_never_relaunches(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    journal_path = state / "journal.jsonl"
    journal_path.write_text(
        "".join(
            json.dumps({"job_id": job_id, "state": "starting"}) + "\n"
            for job_id in ("uncertain", "uncertain-other")
        ) + '{"job_id":"partial","state":'
    )
    launches = tmp_path / "launch-count"
    fake = _executable(
        tmp_path / "must-not-run",
        "import pathlib, sys\n"
        f"pathlib.Path({str(launches)!r}).write_text('launched')\n",
    )

    assert recover_uncertain_runs(state) == ["uncertain", "uncertain-other"]
    assert recover_uncertain_runs(state) == []
    with pytest.raises(SpikeError, match="never replayed"):
        supervise_fake_command([str(fake)], state_dir=state, timeout_s=1, job_id="uncertain")
    with pytest.raises(SpikeError, match="never replayed"):
        supervise_fake_command([str(fake)], state_dir=state, timeout_s=1, job_id="uncertain-other")

    assert not launches.exists()
    raw_journal = journal_path.read_text().splitlines()
    assert raw_journal[2] == '{"job_id":"partial","state":'
    rows = spike._read_jsonl(journal_path)
    assert [(row["job_id"], row["state"]) for row in rows] == [
        ("uncertain", "starting"),
        ("uncertain-other", "starting"),
        ("uncertain", "interrupted"),
        ("uncertain-other", "interrupted"),
    ]
    evidence = [json.loads(line) for line in (state / "evidence.jsonl").read_text().splitlines()]
    assert [(row["kind"], row["job_id"]) for row in evidence] == [
        ("recovery", "uncertain"),
        ("recovery", "uncertain-other"),
    ]
    assert state.stat().st_mode & 0o777 == 0o700


@pytest.mark.skipif(os.name != "posix", reason="fake-harness flock reservation is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_fake_supervisor_atomically_reserves_state_against_supervisor_and_recovery(tmp_path):
    state = tmp_path / "state"
    launches = tmp_path / "launches"
    entered = tmp_path / "reservation-entered"
    release = tmp_path / "release-reservation"
    fake = _executable(
        tmp_path / "slow-fake",
        "import pathlib\n"
        f"with pathlib.Path({str(launches)!r}).open('a') as stream: stream.write('launched\\n')\n"
        "print('{\"type\":\"thread.started\"}', flush=True)\n"
    )
    driver = _executable(
        tmp_path / "supervisor-driver",
        "import pathlib,sys\n"
        "import scripts.remote_agent_host_spike as spike\n"
        "mode,state,command,job,entered,release=sys.argv[1:]\n"
        "if mode=='run' and entered!='-':\n"
        " original=spike._read_jsonl\n"
        " def gated_read(path):\n"
        "  records=original(path)\n"
        "  pathlib.Path(entered).touch()\n"
        "  deadline=__import__('time').monotonic()+5\n"
        "  while not pathlib.Path(release).exists() and __import__('time').monotonic()<deadline: __import__('time').sleep(0.01)\n"
        "  return records\n"
        " spike._read_jsonl=gated_read\n"
        "try:\n"
        " if mode=='run': print(spike.supervise_fake_command([command],state_dir=pathlib.Path(state),timeout_s=5,job_id=job)['state'])\n"
        " else: print(spike.recover_uncertain_runs(pathlib.Path(state)))\n"
        "except spike.SpikeError as exc:\n"
        " print('refused:'+str(exc)); raise SystemExit(3)\n",
    )
    repo_root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": repo_root}
    leader = subprocess.Popen(
        [sys.executable, str(driver), "run", str(state), str(fake), "same-job",
         str(entered), str(release)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not entered.exists() and leader.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists(), "first supervisor did not reach the journal reservation window"
        duplicate = subprocess.run(
            [sys.executable, str(driver), "run", str(state), str(fake), "same-job", "-", "-"],
            env=env, capture_output=True, text=True, timeout=5,
        )
        recovery = subprocess.run(
            [sys.executable, str(driver), "recover", str(state), str(fake), "unused", "-", "-"],
            env=env, capture_output=True, text=True, timeout=5,
        )
    finally:
        release.touch()
        try:
            leader_stdout, leader_stderr = leader.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            leader.kill()
            leader_stdout, leader_stderr = leader.communicate(timeout=2)

    assert leader.returncode == 0, leader_stderr
    assert leader_stdout.strip() == "succeeded"
    assert duplicate.returncode == 3
    assert duplicate.stdout.startswith("refused:Another fake-harness supervisor")
    assert recovery.returncode == 3
    assert recovery.stdout.startswith("refused:Another fake-harness supervisor")
    assert launches.read_text().splitlines() == ["launched"]
    journal = [json.loads(line) for line in (state / "journal.jsonl").read_text().splitlines()]
    assert [row["state"] for row in journal] == ["starting", "running", "succeeded"]


def test_fake_harness_state_lock_fails_closed_without_posix(monkeypatch, tmp_path):
    monkeypatch.setattr(spike.os, "name", "nt")
    with pytest.raises(SpikeError, match="state locking requires POSIX"):
        with spike._locked_state_directory(tmp_path):
            pytest.fail("non-POSIX fake-harness lock must not be acquired")


def test_authenticated_codex_path_is_disabled_even_with_explicit_temp_home(tmp_path):
    with pytest.raises(SpikeError, match="Live Codex runs are disabled"):
        run_live_codex(task="research", codex_home=tmp_path / "isolated")


def _wait_for_pid_exit(pid: int, timeout_s: float) -> bool:
    """True once the pid is gone or is an exited-but-unreaped zombie.

    Mirrors the harness: where PID 1 does not reap orphans promptly the
    process lingers as a zombie that still accepts signal 0 but cannot run.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if spike._pid_identity(pid) is None:
            return True
        time.sleep(0.05)
    return False


def _read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.skipif(os.name != "posix", reason="setsid/fork descendant tracking is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_setsid_detached_descendant_forces_interrupted_and_is_terminated(tmp_path):
    pid_file = tmp_path / "detached.pid"
    fake = _executable(
        tmp_path / "fake-detaching-provider",
        "import os, pathlib, time\n"
        f"pid_file = pathlib.Path({str(pid_file)!r})\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    devnull = os.open('/dev/null', os.O_RDWR)\n"
        "    for fd in (0, 1, 2): os.dup2(devnull, fd)\n"
        "    pid_file.write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "deadline = time.monotonic() + 2\n"
        "while not pid_file.exists() and time.monotonic() < deadline: time.sleep(0.01)\n"
        # The parent lingers briefly so the ppid link exists for at least one
        # supervisor snapshot; the harness documents that an immediate exit
        # after fork is the residual best-effort gap.
        "time.sleep(0.5)\n"
        "raise SystemExit(0)\n",
    )

    try:
        result = supervise_fake_command(
            [str(fake)], state_dir=tmp_path / "state", timeout_s=5, grace_s=0.5,
            job_id="detached",
        )
        assert pid_file.exists(), "detached child never reported its pid"
        detached_pid = int(pid_file.read_text())

        evidence = _read_rows(tmp_path / "state" / "evidence.jsonl")
        supervision = [row for row in evidence if row["kind"] == "supervision_result"]
        assert result["returncode"] == 0
        assert result["state"] == "interrupted", supervision
        assert _wait_for_pid_exit(detached_pid, 2.0), supervision
        assert len(supervision) == 1
        assert supervision[0]["state"] == "interrupted"
        assert supervision[0]["escaped_descendants"] == [detached_pid]
        assert supervision[0]["escaped_descendants_terminated"] is True
        assert supervision[0]["escaped_descendant_signalling"] in (
            spike.SIGNALLING_PIDFD, spike.SIGNALLING_IDENTITY_CHECK
        )
        # Cleanup is only recorded as certain where a non-reusable handle exists.
        assert supervision[0]["escaped_cleanup_certain"] is (
            supervision[0]["escaped_descendant_signalling"] == spike.SIGNALLING_PIDFD
        )
        assert supervision[0]["descendant_tracking_complete"] is True
        journal = _read_rows(tmp_path / "state" / "journal.jsonl")
        assert journal[-1]["state"] == "interrupted"
        assert journal[1]["state"] == "running" and isinstance(journal[1]["pgid"], int)
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except (ProcessLookupError, PermissionError, ValueError):
                pass


@pytest.mark.skipif(os.name != "posix", reason="fake-harness state locking is POSIX-only")
def test_recover_tolerates_oversized_numbers_and_invalid_bytes_in_journal(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    journal = state / "journal.jsonl"
    journal.write_bytes(
        json.dumps({"job_id": "first", "state": "starting"}).encode() + b"\n"
        + b"9" * 5000 + b"\n"
        + b'{"job_id":"\xff","state":"starting"}\n'
        + b"\xff\n"
        + json.dumps({"job_id": "second", "state": "running"}).encode() + b"\n"
    )

    assert recover_uncertain_runs(state) == ["first", "\ufffd", "second"]

    rows = spike._read_jsonl(journal)
    assert [(row["job_id"], row["state"]) for row in rows][-3:] == [
        ("first", "interrupted"), ("\ufffd", "interrupted"), ("second", "interrupted"),
    ]


def test_cli_withholds_os_error_details_when_state_dir_cannot_be_created(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")

    code = spike._main(["recover", "--state-dir", str(blocker / "leaf")])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "refused: harness failure (details withheld)\n"
    assert "Traceback" not in captured.err
    assert str(tmp_path) not in captured.err
    assert "blocker" not in captured.err


@pytest.mark.skipif(os.name != "posix", reason="killpg liveness probes are POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_unrecovered_running_job_blocks_start_and_recovery_records_group_liveness(tmp_path):
    sleeper = subprocess.Popen(
        ["/bin/sleep", "30"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    launches = tmp_path / "launch-count"
    fake = _executable(
        tmp_path / "must-not-run",
        f"import pathlib; pathlib.Path({str(launches)!r}).touch()\n",
    )
    try:
        state = tmp_path / "state"
        state.mkdir(mode=0o700)
        (state / "journal.jsonl").write_text(
            json.dumps({"job_id": "orphan", "state": "starting"}) + "\n"
            + json.dumps({"job_id": "orphan", "state": "running", "pgid": sleeper.pid}) + "\n",
            encoding="utf-8",
        )

        with pytest.raises(SpikeError, match="unrecovered non-terminal jobs; run recover first"):
            supervise_fake_command([str(fake)], state_dir=state, timeout_s=1, job_id="next")
        assert not launches.exists()
        assert [row["job_id"] for row in spike._read_jsonl(state / "journal.jsonl")] == [
            "orphan", "orphan"
        ]

        assert recover_uncertain_runs(state) == ["orphan"]
        assert sleeper.poll() is None, "recovery must observe, never signal, the recorded group"
        journal = spike._read_jsonl(state / "journal.jsonl")
        assert journal[-1]["state"] == "interrupted"
        assert journal[-1]["group_still_alive"] is True
        evidence = _read_rows(state / "evidence.jsonl")
        assert evidence == [{
            "kind": "recovery", "job_id": "orphan", "state": "interrupted",
            "reason": "uncertain_after_restart", "group_still_alive": True,
        }]

        # Generous timeout: a Python fake's start-up under a loaded CI runner
        # must not turn this success path into a cancellation.
        result = supervise_fake_command([str(fake)], state_dir=state, timeout_s=10, job_id="next")
        assert result["state"] == "succeeded"
        assert launches.exists()
    finally:
        try:
            os.killpg(sleeper.pid, 9)
        except ProcessLookupError:
            pass
        sleeper.wait(timeout=5)

    dead_pgid = sleeper.pid
    other = tmp_path / "other-state"
    other.mkdir(mode=0o700)
    (other / "journal.jsonl").write_text(
        json.dumps({"job_id": "gone", "state": "running", "pgid": dead_pgid}) + "\n",
        encoding="utf-8",
    )

    assert recover_uncertain_runs(other) == ["gone"]

    evidence = _read_rows(other / "evidence.jsonl")
    assert evidence[0]["group_still_alive"] is False
    assert spike._read_jsonl(other / "journal.jsonl")[-1]["group_still_alive"] is False


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory permissions are required")
def test_private_dir_creates_each_missing_ancestor_with_owner_only_mode(tmp_path):
    state = tmp_path / "a" / "b" / "leaf"

    assert recover_uncertain_runs(state) == []

    for created in (tmp_path / "a", tmp_path / "a" / "b", state):
        assert created.is_dir()
        assert created.stat().st_mode & 0o777 == 0o700
        assert created.stat().st_uid == os.getuid()


@pytest.mark.skipif(os.name != "posix", reason="symlink refusal relies on O_NOFOLLOW")
def test_symlinked_journal_is_refused_and_target_is_untouched(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    target = tmp_path / "victim.txt"
    target.write_text("original-contents\n", encoding="utf-8")
    (state / "journal.jsonl").symlink_to(target)
    fake = _executable(
        tmp_path / "must-not-run",
        f"import pathlib; pathlib.Path({str(tmp_path / 'launched')!r}).touch()\n",
    )

    with pytest.raises(SpikeError, match="refusing to follow a symlink"):
        supervise_fake_command([str(fake)], state_dir=state, timeout_s=1, job_id="symlink")
    code = spike._main(["demo", "--state-dir", str(state), "--timeout", "0.2"])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.err == "refused: refusing to follow a symlink\n"
    assert target.read_text(encoding="utf-8") == "original-contents\n"
    assert (state / "journal.jsonl").is_symlink()
    assert not (tmp_path / "launched").exists()
    assert not (state / "evidence.jsonl").exists()


@pytest.mark.skipif(os.name != "posix", reason="fake executable shebang test is POSIX-only")
def test_fake_child_environment_is_allowlisted(tmp_path, monkeypatch):
    markers = tmp_path / "markers"
    markers.mkdir()
    monkeypatch.setenv("FAKE_SECRET_TOKEN", "abc")
    monkeypatch.setenv("SPIKE_MARKERS", str(markers))
    fake = _executable(
        tmp_path / "fake-env-reporter",
        "import json, os, pathlib\n"
        "token = os.environ.get('FAKE_SECRET_TOKEN')\n"
        "print(json.dumps({'type': 'env.' + ('leaked' if token else 'clean'), 'token': token}), flush=True)\n"
        "pathlib.Path(os.environ['SPIKE_MARKERS'], 'env.json').write_text(json.dumps(dict(os.environ)))\n",
    )
    state = tmp_path / "state"

    result = supervise_fake_command([str(fake)], state_dir=state, timeout_s=2, job_id="env")

    assert result["state"] == "succeeded"
    assert result["event_names"] == ["env.clean"]
    observed = json.loads((markers / "env.json").read_text())
    assert "FAKE_SECRET_TOKEN" not in observed
    assert observed["SPIKE_MARKERS"] == str(markers)
    assert observed["HOME"] == str(state)
    assert observed["PATH"] == os.defpath
    # The child interpreter adds LC_CTYPE (PEP 538 locale coercion) and macOS
    # CoreFoundation adds __CF_USER_TEXT_ENCODING; neither comes from us.
    runtime_added = {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    assert not {k for k in observed if not k.startswith("SPIKE_")} - {"PATH", "HOME"} - runtime_added
    stored = (state / "journal.jsonl").read_text() + (state / "evidence.jsonl").read_text()
    assert "abc" not in stored


@pytest.mark.skipif(os.name != "posix", reason="fake executable shebang test is POSIX-only")
def test_non_conforming_event_types_are_recorded_as_unknown_event(tmp_path):
    fake = _executable(
        tmp_path / "fake-token-event",
        "import json, sys\n"
        "sys.stdout.write(json.dumps({'type': 'sk-live-ABC123'}) + '\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'Thread.Started'}) + '\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'a.b.c.d.e'}) + '\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'item.completed'}) + '\\n')\n"
        "sys.stdout.write('secret-token-value\\n')\n"
        "sys.stdout.flush()\n",
    )

    result = supervise_fake_command(
        [str(fake)], state_dir=tmp_path / "state", timeout_s=2, job_id="token-event"
    )

    assert result["state"] == "succeeded"
    assert result["event_names"] == [
        "unknown-event", "unknown-event", "unknown-event", "item.completed", "unstructured-output",
    ]
    stored = (
        (tmp_path / "state" / "journal.jsonl").read_text()
        + (tmp_path / "state" / "evidence.jsonl").read_text()
    )
    assert "sk-live-ABC123" not in stored
    assert "ABC123" not in stored
    assert "secret-token-value" not in stored


@pytest.mark.skipif(os.name != "posix", reason="fake executable shebang test is POSIX-only")
def test_inspect_refuses_unrecognised_version_output_without_retaining_it(tmp_path, capsys):
    fake = _executable(
        tmp_path / "fake-codex",
        "import sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        " print('codex-cli 0.157.1 SECRET=xyz')\n"
        "elif sys.argv[1:] == ['exec', '--help']:\n"
        " print('--json')\n"
        "else: raise SystemExit(4)\n",
    )

    code = spike._main([
        "inspect", "--codex-bin", str(fake), "--evidence-dir", str(tmp_path / "evidence")
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == "refused: Unrecognised version output\n"
    evidence = tmp_path / "evidence" / "evidence.jsonl"
    assert not evidence.exists() or "xyz" not in evidence.read_text()
    assert "xyz" not in captured.err


@pytest.mark.parametrize(
    ("first_output_extra", "outside_write_file", "failed"),
    [
        ("outside-sentinel\n", False, "outside_read_denied"),
        ("", True, "outside_write_denied"),
    ],
    ids=["outside-sentinel-leaked", "outside-write-file-exists"],
)
def test_sandbox_probe_refuses_when_boundary_is_not_confirmed(
    tmp_path, monkeypatch, capsys, first_output_extra, outside_write_file, failed
):
    fake = tmp_path / "fake-codex"
    fake.touch()
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        workspace = Path(command[command.index("--cd") + 1])
        if len(calls) == 1:
            (workspace / "write-test.txt").write_text("inside-write", encoding="utf-8")
            if outside_write_file:
                (workspace.parent / "outside" / "write-test.txt").write_text(
                    "outside-write", encoding="utf-8"
                )
            output = "workspace-sentinel\n" + first_output_extra + _sandbox_markers(
                {"inside_read": 0, "outside_read": 1, "inside_write": 0,
                 "outside_write": 2},
                {"outside_read", "outside_write"},
            )
        else:
            output = _sandbox_markers(
                {"auth_read": 1, "config_read": 1}, {"auth_read", "config_read"}
            )
        return subprocess.CompletedProcess(command, 1, output, "")

    monkeypatch.setattr(spike, "_run_probe", fake_run)

    code = spike._main([
        "sandbox-probe", "--codex-bin", str(fake), "--evidence-dir",
        str(tmp_path / "evidence"),
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err == (
        f"refused: Sandbox probe did not confirm: {failed}. Raw output was not retained.\n"
    )
    assert "outside-sentinel" not in captured.err
    assert len(calls) == 2
    assert not (tmp_path / "evidence" / "evidence.jsonl").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout_s", 0.0),
        ("timeout_s", -1.0),
        ("grace_s", -0.1),
    ],
)
def test_non_positive_timeout_or_negative_grace_refuses_before_state_or_launch(
    tmp_path, field, value
):
    launches = tmp_path / "launched"
    fake = _executable(
        tmp_path / "fake-provider",
        f"import pathlib; pathlib.Path({str(launches)!r}).touch()\n",
    )
    state = tmp_path / "state"
    options = {"timeout_s": 1.0, "grace_s": 0.5}
    options[field] = value

    with pytest.raises(SpikeError, match="finite positive timeout"):
        supervise_fake_command([str(fake)], state_dir=state, **options)

    assert not state.exists()
    assert not launches.exists()


@pytest.mark.skipif(os.name != "posix", reason="directory fsync is POSIX-only")
def test_append_jsonl_fsyncs_containing_directory_and_new_ancestors(tmp_path, monkeypatch):
    synced_dirs = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced_dirs.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(spike.os, "fsync", recording_fsync)
    leaf = tmp_path / "a" / "b" / "leaf"
    spike._append_jsonl(leaf / "journal.jsonl", {"job_id": "durable", "state": "starting"})
    # One directory fsync per newly created ancestor (a, b, leaf) plus one for
    # the journal's containing directory after the append.
    assert len(synced_dirs) == 4
    assert (leaf / "journal.jsonl").read_text().strip() == json.dumps(
        {"job_id": "durable", "state": "starting"}, sort_keys=True
    )


def test_track_descendants_drops_reused_pid_with_new_birth_identity(monkeypatch):
    tables = iter([
        [(100, 1, 100, "Ss", "birth-leader"), (4242, 100, 4242, "S", "birth-A")],
        # 4242 exited and its pid was reused by an unrelated process with a new
        # start time and a parent outside the run; 4343 is a genuine new child.
        [(100, 1, 100, "Ss", "birth-leader"), (4242, 1, 4242, "S", "birth-B"),
         (4343, 100, 4343, "S", "birth-C")],
    ])
    monkeypatch.setattr(spike, "_process_table", lambda: next(tables))
    monkeypatch.setattr(spike, "_open_pidfd", lambda pid: None)
    tracked: dict = {}
    spike._track_descendants(100, tracked)
    assert tracked == {4242: (4242, "S", "birth-A", None)}
    spike._track_descendants(100, tracked)
    assert 4242 not in tracked
    assert tracked == {4343: (4343, "S", "birth-C", None)}


def test_terminate_pids_never_signals_a_pid_whose_identity_changed(monkeypatch):
    sent = []

    def fake_kill(pid, sig):
        if sig == 0:
            return None  # "alive"
        sent.append((pid, sig))

    monkeypatch.setattr(spike.os, "kill", fake_kill)
    monkeypatch.setattr(spike, "_pid_identity", lambda pid: "someone-else")
    gone, mode = spike._terminate_pids({987654: ("ours", None)}, grace_s=0.05)
    assert gone is True
    assert mode == spike.SIGNALLING_IDENTITY_CHECK
    assert sent == []


def test_identity_includes_start_time_group_and_command():
    table_identity = spike._identity("Tue Sep 22 19:30:46 2026", 4242, "/usr/bin/python3 helper.py")
    assert table_identity == "Tue Sep 22 19:30:46 2026|pgid=4242|/usr/bin/python3 helper.py"
    # Same second, different group or command: not the same process.
    assert spike._identity("Tue Sep 22 19:30:46 2026", 4243, "/usr/bin/python3 helper.py") != table_identity
    assert spike._identity("Tue Sep 22 19:30:46 2026", 4242, "/bin/sleep 30") != table_identity


@pytest.mark.skipif(os.name != "posix", reason="ps identity lookup is POSIX-only")
def test_pid_identity_matches_process_table_entry_for_live_process():
    sleeper = subprocess.Popen(
        ["/bin/sleep", "5"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    try:
        table = spike._process_table()
        assert table is not None
        row = next(entry for entry in table if entry[0] == sleeper.pid)
        assert spike._pid_identity(sleeper.pid) == row[4]
        assert f"pgid={sleeper.pid}" in row[4]
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)
    assert spike._pid_identity(sleeper.pid) is None


@pytest.mark.skipif(os.name != "posix", reason="setsid/fork descendant tracking is POSIX-only")
@pytest.mark.xdist_group("spike_procs")
def test_exception_after_detach_still_terminates_tracked_descendant(tmp_path, monkeypatch):
    pid_file = tmp_path / "detached.pid"
    fake = _executable(
        tmp_path / "fake-detaching-lingering-provider",
        "import os, pathlib, time\n"
        f"pid_file = pathlib.Path({str(pid_file)!r})\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        "    devnull = os.open('/dev/null', os.O_RDWR)\n"
        "    for fd in (0, 1, 2): os.dup2(devnull, fd)\n"
        "    pid_file.write_text(str(os.getpid()))\n"
        "    time.sleep(30)\n"
        "    os._exit(0)\n"
        "time.sleep(30)\n",
    )
    real_append = spike._append_jsonl

    def failing_append(path, record):
        if record.get("state") == "cancel_requested":
            raise OSError("disk full")
        real_append(path, record)

    monkeypatch.setattr(spike, "_append_jsonl", failing_append)
    try:
        with pytest.raises(OSError, match="disk full"):
            supervise_fake_command(
                [str(fake)], state_dir=tmp_path / "state", timeout_s=1.5, grace_s=0.3,
                job_id="detached-then-fail",
            )
        assert pid_file.exists(), "detached child never reported its pid"
        detached_pid = int(pid_file.read_text())
        assert _wait_for_pid_exit(detached_pid, 2.0)
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), 9)
            except (ProcessLookupError, PermissionError, ValueError):
                pass


def test_terminate_pids_counts_exited_unreaped_pidfd_descendant_as_gone(monkeypatch):
    sent = []

    def fake_pidfd_send_signal(handle, sig):
        # A zombie still accepts signal 0 until something reaps it.
        if sig != 0:
            sent.append((handle, sig))

    monkeypatch.setattr(spike.signal, "pidfd_send_signal", fake_pidfd_send_signal, raising=False)
    monkeypatch.setattr(spike, "_pidfd_exited", lambda handle: True)
    gone, mode = spike._terminate_pids({4242: ("ours", 42)}, grace_s=0.05)
    assert gone is True
    assert mode == spike.SIGNALLING_PIDFD
    assert sent == []


def test_terminate_pids_signals_live_pidfd_descendant_until_it_exits(monkeypatch):
    sent = []
    exited = {"value": False}

    def fake_pidfd_send_signal(handle, sig):
        if sig != 0:
            sent.append((handle, sig))
            exited["value"] = True  # the process dies on TERM

    monkeypatch.setattr(spike.signal, "pidfd_send_signal", fake_pidfd_send_signal, raising=False)
    monkeypatch.setattr(spike, "_pidfd_exited", lambda handle: exited["value"])
    gone, mode = spike._terminate_pids({4242: ("ours", 42)}, grace_s=0.5)
    assert gone is True
    assert mode == spike.SIGNALLING_PIDFD
    assert sent == [(42, signal.SIGTERM)]


@pytest.mark.skipif(os.name != "posix", reason="ps identity lookup is POSIX-only")
def test_pid_identity_reports_zombie_as_gone(tmp_path):
    # A child we deliberately do not reap stays a zombie until wait().
    child = subprocess.Popen(["/bin/sleep", "0"], stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            table = spike._process_table() or []
            row = next((entry for entry in table if entry[0] == child.pid), None)
            if row is not None and row[3].startswith("Z"):
                break
            time.sleep(0.02)
        else:
            pytest.skip("child did not become an observable zombie in time")
        assert spike._pid_identity(child.pid) is None
    finally:
        child.wait(timeout=5)


def test_track_descendants_refuses_handle_whose_identity_changed_after_open(monkeypatch):
    closed = []
    monkeypatch.setattr(spike, "_process_table",
                        lambda: [(100, 1, 100, "Ss", "leader"), (4242, 100, 4242, "S", "birth-A")])
    monkeypatch.setattr(spike, "_open_pidfd", lambda pid: 77)
    monkeypatch.setattr(spike, "_pid_identity", lambda pid: "birth-B")  # reused after snapshot
    monkeypatch.setattr(spike.os, "close", lambda fd: closed.append(fd))
    tracked: dict = {}
    spike._track_descendants(100, tracked)
    assert tracked == {}
    assert closed == [77]


@pytest.mark.skipif(os.name != "posix", reason="zombie observation is POSIX-only")
def test_leader_exit_is_observed_without_reaping():
    process = subprocess.Popen(["/bin/sleep", "0"], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not spike._leader_exited(process) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert spike._leader_exited(process)
        # Not reaped: the pid, and so the process-group id, is still reserved.
        assert process.returncode is None
        assert spike._pid_alive(process.pid)
        assert spike._group_running(process.pid, threading.Thread()) is False
    finally:
        process.wait(timeout=5)
    assert process.returncode == 0


def test_leader_exited_reads_state_from_supplied_snapshot(monkeypatch):
    process = SimpleNamespace(returncode=None, pid=100, poll=lambda: None)
    monkeypatch.setattr(spike.os, "waitid", None, raising=False)
    monkeypatch.setattr(spike, "_pid_state", lambda pid: pytest.fail("must use the snapshot"))
    running = [(100, 1, 100, "Ss", "leader")]
    zombie = [(100, 1, 100, "Z", "leader")]
    assert spike._leader_exited(process, running) is False
    assert spike._leader_exited(process, zombie) is True
    assert spike._leader_exited(process, []) is True  # unreaped children are always listed


def test_supervision_records_incomplete_tracking_when_snapshots_fail(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("process-group supervision is POSIX-only")
    monkeypatch.setattr(spike, "_process_table", lambda: None)
    fake = _executable(tmp_path / "fake-quick", "raise SystemExit(0)\n")
    result = supervise_fake_command([str(fake)], state_dir=tmp_path / "state", timeout_s=10,
                                    job_id="no-snapshots")
    assert result["state"] == "succeeded"
    evidence = _read_rows(tmp_path / "state" / "evidence.jsonl")
    supervision = [row for row in evidence if row["kind"] == "supervision_result"]
    assert supervision[0]["descendant_tracking_complete"] is False
