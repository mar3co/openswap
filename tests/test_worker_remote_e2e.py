"""Real loopback HTTP roundtrip, with mocked login Keychain and inert adapter."""
from datetime import datetime, timezone
import json
import threading

import pytest

from tests.test_worker_remote import FakeAdapter
from tests.test_worker_pairing_status import keychain
from openswap.settings import update_worker_settings
from openswap.worker import cli
from openswap.worker.leases import stable_account_identity
from openswap.worker.models import JobState
from openswap.worker.pairing import load_enrollment
from openswap.worker.protocol import Artifact
from openswap.worker.refserver import ControlStore, make_server
from openswap.worker.remote import RemoteClient, Transport
from openswap.worker.remote_state import ConfiguredRemote
from openswap.worker.runtime import WorkerRuntime


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
            assert b"Synthetic cited result" in Artifact.from_dict(artifact).content
            assert adapter.starts == 1
            assert cli.main(["unpair"], backup_root=root) == 0
            assert not keychain
        finally:
            server.shutdown()
            serving.join(2)
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
    import openswap.worker.remote_state as state_module
    monkeypatch.setattr(state_module, "HEARTBEAT_SECONDS", 0.01)
    store = ControlStore(tmp_path / "service" / "db")
    ready, admitted, uploaded, stop = (threading.Event() for _ in range(4))
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
            original_submit = runtime.submit
            def record_submit(submission, **kwargs):
                result = original_submit(submission, **kwargs)
                admitted.set()
                return result
            runtime.submit = record_submit
            class ObservedTransport(Transport):
                def request(self, operation, data):
                    response = super().request(operation, data)
                    if operation == "heartbeat":
                        ready.set()
                    if operation == "upload":
                        uploaded.set()
                    return response
            def factory(runtime, origin, key, *, worker_id=""):
                return RemoteClient(runtime, origin, key, worker_id=worker_id,
                                    transport=ObservedTransport(origin, key))
            configured = ConfiguredRemote(runtime, client_factory=factory)
            polling = threading.Thread(target=configured.run, args=(stop,))
            polling.start()
            assert ready.wait(2)
            assert cli.main(["submit-test", "--url", url, "--task", "Background synthetic test",
                             "--workspace-id", "research", "--runtime-limit", "60", "--expires-in", "600",
                             "--i-understand-this-is-a-test-tool"], backup_root=root) == 0
            assert admitted.wait(2)
            assert runtime.reconcile_once().state == JobState.SUCCEEDED
            assert uploaded.wait(2)
        finally:
            stop.set()
            if polling is not None:
                polling.join(2)
                assert not polling.is_alive()
            server.shutdown()
            serving.join(2)
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
