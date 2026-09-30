"""Real loopback HTTP roundtrip, with mocked login Keychain and inert adapter."""
import base64
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading

import pytest

from tests.test_worker_remote import FakeAdapter, StoreTransport
from tests.test_worker_pairing_status import PairTransport, keychain
from openswap.settings import update_worker_settings
from openswap.worker import cli, pairing
from openswap.worker import submit_test as submit_test_module
from openswap.worker.leases import stable_account_identity
from openswap.worker.models import JobState, JobSubmission
from openswap.worker.pairing import load_enrollment
from openswap.worker.protocol import Artifact, ProtocolError, Submission, stamp
from openswap.worker.refserver import ControlStore, make_server
from openswap.worker.remote import RemoteClient, Transport
from openswap.worker.remote_state import ConfiguredRemote
from openswap.worker.runtime import WorkerRuntime
from openswap.worker.submit_test import resolve_expiry, submit_test

# Longer than the transport's 5 s socket timeout, so a blocked request cannot
# leave a thread alive at join time and mask the real failure.
WAIT = 6


def test_loopback_pair_cli_submit_fake_run_events_and_artifact(tmp_path, keychain, capsys):
    store = ControlStore(tmp_path / "service" / "db")
    with make_server(store, port=0) as server:
        # make_server has bound/listened before return; no sleep for readiness.
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        url = f"http://127.0.0.1:{server.server_port}"
        root = tmp_path / "worker-home"
        try:
            assert cli.main(["pair", url, store.issue_code()], backup_root=root) == 0
            enrollment = load_enrollment(url)
            update_worker_settings(root, enabled=True)
            adapter = FakeAdapter()
            runtime = WorkerRuntime(root, adapter=adapter,
                                    account_identity=stable_account_identity("codex", "synthetic-e2e"))
            remote = RemoteClient(runtime, url, enrollment.device_key, worker_id=enrollment.worker_id)
            remote.tick()
            assert remote.state == "online"
            capsys.readouterr()
            arguments = ["submit-test", "--url", url, "--task", "Research this synthetic topic",
                         "--workspace-id", "research", "--runtime-limit", "60", "--expires-in", "600",
                         "--i-understand-this-is-a-test-tool"]
            assert cli.main(arguments, backup_root=root) == 0
            job_id = json.loads(capsys.readouterr().out)["job_id"]
            remote.tick()
            result = runtime.reconcile_once()
            assert result.state == JobState.SUCCEEDED
            # Unknown files in the output root must never be exported.
            result_dir = runtime.backup_root / "worker" / "research" / result.job_id
            (result_dir / "not-an-artifact.txt").write_text("must stay local")
            remote.tick()
            transport = Transport(url, enrollment.device_key)
            assert transport.request("job", {"job_id": job_id})["state"] == "succeeded"
            page = transport.request("events", {"job_id": job_id, "after_cursor": 0})
            assert page["next_cursor"] >= 4
            assert any(event["execution_stopped"] is True for event in page["events"])
            metadata = transport.request("artifacts", {"job_id": job_id})["artifacts"]
            assert [item["name"] for item in metadata] == ["result.md"]
            artifact = transport.request("artifacts", {"job_id": job_id, "name": "result.md"})["artifact"]
            content = Artifact.from_dict(artifact).content
            assert b"Synthetic cited result" in content
            assert metadata[0]["sha256"] == artifact["sha256"] == hashlib.sha256(content).hexdigest()
            # Tampered bytes or digest from the service are refused by the client's verification.
            with pytest.raises(ProtocolError, match="hash_mismatch"):
                Artifact.from_dict({**artifact, "sha256": hashlib.sha256(content + b"x").hexdigest()})
            with pytest.raises(ProtocolError, match="hash_mismatch"):
                Artifact.from_dict({**artifact, "content_base64": base64.b64encode(content[:-1] + b"?").decode()})
            assert adapter.starts == 1
            assert cli.main(["unpair"], backup_root=root) == 0
            assert not keychain
        finally:
            server.shutdown()
            serving.join(WAIT)
            assert not serving.is_alive()


@pytest.mark.parametrize("acknowledged", [False, True])
def test_submit_test_requires_guard_and_enrollment(tmp_path, capsys, acknowledged):
    arguments = ["submit-test", "--url", "http://localhost", "--task", "test",
                 "--workspace-id", "research", "--runtime-limit", "60", "--expires-in", "60"]
    if acknowledged:
        arguments.append("--i-understand-this-is-a-test-tool")
    assert cli.main(arguments, backup_root=tmp_path) == 1
    assert ("device_not_paired" if acknowledged else "test_tool_acknowledgement_required") in capsys.readouterr().err


@pytest.mark.parametrize("option,value", [("--expires-in", "nan"), ("--expires-in", "-1"),
                                         ("--runtime-limit", "inf"), ("--runtime-limit", "0")])
def test_submit_test_rejects_invalid_limits(tmp_path, keychain, monkeypatch, capsys, option, value):
    from tests.test_worker_pairing_status import PairTransport
    from openswap.worker import pairing
    monkeypatch.setattr(pairing, "Transport", lambda *_: PairTransport())
    assert cli.main(["pair", "http://localhost", "one-use"], backup_root=tmp_path) == 0
    args = ["submit-test", "--url", "http://localhost", "--task", "test", "--workspace-id", "research",
            "--runtime-limit", "60", "--expires-in", "60", "--i-understand-this-is-a-test-tool"]
    args[args.index(option) + 1] = value
    assert cli.main(args, backup_root=tmp_path) == 1
    assert "invalid_request" in capsys.readouterr().err


def test_configured_background_polling_loop(tmp_path, keychain, capsys, monkeypatch):
    """The polling thread binds, admits and uploads; the test waits on durable signals.

    ``admitted`` is derived from the binding write plus the renew that follows
    local admission, never from ``runtime.submit`` itself: the launch fence
    reads the binding, so signalling before it is durable would race it.
    """
    import openswap.worker.remote_state as state_module
    monkeypatch.setattr(state_module, "HEARTBEAT_SECONDS", 0.01)
    store = ControlStore(tmp_path / "service" / "db")
    ready, bound, admitted, uploaded, stop = (threading.Event() for _ in range(5))
    with make_server(store, port=0) as server:
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        url = f"http://127.0.0.1:{server.server_port}"
        root = tmp_path / "local"
        polling = None
        try:
            assert cli.main(["pair", url, store.issue_code()], backup_root=root) == 0
            enrollment = load_enrollment(url)
            update_worker_settings(root, enabled=True)
            runtime = WorkerRuntime(root, adapter=FakeAdapter(),
                                    account_identity=stable_account_identity("codex", "synthetic-background"))
            class ObservedTransport(Transport):
                def request(self, operation, data):
                    response = super().request(operation, data)
                    if operation == "heartbeat":
                        ready.set()
                    if operation == "renew" and bound.is_set():
                        admitted.set()  # the client renews again only after local admission
                    if operation == "upload":
                        uploaded.set()
                    return response
            def factory(runtime, origin, key, *, worker_id=""):
                client = RemoteClient(runtime, origin, key, worker_id=worker_id,
                                      transport=ObservedTransport(origin, key))
                original_update = client.journal.update
                def observed_update(remote_id, **changes):
                    original_update(remote_id, **changes)
                    if "local_id" in changes:
                        bound.set()
                client.journal.update = observed_update
                return client
            configured = ConfiguredRemote(runtime, client_factory=factory)
            polling = threading.Thread(target=configured.run, args=(stop,))
            polling.start()
            assert ready.wait(WAIT)
            assert cli.main(["submit-test", "--url", url, "--task", "Background synthetic test",
                             "--workspace-id", "research", "--runtime-limit", "60", "--expires-in", "600",
                             "--i-understand-this-is-a-test-tool"], backup_root=root) == 0
            assert admitted.wait(WAIT)
            assert runtime.reconcile_once().state == JobState.SUCCEEDED
            assert uploaded.wait(WAIT)
        finally:
            stop.set()
            if polling is not None:
                polling.join(WAIT)
                assert not polling.is_alive()
            server.shutdown()
            serving.join(WAIT)
            assert not serving.is_alive()


def _paired_store(tmp_path):
    """A reference store with one paired, registered and online worker, without sockets."""
    store = ControlStore(tmp_path / "service" / "db")
    root = tmp_path / "local"
    url = "http://127.0.0.1:8765"
    pairing.pair(root, url, store.issue_code(), transport=StoreTransport(store, None))
    transport = StoreTransport(store, load_enrollment(url).device_key)
    transport.request("register", {})
    transport.request("heartbeat", {"worker_epoch": 1})
    return store, root, url, transport


def _arguments(url, **overrides):
    return {"url": url, "task": "Retry synthetic test", "workspace_id": "research",
            "runtime_limit": 60.0, "expires_in": 600.0, "acknowledged": True, **overrides}


def test_submit_test_retry_with_the_same_key_returns_the_same_job(tmp_path, keychain):
    store, root, url, transport = _paired_store(tmp_path)
    expires_at = stamp(datetime.now(timezone.utc) + timedelta(seconds=600))
    same = dict(_arguments(url, expires_in=None, expires_at=expires_at), idempotency_key="retry-me")
    transport.drop_response = "submit"  # the service committed, the response was lost
    with pytest.raises(ProtocolError, match="service_unavailable"):
        submit_test(root, **same, transport=transport)
    retried = submit_test(root, **same, transport=transport)
    assert retried["state"] == "queued"
    assert submit_test(root, **same, transport=transport) == retried
    with closing(store.connect()) as db:
        assert [row[0] for row in db.execute("SELECT id FROM jobs")] == [retried["job_id"]]
    # A changed payload under the same key is a conflict, not a second job; a
    # relative expiry is such a change, which is why the retry must be absolute.
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        submit_test(root, **{**same, "task": "Different task"}, transport=transport)
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        submit_test(root, **_arguments(url), idempotency_key="retry-me", transport=transport)
    assert submit_test(root, **_arguments(url), transport=transport)["job_id"] != retried["job_id"]


@pytest.mark.parametrize("kwargs", [{}, {"expires_in": 60, "expires_at": "2030-01-01T00:00:00Z"},
                                    {"expires_at": "yesterday"}, {"expires_at": "2000-01-01T00:00:00Z"},
                                    {"expires_at": "2999-01-01T00:00:00Z"}, {"expires_at": "2030-01-01 00:00:00Z"},
                                    {"expires_in": float("nan")}, {"expires_in": 0}, {"expires_in": 86401}])
def test_resolve_expiry_requires_exactly_one_bounded_form(kwargs):
    with pytest.raises(ProtocolError, match="invalid_request"):
        resolve_expiry(**kwargs)


def test_resolve_expiry_round_trips_the_printed_stamp():
    later = resolve_expiry(expires_in=600)
    assert resolve_expiry(expires_at=stamp(later)) == later


def test_submit_test_cli_prints_the_key_to_reuse_on_failure(tmp_path, keychain, monkeypatch, capsys):
    store, root, url, transport = _paired_store(tmp_path)
    transport.unreachable = True
    monkeypatch.setattr(submit_test_module, "Transport", lambda *_: transport)
    arguments = ["submit-test", "--url", url, "--task", "test", "--workspace-id", "research",
                 "--runtime-limit", "60", "--expires-in", "600", "--i-understand-this-is-a-test-tool"]
    assert cli.main([*arguments, "--idempotency-key", "keep-this-key"], backup_root=root) == 1
    err = capsys.readouterr().err
    assert "service_unavailable" in err and "--idempotency-key keep-this-key --expires-at " in err
    assert cli.main(arguments, backup_root=root) == 1
    hint = capsys.readouterr().err.split("Retry with ")[1].split(" to reuse")[0].split()
    generated, expires_at = hint[1], hint[3]
    assert hint[::2] == ["--idempotency-key", "--expires-at"]
    assert len(generated) == 32 and int(generated, 16) >= 0  # a fresh key was generated and printed
    # Retrying with the printed hint resends the identical submission.
    transport.unreachable = False
    retry = [a for a in arguments if a not in {"--expires-in", "600"}]
    assert cli.main([*retry, "--idempotency-key", generated, "--expires-at", expires_at], backup_root=root) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["state"] == "queued"
    assert cli.main([*retry, "--idempotency-key", generated, "--expires-at", expires_at], backup_root=root) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert [op for op, _ in transport.requests if op == "submit"].count("submit") == 4


@pytest.mark.parametrize("key", ["", "key\nline", "k" * 201])
def test_submit_test_rejects_invalid_idempotency_keys(tmp_path, keychain, key):
    store, root, url, transport = _paired_store(tmp_path)
    with pytest.raises(ProtocolError, match="invalid_request"):
        submit_test(root, **_arguments(url), idempotency_key=key, transport=transport)
    assert transport.calls == ["register", "heartbeat"]  # refused before any request


@pytest.mark.parametrize("response", [{}, {"job_id": "abc"}, {"job_id": "abc", "state": "bogus"},
                                      {"job_id": "", "state": "queued"}, {"job_id": 7, "state": "queued"},
                                      {"job_id": "abc", "state": "queued", "task": "echoed"}])
def test_submit_test_cli_rejects_malformed_responses(tmp_path, keychain, monkeypatch, capsys, response):
    monkeypatch.setattr(pairing, "Transport", lambda *_: PairTransport())
    assert cli.main(["pair", "http://localhost", "one-use"], backup_root=tmp_path) == 0
    class CannedTransport:
        def request(self, operation, data):
            return response
    monkeypatch.setattr(submit_test_module, "Transport", lambda *_: CannedTransport())
    capsys.readouterr()
    assert cli.main(["submit-test", "--url", "http://localhost", "--task", "test", "--workspace-id", "research",
                     "--runtime-limit", "60", "--expires-in", "60", "--i-understand-this-is-a-test-tool"],
                    backup_root=tmp_path) == 1
    captured = capsys.readouterr()
    assert "invalid_response" in captured.err and captured.out == ""  # nothing untrusted is echoed


def test_submit_test_cli_surfaces_queue_full_over_http(tmp_path, keychain, capsys):
    store = ControlStore(tmp_path / "service" / "db")
    with make_server(store, port=0) as server:
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        url = f"http://127.0.0.1:{server.server_port}"
        root = tmp_path / "local"
        try:
            assert cli.main(["pair", url, store.issue_code()], backup_root=root) == 0
            enrollment = load_enrollment(url)
            transport = Transport(url, enrollment.device_key)
            transport.request("register", {})
            transport.request("heartbeat", {"worker_epoch": 1})
            expires = datetime.now(timezone.utc) + timedelta(seconds=600)
            for index in range(20):  # the reference queue limit per worker
                job = JobSubmission(f"fill-{index}", "codex", "Fill", "research", "research", expires, 60)
                transport.request("submit", Submission(enrollment.worker_id, job).to_dict())
            assert cli.main(["submit-test", "--url", url, "--task", "One too many", "--workspace-id", "research",
                             "--runtime-limit", "60", "--expires-in", "600", "--idempotency-key", "overflow",
                             "--i-understand-this-is-a-test-tool"], backup_root=root) == 1
            err = capsys.readouterr().err
            assert "queue_full" in err and "service_unavailable" not in err and "--idempotency-key overflow" in err
        finally:
            server.shutdown()
            serving.join(WAIT)
            assert not serving.is_alive()


def test_https_transport_uses_truststore_even_with_uppercase_scheme(monkeypatch):
    import ssl
    import truststore
    calls = []
    def context(protocol):
        calls.append(protocol)
        return ssl.SSLContext(protocol)
    monkeypatch.setattr(truststore, "SSLContext", context)
    transport = Transport("HTTPS://control.example")
    assert transport.url == "https://control.example"
    assert calls == [ssl.PROTOCOL_TLS_CLIENT]
