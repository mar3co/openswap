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
    execution_mode = "disabled"

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


EXECUTION_DISABLED = "disabled"
EXECUTION_LIVE = "live"


def execution_mode(adapter: object | None = None) -> str:
    """Whether this Mac runs remote jobs for real: ``"disabled"`` or ``"live"``.

    The one hook behind the readiness report sent to the control service and
    the setup summary. It reads ``adapter.execution_mode`` (the production
    adapter when ``adapter`` is None), and anything other than an explicit
    ``"live"`` is ``"disabled"``. The production adapter is
    UnavailableCodexAdapter today, which declares ``"disabled"``; an adapter
    that really launches the provider declares ``execution_mode = "live"``.
    Test adapters declare nothing and so report ``"disabled"``.
    """
    if adapter is None:
        adapter = production_adapter()
    mode = getattr(adapter, "execution_mode", None)
    return EXECUTION_LIVE if mode == EXECUTION_LIVE else EXECUTION_DISABLED
