"""Conservative lifecycle control for the unified ChatGPT/Codex macOS app.

This module deliberately does not inspect or modify credentials.  It only
validates, stops, and starts the known OpenAI application bundle so a caller
can put a credential transaction between the two lifecycle operations.
"""

from __future__ import annotations

import os
import math
import plistlib
import shlex
import stat
import subprocess
import sys
import time
from xml.parsers.expat import ExpatError
from dataclasses import dataclass
from pathlib import Path

from openswap.exceptions import ClaudeSwitchError


class DesktopAppError(ClaudeSwitchError):
    """The desktop app cannot be controlled without risking another client."""

    def __init__(self, message: str, *, reason: str = "app_invalid"):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DesktopCapability:
    """Typed installation/process observation. Never carries paths or stderr."""

    state: str
    reason: str
    observed_at: float | None = None


_CAPABILITY_TTL_S = 5.0
_PROCESS_STATES = frozenset({"running", "stopped"})
_TERMINAL_STATES = frozenset({"unsupported", "missing", "invalid"})


def capability_from_error(exc: DesktopAppError) -> DesktopCapability:
    """Map a structured desktop error to a UI-safe capability. Never reads ``str(exc)``."""
    reason = exc.reason if isinstance(getattr(exc, "reason", None), str) and exc.reason else "app_invalid"
    if reason == "unsupported_platform":
        state = "unsupported"
    elif reason == "app_missing":
        state = "missing"
    else:
        state = "invalid"
    return DesktopCapability(state=state, reason=reason)


def capability_is_fresh(capability: DesktopCapability, *, now: float) -> bool:
    """``running``/``stopped`` expire after five monotonic seconds; other terminals do not."""
    if capability.state in _TERMINAL_STATES:
        return True
    if capability.state not in _PROCESS_STATES:
        return False
    observed = capability.observed_at
    if observed is None:
        return False
    return (now - observed) < _CAPABILITY_TTL_S


_BUNDLE_ID = "com.openai.codex"
_DEFAULT_APP = Path("/Applications/ChatGPT.app")
_BUNDLED_CLI = Path("Contents/Resources/codex")
_SUPPORTED_VERSION = "26.908.70816"
_SUPPORTED_BUILD = "9275"
_OPENAI_TEAM_ID = "2DC432GLL2"
_KNOWN_BUNDLE_HELPERS = {"codex-code-mode-host", "codex-app-server", "app-server"}
_UNSUPPORTED_OVERRIDES = (
    "CODEX_CLI_PATH",
    "CODEX_COMMAND",
    "CODEX_EXECUTABLE",
    "CODEX_APP_SERVER_COMMAND",
    "CODEX_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
)
_SECRET_ENV_NAMES = {
    "OPENAI_API_KEY",
    "CODEX_API_KEY",
    "CHATGPT_API_KEY",
    "OPENAI_ACCESS_TOKEN",
    "CODEX_ACCESS_TOKEN",
    "CHATGPT_ACCESS_TOKEN",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "ELECTRON_RUN_AS_NODE",
}
_SUBPROCESS_TIMEOUT = 3.0
_CODESIGN_TIMEOUT = 15.0


@dataclass(frozen=True)
class _Process:
    pid: int
    ppid: int
    comm: str
    args: str


def _processes() -> list[_Process]:
    """Return a process snapshot.  Command details never leave this module."""
    try:
        identity = subprocess.run(
            ["/bin/ps", "-ww", "-axo", "pid=,ppid=,comm="], check=True,
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        )
        arguments = subprocess.run(
            ["/bin/ps", "-ww", "-axo", "pid=,args="], check=True,
            capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DesktopAppError(
            "Could not safely inspect running desktop processes.",
            reason="process_inspect_failed",
        ) from exc

    args_by_pid: dict[int, str] = {}
    for line in arguments.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2:
            continue
        try:
            args_by_pid[int(fields[0])] = fields[1]
        except ValueError:
            continue
    rows: list[_Process] = []
    for line in identity.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) != 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if pid > 1:
            rows.append(_Process(pid, ppid, fields[2], args_by_pid.get(pid, "")))
    return rows


def _argv0(args: str) -> str:
    # POSIX shlex treats backslashes in drive-letter paths as escapes. Preserve
    # them when process snapshots are analyzed by cross-platform tooling.
    probe = args.lstrip().lstrip('"')
    windows_path = len(probe) >= 3 and probe[1] == ":" and probe[2] in {"\\", "/"}
    try:
        parts = shlex.split(args, posix=not windows_path)
    except ValueError:
        return ""
    return parts[0].strip('"') if parts else ""


def _descendants(rows: list[_Process], roots: set[int]) -> set[int]:
    result = set(roots)
    while True:
        added = {row.pid for row in rows if row.ppid in result}
        new = added - result
        if not new:
            return result - roots
        result.update(new)


def _is_codex_process(row: _Process, bundled_cli: Path, app_path: Path) -> bool:
    argv0 = _argv0(row.args)
    if Path(row.comm).name == "codex" or argv0 == str(bundled_cli):
        return True
    candidate = Path(argv0)
    try:
        inside_bundle = candidate.is_relative_to(app_path / "Contents")
    except (TypeError, ValueError):
        inside_bundle = False
    return inside_bundle and candidate.name in _KNOWN_BUNDLE_HELPERS


class DesktopApp:
    """Lifecycle adapter for the exact installed OpenAI desktop bundle."""

    def __init__(self, app_path: Path = _DEFAULT_APP):
        self.app_path = Path(app_path)
        self._validation_cache: tuple[tuple, dict, Path] | None = None

    @property
    def _plist_path(self) -> Path:
        return self.app_path / "Contents/Info.plist"

    @property
    def _bundled_cli(self) -> Path:
        return self.app_path / _BUNDLED_CLI

    def _validate(self) -> tuple[dict, Path]:
        if sys.platform != "darwin":
            raise DesktopAppError(
                "ChatGPT desktop switching is only supported on macOS.",
                reason="unsupported_platform",
            )
        try:
            with self._plist_path.open("rb") as stream:
                info = plistlib.load(stream)
        except FileNotFoundError as exc:
            raise DesktopAppError(
                "The ChatGPT application bundle is missing or invalid.",
                reason="app_missing",
            ) from exc
        except (OSError, ValueError, ExpatError, plistlib.InvalidFileException) as exc:
            raise DesktopAppError(
                "The ChatGPT application bundle is missing or invalid.",
                reason="app_invalid",
            ) from exc
        if not isinstance(info, dict):
            raise DesktopAppError(
                "The ChatGPT application bundle is missing or invalid.",
                reason="app_invalid",
            )
        if info.get("CFBundleIdentifier") != _BUNDLE_ID:
            raise DesktopAppError(
                "The selected application is not the supported ChatGPT app.",
                reason="wrong_bundle",
            )
        if (
            info.get("CFBundleShortVersionString") != _SUPPORTED_VERSION
            or str(info.get("CFBundleVersion", "")) != _SUPPORTED_BUILD
        ):
            raise DesktopAppError(
                "This ChatGPT desktop version has not been validated for account switching.",
                reason="unvalidated_version",
            )
        executable_name = info.get("CFBundleExecutable")
        if not isinstance(executable_name, str) or not executable_name or Path(executable_name).name != executable_name:
            raise DesktopAppError(
                "The ChatGPT application has an invalid executable declaration.",
                reason="invalid_executable",
            )
        executable = self.app_path / "Contents/MacOS" / executable_name
        for path, label in ((executable, "desktop executable"), (self._bundled_cli, "bundled Codex CLI")):
            try:
                mode = path.stat().st_mode
            except OSError as exc:
                raise DesktopAppError(
                    f"The ChatGPT {label} is missing.",
                    reason="helper_missing",
                ) from exc
            if not stat.S_ISREG(mode) or not os.access(path, os.X_OK):
                raise DesktopAppError(
                    f"The ChatGPT {label} is not executable.",
                    reason="helper_not_executable",
                )
        try:
            stats = [item.stat() for item in (self._plist_path, executable, self._bundled_cli)]
            stamp = tuple(
                (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)
                for value in stats
            )
        except OSError as exc:
            raise DesktopAppError(
                "The ChatGPT application changed during validation.",
                reason="app_changed",
            ) from exc
        if self._validation_cache is not None and self._validation_cache[0] == stamp:
            return self._validation_cache[1], self._validation_cache[2]
        self._verify_signature()
        try:
            after_stats = [item.stat() for item in (self._plist_path, executable, self._bundled_cli)]
            after_stamp = tuple(
                (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)
                for value in after_stats
            )
        except OSError as exc:
            raise DesktopAppError(
                "The ChatGPT application changed during validation.",
                reason="app_changed",
            ) from exc
        if after_stamp != stamp:
            raise DesktopAppError(
                "The ChatGPT application changed during validation.",
                reason="app_changed",
            )
        self._validation_cache = (stamp, info, executable)
        return info, executable

    def _verify_signature(self) -> None:
        try:
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(self.app_path)],
                check=True, capture_output=True, text=True, timeout=_CODESIGN_TIMEOUT,
            )
            details = subprocess.run(
                ["/usr/bin/codesign", "-d", "--verbose=4", str(self.app_path)],
                check=True, capture_output=True, text=True, timeout=_CODESIGN_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DesktopAppError(
                "The ChatGPT application signature could not be verified.",
                reason="signature_unverified",
            ) from exc
        output = f"{details.stdout}\n{details.stderr}"
        values = {}
        for line in output.splitlines():
            if "=" in line:
                key, value = line.strip().split("=", 1)
                values[key] = value
        if values.get("Identifier") != _BUNDLE_ID or values.get("TeamIdentifier") != _OPENAI_TEAM_ID:
            raise DesktopAppError(
                "The ChatGPT application is not signed by the expected publisher.",
                reason="unexpected_publisher",
            )

    def _reject_custom_backend(self) -> None:
        present = [name for name in _UNSUPPORTED_OVERRIDES if os.environ.get(name)]
        if present:
            raise DesktopAppError(
                "A custom Codex backend configuration is active; desktop switching is unsupported "
                f"while {', '.join(present)} is set.",
                reason="custom_backend",
            )

    def preflight(self, home: Path) -> dict:
        """Validate the fixed app shape and return non-secret launch metadata."""
        info, executable = self._validate()
        self._reject_custom_backend()
        home = Path(home)
        if not home.is_absolute():
            raise DesktopAppError("The Codex home must be an absolute path.", reason="custom_home")
        supported_home = Path.home() / ".codex"
        if home != supported_home:
            raise DesktopAppError(
                "A custom Codex home is not yet supported for ChatGPT desktop switching.",
                reason="custom_home",
            )
        return {
            "app_path": str(self.app_path),
            "bundle_id": _BUNDLE_ID,
            "version": str(info.get("CFBundleShortVersionString", "")),
            "build": str(info.get("CFBundleVersion", "")),
            "executable": str(executable),
            "bundled_cli": str(self._bundled_cli),
            "codex_home": str(home),
        }

    def _snapshot(self, executable: Path) -> tuple[list[_Process], set[int], set[int]]:
        rows = _processes()
        app_pids = {
            row.pid for row in rows
            if row.comm == str(executable) or _argv0(row.args) == str(executable)
        }
        owned = _descendants(rows, app_pids)
        return rows, app_pids, owned

    def _reject_external_clients(self, rows: list[_Process], owned: set[int]) -> None:
        external = [
            row for row in rows
            if _is_codex_process(row, self._bundled_cli, self.app_path) and row.pid not in owned
        ]
        if external:
            raise DesktopAppError(
                "Another Codex client is running and may share authentication. Close it before switching the desktop app."
            )

    def is_running(self) -> bool:
        _, executable = self._validate()
        _, app_pids, _ = self._snapshot(executable)
        return bool(app_pids)

    def invalidate_validation_cache(self) -> None:
        """Drop the signature cache so the next validate re-runs codesign."""
        self._validation_cache = None

    def observe_capability(self, *, now: float | None = None) -> DesktopCapability:
        """Classify installation and process state without exposing internals."""
        try:
            running = self.is_running()
        except DesktopAppError as exc:
            return capability_from_error(exc)
        observed_at = time.monotonic() if now is None else now
        state = "running" if running else "stopped"
        return DesktopCapability(state=state, reason=state, observed_at=observed_at)

    def assert_stopped(self) -> None:
        _, executable = self._validate()
        rows, app_pids, owned = self._snapshot(executable)
        if app_pids or owned:
            raise DesktopAppError("The ChatGPT desktop app or one of its backends is still running.")
        self._reject_external_clients(rows, owned)

    def _request_terminate(self, pids: set[int], executable: Path) -> None:
        try:
            from AppKit import NSRunningApplication  # type: ignore[import-not-found]
        except ImportError:
            try:
                subprocess.run(
                    ["/usr/bin/osascript", "-e", 'tell application id "com.openai.codex" to quit'],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_SUBPROCESS_TIMEOUT,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise DesktopAppError("Could not request a graceful ChatGPT desktop quit.") from exc
            return
        for pid in pids:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            # Re-check the immutable application identity immediately before
            # signalling.  A PID observed by ps may have exited and been
            # reused while Python imported AppKit.
            if app is None:
                raise DesktopAppError("Could not safely identify the running ChatGPT desktop app.")
            try:
                bundle_id = str(app.bundleIdentifier())
                app_executable = str(app.executableURL().path())
            except (AttributeError, TypeError) as exc:
                raise DesktopAppError("Could not safely identify the running ChatGPT desktop app.") from exc
            if bundle_id != _BUNDLE_ID or app_executable != str(executable):
                raise DesktopAppError("The observed desktop process changed before it could be stopped safely.")
            if not app.terminate():
                raise DesktopAppError("Could not request a graceful ChatGPT desktop quit.")

    def quit(self, timeout: float = 20) -> None:
        """Gracefully quit the app and wait for all processes it owned."""
        if not math.isfinite(timeout) or timeout < 0:
            raise DesktopAppError("Quit timeout must be a finite non-negative number.")
        _, executable = self._validate()
        rows, app_pids, owned = self._snapshot(executable)
        self._reject_external_clients(rows, owned)
        if not app_pids:
            if owned:
                raise DesktopAppError("A ChatGPT backend is running without its owning app; refusing to continue.")
            return
        watched = set(app_pids) | set(owned)
        identities = {row.pid: (row.comm, row.args) for row in rows if row.pid in watched}
        self._request_terminate(app_pids, executable)
        deadline = time.monotonic() + timeout
        while True:
            current = _processes()
            live_pids = {
                row.pid for row in current
                if row.pid in watched and identities.get(row.pid) == (row.comm, row.args)
            }
            new_descendants = _descendants(current, watched & live_pids) - watched
            watched.update(new_descendants)
            identities.update({
                row.pid: (row.comm, row.args) for row in current if row.pid in new_descendants
            })
            if not (watched & live_pids):
                # A backend that daemonized between snapshots is no longer a
                # descendant.  Recognize the bundled/normal Codex executable
                # and fail closed instead of declaring shutdown complete.
                self._reject_external_clients(current, live_pids)
                return
            if time.monotonic() >= deadline:
                raise DesktopAppError(
                    "ChatGPT or one of its backends is still running after the graceful quit request."
                )
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def launch(self, home: Path, timeout: float = 20) -> None:
        """Launch the validated app with an explicit, isolated ``CODEX_HOME``."""
        if not math.isfinite(timeout) or timeout < 0:
            raise DesktopAppError("Launch timeout must be a finite non-negative number.")
        self.preflight(home)
        _, executable = self._validate()
        self.assert_stopped()
        env = dict(os.environ)
        for name in _SECRET_ENV_NAMES | set(_UNSUPPORTED_OVERRIDES) | {"CODEX_HOME"}:
            env.pop(name, None)
        for name in list(env):
            upper = name.upper()
            if upper.startswith(("CODEX_", "OPENAI_", "CHATGPT_")) and any(
                marker in upper for marker in ("AUTH", "TOKEN", "API_KEY", "CREDENTIAL")
            ):
                env.pop(name, None)
        env["CODEX_HOME"] = str(Path(home))
        try:
            launched = subprocess.Popen(
                [str(executable)],
                env=env,
                cwd=str(Path.home()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise DesktopAppError("Could not launch the ChatGPT desktop app.") from exc
        deadline = time.monotonic() + timeout
        stable_observations = 0
        while stable_observations < 2:
            if launched.poll() is not None:
                raise DesktopAppError("ChatGPT exited before its launch could be verified.")
            stable_observations = stable_observations + 1 if self.is_running() else 0
            if stable_observations >= 2:
                return
            if time.monotonic() >= deadline:
                raise DesktopAppError("ChatGPT launch could not be verified before the timeout.")
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
