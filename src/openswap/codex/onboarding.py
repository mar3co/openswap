"""Isolated, browser-led Codex OAuth account onboarding.

The official CLI runs in a private temporary ``CODEX_HOME``.  Its output and
credential-bearing authorization URL remain private to this object; callers
only receive a deliberately small, non-secret state dictionary.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from openswap.codex.auth import auth_path
from openswap.codex.desktop import DesktopSwitchError, _config_check, _credential_identity
from openswap.codex.desktop_app import DesktopApp, DesktopAppError
from openswap.codex.engine import CodexEngine


_URL_RE = re.compile(r"https://[^\s<>\"']+")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_DEVICE_RE = re.compile(
    r"(?:device\s+(?:code|authorization)|enter\s+(?:this|the)\s+(?:one-time\s+)?code)\s*[:：]?\s*"
    r"([A-Z0-9]{4,}(?:-[A-Z0-9]{3,})*)",
    re.IGNORECASE,
)
_OFFICIAL_HOSTS = {"auth.openai.com", "login.openai.com", "auth0.openai.com", "chatgpt.com"}
_ENV_ALLOW = {
    "PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy", "DISPLAY", "WAYLAND_DISPLAY",
}
_MAX_OUTPUT = 1024 * 1024


class LoginSession:
    """One bounded official-CLI login attempt, intended for a worker thread."""

    def __init__(
        self,
        engine: CodexEngine,
        mode: str = "browser",
        *,
        timeout: float = 300.0,
        codex_bin: str | os.PathLike[str] | None = None,
        popen=subprocess.Popen,
        clock=time.monotonic,
    ) -> None:
        if mode not in {"browser", "device"}:
            raise ValueError("mode must be 'browser' or 'device'")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.engine = engine
        self.mode = mode
        self.timeout = float(timeout)
        self._binary = Path(codex_bin) if codex_bin is not None else None
        self._popen = popen
        self._clock = clock
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._process: subprocess.Popen | None = None
        self._temp_home: Path | None = None
        self._auth_text: str | None = None
        self._url: str | None = None
        self._public: dict = {"stage": "starting", "has_url": False}
        self._ran = False
        self._running = False

    def state(self) -> dict:
        with self._lock:
            return {**self._public, "mode": self.mode}

    def login_url(self) -> str | None:
        with self._lock:
            return self._url if self._valid_url(self._url) else None

    def run(self) -> None:
        with self._lock:
            if self._ran:
                raise RuntimeError("This login session has already run.")
            self._ran = True
            self._running = True
        try:
            _config_check(self.engine.home)
            binary = self._resolve_binary()
            if self._cancel.is_set():
                self._finish_cancelled()
                return
            temp_home = Path(tempfile.mkdtemp(prefix="openswap-codex-login-"))
            if os.name != "nt":
                os.chmod(temp_home, 0o700)
            with self._lock:
                self._temp_home = temp_home
            if self._cancel.is_set():
                self._finish_cancelled()
                return
            argv = [str(binary), "login", "-c", 'cli_auth_credentials_store="file"']
            if self.mode == "device":
                argv.append("--device-auth")
            kwargs = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "env": self._environment(temp_home),
                "cwd": str(temp_home),
            }
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                kwargs["start_new_session"] = True
            with self._lock:
                if self._cancel.is_set():
                    self._finish_cancelled_locked()
                    return
                proc = self._popen(argv, **kwargs)
                self._process = proc
                self._public = {"stage": "waiting", "has_url": False}
            self._wait(proc)
            if self._cancel.is_set():
                self._finish_cancelled()
                return
            if proc.returncode != 0:
                raise RuntimeError("The Codex sign-in did not complete. Please try again.")
            try:
                text = auth_path(temp_home).read_text(encoding="utf-8")
                ident = _credential_identity(text, label="The completed login")
            except (OSError, UnicodeError, DesktopSwitchError) as exc:
                raise RuntimeError(
                    "Sign-in finished without usable OAuth credentials. Please try again."
                ) from exc
            with self._lock:
                if self._cancel.is_set():
                    self._finish_cancelled_locked()
                    return
                self._auth_text = text
                self._public = {
                    "stage": "ready", "email": ident.email,
                    "plan": ident.plan_type, "account_id": ident.account_id,
                    "has_url": bool(self._url),
                }
        except TimeoutError:
            self._set_error("Sign-in timed out. Please try again.")
        except (DesktopSwitchError, DesktopAppError) as exc:
            self._set_error(str(exc))
        except (OSError, subprocess.SubprocessError, RuntimeError):
            self._set_error("Could not complete Codex sign-in. Please try again.")
        except BaseException:
            self._set_error("Could not complete Codex sign-in. Please try again.")
        finally:
            with self._lock:
                proc = self._process
            if proc is not None and proc.poll() is None:
                self._terminate(proc)
            with self._lock:
                dead = proc is None or proc.poll() is not None
                if dead:
                    self._process = None
                self._running = False
                terminal = self._public.get("stage") in {"error", "cancelled"}
            if terminal and dead:
                self._cleanup()

    def cancel(self) -> None:
        with self._lock:
            self._cancel.set()
            proc = self._process
        if proc is not None:
            self._terminate(proc)
        with self._lock:
            self._finish_cancelled_locked()
            dead = proc is None or proc.poll() is not None
            running = self._running
        if dead and not running:
            self._cleanup()

    def save(self, alias: str | None = None) -> str:
        with self._lock:
            if (self._cancel.is_set() or self._public.get("stage") != "ready" or
                    self._auth_text is None):
                raise RuntimeError("The login is not ready to save.")
            # Keep the frozen credential blob stable across the engine commit.
            text = self._auth_text
            number = self.engine.add_oauth_account(text, alias=alias)
            self._auth_text = None
            self._url = None
            self._public = {"stage": "saved", "has_url": False}
        self._cleanup()
        return number

    def _wait(self, proc: subprocess.Popen) -> None:
        deadline = self._clock() + self.timeout
        events: queue.Queue[bytes | None | BaseException] = queue.Queue(maxsize=32)
        stop_reader = threading.Event()

        def publish(value: bytes | None | BaseException) -> bool:
            while not stop_reader.is_set():
                try:
                    events.put(value, timeout=0.1)
                    return True
                except queue.Full:
                    continue
            return False

        def read_pipe() -> None:
            try:
                assert proc.stdout is not None
                while True:
                    reader = getattr(proc.stdout, "read1", proc.stdout.read)
                    chunk = reader(4096)
                    if not chunk:
                        break
                    if not publish(chunk if isinstance(chunk, bytes) else chunk.encode()):
                        return
            except BaseException as exc:
                publish(exc)
            finally:
                publish(None)

        reader_thread = threading.Thread(
            target=read_pipe, daemon=True, name="openswap-login-output"
        )
        reader_thread.start()
        tail = ""
        total = 0
        eof = False
        try:
            while True:
                if self._cancel.is_set():
                    self._terminate(proc)
                    return
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._terminate(proc)
                    raise TimeoutError
                try:
                    event = events.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    if proc.poll() is not None and eof:
                        return
                    continue
                if event is None:
                    eof = True
                    if proc.poll() is not None:
                        return
                elif isinstance(event, BaseException):
                    raise OSError("Could not read Codex sign-in output") from event
                else:
                    total += len(event)
                    if total > _MAX_OUTPUT:
                        self._terminate(proc)
                        raise RuntimeError("Codex sign-in produced too much output")
                    tail = (tail + event.decode("utf-8", "replace"))[-65536:]
                    self._inspect_output(tail)
        finally:
            stop_reader.set()
            reader_thread.join(timeout=1.0)
            # BufferedReader.close() may wait on the reader's internal lock.
            # Never call it from this thread while read1() is still blocked
            # (for example when an unkillable descendant retained the pipe).
            if not reader_thread.is_alive() and proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass

    def _inspect_output(self, output: str) -> None:
        output = _ANSI_RE.sub("", output)
        url = next((value.rstrip(".,);]") for value in _URL_RE.findall(output)
                    if self._valid_url(value.rstrip(".,);]"))), None)
        code_match = _DEVICE_RE.search(output) if self.mode == "device" else None
        with self._lock:
            if url:
                self._url = url
            if self._public.get("stage") == "waiting":
                state = {"stage": "waiting", "has_url": bool(self._url)}
                if code_match:
                    state["device_code"] = code_match.group(1).upper()
                self._public = state

    @staticmethod
    def _valid_url(value: str | None) -> bool:
        if not value:
            return False
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        port_ok = False
        try:
            port_ok = parsed.port in (None, 443)
        except ValueError:
            pass
        path = parsed.path.rstrip("/")
        oauth_path = (path.endswith("/oauth/authorize") or path.endswith("/authorize") or
                      path == "/codex/device")
        return (parsed.scheme == "https" and parsed.username is None and
                parsed.password is None and host in _OFFICIAL_HOSTS and port_ok and oauth_path)

    def _resolve_binary(self) -> Path:
        candidate = self._binary
        if candidate is None:
            try:
                app = DesktopApp()
                app._validate()
                candidate = app._bundled_cli
            except DesktopAppError:
                configured = self.engine._codex_bin()
                candidate = Path(configured) if configured else None
        if candidate is None:
            raise OSError("Codex CLI is unavailable")
        if not candidate.is_absolute():
            found = shutil.which(str(candidate))
            candidate = Path(found) if found else candidate
        try:
            resolved = candidate.resolve(strict=True)
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise OSError("Codex CLI is unavailable") from exc
        if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
            raise OSError("Codex CLI is unavailable")
        return resolved

    @staticmethod
    def _environment(home: Path) -> dict[str, str]:
        # Allowlist only: API keys, base URLs and CODEX_* overrides never reach the child.
        env = {key: value for key, value in os.environ.items() if key in _ENV_ALLOW}
        env["CODEX_HOME"] = str(home)
        return env

    def _terminate(self, proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2.0)
        except (OSError, subprocess.SubprocessError):
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=1.0)
            except (OSError, subprocess.SubprocessError):
                pass

    def _set_error(self, message: str) -> None:
        with self._lock:
            if self._cancel.is_set():
                self._finish_cancelled_locked()
            else:
                self._auth_text = None
                self._url = None
                self._public = {"stage": "error", "message": message, "has_url": False}

    def _finish_cancelled(self) -> None:
        with self._lock:
            self._finish_cancelled_locked()
            proc = self._process
            dead = proc is None or proc.poll() is not None
        if dead:
            self._cleanup()

    def _finish_cancelled_locked(self) -> None:
        if self._public.get("stage") != "saved":
            self._auth_text = None
            self._url = None
            self._public = {"stage": "cancelled", "has_url": False}

    def _cleanup(self) -> None:
        with self._lock:
            home, self._temp_home = self._temp_home, None
        if home is not None:
            shutil.rmtree(home, ignore_errors=True)

    def __del__(self) -> None:
        try:
            self.cancel()
        except Exception:
            pass
