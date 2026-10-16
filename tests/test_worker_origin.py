"""Optional submission origin (worker protocol v1 extension).

The ``capabilities`` operation and the ``origin`` submission field on the
protocol, the reference server and the worker client; the Mac-only refusal
list (``worker.refusedOrigins``), its CLI, status and menu copy. No Keychain,
launchctl or provider auth is touched; jobs run on the inert fake adapter.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
import threading

import pytest

from openswap.menubar_display import _remote_tasks_status_copy
from openswap.settings import (
    configure_worker_service,
    load_refused_origins,
    settings_path,
    update_worker_settings,
    write_refused_origins,
)
from openswap.worker import cli
from openswap.worker.journal import LocalJobStore
from openswap.worker.leases import AccountLeaseStore, stable_account_identity
from openswap.worker.models import REFUSABLE_ORIGINS, JobState, JobSubmission, Origin
from openswap.worker.protocol import (
    Claim, ProtocolError, Submission, capability_features, submission_origin,
)
from openswap.worker.refserver import ControlStore, make_server
from openswap.worker.remote import RemoteClient, Transport
from openswap.worker.runtime import WorkerRuntime, read_worker_snapshot
from tests.test_worker_pairing_status import keychain  # noqa: F401 (fixture)
from tests.test_worker_remote import FakeAdapter, StoreTransport

URL = "http://127.0.0.1:8765"
CHATGPT = {"kind": "assistant", "label": "ChatGPT"}


# --- protocol validation --------------------------------------------------------------------


def test_capability_features_accept_bounded_unique_names():
    assert capability_features([]) == ()
    assert capability_features(["origin"]) == ("origin",)
    # Unknown features are valid in a request; a backend ignores them.
    assert capability_features(["origin", "future_thing-2", "x" * 64]) == ("origin", "future_thing-2", "x" * 64)
    assert len(capability_features([f"f{i}" for i in range(20)])) == 20


@pytest.mark.parametrize("value", [
    None, {}, "origin", 1,
    [f"f{i}" for i in range(21)],
    ["origin", "origin"],
    [""], ["x" * 65], ["Origin"], ["has space"], ["dot.ted"], ["tab\t"], [1], [None], [["origin"]],
])
def test_capability_features_refuse(value):
    with pytest.raises(ProtocolError, match="invalid_request"):
        capability_features(value)


@pytest.mark.parametrize("kind", ["slack", "assistant", "portal", "test"])
def test_origin_accepts_each_kind_and_bounded_labels(kind):
    assert submission_origin({"kind": kind, "label": "x"}) == Origin(kind, "x")
    assert submission_origin({"kind": kind, "label": "é" * 100}).label == "é" * 100


@pytest.mark.parametrize("value", [
    None, [], "assistant",
    {"kind": "assistant"},
    {"label": "ChatGPT"},
    {"kind": "assistant", "label": "ChatGPT", "extra": 1},
    {"kind": "unspecified", "label": "ChatGPT"},  # a local policy name, never a wire kind
    {"kind": "Assistant", "label": "ChatGPT"},
    {"kind": "email", "label": "ChatGPT"},
    {"kind": 1, "label": "ChatGPT"},
    {"kind": "assistant", "label": ""},
    {"kind": "assistant", "label": "x" * 101},
    {"kind": "assistant", "label": 7},
    {"kind": "assistant", "label": None},
    {"kind": "assistant", "label": "new\nline"},
    {"kind": "assistant", "label": "tab\there"},
    {"kind": "assistant", "label": "del\x7fhere"},
    {"kind": "assistant", "label": "next\x85line"},  # C1 control (NEL)
])
def test_origin_refuses(value):
    with pytest.raises(ProtocolError, match="invalid_request"):
        submission_origin(value)


def _job(idem="idem"):
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    return JobSubmission(idem, "codex", "Research", "research", "research", expiry, 60)


def _claim(origin=None):
    value = {"job_id": "remote-1", "epoch": 1, "lease_until": "2030-01-01T00:00:00Z",
             "submission": Submission("worker", _job()).to_dict()}
    if origin is not None:
        value["submission"]["origin"] = origin
    return value


def test_submissions_and_claims_accept_origin_only_when_allowed():
    assert Claim.from_dict(_claim()).submission.origin is None
    assert Claim.from_dict(_claim(), allow_origin=True).submission.origin is None
    with pytest.raises(ProtocolError, match="invalid_request"):
        Claim.from_dict(_claim(CHATGPT))
    with pytest.raises(ProtocolError, match="invalid_request"):
        Claim.from_dict(_claim(CHATGPT), allow_account_ref=True)  # one extension never opens the other
    claim = Claim.from_dict(_claim(CHATGPT), allow_origin=True)
    assert claim.submission.origin == Origin("assistant", "ChatGPT")
    # Round-trips exactly as submitted.
    assert claim.to_dict()["submission"]["origin"] == CHATGPT
    assert "origin" not in Claim.from_dict(_claim(), allow_origin=True).to_dict()["submission"]
    for bad in ({"kind": "assistant", "label": "a\x00b"}, {"kind": "nope", "label": "x"}, "assistant"):
        with pytest.raises(ProtocolError, match="invalid_request"):
            Claim.from_dict(_claim(bad), allow_origin=True)


def test_origin_is_part_of_the_normalized_payload():
    plain = Submission("worker", _job())
    with_origin = Submission("worker", _job(), origin=Origin("assistant", "ChatGPT"))
    assert plain.to_dict() != with_origin.to_dict()
    again = Submission.from_dict(json.loads(json.dumps(with_origin.to_dict())), allow_origin=True)
    assert again == with_origin and again.to_dict() == with_origin.to_dict()


# --- reference server --------------------------------------------------------------------


@pytest.fixture
def service(tmp_path):
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(tmp_path / "service" / "db", clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    key = paired["device_key"]
    epoch = store.request("register", {}, key)["worker_epoch"]
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    return store, paired["worker_id"], key, epoch, ticks


def _service_submit(service, idem="one", origin=None):
    store, worker_id, key, _, ticks = service
    job = JobSubmission(idem, "codex", "Research", "research", "research",
                        datetime.fromtimestamp(ticks[0] + 600, timezone.utc), 60)
    value = Submission(worker_id, job).to_dict()
    if origin is not None:
        value["origin"] = origin
    return store.request("submit", value, key)


def _advertise(service, features=("origin",), epoch=None):
    store, _, key, current, _ = service
    return store.request("capabilities", {"worker_epoch": epoch or current, "features": list(features)}, key)


def test_refserver_capabilities_replace_the_set_and_ignore_unknown_features(service):
    store, worker_id, key, epoch, _ = service
    assert store.capabilities(worker_id) is None  # not sent yet
    assert _advertise(service, ["future-thing", "origin"]) == {"features": ["origin"]}
    assert store.capabilities(worker_id) == ["origin"]
    assert _advertise(service, []) == {"features": []}
    assert store.capabilities(worker_id) == []
    assert _advertise(service, ["future-thing"]) == {"features": []}
    with pytest.raises(ProtocolError, match="stale_epoch"):
        _advertise(service, epoch=epoch + 1)
    for bad in ({"worker_epoch": epoch}, {"features": ["origin"]},
                {"worker_epoch": epoch, "features": ["origin"], "extra": 1},
                {"worker_epoch": epoch, "features": ["origin", "origin"]},
                {"worker_epoch": epoch, "features": ["Origin"]},
                {"worker_epoch": epoch, "features": "origin"}):
        with pytest.raises(ProtocolError, match="invalid_request"):
            store.request("capabilities", bad, key)
    assert store.capabilities(worker_id) == []  # refused requests change nothing


def test_refserver_accepts_origin_only_from_a_worker_that_advertised_it(service):
    store, _, key, epoch, _ = service
    with pytest.raises(ProtocolError, match="invalid_request"):
        _service_submit(service, origin=CHATGPT)  # nothing advertised yet
    _advertise(service, [])
    with pytest.raises(ProtocolError, match="invalid_request"):
        _service_submit(service, origin=CHATGPT)  # advertised, but not origin
    _advertise(service)
    for bad in ({"kind": "assistant"}, {"kind": "nope", "label": "x"}, {"kind": "assistant", "label": "a\nb"},
                {"kind": "assistant", "label": "ChatGPT", "extra": 1}):
        with pytest.raises(ProtocolError, match="invalid_request"):
            _service_submit(service, idem="bad", origin=bad)
    job = _service_submit(service, origin=CHATGPT)
    # The origin is part of the normalized idempotency payload, compared verbatim.
    assert _service_submit(service, origin=dict(CHATGPT)) == job
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        _service_submit(service)
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        _service_submit(service, origin={"kind": "assistant", "label": "Claude"})
    with pytest.raises(ProtocolError, match="idempotency_conflict"):
        _service_submit(service, origin={"kind": "slack", "label": "ChatGPT"})
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert claim["job_id"] == job["job_id"] and claim["submission"]["origin"] == CHATGPT


def test_refserver_registration_clears_capabilities_and_gates_origin_jobs(service):
    """A new registration forgets the advertised set; a job carrying origin waits
    until that registration advertises it again, while plain jobs still flow."""
    store, worker_id, key, _, _ = service
    _advertise(service)
    tagged = _service_submit(service, origin=CHATGPT)
    epoch = store.request("register", {}, key)["worker_epoch"]
    store.request("heartbeat", {"worker_epoch": epoch}, key)
    assert store.capabilities(worker_id) is None
    with pytest.raises(ProtocolError, match="invalid_request"):
        _service_submit(service, idem="new", origin=CHATGPT)
    plain = _service_submit(service, idem="plain")
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert claim["job_id"] == plain["job_id"] and "origin" not in claim["submission"]
    store.request("reconcile", {"worker_epoch": epoch, "job_id": plain["job_id"], "epoch": claim["epoch"],
                                "state": "failed", "execution_stopped": False, "unlaunched": True}, key)
    assert store.request("poll", {"worker_epoch": epoch}, key)["claim"] is None
    _advertise(service, epoch=epoch)
    claim = store.request("poll", {"worker_epoch": epoch}, key)["claim"]
    assert claim["job_id"] == tagged["job_id"] and claim["submission"]["origin"] == CHATGPT


def test_refserver_revocation_clears_capabilities(service):
    store, worker_id, _, _, _ = service
    _advertise(service)
    store.revoke(worker_id)
    assert store.capabilities(worker_id) is None


def test_refserver_capabilities_storage_upgrades_an_older_database(tmp_path):
    path = tmp_path / "service" / "db"
    ControlStore(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE capabilities")
    store = ControlStore(path)  # reopening adds the table in place
    paired = store.request("pair", {"code": store.issue_code()})
    epoch = store.request("register", {}, paired["device_key"])["worker_epoch"]
    assert store.request("capabilities", {"worker_epoch": epoch, "features": ["origin"]},
                         paired["device_key"]) == {"features": ["origin"]}


# --- worker client: capabilities -----------------------------------------------------------


class _NoCapabilitiesTransport(StoreTransport):
    """A backend without the extension: ``capabilities`` is an unknown operation."""

    code = "unsupported_version"

    def request(self, operation, data):
        if operation == "capabilities":
            self.calls.append(operation)
            raise ProtocolError(self.code, 404)
        return super().request(operation, data)


def _setup(tmp_path, transport_class=StoreTransport, worker_id=None):
    ticks = [datetime.now(timezone.utc).timestamp()]
    store = ControlStore(tmp_path / "service" / "db", clock=lambda: ticks[0])
    paired = store.request("pair", {"code": store.issue_code()})
    root = tmp_path / "local"
    update_worker_settings(root, enabled=True)
    configure_worker_service(root, URL)
    adapter = FakeAdapter()
    runtime = WorkerRuntime(root, adapter=adapter, account_identity=stable_account_identity("codex", "synthetic"))
    transport = transport_class(store, paired["device_key"])
    remote = RemoteClient(runtime, URL, paired["device_key"], worker_id=worker_id or paired["worker_id"],
                          transport=transport)
    remote.tick()
    return remote, runtime, adapter, store, ticks, paired, transport, root


@pytest.fixture
def origin_setup(tmp_path):
    return _setup(tmp_path)


def _submit(setup, idem="one", origin=None):
    _, _, _, store, ticks, paired, _, _ = setup
    job = JobSubmission(idem, "codex", "Research", "research", "research",
                        datetime.fromtimestamp(ticks[0] + 100, timezone.utc), 60)
    value = Submission(paired["worker_id"], job).to_dict()
    if origin is not None:
        value["origin"] = origin
    return store.request("submit", value, paired["device_key"])["job_id"]


def _sent(transport):
    return [data for op, data in transport.requests if op == "capabilities"]


def _reconciled(transport):
    return [(d["state"], d["execution_stopped"], d["unlaunched"]) for op, d in transport.requests if op == "reconcile"]


def test_registration_advertises_origin_once(origin_setup):
    remote, _, _, store, _, paired, transport, _ = origin_setup
    assert _sent(transport) == [{"worker_epoch": remote.worker_epoch, "features": ["origin", "key_rotation"]}]
    assert store.capabilities(paired["worker_id"]) == ["origin", "key_rotation"]
    assert remote.origin_accepted()
    # Recorded durably for this enrollment and registration.
    assert remote.journal.capabilities() == (remote.worker_epoch, ("origin", "key_rotation"))
    for _ in range(3):
        remote.tick()
    assert len(_sent(transport)) == 1  # the set never changes at runtime: sent once per registration
    store.request("register", {}, paired["device_key"])
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 2 and _sent(transport)[-1]["worker_epoch"] == remote.worker_epoch
    assert remote.origin_accepted() and remote.journal.capabilities() == (remote.worker_epoch, ("origin", "key_rotation"))


@pytest.mark.parametrize("code", ["unsupported_version", "not_found"])
def test_unsupported_backend_is_recorded_until_the_next_registration(tmp_path, code):
    """Spec servers answer 404 `unsupported_version`; reference servers that
    predate the extension answer 404 `not_found`. Both stop the operation."""
    class Transport404(_NoCapabilitiesTransport):
        pass

    Transport404.code = code
    setup = _setup(tmp_path, Transport404)
    remote, runtime, adapter, store, _, paired, transport, _ = setup
    remote.tick()
    assert transport.calls.count("capabilities") == 1
    assert remote.state == "online" and not remote.origin_accepted()
    # Heartbeats and claims carry on: a plain job still runs.
    _submit(setup)
    remote.tick()
    assert "poll" in transport.calls and runtime.reconcile_once().state == JobState.SUCCEEDED
    store.request("register", {}, paired["device_key"])
    remote.tick()
    remote.tick()
    assert transport.calls.count("capabilities") == 2  # discovery again after a new registration


def test_other_errors_retry_without_blocking_claims(origin_setup):
    remote, runtime, _, store, _, paired, transport, _ = origin_setup
    store.request("register", {}, paired["device_key"])
    remote.tick()  # stale epoch: re-registers on the next tick
    transport.reject["capabilities"] = ProtocolError("service_unavailable", 503)
    remote.tick()
    assert remote.state == "online" and not remote.origin_accepted()
    _submit(origin_setup)
    remote.tick()  # retried, acknowledged; the plain claim is taken in the same pass
    assert remote.origin_accepted() and runtime.store.queue()
    sent = len(_sent(transport))
    remote.tick()
    assert len(_sent(transport)) == sent


@pytest.mark.parametrize("answer", [
    {"features": "origin"}, {"features": ["origin", "other"]}, {"features": ["origin"], "extra": 1}, {},
])
def test_malformed_acknowledgement_is_retried(origin_setup, answer):
    remote, _, _, store, _, paired, transport, _ = origin_setup
    original = transport.request

    def malformed(operation, data):
        value = original(operation, data)
        return answer if operation == "capabilities" else value

    transport.request = malformed
    store.request("register", {}, paired["device_key"])
    remote.tick()
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 3 and not remote.origin_accepted()  # never taken as acknowledged
    transport.request = original
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 4 and remote.origin_accepted()


def test_a_backend_that_accepts_no_origin_leaves_it_off(origin_setup):
    remote, _, _, store, _, paired, transport, _ = origin_setup
    original = transport.request

    def none_accepted(operation, data):
        value = original(operation, data)
        return {"features": []} if operation == "capabilities" else value

    transport.request = none_accepted
    store.request("register", {}, paired["device_key"])
    remote.tick()
    remote.tick()
    remote.tick()
    assert len(_sent(transport)) == 2 and not remote.origin_accepted()  # acknowledged: not resent
    assert remote.journal.capabilities() == (remote.worker_epoch, ())


def test_a_claim_with_origin_is_malformed_before_this_registration_advertised_it(tmp_path):
    setup = _setup(tmp_path, _NoCapabilitiesTransport)
    remote, runtime, _, store, _, paired, _, _ = setup
    # The service holds an origin the worker never advertised (another client
    # claimed the capability for this registration): the claim is malformed.
    store.request("capabilities", {"worker_epoch": remote.worker_epoch, "features": ["origin"]},
                  paired["device_key"])
    _submit(setup, origin=CHATGPT)
    remote.tick()
    assert remote.state == "offline" and remote.journal.pending() == [] and not runtime.store.queue()


def test_a_stored_origin_claim_survives_a_restart(origin_setup):
    remote, runtime, adapter, store, _, paired, transport, _ = origin_setup
    _submit(origin_setup, origin=CHATGPT)
    remote.tick()
    restarted = RemoteClient(runtime, URL, paired["device_key"], worker_id=paired["worker_id"], transport=transport)
    (binding,) = restarted.journal.pending()
    assert restarted._stored_claim(binding).submission.origin == Origin("assistant", "ChatGPT")


# --- runtime: recording and the local refusal ------------------------------------------------


def _run(setup):
    remote, runtime, *_ = setup
    remote.tick()
    result = runtime.reconcile_once()
    remote.tick()
    return result


def test_an_allowed_origin_runs_and_is_recorded(origin_setup):
    remote, runtime, adapter, store, _, paired, transport, root = origin_setup
    job_id = _submit(origin_setup, origin=CHATGPT)
    remote.tick()
    (queued,) = runtime.store.queue()
    assert queued.origin == Origin("assistant", "ChatGPT")  # recorded when admitted
    result = runtime.reconcile_once()
    remote.tick()
    assert result.state == JobState.SUCCEEDED and adapter.starts == 1
    assert runtime.get(result.job_id).origin == Origin("assistant", "ChatGPT")
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "succeeded"
    snapshot = read_worker_snapshot(root).to_dict()
    assert snapshot["last_job"]["origin"] == CHATGPT
    assert "Research" not in json.dumps(snapshot)  # never the task text


def test_a_refused_origin_fails_unlaunched_without_a_lease(origin_setup):
    remote, runtime, adapter, store, _, paired, transport, root = origin_setup
    assert cli.main(["origins", "refuse", "assistant"], backup_root=root) == 0
    job_id = _submit(origin_setup, origin=CHATGPT)
    remote.tick()
    leases = []
    original = runtime.leases.mutation_guard
    runtime.leases.mutation_guard = lambda *a, **k: leases.append(1) or original(*a, **k)
    result = runtime.reconcile_once()
    assert (result.state, result.diagnostic_code) == (JobState.FAILED, "origin_refused")
    assert result.origin == Origin("assistant", "ChatGPT") and result.pinned_account_ref is None
    assert adapter.starts == 0 and leases == []
    assert AccountLeaseStore(root, "codex").current() is None and runtime.leases.for_job(result.job_id) is None
    remote.tick()
    # `origin_refused` stays local: the service's diagnostic list is closed.
    sent = [event for op, data in transport.requests if op == "events" for event in data["events"]]
    codes = {event["diagnostic_code"] for event in sent}
    assert "provider_unavailable" in codes and "origin_refused" not in codes
    assert _reconciled(transport) == [("failed", False, True)]
    assert store.request("job", {"job_id": job_id}, paired["device_key"])["state"] == "failed"
    local_codes = {e.diagnostic_code for e in runtime.events(result.job_id).events}
    assert "origin_refused" in local_codes


def test_refusing_another_kind_still_runs_this_one(origin_setup):
    _, _, adapter, _, _, _, _, root = origin_setup
    cli.set_origin_refused(root, "slack", True)
    cli.set_origin_refused(root, "unspecified", True)
    _submit(origin_setup, origin=CHATGPT)
    assert _run(origin_setup).state == JobState.SUCCEEDED and adapter.starts == 1


def test_unspecified_refuses_a_remote_job_without_an_origin(origin_setup):
    _, runtime, adapter, _, _, _, transport, root = origin_setup
    cli.set_origin_refused(root, "unspecified", True)
    _submit(origin_setup)
    result = _run(origin_setup)
    assert (result.state, result.diagnostic_code) == (JobState.FAILED, "origin_refused")
    assert result.origin is None and adapter.starts == 0
    assert _reconciled(transport) == [("failed", False, True)]
    # A job that says where it came from still runs.
    _submit(origin_setup, idem="two", origin={"kind": "slack", "label": "OpenTag"})
    assert _run(origin_setup).state == JobState.SUCCEEDED and adapter.starts == 1


def test_a_refusal_made_after_the_claim_applies_at_launch(origin_setup):
    remote, runtime, adapter, _, _, _, _, root = origin_setup
    _submit(origin_setup, origin=CHATGPT)
    remote.tick()
    assert runtime.store.queue()
    cli.set_origin_refused(root, "assistant", True)
    result = runtime.reconcile_once()
    assert result.diagnostic_code == "origin_refused" and adapter.starts == 0
    cli.set_origin_refused(root, "assistant", False)
    _submit(origin_setup, idem="two", origin=CHATGPT)
    assert _run(origin_setup).state == JobState.SUCCEEDED


def test_an_unreadable_refusal_list_refuses_every_origin(origin_setup):
    _, _, adapter, _, _, _, _, root = origin_setup
    path = settings_path(root)
    raw = json.loads(path.read_text())
    raw["worker"]["refusedOrigins"] = "assistant"  # not a list
    path.write_text(json.dumps(raw))
    assert load_refused_origins(root) == REFUSABLE_ORIGINS
    _submit(origin_setup, origin={"kind": "slack", "label": "OpenTag"})
    result = _run(origin_setup)
    assert result.diagnostic_code == "origin_refused" and adapter.starts == 0


def test_local_jobs_have_no_origin_and_are_never_refused(tmp_path):
    root = tmp_path / "local"
    update_worker_settings(root, enabled=True)
    write_refused_origins(root, REFUSABLE_ORIGINS)
    adapter = FakeAdapter()
    runtime = WorkerRuntime(root, adapter=adapter, account_identity=stable_account_identity("codex", "local"))
    job = JobSubmission("local-one", "codex", "Research", "research", "research",
                        datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + 100, timezone.utc), 60)
    runtime.submit(job)
    assert runtime.reconcile_once().state == JobState.SUCCEEDED and adapter.starts == 1
    from openswap.worker.journal import AdmissionError

    with pytest.raises(AdmissionError):
        runtime.submit(job, origin=Origin("test", "x"))


def test_the_journal_gains_the_origin_columns_in_place(tmp_path):
    """A journal from an earlier release (no origin columns) still opens, reads
    read-only for status, and is upgraded by the worker."""
    store = LocalJobStore(tmp_path)
    epoch, _ = store.start_epoch(1)
    job = JobSubmission("old", "codex", "Research", "research", "research",
                        datetime(2030, 1, 1, tzinfo=timezone.utc), 60)
    created = store.create(job, owner_ref="local-user", worker_epoch=epoch)
    with sqlite3.connect(store.path) as db:
        db.execute("ALTER TABLE jobs DROP COLUMN origin_kind")
        db.execute("ALTER TABLE jobs DROP COLUMN origin_label")
    assert read_worker_snapshot(tmp_path).queue_depth == 1  # read-only status: no columns, no error
    assert store.get(created.job_id).origin is None  # a write connection adds them
    with sqlite3.connect(store.path) as db:
        assert {"origin_kind", "origin_label"} <= {r[1] for r in db.execute("PRAGMA table_info(jobs)")}


def test_the_control_service_has_no_way_to_set_refused_origins():
    from openswap.worker import ipc, protocol, remote

    for module in (protocol, ipc, remote):
        source = open(module.__file__, encoding="utf-8").read()
        assert "refusedOrigins" not in source and "refused_origins" not in source


# --- CLI and status ---------------------------------------------------------------------


def _main(root, *argv):
    return cli.main(list(argv), backup_root=root)


def test_origins_list_refuse_allow_and_json(tmp_path, capsys):
    root = tmp_path / "root"
    assert _main(root, "origins") == 0
    out = capsys.readouterr().out
    assert "Task origins" in out and "refused" not in out
    for kind in REFUSABLE_ORIGINS:
        assert kind in out
    assert not (root / "worker").exists()  # listing is a pure read
    assert _main(root, "origins", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"origins": [{"kind": k, "refused": False} for k in REFUSABLE_ORIGINS], "refused_origins": []}

    assert _main(root, "origins", "refuse", "assistant") == 0
    assert "refuses tasks from assistants" in capsys.readouterr().out
    assert _main(root, "origins", "refuse", "unspecified", "--json") == 0
    assert json.loads(capsys.readouterr().out)["refused_origins"] == ["assistant", "unspecified"]
    assert json.loads(settings_path(root).read_text())["worker"]["refusedOrigins"] == ["assistant", "unspecified"]
    assert _main(root, "origins", "refuse", "assistant") == 0  # idempotent
    capsys.readouterr()
    assert _main(root, "origins") == 0
    out = capsys.readouterr().out
    assert "✗ assistant" in out and "✓ slack" in out
    assert _main(root, "origins", "list", "--json") == 0
    assert json.loads(capsys.readouterr().out)["origins"][1] == {"kind": "assistant", "refused": True}

    assert _main(root, "origins", "allow", "assistant") == 0
    assert "again" in capsys.readouterr().out
    assert _main(root, "origins", "allow", "unspecified") == 0
    assert load_refused_origins(root) == ()
    assert "refusedOrigins" not in json.loads(settings_path(root).read_text())["worker"]


@pytest.mark.parametrize("argv", [["origins", "refuse", "email"], ["origins", "block", "slack"]])
def test_origins_refuses_unknown_words(tmp_path, argv, capsys):
    with pytest.raises(SystemExit) as exited:
        _main(tmp_path, *argv)
    assert exited.value.code == 2
    assert load_refused_origins(tmp_path) == ()


@pytest.mark.parametrize("argv", [["origins", "refuse"], ["origins", "list", "slack"]])
def test_origins_needs_a_kind_only_to_change_one(tmp_path, argv, capsys):
    assert _main(tmp_path, *argv) == 2
    assert capsys.readouterr().err
    assert load_refused_origins(tmp_path) == ()


def test_origin_changes_take_the_lifecycle_lock(tmp_path, monkeypatch):
    held = []
    real = cli.lifecycle_lock

    from contextlib import contextmanager

    @contextmanager
    def lock(root):
        with real(root):
            held.append(True)
            yield
            held.append(False)

    def write(root, kinds):
        assert held == [True]
        return tuple(kinds)

    import openswap.settings as settings_module

    monkeypatch.setattr(cli, "lifecycle_lock", lock)
    monkeypatch.setattr(settings_module, "write_refused_origins", write)
    assert cli.set_origin_refused(tmp_path, "assistant", True) == ("assistant",)
    assert held == [True, False]


def test_status_shows_origins_and_refusals(tmp_path, monkeypatch, capsys):
    snapshot = {"enabled": True, "process_state": "running",
                "active_job": {"job_id": "abc", "state": "running", "origin": CHATGPT}}
    monkeypatch.setattr(cli, "read_status", lambda _root: dict(snapshot))
    assert _main(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "From" in out and "ChatGPT (assistant)" in out and "Refused origins" not in out
    assert _main(tmp_path, "status", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active_job"]["origin"] == CHATGPT and "refused_origins" not in payload

    write_refused_origins(tmp_path, ["assistant", "unspecified"])
    snapshot = {"enabled": True, "process_state": "running", "active_job": None,
                "last_job": {"job_id": "abc", "state": "failed", "origin": CHATGPT}}
    assert _main(tmp_path, "status") == 0
    out = capsys.readouterr().out
    assert "Last task from" in out and "ChatGPT (assistant), failed" in out
    assert "Refused origins" in out and "assistant, unspecified" in out
    assert _main(tmp_path, "status", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["refused_origins"] == ["assistant", "unspecified"]
    assert payload["last_job"]["origin"] == CHATGPT


def test_status_strips_unprintable_label_characters():
    assert cli.origin_text({"kind": "assistant", "label": "Chat‎GPT‮"}) == "ChatGPT (assistant)"
    assert cli.origin_text(None) == "not given"
    assert cli.origin_text({"kind": "nope", "label": "x"}) == "not given"


def test_status_reads_the_origin_from_the_journal(origin_setup, capsys):
    _, _, _, _, _, _, _, root = origin_setup
    _submit(origin_setup, origin=CHATGPT)
    _run(origin_setup)
    assert _main(root, "status", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active_job"] is None and payload["last_job"]["origin"] == CHATGPT
    assert payload["last_job"]["state"] == "succeeded"


def test_menu_status_shows_the_running_tasks_origin():
    base = {"process_state": "running", "provider": {"available": True}, "queue_depth": 0}
    text = _remote_tasks_status_copy({**base, "active_job": {"state": "running", "origin": CHATGPT}},
                                     enabled=True, paused=False)
    assert "job running from ChatGPT (assistant) · queue 0" in text
    plain = _remote_tasks_status_copy({**base, "active_job": {"state": "running"}}, enabled=True, paused=False)
    assert "job running · queue 0" in plain
    hostile = _remote_tasks_status_copy(
        {**base, "active_job": {"state": "running", "origin": {"kind": "assistant", "label": "a\nb" + "x" * 200}}},
        enabled=True, paused=False)
    assert "\n" not in hostile and "x" * 101 not in hostile
    unknown = _remote_tasks_status_copy(
        {**base, "active_job": {"state": "running", "origin": {"kind": "email", "label": "x"}}},
        enabled=True, paused=False)
    assert "from" not in unknown


# --- end to end over loopback: submit-test and a refused assistant job ------------------------


WAIT = 20


def test_loopback_origin_submit_test_and_refusal(tmp_path, keychain, capsys):  # noqa: F811
    store = ControlStore(tmp_path / "service" / "db")
    with make_server(store, port=0) as server:
        serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        serving.start()
        url = f"http://127.0.0.1:{server.server_port}"
        root = tmp_path / "worker-home"
        try:
            assert _main(root, "pair", url, store.issue_code()) == 0
            from openswap.worker.pairing import load_enrollment

            enrollment = load_enrollment(url)
            update_worker_settings(root, enabled=True)
            adapter = FakeAdapter()
            runtime = WorkerRuntime(root, adapter=adapter,
                                    account_identity=stable_account_identity("codex", "synthetic-origin"))
            remote = RemoteClient(runtime, url, enrollment.device_key, worker_id=enrollment.worker_id)
            remote.tick()
            assert remote.state == "online" and remote.origin_accepted()
            assert _main(root, "origins", "refuse", "assistant") == 0
            capsys.readouterr()
            base = ["submit-test", "--url", url, "--task", "Research this synthetic topic",
                    "--workspace-id", "research", "--runtime-limit", "60", "--expires-in", "600",
                    "--i-understand-this-is-a-test-tool"]
            with pytest.raises(SystemExit):
                _main(root, *base, "--origin-kind", "assistant")  # both or neither
            capsys.readouterr()
            assert _main(root, *base, "--origin-kind", "assistant", "--origin-label", "bad\x7flabel") == 1
            refused = capsys.readouterr().err
            assert "invalid_request" in refused and "--origin-kind=assistant" in refused
            assert _main(root, *base, "--origin-kind", "assistant", "--origin-label", "ChatGPT") == 0
            job_id = json.loads(capsys.readouterr().out)["job_id"]
            remote.tick()
            result = runtime.reconcile_once()
            assert (result.state, result.diagnostic_code) == (JobState.FAILED, "origin_refused")
            assert adapter.starts == 0
            remote.tick()
            transport = Transport(url, enrollment.device_key)
            assert transport.request("job", {"job_id": job_id})["state"] == "failed"
            # A Slack job on the same Mac still runs.
            assert _main(root, *base, "--origin-kind", "slack", "--origin-label", "OpenTag") == 0
            slack_job = json.loads(capsys.readouterr().out)["job_id"]
            remote.tick()
            assert runtime.reconcile_once().state == JobState.SUCCEEDED
            remote.tick()
            assert transport.request("job", {"job_id": slack_job})["state"] == "succeeded"
            assert adapter.starts == 1
        finally:
            server.shutdown()
            serving.join(WAIT)
    assert not serving.is_alive()


def test_a_refusal_made_while_the_launch_is_authorized_still_stops_it(origin_setup):
    remote, runtime, adapter, _, _, _, transport, root = origin_setup
    _submit(origin_setup, origin=CHATGPT)
    remote.tick()
    guard = runtime.remote_launch_guard
    assert guard is not None

    def refuse_then_authorize(job_id):
        # The owner refuses assistants after the first check, while the
        # remote authorization is still in flight.
        cli.set_origin_refused(root, "assistant", True)
        return guard(job_id)

    runtime.remote_launch_guard = refuse_then_authorize
    result = runtime.reconcile_once()
    assert (result.state, result.diagnostic_code) == (JobState.FAILED, "origin_refused")
    assert adapter.starts == 0
    lease = runtime.leases.for_job(result.job_id)
    assert (lease.state, lease.reason) == ("released", "unlaunched")  # released with proof nothing ran
    remote.tick()
    assert _reconciled(transport) == [("failed", False, True)]
