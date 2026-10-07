"""Version-one wire types and strict validation; no provider execution details."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import hashlib
import ipaddress
import math
import re
from urllib.parse import urlsplit

from openswap.worker.models import MAX_JOB_RUNTIME_SECONDS, JobSubmission, JobState, SafeEvent, SafeEventKind
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


def text(value: object, limit: int = 200, *, allow_multiline: bool = False) -> str:
    """Bounded non-empty string; control characters are refused except newline/CR/tab when multiline."""
    if not isinstance(value, str) or not value or len(value) > limit or any(
            ord(c) < 32 and not (allow_multiline and c in "\n\r\t") for c in value):
        raise ProtocolError("invalid_request")
    return value


def integer(value: object, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum or value > 2**53 - 1:
        raise ProtocolError("invalid_request")
    return value


# Fractions are capped at microseconds: longer ones would be truncated silently
# and two distinct expiries could canonicalize to one idempotent payload.
# Seconds are 00-59: RFC 3339 leap seconds (":60") are excluded from the wire
# grammar because they cannot be represented as a datetime instant. Offset
# hours use the same 00-23 range as the time of day.
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?([Zz]|[+-]([01]\d|2[0-3]):[0-5]\d)")


def timestamp(value: object) -> datetime:
    """Strict RFC 3339 date-time with an explicit offset: no space separator, basic format, week dates
    or more than six fractional digits."""
    raw = text(value, 64)
    if not _RFC3339.fullmatch(raw):
        raise ProtocolError("invalid_request")
    try:
        parsed = datetime.fromisoformat(raw[:10] + "T" + raw[11:].upper().replace("Z", "+00:00"))
        # Instants whose UTC form leaves the representable range (year 1 or
        # 9999 at an extreme offset) are wire errors, not internal ones.
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ProtocolError("invalid_request") from None


def stamp(value: datetime) -> str:
    """Wire form of a timestamp: UTC with the `Z` designator, matching models._iso."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def validate_url(url: object) -> str:
    """Only literal loopback addresses/localhost permit HTTP; never follow redirects.

    Returns the normalized origin (lowercase scheme and host, no trailing slash). Non-strings,
    whitespace, control characters, `?` and `#` are refused even when empty.
    """
    try:
        if (not isinstance(url, str) or not url or len(url) > 2048 or "?" in url or "#" in url
                or any(c.isspace() or ord(c) < 32 for c in url)):
            raise ValueError
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
        # Hostnames are case-insensitive and a default port is implied: one origin
        # must hash to one enrollment.
        scheme, netloc = parts.scheme.lower(), parts.netloc.lower()
        if port == {"http": 80, "https": 443}[scheme] or (port is None and netloc.endswith(":")):
            netloc = netloc.rsplit(":", 1)[0]
        return parts._replace(scheme=scheme, netloc=netloc).geturl().rstrip("/")
    except (TypeError, ValueError):
        raise ProtocolError("https_required") from None


def runtime_limit(value: object) -> int | float:
    """Normalize the runtime limit so 600 and 600.0 share one canonical wire form.

    The documented bound (finite, greater than zero, at most 14,400 s) is
    enforced here, before model construction, so an oversized integer cannot
    overflow the model's float check.
    """
    if type(value) not in (int, float) or (isinstance(value, float) and not math.isfinite(value)):
        raise ProtocolError("invalid_request")
    if not 0 < value <= MAX_JOB_RUNTIME_SECONDS:
        raise ProtocolError("invalid_request")
    return int(value) if isinstance(value, float) and value.is_integer() else value


MAX_ADVERTISED_ACCOUNTS = 20
MAX_ACCOUNT_LABEL = 100


@dataclass(frozen=True)
class Submission:
    worker_id: str
    job: JobSubmission
    # Optional account choice extension: an ``account_ref`` the target worker
    # advertised. Accepted only where the caller says the extension applies.
    account_ref: str | None = None

    @classmethod
    def from_dict(cls, value: object, *, allow_account_ref: bool = False) -> Submission:
        data = fields(value, {"worker_id", "idempotency_key", "provider", "task", "capability_profile",
                              "workspace_id", "expires_at", "runtime_limit_s"},
                      {"account_ref"} if allow_account_ref else frozenset())
        try:
            job = JobSubmission(text(data["idempotency_key"]), text(data["provider"]),
                                text(data["task"], 32_000, allow_multiline=True), text(data["capability_profile"], 80),
                                text(data["workspace_id"]), timestamp(data["expires_at"]),
                                runtime_limit(data["runtime_limit_s"]))
            account_ref = text(data["account_ref"]) if "account_ref" in data else None
            return cls(text(data["worker_id"]), job, account_ref)
        except (ValueError, TypeError):
            raise ProtocolError("invalid_request") from None

    def to_dict(self) -> dict:
        value = {"worker_id": self.worker_id, "idempotency_key": self.job.idempotency_key,
                 "provider": self.job.provider, "task": self.job.task,
                 "capability_profile": self.job.capability_profile, "workspace_id": self.job.workspace_id,
                 "expires_at": stamp(self.job.expires_at), "runtime_limit_s": runtime_limit(self.job.runtime_limit_s)}
        if self.account_ref is not None:
            value["account_ref"] = self.account_ref
        return value


@dataclass(frozen=True)
class Claim:
    job_id: str
    epoch: int
    lease_until: datetime
    submission: Submission

    @classmethod
    def from_dict(cls, value: object, *, allow_account_ref: bool = False) -> Claim:
        """``allow_account_ref`` only for a worker that advertised accounts: any other
        worker treats a submission carrying ``account_ref`` as a malformed claim."""
        data = fields(value, {"job_id", "epoch", "lease_until", "submission"})
        return cls(text(data["job_id"]), integer(data["epoch"], 1), timestamp(data["lease_until"]),
                   Submission.from_dict(data["submission"], allow_account_ref=allow_account_ref))

    def to_dict(self) -> dict:
        return {"job_id": self.job_id, "epoch": self.epoch, "lease_until": stamp(self.lease_until),
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
        digest = data["sha256"]
        # A malformed digest is a schema error; hash_mismatch is for a well-formed one that differs.
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ProtocolError("invalid_request")
        if size > MAX_ARTIFACT:
            raise ProtocolError("artifact_too_large", 413)
        try:
            encoded = data["content_base64"]
            if not isinstance(encoded, str) or len(encoded) > MAX_BODY:
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
        except ValueError:
            raise ProtocolError("invalid_request") from None
        if len(content) > MAX_ARTIFACT:
            raise ProtocolError("artifact_too_large", 413)
        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
            raise ProtocolError("hash_mismatch")
        return cls(name, content)


@dataclass(frozen=True)
class AdvertisedAccount:
    """One entry of the optional ``accounts`` operation: an opaque reference,
    an owner-chosen label and whether it is the worker's default."""
    account_ref: str
    label: str
    default: bool

    def to_dict(self) -> dict:
        return {"account_ref": self.account_ref, "label": self.label, "default": self.default}


def advertised_accounts(value: object) -> tuple[AdvertisedAccount, ...]:
    """Validate an ``accounts`` array: 0-20 closed entries, unique references, at most one default."""
    if not isinstance(value, list) or len(value) > MAX_ADVERTISED_ACCOUNTS:
        raise ProtocolError("invalid_request")
    entries = []
    for item in value:
        data = fields(item, {"account_ref", "label", "default"})
        if type(data["default"]) is not bool:
            raise ProtocolError("invalid_request")
        entries.append(AdvertisedAccount(text(data["account_ref"]), text(data["label"], MAX_ACCOUNT_LABEL),
                                         data["default"]))
    if len({entry.account_ref for entry in entries}) != len(entries) or sum(e.default for e in entries) > 1:
        raise ProtocolError("invalid_request")
    return tuple(entries)
