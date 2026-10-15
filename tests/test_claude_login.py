import os
import subprocess
import threading

import pytest

from openswap.claude_login import (
    ClaudeLoginRun,
    LoginResult,
    login_argv,
    login_outcome,
    remove_legacy_login_command,
)


posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX shell and process groups")


def _run(script, *, timeout=5.0):
    results = []
    done = threading.Event()

    def on_done(result):
        results.append(result)
        done.set()

    run = ClaudeLoginRun(
        "/bin/sh", "a@x.com", timeout=timeout, on_done=on_done,
        popen=lambda argv, **kw: subprocess.Popen(["/bin/sh", "-c", script], **kw),
    )
    run.start()
    return run, results, done


def test_login_argv():
    assert login_argv("/opt/homebrew/bin/claude", "a@x.com") == [
        "/opt/homebrew/bin/claude", "auth", "login", "--claudeai",
        "--email", "a@x.com",
    ]
    assert login_argv("/opt/homebrew/bin/claude", None) == [
        "/opt/homebrew/bin/claude", "auth", "login", "--claudeai",
    ]


def test_remove_legacy_login_command_tolerates_absence(tmp_path):
    stale = tmp_path / "login.command"
    stale.write_text("#!/bin/bash\n")
    remove_legacy_login_command(tmp_path)
    assert not stale.exists()
    remove_legacy_login_command(tmp_path)


@posix_only
def test_run_reports_exit_code_and_ansi_stripped_output_tail():
    _run_obj, results, done = _run(
        "printf 'Opening browser\\n\\033[31mLogin successful\\033[0m\\n'; exit 0"
    )
    assert done.wait(5)
    (result,) = results
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.canceled is False
    assert result.output_tail == "Opening browser\nLogin successful"


@posix_only
def test_tail_strips_osc_hyperlinks_without_eating_the_text_between():
    _run_obj, results, done = _run(
        "printf '\\033]8;;https://x\\033\\\\one\\033]8;;\\033\\\\\\nError: real\\n"
        "\\033]8;;https://y\\033\\\\two\\033]8;;\\033\\\\\\n'; exit 1"
    )
    assert done.wait(5)
    (result,) = results
    assert result.output_tail == "one\nError: real\ntwo"


@posix_only
def test_run_merges_stderr_and_keeps_only_the_tail():
    _run_obj, results, done = _run(
        "echo noise; echo 'Error: boom' >&2; exit 3"
    )
    assert done.wait(5)
    (result,) = results
    assert result.returncode == 3
    assert "Error: boom" in result.output_tail


@posix_only
def test_run_times_out_and_terminates_the_cli():
    _run_obj, results, done = _run("sleep 30", timeout=0.3)
    assert done.wait(5)
    (result,) = results
    assert result.timed_out is True
    assert result.returncode is None


@posix_only
def test_cancel_terminates_and_reports_canceled_not_failed():
    run, results, done = _run("sleep 30")
    run.cancel()
    assert done.wait(5)
    (result,) = results
    assert result.canceled is True
    assert result.timed_out is False
    assert result.returncode is None


@pytest.mark.parametrize("exc", [OSError("no such binary"), RuntimeError("no such binary")])
def test_popen_failure_is_reported_as_a_result(exc):
    results = []

    def boom(argv, **kw):
        raise exc

    run = ClaudeLoginRun("/nope/claude", "a@x.com", on_done=results.append, popen=boom)
    run.start().join(5)
    (result,) = results
    assert result.returncode is None
    assert result.timed_out is False
    assert "no such binary" in result.output_tail


SLOT = ("a@x.com", "org-1")


@pytest.mark.parametrize(
    "result, live, expected",
    [
        (LoginResult(None, False, True, ""), SLOT, "canceled"),
        # The dead token can still be Claude Code's active login when the
        # sign-in starts, so a match never excuses a timeout or failure.
        (LoginResult(None, True, False, ""), SLOT, "timed_out"),
        (LoginResult(1, False, False, "Error: boom"), SLOT, "failed"),
        (LoginResult(0, False, False, ""), SLOT, "signed_in"),
        (LoginResult(0, False, False, ""), ("a@x.com", "org-2"), "mismatch"),
        (LoginResult(0, False, False, ""), ("b@x.com", "org-1"), "mismatch"),
        (LoginResult(0, False, False, ""), None, "no_login"),
    ],
)
def test_login_outcome(result, live, expected):
    assert login_outcome(result, live=live, slot=SLOT) == expected
