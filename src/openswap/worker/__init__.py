"""Local worker infrastructure. Remote access and live Codex execution are disabled."""

from openswap.worker.models import JobState, WorkerSnapshot

__all__ = ["JobState", "WorkerSnapshot"]
