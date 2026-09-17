#!/usr/bin/env python3
"""Safely probe whether a Codex app-server reloads file-backed auth.

The probe uses a disposable home directory and synthetic API keys. It never
starts a model turn, reads the user's Codex configuration, or prints secrets or
raw protocol messages.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import queue
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Any


class ProbeError(RuntimeError):
    """A sanitized probe failure safe to show to the operator."""


ACCOUNT_TYPES = {"apiKey", "chatgpt", "amazonBedrock"}
AUTH_MODES = {
    "apikey",
    "chatgpt",
    "chatgptAuthTokens",
    "agentIdentity",
    "personalAccessToken",
    "bedrockApiKey",
}
LOGIN_TYPES = {"apiKey", "chatgpt", "chatgptDeviceCode", "chatgptAuthTokens"}


def safe_enum(value: object, allowed: set[str]) -> str | None:
    if value is None:
        return None
    return value if isinstance(value, str) and value in allowed else "unknown"


def isolated_child_env(home: Path) -> dict[str, str]:
    return {
        "CODEX_HOME": str(home / "codex"),
        "HOME": str(home / "home"),
        "USERPROFILE": str(home / "home"),
        "XDG_CACHE_HOME": str(home / "xdg-cache"),
        "XDG_CONFIG_HOME": str(home / "xdg-config"),
        "XDG_DATA_HOME": str(home / "xdg-data"),
        "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8",
    }


class JsonRpcProcess:
    def __init__(self, binary: Path, home: Path, timeout: float) -> None:
        self.timeout = timeout
        env = isolated_child_env(home)
        for directory in env.values():
            if directory.startswith(str(home)):
                Path(directory).mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [
                str(binary),
                "app-server",
                "--stdio",
                "--config",
                'cli_auth_credentials_store="file"',
            ],
            cwd=home,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=os.name != "nt",
        )
        self.messages: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.reader = threading.Thread(target=self._read_stdout, daemon=True)
        self.reader.start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(message, dict):
                    self.messages.put(message)
        finally:
            self.messages.put(None)

    def send(self, message: dict[str, Any]) -> None:
        if self.proc.poll() is not None or self.proc.stdin is None:
            raise ProbeError("app-server exited unexpectedly")
        self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def receive_response(self, request_id: int) -> tuple[dict[str, Any], list[str | None]]:
        deadline = time.monotonic() + self.timeout
        auth_updates: list[str | None] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError(f"timed out waiting for response {request_id}")
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise ProbeError(f"timed out waiting for response {request_id}") from exc
            if message is None:
                raise ProbeError("app-server closed its output unexpectedly")
            if message.get("method") == "account/updated":
                params = message.get("params")
                value = params.get("authMode") if isinstance(params, dict) else None
                auth_updates.append(safe_enum(value, AUTH_MODES))
            if message.get("id") == request_id:
                if "error" in message:
                    error = message.get("error")
                    code = error.get("code") if isinstance(error, dict) else "unknown"
                    raise ProbeError(f"app-server rejected response {request_id} (code {code})")
                return message.get("result", {}), auth_updates

    def observe_updates(self, seconds: float) -> list[str | None]:
        deadline = time.monotonic() + seconds
        updates: list[str | None] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return updates
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty:
                return updates
            if message is None:
                return updates
            if message.get("method") == "account/updated":
                params = message.get("params")
                value = params.get("authMode") if isinstance(params, dict) else None
                updates.append(safe_enum(value, AUTH_MODES))

    def close(self) -> None:
        self._stop_process()
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        if self.proc.stdout is not None:
            try:
                self.proc.stdout.close()
            except OSError:
                pass
        self.reader.join(timeout=self.timeout)

    def _signal_group(self, sig: signal.Signals) -> None:
        """Signal this probe's isolated POSIX process group if it still exists."""
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            pass

    def _stop_process(self) -> None:
        """Bound cleanup and prevent descendants from retaining stdio pipes."""
        if os.name == "nt":
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=self.timeout)
            return

        if self.proc.poll() is None:
            self._signal_group(signal.SIGTERM)
            try:
                self.proc.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                self._signal_group(signal.SIGKILL)
                self.proc.wait(timeout=self.timeout)
        # The parent may exit while descendants from its private session remain.
        # Kill that group before closing stdout so a descendant cannot hold it open.
        self._signal_group(signal.SIGKILL)

    def __enter__(self) -> JsonRpcProcess:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def account_type(result: dict[str, Any]) -> str | None:
    account = result.get("account")
    value = account.get("type") if isinstance(account, dict) else None
    return safe_enum(value, ACCOUNT_TYPES)


def initialize(server: JsonRpcProcess) -> None:
    server.send(
        {
            "method": "initialize",
            "id": 0,
            "params": {
                "clientInfo": {
                    "name": "openswap_auth_reload_probe",
                    "title": "OpenSwap Auth Reload Probe",
                    "version": "1",
                }
            },
        }
    )
    server.receive_response(0)
    server.send({"method": "initialized", "params": {}})


def read_account(server: JsonRpcProcess, request_id: int) -> tuple[str | None, list[str | None]]:
    server.send(
        {
            "method": "account/read",
            "id": request_id,
            "params": {"refreshToken": False},
        }
    )
    result, updates = server.receive_response(request_id)
    return account_type(result), updates


def resolve_binary(value: str) -> Path:
    candidate = shutil.which(value)
    if candidate is None:
        raise ProbeError("Codex binary was not found")
    path = Path(candidate).resolve()
    if not path.is_file():
        raise ProbeError("Codex binary is not a file")
    return path


def validate_observe_seconds(value: float) -> float:
    if not math.isfinite(value) or value < 0:
        raise ProbeError("observation duration must be finite and non-negative")
    return value


def write_synthetic_auth_atomically(auth_file: Path) -> None:
    """Publish complete synthetic credentials without exposing a partial file."""
    temporary = auth_file.with_name(f".{auth_file.name}.probe-tmp")
    try:
        temporary.write_text(json.dumps({"OPENAI_API_KEY": "sk-fabricated-file"}))
        temporary.chmod(0o600)
        os.replace(temporary, auth_file)
    finally:
        temporary.unlink(missing_ok=True)


def run_probe(binary: Path, observe_seconds: float, timeout: float = 5.0) -> dict[str, Any]:
    observe_seconds = validate_observe_seconds(observe_seconds)
    phases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="openswap-codex-auth-probe-") as raw_home:
        root = Path(raw_home)
        auth_file = root / "codex" / "auth.json"

        with JsonRpcProcess(binary, root, timeout) as server:
            initialize(server)
            initial_type, _ = read_account(server, 1)
            phases.append({"phase": "initial", "accountType": initial_type})
            write_synthetic_auth_atomically(auth_file)
            updates = server.observe_updates(observe_seconds)
            current_type, read_updates = read_account(server, 2)
            phases.append(
                {
                    "phase": "file_added_live",
                    "accountType": current_type,
                    "accountUpdated": bool(updates or read_updates),
                }
            )

        with JsonRpcProcess(binary, root, timeout) as server:
            initialize(server)
            restarted_type, _ = read_account(server, 3)
            phases.append({"phase": "after_add_restart", "accountType": restarted_type})
            auth_file.unlink()
            updates = server.observe_updates(observe_seconds)
            current_type, read_updates = read_account(server, 4)
            phases.append(
                {
                    "phase": "file_removed_live",
                    "accountType": current_type,
                    "accountUpdated": bool(updates or read_updates),
                }
            )

        with JsonRpcProcess(binary, root, timeout) as server:
            initialize(server)
            removed_type, _ = read_account(server, 5)
            phases.append({"phase": "after_remove_restart", "accountType": removed_type})
            server.send(
                {
                    "method": "account/login/start",
                    "id": 6,
                    "params": {"type": "apiKey", "apiKey": "sk-fabricated-protocol"},
                }
            )
            login_result, login_updates = server.receive_response(6)
            late_updates = server.observe_updates(observe_seconds)
            logged_in_type, read_updates = read_account(server, 7)
            phases.append(
                {
                    "phase": "protocol_api_key_login",
                    "accountType": logged_in_type,
                    "loginType": safe_enum(login_result.get("type"), LOGIN_TYPES),
                    "authModes": login_updates + late_updates + read_updates,
                    "authFileCreated": auth_file.is_file(),
                }
            )

    return {"binary": str(binary), "observeSeconds": observe_seconds, "phases": phases}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex", help="Codex executable path or command")
    parser.add_argument(
        "--observe-seconds",
        type=float,
        default=0.5,
        help="seconds to wait for unsolicited account updates (default: 0.5)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = run_probe(
            resolve_binary(args.codex), validate_observe_seconds(args.observe_seconds)
        )
    except (OSError, ProbeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__}))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
