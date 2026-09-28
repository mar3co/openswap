"""Provider boundary for local worker jobs.

The production factory is intentionally unavailable while Plan 017 Phase 1 is
blocked. Tests can implement the protocol directly; no CLI flag or executable
override installs a fake adapter in the application.
"""

from __future__ import annotations

from typing import Protocol

from openswap.worker.models import (
    InterruptResult,
    JobRecord,
    ProviderAvailability,
    ProviderRun,
    ResolvedWorkspace,
    SafeEvent,
)


class ProviderAdapter(Protocol):
    def probe(self) -> ProviderAvailability: ...

    def start(self, job: JobRecord, workspace: ResolvedWorkspace, *, worker_epoch: int) -> ProviderRun: ...

    def events(self, run: ProviderRun, *, after_cursor: int) -> tuple[SafeEvent, ...]: ...

    def interrupt(self, run: ProviderRun) -> InterruptResult: ...


class UnavailableCodexAdapter:
    """Fail-closed production adapter; never inspects auth or launches Codex."""

    diagnostic_code = "live_adapter_disabled"

    def probe(self) -> ProviderAvailability:
        return ProviderAvailability(False, self.diagnostic_code, None)

    def start(self, job: JobRecord, workspace: ResolvedWorkspace, *, worker_epoch: int) -> ProviderRun:
        raise RuntimeError(self.diagnostic_code)

    def events(self, run: ProviderRun, *, after_cursor: int) -> tuple[SafeEvent, ...]:
        return ()

    def interrupt(self, run: ProviderRun) -> InterruptResult:
        return InterruptResult(requested=False, execution_stopped=False, diagnostic_code=self.diagnostic_code)


def production_adapter() -> ProviderAdapter:
    """Return the only application adapter enabled in Phase 2."""
    return UnavailableCodexAdapter()
