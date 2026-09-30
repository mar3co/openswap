from datetime import datetime, timedelta, timezone
import pytest
from openswap.settings import load_worker_settings, configure_worker_service
from openswap.worker.protocol import Artifact, Claim, ProtocolError, Submission, validate_url, event_from_dict
from openswap.worker.models import JobSubmission


def submission():
    return Submission("worker", JobSubmission("key", "codex", "Research", "research", "research",
                       datetime.now(timezone.utc) + timedelta(hours=1), 60))


def test_wire_roundtrip_and_settings(tmp_path):
    request = submission()
    assert Submission.from_dict(request.to_dict()) == request
    claim = Claim("job", 1, request.job.expires_at, request)
    assert Claim.from_dict(claim.to_dict()) == claim
    assert Artifact.from_dict(Artifact("result.md", b"result").to_dict()).content == b"result"
    assert load_worker_settings(tmp_path).control_service_url is None
    configure_worker_service(tmp_path, "https://control.example/")
    assert load_worker_settings(tmp_path).control_service_url == "https://control.example"
    assert not load_worker_settings(tmp_path).enabled
    configure_worker_service(tmp_path, None)
    assert load_worker_settings(tmp_path).control_service_url is None


@pytest.mark.parametrize("extra", ["account", "model", "path", "env", "argv"])
def test_closed_submission(extra):
    with pytest.raises(ProtocolError):
        Submission.from_dict({**submission().to_dict(), extra: "forbidden"})


@pytest.mark.parametrize("url", ["http://example.com", "ftp://localhost", "https://user:pass@host",
                                "https://host/path", "https://host?x=1", "http://127.0.0.1.evil", "https://host:bad"])
def test_https_enforcement(url):
    with pytest.raises(ProtocolError):
        validate_url(url)


@pytest.mark.parametrize("url", ["http://localhost:9000", "http://127.0.0.1:9000", "http://[::1]:9000", "https://host"])
def test_valid_urls(url):
    assert validate_url(url) == url


@pytest.mark.parametrize("change", [{"name": "../secret"}, {"size": 2_000_000}, {"sha256": "bad"}])
def test_artifact_validation(change):
    with pytest.raises(ProtocolError):
        Artifact.from_dict({**Artifact("result.md", b"x").to_dict(), **change})


def test_events_refuse_unstructured_payload():
    with pytest.raises(ProtocolError):
        event_from_dict({"raw": "secret"})


def test_multiline_task_is_valid_text():
    from dataclasses import replace
    request = submission()
    request = replace(request, job=replace(request.job, task="Research this:\n- topic one\n- topic two"))
    assert Submission.from_dict(request.to_dict()) == request


def test_url_rejects_whitespace_before_parsing():
    with pytest.raises(ProtocolError, match="https_required"):
        validate_url("\nhttps://control.example")
