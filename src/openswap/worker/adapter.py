"""Provider boundary for local worker jobs.

On a supported Mac the production factory returns the live Codex adapter,
which still runs nothing until the owner enables live execution from passing
``openswap worker live-check`` evidence (``openswap.worker.live``). Elsewhere
it returns the fail-closed unavailable adapter. Tests can implement the
protocol directly; no CLI flag or executable override installs a fake adapter
in the application.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from openswap.worker.models import (
    InterruptResult,
    JobRecord,
    ProviderAvailability,
    ProviderRun,
    ResolvedWorkspace,
    SafeEvent,
)


class ProviderLaunchRefused(RuntimeError):
    """``start()`` refused before anything could run: nothing was launched.

    The runtime releases the account lease as unlaunched and fails the job with
    ``diagnostic_code`` (an allowlisted journal code). Any other exception from
    ``start()`` leaves the launch uncertain.
    """

    def __init__(self, diagnostic_code: str):
        super().__init__(diagnostic_code)
        self.diagnostic_code = diagnostic_code


class ProviderAdapter(Protocol):
    def probe(self) -> ProviderAvailability: ...

    def start(self, job: JobRecord, workspace: ResolvedWorkspace, *, worker_epoch: int) -> ProviderRun: ...

    def events(self, run: ProviderRun, *, after_cursor: int) -> tuple[SafeEvent, ...]: ...

    def interrupt(self, run: ProviderRun) -> InterruptResult: ...


class UnavailableCodexAdapter:
    """Fail-closed production adapter; never inspects auth or launches Codex."""

    diagnostic_code = "live_adapter_disabled"
    execution_mode = "disabled"

    def probe(self) -> ProviderAvailability:
        return ProviderAvailability(False, self.diagnostic_code, None)

    def start(self, job: JobRecord, workspace: ResolvedWorkspace, *, worker_epoch: int) -> ProviderRun:
        raise RuntimeError(self.diagnostic_code)

    def events(self, run: ProviderRun, *, after_cursor: int) -> tuple[SafeEvent, ...]:
        return ()

    def interrupt(self, run: ProviderRun) -> InterruptResult:
        return InterruptResult(requested=False, execution_stopped=False, diagnostic_code=self.diagnostic_code)


def production_adapter(backup_root: Path | None = None) -> ProviderAdapter:
    """The application adapter: live-capable Codex on Apple silicon Macs, else unavailable.

    ``backup_root`` defaults to OpenSwap's backup root. Either adapter exposes
    ``execution_mode`` ("live" only once the owner opted in), so callers that
    report the mode can read it from whichever adapter they hold.
    """
    from openswap.worker.codex_cli import platform_supported

    if not platform_supported():
        return UnavailableCodexAdapter()
    if backup_root is None:
        from openswap.paths import get_backup_root

        backup_root = get_backup_root()
    from openswap.worker.codex_exec import CodexExecAdapter

    return CodexExecAdapter(Path(backup_root))


def production_claude_adapter(backup_root: Path | None = None) -> ProviderAdapter:
    """The Claude Code adapter on Apple silicon Macs (runs nothing until its own opt-in), else unavailable."""
    from openswap.worker.codex_cli import platform_supported

    if not platform_supported():
        return UnavailableCodexAdapter()
    if backup_root is None:
        from openswap.paths import get_backup_root

        backup_root = get_backup_root()
    from openswap.worker.claude_exec import ClaudeCodeAdapter

    return ClaudeCodeAdapter(Path(backup_root))
