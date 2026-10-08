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
import os
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


def pinned_provider(backup_root: Path) -> str:
    """The provider of the pinned account (Codex when nothing valid is pinned)."""
    from openswap.settings import load_worker_settings
    from openswap.worker.accounts import provider_of

    try:
        pin = load_worker_settings(Path(backup_root)).pinned_account_ref
    except Exception:
        pin = None
    return provider_of(pin) or "codex"


def pinned_execution_mode(backup_root: Path) -> str:
    """``execution_mode`` for the pinned account's provider: what status reports."""
    return execution_mode(backup_root, pinned_provider(backup_root))


def execution_mode(backup_root: Path, provider: str = "codex") -> str:
    """``"live"`` when the owner's opt-in for ``provider`` is recorded on a supported Mac.

    Cheap (one settings read): the binary itself is re-verified by the
    adapter before each launch, which fails the job closed if it changed.
    Each provider has its own opt-in, from its own passing live check.
    """
    try:
        live = load_live_execution(Path(backup_root), provider)
    except Exception:
        return DISABLED
    if not live.enabled or not platform_supported():
        return DISABLED
    # An opt-in restored or copied from another Mac or install never applies.
    return LIVE if live.host_binding == current_host_binding(backup_root) else DISABLED


_HOST_CACHE: dict = {}


def current_host_binding(backup_root: Path) -> str | None:
    """:func:`host_binding` for this process, cached (it does not change while running)."""
    key = (id(host_binding), os.path.realpath(backup_root))
    if key not in _HOST_CACHE:
        value = host_binding(Path(backup_root))
        if value is None:
            return None  # not cached: try again next time
        _HOST_CACHE[key] = value
    return _HOST_CACHE[key]


def evidence_dir(backup_root: Path) -> Path:
    return Path(backup_root) / "worker" / "live-evidence"


def host_binding(backup_root: Path, *, run=None) -> str | None:
    """A stable, non-identifying binding of evidence to this Mac and this install.

    SHA-256 of the hardware UUID (``IOPlatformUUID``) and the backup root's
    real path, so evidence gathered on another Mac, or for another OpenSwap
    install on this one, is never accepted. None when it cannot be read.
    """
    import subprocess

    try:
        result = (run or subprocess.run)(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                         capture_output=True, text=True, check=False, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"', result.stdout or "")
    if result.returncode != 0 or found is None:
        return None
    material = f"openswap-live-check:{found.group(1).upper()}:{os.path.realpath(backup_root)}"
    return hashlib.sha256(material.encode()).hexdigest()


def evidence_provider(data: object) -> str:
    """The provider a live-check evidence document is for (Codex for older files)."""
    provider = data.get("provider", "codex") if isinstance(data, dict) else None
    return provider if provider in ("codex", "claude") else "invalid"


def evidence_problems(data: object, *, pinned=None, provider: str = "codex",
                      host: str | None = None) -> tuple[str, ...]:
    """Why ``data`` is not passing phase-1 evidence for this provider's binary (empty when it is)."""
    if not isinstance(data, dict):
        return ("evidence_invalid",)
    problems = []
    if data.get("kind") != EVIDENCE_KIND or data.get("schema") != EVIDENCE_SCHEMA:
        problems.append("evidence_invalid")
    if evidence_provider(data) != provider:
        problems.append("evidence_for_another_provider")
    if provider == "claude":
        cli = data.get("cli")
        if not isinstance(cli, dict) or not isinstance(cli.get("binary_sha256"), str):
            problems.append("claude_binary_missing")
        elif pinned is not None and cli.get("binary_sha256") != pinned.binary_sha256:
            problems.append("claude_binary_mismatch")
    else:
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
    if host is not None and data.get("host_binding") != host:
        # Gathered on another Mac (or for another install): this one's
        # sandbox, containment and managed configuration were never tested.
        problems.append("evidence_from_another_mac")
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


def latest_evidence(backup_root: Path, provider: str = "codex") -> Path | None:
    """The newest evidence file for ``provider``."""
    directory = evidence_dir(backup_root)
    pattern = "live-check-claude-*.json" if provider == "claude" else "live-check-[0-9]*.json"
    try:
        files = sorted(p for p in directory.glob(pattern) if p.is_file() and not p.is_symlink())
    except OSError:
        return None
    return files[-1] if files else None


def enable_live(backup_root: Path, evidence_path: Path, pinned, provider: str = "codex") -> LiveExecutionSettings:
    """Record the owner's opt-in for ``provider``, bound to passing evidence for ``pinned``."""
    if not platform_supported():
        raise LiveModeError("unsupported_platform")
    data, digest = read_evidence(Path(evidence_path))
    host = host_binding(Path(backup_root))
    if host is None:
        raise LiveModeError("host_unverifiable")
    problems = evidence_problems(data, pinned=pinned, provider=provider, host=host)
    if problems:
        raise LiveModeError("evidence_not_passing", problems)
    account = evidence_account(data)
    with live_lock(backup_root):
        # Each passing check adds its account; a new binary starts over.
        current = load_live_execution(Path(backup_root), provider)
        accounts = current.accounts if current.enabled and current.codex_sha256 == pinned.binary_sha256 else ()
        return write_live_execution(Path(backup_root), LiveExecutionSettings(
            enabled=True, evidence_sha256=digest, codex_sha256=pinned.binary_sha256,
            enabled_at=datetime.now(timezone.utc).isoformat(),
            accounts=tuple(dict.fromkeys((*accounts, account))), host_binding=host,
        ), provider)


def disable_live(backup_root: Path, provider: str = "codex") -> LiveExecutionSettings:
    """Turn live execution off for ``provider``. Waits for a launch in progress
    to commit or refuse; once this returns, no further job of that provider
    starts. A job already running is not stopped (``openswap worker stop``
    does that)."""
    with live_lock(backup_root):
        return write_live_execution(Path(backup_root), LiveExecutionSettings(), provider)


def live_status(backup_root: Path, provider: str = "codex") -> dict:
    live = load_live_execution(Path(backup_root), provider)
    return {
        "provider": provider,
        "execution_mode": execution_mode(backup_root, provider),
        "opted_in": live.enabled,
        "evidence_sha256": live.evidence_sha256,
        "binary_sha256": live.codex_sha256,
        "codex_sha256": live.codex_sha256,
        "enabled_at": live.enabled_at,
        "checked_accounts": list(live.accounts),
    }
