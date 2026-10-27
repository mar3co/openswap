"""Run ``claude auth login`` from the extra, with no Terminal window.

The CLI needs no TTY: it opens the browser itself, waits for the OAuth
callback, writes the login and exits. The extra only has to wait for the exit
and then check who actually signed in.
"""

from __future__ import annotations

import codecs
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from openswap.paths import get_backup_root

_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_TAIL_CHARS = 2000
DEFAULT_TIMEOUT_S = 600.0


def login_argv(claude_bin: str, email: str | None) -> list[str]:
    argv = [claude_bin, "auth", "login", "--claudeai"]
    return argv + ["--email", email] if email else argv


def remove_legacy_login_command(root: Path | None = None) -> None:
    """Delete the ``login.command`` older extras wrote for Terminal."""
    try:
        ((root or get_backup_root()) / "login.command").unlink(missing_ok=True)
    except OSError:
        pass


@dataclass(frozen=True)
class LoginResult:
    returncode: int | None
    timed_out: bool
    canceled: bool
    output_tail: str


def new_login_outcome(result: LoginResult, *, live: tuple[str, str] | None) -> str:
    """canceled | timed_out | failed | no_login | signed_in, for any account."""
    if result.canceled:
        return "canceled"
    if result.timed_out:
        return "timed_out"
    if result.returncode != 0:
        return "failed"
    if live is None:
        return "no_login"
    return "signed_in"


def login_outcome(
    result: LoginResult,
    *,
    live: tuple[str, str] | None,
    slot: tuple[str, str] | None,
) -> str:
    """canceled | timed_out | failed | no_login | mismatch | signed_in.

    The live login is checked only after a clean exit: a slot whose dead
    token is still Claude Code's active login matches before the sign-in
    even starts, so a match alone proves nothing. A clean exit only means
    *someone* signed in; the slot is captured only when that someone is it.
    """
    outcome = new_login_outcome(result, live=live)
    if outcome == "signed_in" and live != slot:
        return "mismatch"
    return outcome


class ClaudeLoginRun:
    """One ``claude auth login`` process, waited on from a worker thread."""

    def __init__(
        self,
        claude_bin: str,
        email: str | None,
        *,
        on_done,
        timeout: float = DEFAULT_TIMEOUT_S,
        popen=subprocess.Popen,
        clock=time.monotonic,
        env: dict[str, str] | None = None,
    ) -> None:
        self._argv = login_argv(claude_bin, email)
        self._on_done = on_done
        self._timeout = timeout
        self._popen = popen
        self._clock = clock
        self._env = dict(env) if env is not None else None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._canceled = False

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self._run, daemon=True, name="openswap-claude-login"
        )
        thread.start()
        return thread

    def cancel(self) -> None:
        with self._lock:
            self._canceled = True
            proc = self._proc
        if proc is not None:
            _terminate(proc)

    def _run(self) -> None:
        try:
            self._on_done(self._wait_for_exit())
        except Exception as exc:
            self._on_done(LoginResult(None, False, False, f"{type(exc).__name__}: {exc}"))

    def _wait_for_exit(self) -> LoginResult:
        proc = self._popen(
            self._argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            **({"env": self._env} if self._env is not None else {}),
        )
        with self._lock:
            self._proc = proc
            if self._canceled:
                _terminate(proc)
        tail = _Tail()
        reader = threading.Thread(
            target=tail.read_from, args=(proc.stdout,), daemon=True
        )
        reader.start()
        timed_out = False
        try:
            proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(proc)
        reader.join(timeout=2.0)
        # Closing a BufferedReader can wait on a read1() that a lingering
        # grandchild keeps blocked, so leave it to GC in that case.
        if not reader.is_alive() and proc.stdout is not None:
            proc.stdout.close()
        with self._lock:
            canceled = self._canceled
        returncode = None if timed_out or canceled else proc.returncode
        return LoginResult(returncode, timed_out, canceled, tail.text())


class _Tail:
    def __init__(self) -> None:
        self._buf = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read_from(self, stream) -> None:
        try:
            while True:
                chunk = stream.read1(4096)
                if not chunk:
                    return
                self._buf = (self._buf + self._decoder.decode(chunk))[-_TAIL_CHARS:]
        except Exception:
            return

    def text(self) -> str:
        lines = [line.rstrip() for line in _ANSI_RE.sub("", self._buf).splitlines()]
        return "\n".join(line for line in lines if line.strip())


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=2.0)
    except (OSError, subprocess.SubprocessError):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=1.0)
        except (OSError, subprocess.SubprocessError):
            pass
