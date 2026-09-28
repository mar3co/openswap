"""Opt-in LaunchAgent management for the isolated Remote Agent Host worker.

This is intentionally separate from :mod:`openswap.launch_agent`: the menu
bar owns an interactive agent with a different argv and lifecycle. These
functions only shape or manage the worker's per-user launchd service; callers
must persist the explicit worker opt-in before installing it.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

from openswap import launch_agent as menubar_launch_agent
from openswap.exceptions import ClaudeSwitchError

LABEL = "com.opensoft.openswap.worker"
LAUNCHCTL_TIMEOUT_SECONDS = 5.0
_UNLOAD_TIMEOUT_SECONDS = 5.0
_UNLOAD_POLL_SECONDS = 0.1


def _require_macos() -> None:
    if sys.platform != "darwin":
        raise ClaudeSwitchError("The Remote Agent Host worker is only available on macOS.")


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def log_paths(home: Path | None = None) -> tuple[Path, Path]:
    logs = (home or Path.home()) / "Library" / "Logs"
    return logs / f"{LABEL}.log", logs / f"{LABEL}.err"


def build_plist(program: list[str] | None = None, home: Path | None = None) -> bytes:
    """Build the worker plist without loading or starting launchd."""
    program = program or menubar_launch_agent.resolve_program()
    out_log, err_log = log_paths(home)
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [*program, "worker", "run"],
            "RunAtLoad": True,
            # A clean worker exit (for example, opt-out) must not relaunch.
            # Explicit disable also bootouts the service below.
            "KeepAlive": {"SuccessfulExit": False},
            "EnvironmentVariables": {
                "PATH": menubar_launch_agent._path_env(program),
            },
            "StandardOutPath": str(out_log),
            "StandardErrorPath": str(err_log),
        }
    )


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=LAUNCHCTL_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:  # pragma: no cover - base macOS binary
        raise ClaudeSwitchError("launchctl is unavailable on this Mac.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClaudeSwitchError("launchctl command timed out.") from exc
    except OSError as exc:
        raise ClaudeSwitchError("launchctl command failed.") from exc


def _service_target(uid: int | None = None) -> str:
    return menubar_launch_agent.service_target(LABEL, uid)


def _domain_target(uid: int | None = None) -> str:
    return menubar_launch_agent.domain_target(uid)


def _is_loaded(uid: int | None = None) -> bool:
    return _launchctl("print", _service_target(uid)).returncode == 0


def _wait_until_unloaded(uid: int | None = None) -> bool:
    deadline = time.monotonic() + _UNLOAD_TIMEOUT_SECONDS
    while _is_loaded(uid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_UNLOAD_POLL_SECONDS)
    return True


def _service_state(printed_stdout: str) -> tuple[str | None, int | None]:
    """The top-level ``state`` and ``pid`` from ``launchctl print`` output."""
    state: str | None = None
    pid: int | None = None
    for line in printed_stdout.splitlines():
        if not line.startswith("\t") or line.startswith("\t\t"):
            continue
        stripped = line.strip()
        if state is None and stripped.startswith("state = "):
            state = stripped.removeprefix("state = ").strip()
        elif pid is None and stripped.startswith("pid = "):
            raw = stripped.removeprefix("pid = ").strip()
            if raw.isdigit():
                pid = int(raw)
    return state, pid


def status(home: Path | None = None, uid: int | None = None) -> dict:
    """Report worker service state; never starts, installs, or enables it."""
    _require_macos()
    printed = _launchctl("print", _service_target(uid))
    loaded = printed.returncode == 0
    state, pid = _service_state(printed.stdout) if loaded else (None, None)
    target = plist_path(home)
    return {
        "label": LABEL,
        "installed": target.exists(),
        "loaded": loaded,
        "state": state,
        "pid": pid,
        "plist": str(target),
    }


def install(
    home: Path | None = None,
    program: list[str] | None = None,
    uid: int | None = None,
) -> dict:
    """Install and bootstrap the worker after its default-off policy is enabled."""
    _require_macos()
    program = program or menubar_launch_agent.resolve_program()
    target = plist_path(home)
    out_log, err_log = log_paths(home)
    printed = _launchctl("print", _service_target(uid))
    if printed.returncode == 0:
        # Enabling twice must not tear down a worker that may own a live job.
        # A loaded job with no running process (it exited cleanly, which
        # KeepAlive does not relaunch, or a disable stopped short of bootout)
        # is started so enable never reports success with no worker running.
        state, pid = _service_state(printed.stdout)
        kickstarted = False
        if state != "running" and pid is None:
            kicked = _launchctl("kickstart", _service_target(uid))
            if kicked.returncode != 0:
                raise ClaudeSwitchError(
                    f"launchctl kickstart failed (exit {kicked.returncode})."
                )
            kickstarted = True
        return {
            "label": LABEL,
            "plist": str(target),
            "program": [*program, "worker", "run"],
            "stdout_log": str(out_log),
            "stderr_log": str(err_log),
            "already_loaded": True,
            "kickstarted": kickstarted,
        }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        out_log.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(build_plist(program, home))
    except OSError as exc:
        raise ClaudeSwitchError(f"Could not write the worker LaunchAgent: {exc}") from exc

    booted = _launchctl("bootstrap", _domain_target(uid), str(target))
    if booted.returncode != 0:
        raise ClaudeSwitchError(
            f"launchctl bootstrap failed (exit {booted.returncode})."
        )
    return {
        "label": LABEL,
        "plist": str(target),
        "program": [*program, "worker", "run"],
        "stdout_log": str(out_log),
        "stderr_log": str(err_log),
        "already_loaded": False,
    }


def uninstall(home: Path | None = None, uid: int | None = None) -> dict:
    """Stop the service and remove its plist. Never launches the worker."""
    _require_macos()
    target = plist_path(home)
    was_loaded = _is_loaded(uid)
    if was_loaded:
        booted_out = _launchctl("bootout", _service_target(uid))
        if booted_out.returncode != 0 and _is_loaded(uid):
            raise ClaudeSwitchError(
                f"launchctl bootout failed (exit {booted_out.returncode})."
            )
        if not _wait_until_unloaded(uid):
            raise ClaudeSwitchError(
                "Worker LaunchAgent is still loaded; it was not removed."
            )
    existed = target.exists()
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        raise ClaudeSwitchError(f"Could not remove the worker LaunchAgent: {exc}") from exc
    return {"label": LABEL, "was_loaded": was_loaded, "removed_plist": existed}
