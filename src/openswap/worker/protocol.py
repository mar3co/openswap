"""Version-one wire types and strict validation; no provider execution details."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import ipaddress
import re
from urllib.parse import urlsplit

from openswap.worker.models import JobSubmission, JobState, SafeEvent, SafeEventKind
from openswap.worker.journal import validate_event_fields

VERSION = 1
MAX_BODY = 1_500_000
MAX_ARTIFACT = 1_048_576
MAX_ARTIFACTS = 8
HEARTBEAT_SECONDS = 5
MISSED_HEARTBEATS = 3
LEASE_SECONDS = 20
DEVICE_TTL_SECONDS = 30 * 86400
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "interrupted", "expired"})


class ProtocolError(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


def fields(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ProtocolError("invalid_request")
    return value


def text(value: object, limit: int = 200) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ProtocolError("invalid_request")
    return value


def integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2**53 - 1:
        raise ProtocolError("invalid_request")
    return value


def timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(text(value, 64).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ProtocolError("invalid_request") from None


def validate_url(url: str) -> str:
    """Only literal loopback addresses/localhost permit HTTP; never follow redirects."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        loopback = host == "localhost"
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.path not in ("", "/")
                or parts.scheme not in ("http", "https")
                or (parts.scheme == "http" and not loopback)
                or (port is not None and port == 0)):
            raise ValueError
        return url.rstrip("/")
    except (TypeError, ValueError):
        raise ProtocolError("https_required") from None


@dataclass(frozen=True)
class Submission:
    worker_id: str
    job: JobSubmission

    @classmethod
    def from_dict(cls, value: object) -> Submission:
        data = fields(value, {"worker_id", "idempotency_key", "provider", "task", "capability_profile",
                              "workspace_id", "expires_at", "runtime_limit_s"})
        try:
            if type(data["runtime_limit_s"]) not in (int, float):
                raise ValueError
            job = JobSubmission(text(data["idempotency_key"]), text(data["provider"]),
                                text(data["task"], 32_000), text(data["capability_profile"], 80),
                                text(data["workspace_id"]), timestamp(data["expires_at"]),
                                data["runtime_limit_s"])
            return cls(text(data["worker_id"]), job)
        except (ValueError, TypeError):
            raise ProtocolError("invalid_request") from None

    def to_dict(self) -> dict:
        return {"worker_id": self.worker_id, "idempotency_key": self.job.idempotency_key,
                "provider": self.job.provider, "task": self.job.task,
                "capability_profile": self.job.capability_profile, "workspace_id": self.job.workspace_id,
                "expires_at": self.job.expires_at.isoformat(), "runtime_limit_s": self.job.runtime_limit_s}


@dataclass(frozen=True)
class Claim:
    job_id: str
    epoch: int
    lease_until: datetime
    submission: Submission

    @classmethod
    def from_dict(cls, value: object) -> Claim:
        data = fields(value, {"job_id", "epoch", "lease_until", "submission"})
        return cls(text(data["job_id"]), integer(data["epoch"], 1), timestamp(data["lease_until"]),
                   Submission.from_dict(data["submission"]))

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "epoch": self.epoch, "lease_until": self.lease_until.isoformat(),
                "submission": self.submission.to_dict()}


def event_from_dict(value: object) -> SafeEvent:
    data = fields(value, {"job_id", "cursor", "timestamp", "kind", "state", "diagnostic_code", "execution_stopped"})
    try:
        kind = SafeEventKind(data["kind"])
        state = JobState(data["state"]) if data["state"] is not None else None
        code = validate_event_fields(kind=kind, state=state, diagnostic_code=data["diagnostic_code"],
                                     execution_stopped=data["execution_stopped"])
        return SafeEvent(text(data["job_id"]), integer(data["cursor"], 1), timestamp(data["timestamp"]),
                         kind, state, code, data["execution_stopped"])
    except (TypeError, ValueError):
        raise ProtocolError("invalid_request") from None


@dataclass(frozen=True)
class Artifact:
    name: str
    content: bytes

    def to_dict(self) -> dict:
        return {"name": self.name, "size": len(self.content), "sha256": hashlib.sha256(self.content).hexdigest(),
                "content_base64": base64.b64encode(self.content).decode("ascii")}

    @classmethod
    def from_dict(cls, value: object) -> Artifact:
        data = fields(value, {"name", "size", "sha256", "content_base64"})
        name = text(data["name"], 100)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name) or name in {".", ".."}:
            raise ProtocolError("invalid_request")
        size = integer(data["size"])
        if size > MAX_ARTIFACT:
            raise ProtocolError("artifact_too_large", 413)
        try:
            content = base64.b64decode(text(data["content_base64"], MAX_BODY), validate=True) if size else b""
        except ValueError:
            raise ProtocolError("invalid_request") from None
        if len(content) != size or hashlib.sha256(content).hexdigest() != data["sha256"]:
            raise ProtocolError("hash_mismatch")
        return cls(name, content)
