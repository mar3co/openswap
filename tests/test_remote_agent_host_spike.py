from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
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
    assert "secret" not in stored.lower()


@pytest.mark.skipif(os.name != "posix", reason="process-group supervision is POSIX-only")
def test_timeout_terminates_fake_process_group_including_child(tmp_path):
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
    old = os.environ.get("SPIKE_MARKERS")
    os.environ["SPIKE_MARKERS"] = str(markers)
    try:
        result = supervise_fake_command(
            [str(fake)], state_dir=tmp_path / "state", timeout_s=2.0, grace_s=1.0,
            job_id="kill-group",
        )
    finally:
        if old is None:
            os.environ.pop("SPIKE_MARKERS", None)
        else:
            os.environ["SPIKE_MARKERS"] = old

    child_pid = (markers / "child.pid").read_text()
    assert result["state"] == "cancelled"
    deadline = time.monotonic() + 2
    while not (markers / f"{child_pid}.stopped").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (markers / f"{child_pid}.stopped").exists()
    assert not (markers / f"{child_pid}.alive").exists()
    journal = [json.loads(line) for line in (tmp_path / "state" / "journal.jsonl").read_text().splitlines()]
    assert [row["state"] for row in journal] == ["starting", "running", "cancel_requested", "cancelled"]


@pytest.mark.skipif(os.name != "posix", reason="process-group supervision is POSIX-only")
def test_timeout_kills_child_that_ignores_term_before_reporting_cancelled(tmp_path):
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
    old = os.environ.get("SPIKE_MARKERS")
    os.environ["SPIKE_MARKERS"] = str(markers)
    try:
        result = supervise_fake_command(
            [str(fake)], state_dir=tmp_path / "state", timeout_s=2.0, grace_s=0.2,
            job_id="kill-ignoring-child",
        )
    finally:
        if old is None:
            os.environ.pop("SPIKE_MARKERS", None)
        else:
            os.environ["SPIKE_MARKERS"] = old

    beat = markers / "heartbeat"
    first_value = beat.read_text()
    time.sleep(0.15)
    assert beat.read_text() == first_value
    assert (markers / "parent.stopped").exists()
    assert result["state"] == "cancelled"


@pytest.mark.skipif(os.name != "posix", reason="process-group supervision is POSIX-only")
def test_successful_parent_with_live_child_is_interrupted_and_child_is_killed(tmp_path):
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
    old = os.environ.get("SPIKE_MARKERS")
    os.environ["SPIKE_MARKERS"] = str(markers)
    try:
        result = supervise_fake_command(
            [str(fake)], state_dir=tmp_path / "state", timeout_s=3, grace_s=0.15,
            job_id="leader-exited",
        )
    finally:
        if old is None:
            os.environ.pop("SPIKE_MARKERS", None)
        else:
            os.environ["SPIKE_MARKERS"] = old

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
def test_noisy_fake_output_is_bounded_and_cancellation_still_completes(tmp_path):
    fake = _executable(
        tmp_path / "fake-noisy-provider",
        "import json, sys, time\n"
        f"sys.stdout.write('x' * ({spike.MAX_EVENT_LINE_CHARS} + 100_000) + '\\n')\n"
        "for i in range(10000):\n"
        " sys.stdout.write(json.dumps({'type': f'event-{i}'}) + '\\n')\n"
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
    state.mkdir()
    (state / "journal.jsonl").write_text(
        json.dumps({"job_id": "uncertain", "state": "starting"}) + "\n"
    )
    launches = tmp_path / "launch-count"
    fake = _executable(
        tmp_path / "must-not-run",
        "import pathlib, sys\n"
        f"pathlib.Path({str(launches)!r}).write_text('launched')\n",
    )

    assert recover_uncertain_runs(state) == ["uncertain"]
    with pytest.raises(SpikeError, match="never replayed"):
        supervise_fake_command([str(fake)], state_dir=state, timeout_s=1, job_id="uncertain")

    assert not launches.exists()
    rows = [json.loads(line) for line in (state / "journal.jsonl").read_text().splitlines()]
    assert [row["state"] for row in rows] == ["starting", "interrupted"]


@pytest.mark.skipif(os.name != "posix", reason="fake-harness flock reservation is POSIX-only")
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
        deadline = time.monotonic() + 3
        while not entered.exists() and leader.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists(), "first supervisor did not reach the journal reservation window"
        duplicate = subprocess.run(
            [sys.executable, str(driver), "run", str(state), str(fake), "same-job", "-", "-"],
            env=env, capture_output=True, text=True, timeout=2,
        )
        recovery = subprocess.run(
            [sys.executable, str(driver), "recover", str(state), str(fake), "unused", "-", "-"],
            env=env, capture_output=True, text=True, timeout=2,
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
