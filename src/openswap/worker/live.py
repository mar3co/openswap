"""Whether this Mac runs real provider jobs: the single live-execution switch.

``execution_mode(backup_root)`` is the one place that answers "disabled" or
"live". It is "live" only after the owner explicitly enabled it from a passing
``openswap worker live-check`` evidence file; the default is "disabled". The
adapter re-checks this before every probe and launch, the worker status reports
it, and a control-service extension can report it through
``WorkerRuntime.execution_mode()``.

Enabling never happens implicitly: not on install, pairing, enable, an upgrade
or a passing check without the owner's yes.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from openswap.locking import FileLock

from openswap.settings import LiveExecutionSettings, load_live_execution, write_live_execution
from openswap.worker.codex_cli import CODEX_VERSION_OUTPUT, PinnedCodex, platform_supported

DISABLED = "disabled"
LIVE = "live"

EVIDENCE_KIND = "openswap_live_check"
EVIDENCE_SCHEMA = 1
MAX_EVIDENCE_BYTES = 1024 * 1024
# Every phase-1 live gate the evidence must report as passed.
REQUIRED_GATES = (
    "pinned_cli",
    "account_identity",
    "default_login_unchanged",
    "tool_surface",
    "sandbox_wrapper",
    "research_run",
    "sandbox_exec",
    "stop",
    "kill_recovery",
)


class LiveModeError(RuntimeError):
    def __init__(self, code: str, problems: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.problems = problems


LIVE_LOCK_TIMEOUT_SECONDS = 60.0


@contextmanager
def live_lock(backup_root: Path, *, timeout: float = LIVE_LOCK_TIMEOUT_SECONDS):
    """Serialize the opt-in with launch commitment across processes.

    ``enable``/``disable`` write the opt-in while holding it, and the adapter
    holds it from its last mode check until the provider is released, so once
    ``live disable`` returns no job that has not already been released can
    start. Raises :class:`LiveModeError` when it cannot be taken in time.
    """
    from openswap.worker.containment import ensure_private_dir

    worker_dir = Path(backup_root) / "worker"
    try:
        ensure_private_dir(worker_dir)
        lock = FileLock(worker_dir / "live.lock", timeout=timeout)
        acquired = lock.acquire()
    except Exception:
        # Unwritable, read-only or exhausted: a controlled refusal, never a
        # traceback, and to the adapter proof that nothing was launched.
        raise LiveModeError("live_lock_unavailable") from None
    if not acquired:
        raise LiveModeError("live_lock_busy")
    try:
        yield
    finally:
        lock.release()


def execution_mode(backup_root: Path) -> str:
    """``"live"`` when the owner's opt-in is recorded on a supported Mac, else ``"disabled"``.

    Cheap (one settings read): the binary itself is re-verified by the
    adapter before each launch, which fails the job closed if it changed.
    """
    try:
        live = load_live_execution(Path(backup_root))
    except Exception:
        return DISABLED
    return LIVE if live.enabled and platform_supported() else DISABLED


def evidence_dir(backup_root: Path) -> Path:
    return Path(backup_root) / "worker" / "live-evidence"


def evidence_problems(data: object, *, pinned: PinnedCodex | None = None) -> tuple[str, ...]:
    """Why ``data`` is not passing phase-1 evidence for this binary (empty when it is)."""
    if not isinstance(data, dict):
        return ("evidence_invalid",)
    problems = []
    if data.get("kind") != EVIDENCE_KIND or data.get("schema") != EVIDENCE_SCHEMA:
        problems.append("evidence_invalid")
    codex = data.get("codex")
    if not isinstance(codex, dict) or codex.get("version") != CODEX_VERSION_OUTPUT:
        problems.append("codex_version_mismatch")
    elif pinned is not None and codex.get("binary_sha256") != pinned.binary_sha256:
        problems.append("codex_binary_mismatch")
    gates = data.get("gates")
    if not isinstance(gates, dict):
        problems.append("gates_missing")
    else:
        for name in REQUIRED_GATES:
            gate = gates.get(name)
            if not isinstance(gate, dict) or gate.get("passed") is not True:
                problems.append(f"gate_failed:{name}")
    if data.get("passed") is not True:
        problems.append("evidence_not_passed")
    if evidence_account(data) is None:
        problems.append("evidence_account_missing")
    return tuple(dict.fromkeys(problems))


def evidence_account(data: object) -> str | None:
    """The account identity the evidence was gathered on, if well-formed."""
    account = data.get("account") if isinstance(data, dict) else None
    identity = account.get("identity") if isinstance(account, dict) else None
    if isinstance(identity, str) and re.fullmatch(r"(?:codex|claude):[0-9a-f]{64}", identity):
        return identity
    return None


def read_evidence(path: Path) -> tuple[dict, str]:
    """The evidence document and the SHA-256 of its exact bytes."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_EVIDENCE_BYTES + 1)
    except OSError:
        raise LiveModeError("evidence_unreadable") from None
    if len(raw) > MAX_EVIDENCE_BYTES:
        raise LiveModeError("evidence_invalid")
    try:
        data = json.loads(raw)
    except ValueError:
        raise LiveModeError("evidence_invalid") from None
    return data, hashlib.sha256(raw).hexdigest()


def latest_evidence(backup_root: Path) -> Path | None:
    directory = evidence_dir(backup_root)
    try:
        files = sorted(p for p in directory.glob("live-check-*.json") if p.is_file() and not p.is_symlink())
    except OSError:
        return None
    return files[-1] if files else None


def enable_live(backup_root: Path, evidence_path: Path, pinned: PinnedCodex) -> LiveExecutionSettings:
    """Record the owner's opt-in, bound to passing evidence for ``pinned``."""
    if not platform_supported():
        raise LiveModeError("unsupported_platform")
    data, digest = read_evidence(Path(evidence_path))
    problems = evidence_problems(data, pinned=pinned)
    if problems:
        raise LiveModeError("evidence_not_passing", problems)
    account = evidence_account(data)
    with live_lock(backup_root):
        # Each passing check adds its account; a new binary starts over.
        current = load_live_execution(Path(backup_root))
        accounts = current.accounts if current.enabled and current.codex_sha256 == pinned.binary_sha256 else ()
        return write_live_execution(Path(backup_root), LiveExecutionSettings(
            enabled=True, evidence_sha256=digest, codex_sha256=pinned.binary_sha256,
            enabled_at=datetime.now(timezone.utc).isoformat(),
            accounts=tuple(dict.fromkeys((*accounts, account))),
        ))


def disable_live(backup_root: Path) -> LiveExecutionSettings:
    """Turn live execution off. Waits for a launch in progress to commit or
    refuse; once this returns, no further job starts. A job already running
    is not stopped (``openswap worker stop`` does that)."""
    with live_lock(backup_root):
        return write_live_execution(Path(backup_root), LiveExecutionSettings())


def live_status(backup_root: Path) -> dict:
    live = load_live_execution(Path(backup_root))
    return {
        "execution_mode": execution_mode(backup_root),
        "opted_in": live.enabled,
        "evidence_sha256": live.evidence_sha256,
        "codex_sha256": live.codex_sha256,
        "enabled_at": live.enabled_at,
        "checked_accounts": list(live.accounts),
    }
