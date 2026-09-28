from __future__ import annotations

import json
import os
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


def test_unavailable_group_enumeration_keeps_existing_group_uncertain(monkeypatch):
    signaled = []
    monkeypatch.setattr(spike, "_group_has_running_members", lambda _pgid: None)
    monkeypatch.setattr(spike.os, "killpg", lambda pgid, sig: signaled.append((pgid, sig)))

    assert spike._group_running(4321, threading.Thread()) is True
    assert signaled == [(4321, 0)]


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


def test_authenticated_codex_path_is_disabled_even_with_explicit_temp_home(tmp_path):
    with pytest.raises(SpikeError, match="Live Codex runs are disabled"):
        run_live_codex(task="research", codex_home=tmp_path / "isolated")
