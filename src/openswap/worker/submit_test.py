"""Test-only owner submit tool; fake adapters are injectable only by Python tests."""
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from uuid import uuid4

from openswap.settings import load_worker_settings
from openswap.worker.models import JobState, JobSubmission
from openswap.worker.pairing import load_enrollment
from openswap.worker.protocol import ProtocolError, Submission, fields, text, timestamp, validate_url
from openswap.worker.remote import Transport


def resolve_expiry(*, expires_in: float | None = None, expires_at: datetime | str | None = None,
                   allow_past: bool = False) -> datetime:
    """Exactly one of a relative or an absolute expiry, at most a day ahead.

    A retry has to resend the identical payload, so it must pass the absolute
    ``expires_at`` of the first attempt: a fresh relative value would change the
    payload and the service would answer ``idempotency_conflict``. With
    ``allow_past`` (an explicit key and explicit expiry, that is a retry) an
    expiry that has since passed is sent as it was, so the service can replay
    the original job instead of the local check hiding whether it was admitted.
    """
    if (expires_in is None) == (expires_at is None):
        raise ProtocolError("invalid_request")
    now = datetime.now(timezone.utc)
    if expires_at is None:
        if (type(expires_in) not in (int, float) or not math.isfinite(expires_in)
                or not 0 < expires_in <= 86400):
            raise ProtocolError("invalid_request")
        return now + timedelta(seconds=expires_in)
    if isinstance(expires_at, str):
        expires_at = timestamp(expires_at)
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        raise ProtocolError("invalid_request")
    # A retry's past expiry has no age limit: the service keeps the idempotency
    # record, so even a job accepted long ago can still be recovered by its key.
    ahead = (expires_at - now).total_seconds()
    if ahead > 86400 or (ahead <= 0 and not allow_past):
        raise ProtocolError("invalid_request")
    return expires_at


def submit_test(root: Path, *, url: str, task: str, workspace_id: str,
                runtime_limit: float, acknowledged: bool, expires_in: float | None = None,
                expires_at: datetime | str | None = None, idempotency_key: str | None = None,
                transport=None) -> dict:
    """Submit one canonical test job; a retry with the same key returns the same job.

    The key defaults to a fresh random value. Callers that may retry after a
    lost response must reuse the key and the absolute expiry they sent, or the
    service admits a second job (new key) or refuses the changed payload.
    """
    if acknowledged is not True:
        raise ProtocolError("test_tool_acknowledgement_required")
    url = validate_url(url)
    key = uuid4().hex if idempotency_key is None else text(idempotency_key)
    if load_worker_settings(root).control_service_url != url:
        raise ProtocolError("device_not_paired")
    enrollment = load_enrollment(url)
    if enrollment is None:
        raise ProtocolError("device_not_paired")
    # An explicit key with an explicit expiry is a retry: send the original
    # payload even after its expiry so the service returns the original job.
    expiry = resolve_expiry(expires_in=expires_in, expires_at=expires_at,
                            allow_past=idempotency_key is not None and expires_at is not None)
    try:
        job = JobSubmission(key, "codex", task, "research", workspace_id, expiry, runtime_limit)
        request = Submission.from_dict(Submission(enrollment.worker_id, job).to_dict())
    except (TypeError, ValueError, OverflowError):
        raise ProtocolError("invalid_request") from None
    result = (transport or Transport(url, enrollment.device_key)).request("submit", request.to_dict())
    # The response is untrusted: only a bounded job ID and a known state are printed.
    try:
        fields(result, {"job_id", "state"})
        return {"job_id": text(result["job_id"]), "state": JobState(result["state"]).value}
    except (ProtocolError, ValueError):
        raise ProtocolError("invalid_response") from None
