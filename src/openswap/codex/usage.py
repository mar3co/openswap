"""Codex rate limits via ``codex app-server`` (JSON-RPC over stdio), no quota spent."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from openswap import oauth
from openswap.codex.auth import CODEX_HOME_ENV

APP_SERVER_TIMEOUT_S = 20.0
CLIENT_INFO = {"name": "openswap", "title": "OpenSwap", "version": "0.1.0"}
FIVE_HOUR_MAX_MINS = 600  # a window this short or shorter is the 5h bucket


class CodexUsageError(Exception):
    """app-server did not answer ``account/rateLimits/read``."""


def _window(entry: object) -> dict | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("usedPercent"), (int, float)):
        return None
    out = {"pct": float(entry["usedPercent"])}
    ts = entry.get("resetsAt")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        iso = datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
        out["resets_at"] = iso
        out["countdown"], out["clock"] = oauth.format_reset(iso)
    return out


def rate_limits_to_usage(payload: dict, now: float) -> dict | None:
    """``rateLimits`` → OpenSwap usage dict (``five_hour`` / ``seven_day``)."""
    usage: dict = {}
    for key in ("primary", "secondary"):
        entry = payload.get(key) if isinstance(payload, dict) else None
        window = _window(entry)
        if window is None:
            continue
        mins = entry.get("windowDurationMins")
        label = "five_hour" if isinstance(mins, (int, float)) and mins <= FIVE_HOUR_MAX_MINS else "seven_day"
        usage.setdefault(label, window)
    return usage or None


def _send(proc, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _await(proc, wanted_id: int) -> dict:
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == wanted_id:
            if "error" in msg:
                err = msg["error"]
                text = err.get("message") if isinstance(err, dict) else str(err)
                raise CodexUsageError(f"app-server error: {text}")
            return msg.get("result") or {}
    raise CodexUsageError("app-server closed before answering")


def read_rate_limits(
    home: Path,
    *,
    codex_bin: str,
    popen: Callable[..., object] = subprocess.Popen,
    environ: Mapping[str, str] | None = None,
    timeout: float = APP_SERVER_TIMEOUT_S,
) -> dict:
    """``rateLimits`` for the login in ``home`` (its ``CODEX_HOME``)."""
    env = dict(os.environ if environ is None else environ)
    env.pop("OPENAI_API_KEY", None)
    env[CODEX_HOME_ENV] = str(home)
    try:
        proc = popen(
            [codex_bin, "app-server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env, cwd=str(home),
        )
    except OSError as exc:
        # Popen failed before creating a process, so callers may release this
        # short reservation with explicit unlaunched evidence.
        exc._openswap_unlaunched = True
        raise
    killer = threading.Timer(timeout, getattr(proc, "kill", lambda: None))
    killer.start()
    failure = None
    try:
        _send(proc, {"id": 1, "method": "initialize", "params": {"clientInfo": CLIENT_INFO}})
        _await(proc, 1)
        _send(proc, {"method": "initialized"})
        _send(proc, {"id": 2, "method": "account/rateLimits/read", "params": {}})
        result = _await(proc, 2)
    except BaseException as exc:
        failure = exc
    finally:
        killer.cancel()
        stopped = False
        try:
            proc.terminate()
            proc.wait(timeout=5)
            stopped = True
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
                stopped = True
            except Exception:
                stopped = False
    if failure is not None:
        failure._openswap_process_stopped = stopped
        raise failure
    # Owner decision (plan 017): as for a kickoff ping, the direct app-server
    # child's exit is the stop evidence for this short usage read; a helper it
    # detached could outlive it (the documented best-effort process-tree limit).
    if not stopped:
        error = CodexUsageError("app-server stop could not be confirmed")
        error._openswap_process_stopped = False
        raise error
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        error = CodexUsageError("app-server reply had no rateLimits")
        error._openswap_process_stopped = stopped
        raise error
    return limits
