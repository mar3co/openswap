"""Test-only owner submit tool; fake adapters are injectable only by Python tests."""
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from uuid import uuid4

from openswap.settings import load_worker_settings
from openswap.worker.models import JobSubmission
from openswap.worker.pairing import load_enrollment
from openswap.worker.protocol import ProtocolError, Submission, validate_url
from openswap.worker.remote import Transport


def submit_test(root: Path, *, url: str, task: str, workspace_id: str,
                runtime_limit: float, expires_in: float, acknowledged: bool,
                transport=None) -> dict:
    if acknowledged is not True:
        raise ProtocolError("test_tool_acknowledgement_required")
    url = validate_url(url)
    if load_worker_settings(root).control_service_url != url:
        raise ProtocolError("device_not_paired")
    enrollment = load_enrollment(url)
    if enrollment is None:
        raise ProtocolError("device_not_paired")
    if not math.isfinite(expires_in) or not 0 < expires_in <= 86400:
        raise ProtocolError("invalid_request")
    try:
        job = JobSubmission(uuid4().hex, "codex", task, "research", workspace_id,
                            datetime.now(timezone.utc) + timedelta(seconds=expires_in), runtime_limit)
        request = Submission.from_dict(Submission(enrollment.worker_id, job).to_dict())
    except (TypeError, ValueError, OverflowError):
        raise ProtocolError("invalid_request") from None
    return (transport or Transport(url, enrollment.device_key)).request("submit", request.to_dict())
