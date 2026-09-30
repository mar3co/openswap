from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import logging
import pytest
from openswap.settings import load_worker_settings, configure_worker_service, settings_path
from openswap.worker.protocol import (
    MAX_ARTIFACT, Artifact, Claim, ProtocolError, Submission, timestamp, validate_url, event_from_dict,
)
from openswap.worker.models import JobSubmission, RemoteConnectivity, _iso


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
                                "https://host/path", "https://host?x=1", "http://127.0.0.1.evil", "https://host:bad",
                                "https://host?", "https://host#", "https://host#frag", " https://host",
                                "https://host ", "\nhttps://host", "https://ho\x01st", "", None, 123, True,
                                ["https://host"], "https://" + "h" * 2100])
def test_https_enforcement(url):
    with pytest.raises(ProtocolError, match="https_required"):
        validate_url(url)


@pytest.mark.parametrize("url", ["http://localhost:9000", "http://127.0.0.1:9000", "http://[::1]:9000", "https://host"])
def test_valid_urls(url):
    assert validate_url(url) == url


def test_url_normalizes_scheme_and_trailing_slash():
    assert validate_url("HTTPS://host/") == "https://host"
    assert validate_url("HTTP://localhost:9000") == "http://localhost:9000"
    assert validate_url("https://Control.Example:8443") == "https://control.example:8443"


def test_invalid_control_service_url_loads_disabled(tmp_path, caplog):
    for bad in (True, 123, "https://host#x", " https://host"):
        settings_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        settings_path(tmp_path).write_text(json.dumps({"worker": {"enabled": True, "controlServiceUrl": bad}}))
        with caplog.at_level(logging.WARNING, logger="openswap"):
            loaded = load_worker_settings(tmp_path)
        assert loaded.control_service_url is None and not loaded.enabled
        assert "control service URL is invalid" in caplog.text


def test_configure_worker_service_replaces_malformed_section(tmp_path):
    path = settings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schemaVersion": 1, "keep": "me", "worker": None}))
    configure_worker_service(tmp_path, "https://control.example")
    raw = json.loads(path.read_text())
    assert raw["keep"] == "me" and raw["worker"]["controlServiceUrl"] == "https://control.example"
    assert load_worker_settings(tmp_path).control_service_url == "https://control.example"


def test_remote_connectivity_matches_protocol_vocabulary():
    assert {state.value for state in RemoteConnectivity} == {"disabled", "online", "offline", "revoked", "expired"}


def test_configure_worker_service_writes_schema_version(tmp_path):
    configure_worker_service(tmp_path, "https://control.example")
    assert json.loads(settings_path(tmp_path).read_text())["schemaVersion"]


def test_multiline_task_is_valid_text():
    request = submission()
    request = replace(request, job=replace(request.job, task="Research this:\n- topic one\r\n\t- topic two"))
    assert Submission.from_dict(request.to_dict()) == request


@pytest.mark.parametrize("field, value", [("task", "bad\x00task"), ("task", "bad\x1btask"),
                                          ("idempotency_key", "key\nline"), ("workspace_id", "ws\t1"),
                                          ("worker_id", "w\rid")])
def test_other_control_characters_and_multiline_identifiers_refused(field, value):
    with pytest.raises(ProtocolError, match="invalid_request"):
        Submission.from_dict({**submission().to_dict(), field: value})


def test_runtime_limit_normalization_is_canonical():
    payload = submission().to_dict()
    integral = Submission.from_dict({**payload, "runtime_limit_s": 600})
    floating = Submission.from_dict({**payload, "runtime_limit_s": 600.0})
    assert integral == floating
    assert json.dumps(integral.to_dict(), sort_keys=True) == json.dumps(floating.to_dict(), sort_keys=True)
    assert integral.to_dict()["runtime_limit_s"] == 600 and type(integral.to_dict()["runtime_limit_s"]) is int
    assert Submission.from_dict({**payload, "runtime_limit_s": 0.5}).to_dict()["runtime_limit_s"] == 0.5
    for bad in (True, "600", None, float("inf"), float("nan"), 0, -1):
        with pytest.raises(ProtocolError, match="invalid_request"):
            Submission.from_dict({**payload, "runtime_limit_s": bad})


@pytest.mark.parametrize("bad", [10 ** 310, 0, -1, 14_401, 14_400.5, float("inf"), float("nan"), True])
def test_runtime_limit_bounds_are_wire_errors(bad):
    payload = submission().to_dict()
    with pytest.raises(ProtocolError, match="invalid_request"):
        Submission.from_dict({**payload, "runtime_limit_s": bad})


@pytest.mark.parametrize("value", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00"])
def test_out_of_range_utc_instants_are_wire_errors(value):
    with pytest.raises(ProtocolError, match="invalid_request"):
        timestamp(value)


def test_wire_timestamps_use_z_like_models():
    request = submission()
    expected = _iso(request.job.expires_at)
    assert request.to_dict()["expires_at"] == expected and expected.endswith("Z")
    assert Claim("job", 1, request.job.expires_at, request).to_dict()["lease_until"] == expected
    offset = request.job.expires_at.astimezone(timezone(timedelta(hours=2))).isoformat()
    assert Submission.from_dict({**request.to_dict(), "expires_at": offset}).to_dict()["expires_at"] == expected


@pytest.mark.parametrize("value", ["2026-10-01T00:00:00Z", "2026-10-01t00:00:00z", "2026-10-01T00:00:00.5Z",
                                  "2026-10-01T00:00:00.123456+00:00", "2026-10-01T02:30:00+02:30",
                                  "2026-09-30T23:00:00-01:00"])
def test_timestamp_accepts_rfc3339(value):
    assert timestamp(value) == datetime(2026, 10, 1, tzinfo=timezone.utc).replace(
        microsecond=timestamp(value).microsecond)


@pytest.mark.parametrize("value", ["2026-10-01 00:00:00Z", "20261001T000000Z", "2026-W40-4T00:00:00Z",
                                  "2026-10-01T00:00:00+00:00:00", "2026-10-01T00:00:00", "2026-10-01T24:00:00Z",
                                  "2026-10-01T00:00:00.1234567Z", "2026-10-01T00:00:00.Z",
                                  "2026-10-01", "2026-10-01T00:00Z", "2026-10-01T00:00:00+0000",
                                  "2026-10-01T00:00:00Z ", "2026-13-01T00:00:00Z", "", None, 1_700_000_000])
def test_timestamp_rejects_loose_forms(value):
    with pytest.raises(ProtocolError, match="invalid_request"):
        timestamp(value)


@pytest.mark.parametrize("change", [{"name": "../secret"}, {"size": 2_000_000}, {"sha256": "bad"}])
def test_artifact_validation(change):
    with pytest.raises(ProtocolError):
        Artifact.from_dict({**Artifact("result.md", b"x").to_dict(), **change})


def test_empty_artifact_accepted_but_size_zero_content_still_validated():
    empty = Artifact("empty.md", b"")
    assert Artifact.from_dict(empty.to_dict()).content == b""
    for bad in ("not base64!", None, 5, ["="], "AAAA" * 400_000):
        with pytest.raises(ProtocolError, match="invalid_request"):
            Artifact.from_dict({**empty.to_dict(), "content_base64": bad})
    with pytest.raises(ProtocolError, match="hash_mismatch"):
        Artifact.from_dict({**empty.to_dict(), "content_base64": Artifact("x", b"x").to_dict()["content_base64"]})


def test_oversize_content_is_artifact_too_large_not_hash_mismatch():
    big = Artifact("big.bin", b"a" * (MAX_ARTIFACT + 1)).to_dict()
    with pytest.raises(ProtocolError, match="artifact_too_large") as failure:
        Artifact.from_dict({**big, "size": 1})
    assert failure.value.status == 413
    with pytest.raises(ProtocolError, match="artifact_too_large"):
        Artifact.from_dict(big)


def test_events_refuse_unstructured_payload():
    with pytest.raises(ProtocolError):
        event_from_dict({"raw": "secret"})
