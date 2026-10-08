"""Live Codex adapter, pinned CLI, isolated sign-in and live opt-in (no real Codex)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the live Codex adapter is macOS-only")

from openswap.settings import (
    LiveExecutionSettings,
    WorkerWorkspace,
    configure_worker_local_policy,
    load_live_execution,
    update_worker_settings,
    write_live_execution,
)
from openswap.worker import codex_cli, codex_exec, live, live_cli
from openswap.worker import containment as cont
from openswap.worker.adapter import ProviderLaunchRefused, UnavailableCodexAdapter, production_adapter
from openswap.worker.codex_exec import CodexExecAdapter
from openswap.worker.containment import ContainmentError, JobHandle, StopProof
from openswap.worker.leases import AccountLeaseStore, stable_account_identity
from openswap.worker.models import (
    InterruptResult,
    JobRecord,
    JobState,
    JobSubmission,
    ProviderRun,
    ResolvedWorkspace,
    SafeEventKind,
)
from openswap.worker.runtime import WorkerRuntime

ACCOUNT_ID = "acct-live-1"
IDENTITY = stable_account_identity("codex", ACCOUNT_ID)
OTHER_IDENTITY = stable_account_identity("codex", "acct-other")
BINARY_SHA = "ab" * 32


def _jwt(claims: dict) -> str:
    def b64(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"


def auth_json(account_id: str = ACCOUNT_ID) -> str:
    claims = {"email": "owner@example.com",
              "https://api.openai.com/auth": {"chatgpt_account_id": account_id, "chatgpt_plan_type": "pro"}}
    return json.dumps({"tokens": {"id_token": _jwt(claims), "account_id": account_id,
                                  "refresh_token": "synthetic-refresh", "access_token": "synthetic"}})


def pinned(binary="/fake/codex"):
    return codex_cli.PinnedCodex(Path(binary), codex_cli.CODEX_VERSION_OUTPUT, codex_cli.ASSET_SHA256, BINARY_SHA)


class FakeContainment:
    """Records launches; ``script`` lines become the provider's stdout."""

    def __init__(self, script=(), *, exit_status=0, result="# Result\nhttps://example.com\n"):
        self.script = list(script)
        self.exit = exit_status
        self.result = result
        self.proof = StopProof(True, False, 0)
        self.launches = []
        self.stops = 0
        self.launch_error = None
        self.running = False

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        if self.launch_error is not None:
            raise self.launch_error
        self.launches.append({"job_id": job_id, "run_dir": run_dir, "argv": argv, "env": env,
                              "cwd": cwd, "stdin": stdin_text})
        handle = JobHandle(cont.job_label(job_id), "gui/501", Path(run_dir), "boot", 4000 + len(self.launches),
                           900, True)
        cont._save_handle(handle)
        (Path(run_dir) / "stdout.jsonl").write_text("".join(json.dumps(line) + "\n" for line in self.script))
        if self.result is not None:
            (Path(run_dir) / "last-message.md").write_text(self.result)
        if self.exit is not None:
            (Path(run_dir) / "exit").write_text(f"{self.exit}\n")
        else:
            self.running = True
        return handle

    def exit_status(self, handle):
        return self.exit

    def leader_alive(self, handle):
        return self.running

    def stop(self, handle, timeout=15.0):
        self.stops += 1
        self.running = False
        return self.proof

    def recover(self, run_dir):
        return self.proof if cont.load_handle(Path(run_dir)) is not None else None


SUCCESS_SCRIPT = [
    {"type": "thread.started", "thread_id": "t-1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "1", "type": "web_search", "query": "q"}},
    {"type": "item.completed", "item": {"id": "2", "type": "agent_message", "text": "done"}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}},
]


def make_adapter(tmp_path, containment, *, mode="live"):
    return CodexExecAdapter(
        tmp_path, containment=containment, verify=lambda **kw: pinned(),
        mode=lambda: mode, sleep=lambda s: None, managed=lambda home: [],
    )


def sign_in(tmp_path, identity=IDENTITY, account_id=ACCOUNT_ID):
    home = codex_exec.prepare_home(tmp_path, identity)
    (home / "auth.json").write_text(auth_json(account_id))
    return home


def job_record(job_id="a" * 32, identity=IDENTITY, task="Find the latest Python release"):
    now = datetime.now(timezone.utc)
    return JobRecord(
        job_id=job_id, idempotency_key="k", owner_ref="local-user", provider="codex", task=task,
        capability_profile="research", workspace_id="research", state=JobState.STARTING,
        created_at=now, updated_at=now, expires_at=now + timedelta(hours=1), runtime_limit_s=600,
        pinned_account_ref=identity, provider_session_id=None, worker_epoch=1, generation=3,
        diagnostic_code=None, event_cursor=0,
    )


def workspace(tmp_path, *sources):
    out = tmp_path / "research" / ("a" * 32)
    out.mkdir(parents=True, exist_ok=True)
    return ResolvedWorkspace("research", out, tuple(sources))


def drain(adapter, run, limit=20):
    events = []
    cursor = 0
    for _ in range(limit):
        batch = adapter.events(run, after_cursor=cursor)
        for event in batch:
            assert event.cursor > cursor
            cursor = event.cursor
            events.append(event)
        if events and events[-1].kind == SafeEventKind.PROVIDER_FINISHED:
            break
    return events


# -- configuration, argv and environment ----------------------------------------


def test_config_selects_the_research_profile_and_never_a_sandbox_mode(tmp_path):
    text = codex_exec.codex_config((Path("/Users/me/src/app"),))
    assert 'default_permissions = "openswap-research"' in text
    assert "sandbox_mode" not in text
    assert '":root" = "deny"' in text and '":tmpdir" = "deny"' in text and '":slash_tmp" = "deny"' in text
    assert '"/Users/me/src/app" = "read"' in text
    assert '[permissions.openswap-research.filesystem.":workspace_roots"]\n"." = "write"' in text
    assert 'cli_auth_credentials_store = "file"' in text
    assert "project_root_markers = []" in text and "project_doc_max_bytes = 0" in text
    assert 'web_search = "live"' in text
    assert "[permissions.openswap-research.network]\nenabled = false" in text
    with pytest.raises(ValueError):
        codex_exec.codex_config((Path("/bad\npath"),))


def test_argv_is_fixed_and_has_no_override_surfaces(tmp_path):
    argv = codex_exec.codex_argv(Path("/x/codex"), tmp_path / "out", tmp_path / "run")
    assert argv[0] == "/x/codex" and argv[-1] == "-"
    assert "exec" in argv and "--json" in argv and "--skip-git-repo-check" in argv
    assert argv.index("--ignore-rules") > argv.index("exec")  # no user/project exec-policy rules
    for forbidden in ("--sandbox", "-s", "--add-dir", "-m", "--model", "-c", "--config", "resume", "fork",
                      "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--search"):
        assert forbidden not in argv
    disabled = [argv[i + 1] for i, value in enumerate(argv) if value == "--disable"]
    assert "shell_tool" not in disabled
    assert {"apps", "hooks", "plugins", "browser_use", "computer_use", "multi_agent"} <= set(disabled)
    # The unsandboxed final-message write goes to the private run directory.
    assert argv[argv.index("--output-last-message") + 1] == str(tmp_path / "run" / "last-message.md")


def test_environment_is_an_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-pass")
    monkeypatch.setenv("CODEX_API_KEY", "nope")
    env = codex_exec.codex_env(tmp_path / "home", tmp_path / "run")
    assert set(env) == {"PATH", "HOME", "CODEX_HOME", "TMPDIR", "LANG", "USER", "LOGNAME", "SHELL"}
    assert env["CODEX_HOME"] == str(tmp_path / "home")
    assert env["HOME"] == str(tmp_path / "home" / "home")
    assert env["TMPDIR"] == str(tmp_path / "run" / "tmp")


def test_isolated_home_is_per_account_and_identity_checked(tmp_path):
    home = sign_in(tmp_path)
    assert home == codex_exec.isolated_home(tmp_path, IDENTITY)
    assert home != codex_exec.isolated_home(tmp_path, OTHER_IDENTITY)
    assert codex_exec.home_identity(home) == IDENTITY
    (home / "auth.json").write_text(auth_json("acct-other"))
    assert codex_exec.home_identity(home) == OTHER_IDENTITY
    (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-x"}')
    assert codex_exec.home_identity(home) is None
    if os.name == "posix":
        assert (home.stat().st_mode & 0o777) == 0o700
        assert ((home / "config.toml").stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_symlinked_auth_file_is_not_trusted(tmp_path):
    home = codex_exec.prepare_home(tmp_path, IDENTITY)
    real = tmp_path / "elsewhere.json"
    real.write_text(auth_json())
    (home / "auth.json").symlink_to(real)
    assert codex_exec.home_identity(home) is None


@pytest.mark.parametrize("message, code", [
    ("You've hit your usage limit", "provider_rate_limited"),
    ("HTTP 429 Too Many Requests", "provider_rate_limited"),
    ("401 Unauthorized: refresh token expired", "provider_auth_unavailable"),
    ("stream disconnected", "provider_unavailable"),
])
def test_failure_classification(message, code):
    assert codex_exec.classify_failure(message) == code


# -- adapter lifecycle ------------------------------------------------------------


def test_probe_is_disabled_until_live_mode_and_unavailable_when_binary_fails(tmp_path):
    adapter = make_adapter(tmp_path, FakeContainment(), mode="disabled")
    assert adapter.probe().diagnostic_code == "live_adapter_disabled"
    adapter = make_adapter(tmp_path, FakeContainment())
    assert adapter.probe().available is True

    def broken(**kw):
        raise codex_cli.CodexCliError("binary_hash_mismatch")

    adapter = CodexExecAdapter(tmp_path, containment=FakeContainment(), verify=broken, mode=lambda: "live")
    assert adapter.probe().diagnostic_code == "provider_unavailable"


def test_probe_refuses_a_binary_other_than_the_checked_one(tmp_path):
    write_live_execution(tmp_path, LiveExecutionSettings(True, "cd" * 32, "ef" * 32, "now"))
    adapter = make_adapter(tmp_path, FakeContainment())
    assert adapter.probe().diagnostic_code == "provider_unavailable"


def test_start_refuses_without_live_mode_or_matching_sign_in(tmp_path):
    containment = FakeContainment()
    with pytest.raises(ProviderLaunchRefused) as error:
        make_adapter(tmp_path, containment, mode="disabled").start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert error.value.diagnostic_code == "live_adapter_disabled"
    adapter = make_adapter(tmp_path, containment)
    with pytest.raises(ProviderLaunchRefused) as error:
        adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_auth_unavailable"
    sign_in(tmp_path, IDENTITY, "acct-other")  # signed in, but to another account
    with pytest.raises(ProviderLaunchRefused) as error:
        adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_auth_unavailable"
    assert containment.launches == []


def test_successful_run_maps_to_started_then_succeeded_with_stop_proof(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    adapter = make_adapter(tmp_path, containment)
    source = tmp_path / "source"
    source.mkdir()
    run = adapter.start(job_record(), workspace(tmp_path, source), worker_epoch=7)
    assert isinstance(run, ProviderRun) and run.worker_epoch == 7 and run.generation == 3
    launch = containment.launches[0]
    assert launch["env"]["CODEX_HOME"] == str(codex_exec.isolated_home(tmp_path, IDENTITY))
    assert "Find the latest Python release" in launch["stdin"]
    assert str(source) in launch["stdin"]
    assert launch["cwd"] == workspace(tmp_path).output_root
    config = (codex_exec.isolated_home(tmp_path, IDENTITY) / "config.toml").read_text()
    assert f'{json.dumps(str(source))} = "read"' in config
    events = drain(adapter, run)
    assert [e.kind for e in events] == [SafeEventKind.PROVIDER_STARTED, SafeEventKind.PROVIDER_FINISHED]
    finished = events[-1]
    assert finished.state == JobState.SUCCEEDED and finished.execution_stopped is True
    assert finished.diagnostic_code is None
    summary = json.loads((codex_exec.runs_root(tmp_path) / ("a" * 32) / "summary.json").read_text())
    assert summary["item_counts"] == {"web_search": 1, "agent_message": 1}
    assert summary["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert "done" not in json.dumps(summary)  # no model text
    assert adapter.events(run, after_cursor=finished.cursor) == ()
    assert (workspace(tmp_path).output_root / "result.md").read_text() == "# Result\nhttps://example.com\n"


def test_unproven_stop_is_reported_without_execution_stopped(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    containment.proof = StopProof(False, False, 2)
    adapter = make_adapter(tmp_path, containment)
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert drain(adapter, run)[-1].execution_stopped is False


@pytest.mark.parametrize("script, exit_status, result, code", [
    ([{"type": "thread.started"}, {"type": "turn.failed", "error": {"message": "usage limit reached"}}],
     1, None, "provider_rate_limited"),
    ([{"type": "thread.started"}, {"type": "error", "message": "401 Unauthorized"}], 1, None,
     "provider_auth_unavailable"),
    ([{"type": "thread.started"}, {"type": "turn.completed"}], 0, None, "provider_unavailable"),
    ([{"type": "thread.started"}], 0, "x", "provider_unavailable"),  # no turn.completed
])
def test_failures_map_to_failed_with_allowlisted_codes(tmp_path, script, exit_status, result, code):
    sign_in(tmp_path)
    containment = FakeContainment(script, exit_status=exit_status, result=result)
    adapter = make_adapter(tmp_path, containment)
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    finished = drain(adapter, run)[-1]
    assert finished.state == JobState.FAILED and finished.diagnostic_code == code


def test_malformed_and_oversized_lines_are_skipped(tmp_path, monkeypatch):
    sign_in(tmp_path)
    monkeypatch.setattr(codex_exec, "MAX_LINE_BYTES", 120)
    containment = FakeContainment([{"type": "thread.started", "pad": "x" * 200}, *SUCCESS_SCRIPT])
    adapter = make_adapter(tmp_path, containment)
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    run_dir = containment.launches[0]["run_dir"]
    with open(run_dir / "stdout.jsonl", "a") as handle:
        handle.write("not json\n[1,2]\n")
    events = drain(adapter, run)
    assert events[-1].state == JobState.SUCCEEDED
    assert json.loads((run_dir / "summary.json").read_text())["unparsed_lines"] == 3


def test_interrupt_reports_only_the_sweep_proof(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment([{"type": "thread.started"}], exit_status=None, result=None)
    adapter = make_adapter(tmp_path, containment)
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert [e.kind for e in adapter.events(run, after_cursor=0)] == [SafeEventKind.PROVIDER_STARTED]
    assert adapter.events(run, after_cursor=1) == ()  # still running
    result = adapter.interrupt(run)
    assert result == InterruptResult(True, True, None)
    assert adapter.events(run, after_cursor=1) == ()

    containment = FakeContainment([], exit_status=None, result=None)
    containment.proof = StopProof(False, True, 1)
    adapter = make_adapter(tmp_path, containment)
    run = adapter.start(job_record("b" * 32), workspace(tmp_path), worker_epoch=1)
    assert adapter.interrupt(run) == InterruptResult(True, False, "execution_uncertain")


def test_a_job_is_never_launched_twice(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    adapter = make_adapter(tmp_path, containment)
    adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    with pytest.raises(ProviderLaunchRefused) as error:
        adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert error.value.diagnostic_code == "execution_uncertain"
    assert len(containment.launches) == 1


def test_containment_failure_before_release_is_a_refusal_after_is_uncertain(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment()
    containment.launch_error = ContainmentError("launchd_bootstrap_failed")
    adapter = make_adapter(tmp_path, containment)
    with pytest.raises(ProviderLaunchRefused):
        adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    containment.launch_error = ContainmentError("job_release_failed", launched=True)
    with pytest.raises(RuntimeError) as error:
        adapter.start(job_record("c" * 32), workspace(tmp_path), worker_epoch=1)
    assert not isinstance(error.value, ProviderLaunchRefused)


def test_recover_uses_the_saved_handle(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment([], exit_status=None, result=None)
    adapter = make_adapter(tmp_path, containment)
    adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    fresh = make_adapter(tmp_path, containment)
    assert fresh.recover("a" * 32) == InterruptResult(True, True, None)
    assert fresh.recover("d" * 32) is None


# -- runtime integration -------------------------------------------------------------


def _runtime(tmp_path, containment, *, mode="live"):
    update_worker_settings(tmp_path, enabled=True)
    configure_worker_local_policy(
        tmp_path, pinned_account_ref=IDENTITY,
        workspaces=(WorkerWorkspace("research", (tmp_path / "research").resolve()),),
    )
    adapter = make_adapter(tmp_path, containment, mode=mode)
    return WorkerRuntime(tmp_path, adapter=adapter, account_identity=IDENTITY, sleeper=lambda s: None)


def _submit(runtime):
    return runtime.submit(JobSubmission(
        idempotency_key="key-1", provider="codex", task="Research", capability_profile="research",
        workspace_id="research", expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        runtime_limit_s=600,
    ))


def test_runtime_runs_a_live_job_and_releases_the_lease_on_proof(tmp_path):
    sign_in(tmp_path)
    runtime = _runtime(tmp_path, FakeContainment(SUCCESS_SCRIPT))
    job = _submit(runtime)
    final = runtime.reconcile_once()
    assert final.state == JobState.SUCCEEDED
    lease = AccountLeaseStore(tmp_path, "codex").read_current()
    assert lease.state == "released" and lease.reason == "confirmed_stopped"
    kinds = [e.kind for e in runtime.events(job.job_id).events]
    assert SafeEventKind.PROVIDER_STARTED in kinds and SafeEventKind.PROVIDER_FINISHED in kinds


def test_runtime_records_a_refused_launch_as_failed_and_unlaunched(tmp_path):
    runtime = _runtime(tmp_path, FakeContainment(SUCCESS_SCRIPT))  # never signed in
    _submit(runtime)
    final = runtime.reconcile_once()
    assert final.state == JobState.FAILED and final.diagnostic_code == "provider_auth_unavailable"
    lease = AccountLeaseStore(tmp_path, "codex").read_current()
    assert lease.state == "released" and lease.reason == "unlaunched"


def test_runtime_disabled_mode_fails_with_live_adapter_disabled(tmp_path):
    runtime = _runtime(tmp_path, FakeContainment(SUCCESS_SCRIPT), mode="disabled")
    _submit(runtime)
    final = runtime.reconcile_once()
    assert final.state == JobState.FAILED and final.diagnostic_code == "live_adapter_disabled"


def test_worker_restart_recovers_the_orphaned_job_and_frees_the_lease_on_proof(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment([{"type": "thread.started"}], exit_status=None, result=None)
    runtime = _runtime(tmp_path, containment)
    job = _submit(runtime)
    # Simulate a worker crash mid-run: launch, then abandon the process state.
    claimed = runtime.store.claim(job.job_id, worker_epoch=runtime.worker_epoch,
                                  expected_generation=job.generation)
    prepared = runtime._prepare_run(claimed)
    assert prepared[0].state == JobState.RUNNING
    store = AccountLeaseStore(tmp_path, "codex")
    assert store.read_current().state == "active"

    containment.proof = StopProof(False, True, 1)
    restarted = WorkerRuntime(tmp_path, adapter=make_adapter(tmp_path, containment),
                              account_identity=IDENTITY)
    assert restarted.get(job.job_id).state == JobState.INTERRUPTED
    assert store.read_current().state == "uncertain"  # no proof: stays locked

    containment.proof = StopProof(True, False, 0)
    WorkerRuntime(tmp_path, adapter=make_adapter(tmp_path, containment), account_identity=IDENTITY)
    lease = store.read_current()
    assert lease.state == "released" and lease.reason == "confirmed_stopped"


def test_execution_mode_hook(tmp_path, monkeypatch):
    runtime = WorkerRuntime(tmp_path, adapter=UnavailableCodexAdapter())
    assert runtime.execution_mode() == "disabled"
    monkeypatch.setattr(live, "platform_supported", lambda: True)
    adapter = CodexExecAdapter(tmp_path, containment=FakeContainment(), verify=lambda **kw: pinned(),
                               managed=lambda home: [])
    runtime = WorkerRuntime(tmp_path, adapter=adapter)
    assert runtime.execution_mode() == "disabled" and adapter.execution_mode == "disabled"
    assert UnavailableCodexAdapter.execution_mode == "disabled"
    write_live_execution(tmp_path, LiveExecutionSettings(True, "cd" * 32, BINARY_SHA, "now"))
    assert runtime.execution_mode() == "live"
    assert adapter.execution_mode == "live"
    assert runtime.status().provider.available is True


def test_production_adapter_is_unavailable_off_apple_silicon(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_cli, "platform_supported", lambda *a, **k: False)
    assert isinstance(production_adapter(tmp_path), UnavailableCodexAdapter)
    assert isinstance(production_adapter(), UnavailableCodexAdapter)
    monkeypatch.setattr(codex_cli, "platform_supported", lambda *a, **k: True)
    assert isinstance(production_adapter(tmp_path), CodexExecAdapter)
    assert isinstance(production_adapter(), CodexExecAdapter)  # default backup root


# -- pinned CLI install --------------------------------------------------------------


def _archive(members: dict[str, bytes], symlink: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if symlink:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
    return buffer.getvalue()


def _version_run(output=codex_cli.CODEX_VERSION_OUTPUT, rc=0):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, rc, output + "\n", "")
    run.calls = calls
    return run


def test_install_refuses_an_archive_that_is_not_the_published_one(tmp_path):
    archive = tmp_path / "codex.tar.gz"
    archive.write_bytes(_archive({codex_cli.ASSET_MEMBER: b"binary"}))
    with pytest.raises(codex_cli.CodexCliError) as error:
        codex_cli.install(tmp_path, archive_path=archive, run=_version_run(), supported=True)
    assert error.value.code == "archive_hash_mismatch"
    assert not codex_cli.binary_path(tmp_path).exists()


def test_install_refuses_off_apple_silicon(tmp_path):
    with pytest.raises(codex_cli.CodexCliError) as error:
        codex_cli.install(tmp_path, supported=False)
    assert error.value.code == "unsupported_platform"


@pytest.mark.parametrize("members, symlink", [
    ({codex_cli.ASSET_MEMBER: b"x", "extra": b"y"}, None),
    ({"other-name": b"x"}, None),
    ({}, codex_cli.ASSET_MEMBER),
])
def test_install_refuses_unexpected_archive_layouts(tmp_path, monkeypatch, members, symlink):
    data = _archive(members, symlink)
    monkeypatch.setattr(codex_cli, "ASSET_SHA256", hashlib.sha256(data).hexdigest())
    archive = tmp_path / "codex.tar.gz"
    archive.write_bytes(data)
    with pytest.raises(codex_cli.CodexCliError) as error:
        codex_cli.install(tmp_path, archive_path=archive, run=_version_run(), supported=True)
    assert error.value.code == "archive_unexpected_layout"


def test_install_then_verify_and_detect_tampering(tmp_path, monkeypatch):
    data = _archive({codex_cli.ASSET_MEMBER: b"#!/bin/sh\necho codex-cli 0.157.1\n"})
    monkeypatch.setattr(codex_cli, "ASSET_SHA256", hashlib.sha256(data).hexdigest())
    fetched = []

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(url, timeout):
        fetched.append(url)
        return Response(data)

    run = _version_run()
    pinned_cli = codex_cli.install(tmp_path, opener=opener, run=run, supported=True)
    assert fetched == [codex_cli.ASSET_URL]
    assert pinned_cli.version == "codex-cli 0.157.1"
    assert pinned_cli.binary_sha256 == hashlib.sha256(b"#!/bin/sh\necho codex-cli 0.157.1\n").hexdigest()
    assert run.calls[-1][0] == [str(codex_cli.binary_path(tmp_path)), "--version"]
    assert set(run.calls[-1][1]["env"]) == {"PATH", "HOME", "CODEX_HOME"}
    if os.name == "posix":
        assert (codex_cli.install_dir(tmp_path).stat().st_mode & 0o777) == 0o700
    assert codex_cli.verify(tmp_path, run=run, supported=True) == pinned_cli
    with pytest.raises(codex_cli.CodexCliError) as error:
        codex_cli.verify(tmp_path, run=_version_run("codex-cli 0.158.0"), supported=True)
    assert error.value.code == "version_mismatch"
    codex_cli.binary_path(tmp_path).write_bytes(b"tampered")
    with pytest.raises(codex_cli.CodexCliError) as error:
        codex_cli.verify(tmp_path, run=run, supported=True)
    assert error.value.code == "binary_hash_mismatch"


def test_verify_without_install(tmp_path):
    with pytest.raises(codex_cli.CodexCliError) as error:
        codex_cli.verify(tmp_path, supported=True)
    assert error.value.code == "not_installed"


# -- live opt-in -----------------------------------------------------------------------


def passing_evidence(binary_sha=BINARY_SHA, **overrides):
    data = {
        "kind": live.EVIDENCE_KIND, "schema": live.EVIDENCE_SCHEMA, "passed": True,
        "codex": {"version": codex_cli.CODEX_VERSION_OUTPUT, "binary_sha256": binary_sha},
        "gates": {name: {"passed": True} for name in live.REQUIRED_GATES},
    }
    data.update(overrides)
    return data


def test_evidence_must_pass_every_gate_for_this_binary():
    assert live.evidence_problems(passing_evidence(), pinned=pinned()) == ()
    failing = passing_evidence()
    failing["gates"]["stop"] = {"passed": False}
    assert "gate_failed:stop" in live.evidence_problems(failing)
    assert "codex_binary_mismatch" in live.evidence_problems(passing_evidence("00" * 32), pinned=pinned())
    assert "evidence_not_passed" in live.evidence_problems(passing_evidence(passed=False))
    assert live.evidence_problems([]) == ("evidence_invalid",)


def test_enable_and_disable_live(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "platform_supported", lambda: True)
    assert live.execution_mode(tmp_path) == "disabled"
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(passing_evidence()))
    settings = live.enable_live(tmp_path, evidence, pinned())
    assert settings.enabled and settings.codex_sha256 == BINARY_SHA
    assert settings.evidence_sha256 == hashlib.sha256(evidence.read_bytes()).hexdigest()
    assert live.execution_mode(tmp_path) == "live"
    monkeypatch.setattr(live, "platform_supported", lambda: False)
    assert live.execution_mode(tmp_path) == "disabled"
    monkeypatch.setattr(live, "platform_supported", lambda: True)
    live.disable_live(tmp_path)
    assert live.execution_mode(tmp_path) == "disabled"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(passing_evidence(passed=False)))
    with pytest.raises(live.LiveModeError) as error:
        live.enable_live(tmp_path, bad, pinned())
    assert error.value.code == "evidence_not_passing"
    assert live.execution_mode(tmp_path) == "disabled"


@pytest.mark.parametrize("value", [
    {"enabled": "yes"}, {"enabled": True}, {"enabled": True, "evidenceSha256": "x", "codexSha256": "y",
                                             "enabledAt": "now"}, "on",
])
def test_malformed_live_setting_reads_disabled_and_keeps_worker_policy(tmp_path, value):
    (tmp_path / "settings.json").write_text(json.dumps({"worker": {"enabled": True, "liveExecution": value}}))
    assert load_live_execution(tmp_path) == LiveExecutionSettings()
    from openswap.settings import load_worker_settings
    assert load_worker_settings(tmp_path).enabled is True


def test_worker_policy_writes_keep_the_live_opt_in(tmp_path):
    write_live_execution(tmp_path, LiveExecutionSettings(True, "cd" * 32, BINARY_SHA, "now"))
    update_worker_settings(tmp_path, enabled=True, paused=True)
    configure_worker_local_policy(tmp_path, pinned_account_ref=IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (tmp_path / "r").resolve()),))
    assert load_live_execution(tmp_path).enabled is True


# -- isolated sign-in ------------------------------------------------------------------


def _roster(tmp_path, accounts):
    (tmp_path / "codex").mkdir(exist_ok=True)
    (tmp_path / "codex" / "sequence.json").write_text(json.dumps({
        "schemaVersion": 1, "activeAccountNumber": None, "sequence": list(accounts),
        "accounts": {number: {"email": f"{number}@example.com", "accountId": account}
                     for number, account in accounts.items()},
    }))


@pytest.fixture(autouse=True)
def no_managed_codex_layer(monkeypatch):
    # Login/logout check this Mac's managed Codex layers; tests opt in to one.
    monkeypatch.setattr(live_cli, "managed_codex_config", lambda home, **kw: [])


def _login_run(account_id):
    calls = []

    def run(argv, env, check=False, **kwargs):
        calls.append((list(argv), dict(env)))
        home = Path(env["CODEX_HOME"])
        if argv[1] == "login":
            (home / "auth.json").write_text(auth_json(account_id))
        elif argv[1] == "logout":
            (home / "auth.json").unlink(missing_ok=True)
        return subprocess.CompletedProcess(argv, 0, "", "")
    run.calls = calls
    return run


def test_login_signs_in_only_the_isolated_home_under_a_lease(tmp_path, monkeypatch):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    default_home = tmp_path / "default-codex"
    default_home.mkdir()
    (default_home / "auth.json").write_text("default-login")
    monkeypatch.setenv("CODEX_HOME", str(default_home))
    run = _login_run(ACCOUNT_ID)
    seen_lease = []
    original = live_cli.home_identity

    def spy(home):
        seen_lease.append(AccountLeaseStore(tmp_path, "codex").read_current().state)
        return original(home)

    monkeypatch.setattr(live_cli, "home_identity", spy)
    result = live_cli.login(tmp_path, "1", run=run, verify=pinned)
    assert result == {"slot": "1", "account_ref": IDENTITY, "signed_in": True}
    argv, env = run.calls[0]
    assert argv == ["/fake/codex", "login"]
    assert env["CODEX_HOME"] == str(codex_exec.isolated_home(tmp_path, IDENTITY))
    assert (default_home / "auth.json").read_text() == "default-login"
    assert seen_lease == ["active"]
    assert AccountLeaseStore(tmp_path, "codex").read_current().state == "released"


def test_login_to_the_wrong_account_is_signed_back_out(tmp_path):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    run = _login_run("acct-someone-else")
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.login(tmp_path, "1", run=run, verify=pinned)
    assert error.value.code == "login_account_mismatch"
    assert [call[0][1] for call in run.calls] == ["login", "logout"]
    assert codex_exec.home_identity(codex_exec.isolated_home(tmp_path, IDENTITY)) is None
    assert AccountLeaseStore(tmp_path, "codex").read_current().state == "released"


def test_login_refuses_while_the_account_is_leased(tmp_path):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    AccountLeaseStore(tmp_path, "codex").acquire(
        job_id="b" * 32, account_identity=IDENTITY, worker_pid=os.getpid(), worker_epoch=1, ttl_s=60,
    )
    run = _login_run(ACCOUNT_ID)
    with pytest.raises(Exception):
        live_cli.login(tmp_path, "1", run=run, verify=pinned)
    assert run.calls == []


def test_codex_status_lists_isolated_sign_ins(tmp_path):
    _roster(tmp_path, {"1": ACCOUNT_ID, "2": "acct-other"})
    sign_in(tmp_path)
    status = live_cli.codex_status(tmp_path)
    assert status["cli"]["installed"] is False
    signed = {a["slot"]: a["isolated_sign_in"] for a in status["accounts"]}
    assert signed == {"1": True, "2": False}
    assert status["execution_mode"] == "disabled"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_planted_result_symlink_is_replaced_not_followed(tmp_path):
    sign_in(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("owner data")
    ws = workspace(tmp_path)
    (ws.output_root / "result.md").symlink_to(victim)
    adapter = make_adapter(tmp_path, FakeContainment(SUCCESS_SCRIPT))
    run = adapter.start(job_record(), ws, worker_epoch=1)
    assert drain(adapter, run)[-1].state == JobState.SUCCEEDED
    assert victim.read_text() == "owner data"
    result = ws.output_root / "result.md"
    assert not result.is_symlink() and result.read_text().startswith("# Result")


def test_result_is_not_published_without_stop_proof(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    containment.proof = StopProof(False, False, 1)
    adapter = make_adapter(tmp_path, containment)
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    finished = drain(adapter, run)[-1]
    assert finished.state == JobState.FAILED and finished.execution_stopped is False
    assert not (workspace(tmp_path).output_root / "result.md").exists()


def test_the_default_research_workspace_may_launch(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    adapter = make_adapter(tmp_path, containment)
    out = tmp_path / "worker" / "research" / ("a" * 32)
    out.mkdir(parents=True)
    adapter.start(job_record(), ResolvedWorkspace("research", out, ()), worker_epoch=1)
    assert len(containment.launches) == 1


def test_granted_root_rules(tmp_path):
    allowed = codex_exec.granted_root_allowed
    assert allowed(tmp_path, tmp_path / "worker" / "research" / "job")
    assert allowed(tmp_path, tmp_path.parent / "elsewhere")
    assert not allowed(tmp_path, tmp_path)
    assert not allowed(tmp_path, tmp_path / "worker")
    assert not allowed(tmp_path, tmp_path / "worker" / "codex-homes")
    assert not allowed(tmp_path, tmp_path / "worker" / "runs" / "x")
    assert not allowed(tmp_path, tmp_path / "worker" / "leases")


def test_roots_overlapping_the_private_worker_dir_are_refused(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    adapter = make_adapter(tmp_path, containment)
    out = workspace(tmp_path).output_root
    for sources in ((tmp_path,), (tmp_path / "worker" / "codex-homes",)):
        with pytest.raises(ProviderLaunchRefused) as error:
            adapter.start(job_record(), ResolvedWorkspace("research", out, sources), worker_epoch=1)
        assert error.value.diagnostic_code == "provider_unavailable"
    assert containment.launches == []


def test_finished_runs_leave_the_registry_but_keep_their_proof(tmp_path):
    sign_in(tmp_path)
    adapter = make_adapter(tmp_path, FakeContainment(SUCCESS_SCRIPT))
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    drain(adapter, run)
    assert adapter._runs == {}
    assert adapter.interrupt(run) == InterruptResult(False, True, None)
    for index in range(codex_exec.FINISHED_RUNS_KEPT + 5):
        adapter._forget(10_000 + index, True)
    assert len(adapter._finished) == codex_exec.FINISHED_RUNS_KEPT


def test_launch_refuses_while_the_live_lock_is_held(tmp_path, monkeypatch):
    from openswap.locking import FileLock

    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    adapter = make_adapter(tmp_path, containment)
    original = codex_exec.live_lock
    monkeypatch.setattr(codex_exec, "live_lock", lambda root: original(root, timeout=0.2))
    holder = FileLock(tmp_path / "worker" / "live.lock", timeout=1)
    assert holder.acquire()
    try:
        with pytest.raises(ProviderLaunchRefused) as error:
            adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
        assert error.value.diagnostic_code == "provider_unavailable"
    finally:
        holder.release()
    assert containment.launches == []
    adapter.start(job_record("e" * 32), workspace(tmp_path), worker_epoch=1)
    assert len(containment.launches) == 1


def test_enable_and_disable_take_the_live_lock(tmp_path, monkeypatch):
    taken = []
    original = live.live_lock

    def spy(root, **kwargs):
        taken.append(root)
        return original(root, **kwargs)

    monkeypatch.setattr(live, "live_lock", spy)
    live.disable_live(tmp_path)
    assert taken == [tmp_path]


def test_logout_fails_while_the_sign_in_remains(tmp_path):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    sign_in(tmp_path)

    def stubborn(argv, env, check=False, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "")

    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.logout(tmp_path, "1", run=stubborn, verify=pinned)
    assert error.value.code == "logout_failed"
    assert live_cli.logout(tmp_path, "1", run=_login_run(ACCOUNT_ID), verify=pinned)["signed_in"] is False


def test_a_short_or_failed_result_write_fails_the_job_without_a_partial_file(tmp_path, monkeypatch):
    sign_in(tmp_path)
    adapter = make_adapter(tmp_path, FakeContainment(SUCCESS_SCRIPT))
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    real_write = os.write
    calls = []

    def flaky_write(fd, data):
        calls.append(len(data))
        if len(calls) == 1:
            return real_write(fd, bytes(data[:3]))  # a short write
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(codex_exec.os, "write", flaky_write)
    finished = drain(adapter, run)[-1]
    monkeypatch.setattr(codex_exec.os, "write", real_write)
    assert finished.state == JobState.FAILED and finished.execution_stopped is True
    assert not (workspace(tmp_path).output_root / "result.md").exists()


def test_short_writes_are_completed(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    out = tmp_path / "out"
    run_dir.mkdir()
    out.mkdir()
    (run_dir / codex_exec.LAST_MESSAGE_FILE).write_text("abcdefghij")
    real_write = os.write
    monkeypatch.setattr(codex_exec.os, "write", lambda fd, data: real_write(fd, bytes(data[:4])))
    assert codex_exec.publish_result(run_dir, out) is True
    monkeypatch.setattr(codex_exec.os, "write", real_write)
    assert (out / "result.md").read_text() == "abcdefghij"


def test_a_short_read_never_publishes_a_truncated_result(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    out = tmp_path / "out"
    run_dir.mkdir()
    out.mkdir()
    (run_dir / codex_exec.LAST_MESSAGE_FILE).write_text("abcdefghij")
    real_read = os.read
    monkeypatch.setattr(codex_exec.os, "read", lambda fd, n: real_read(fd, min(n, 3)))
    assert codex_exec.publish_result(run_dir, out) is True
    monkeypatch.setattr(codex_exec.os, "read", real_read)
    assert (out / "result.md").read_text() == "abcdefghij"
    (out / "result.md").unlink()
    reads = iter([b"abc", b""])
    monkeypatch.setattr(codex_exec.os, "read", lambda fd, n: next(reads))
    assert codex_exec.publish_result(run_dir, out) is False
    monkeypatch.setattr(codex_exec.os, "read", real_read)
    assert not (out / "result.md").exists()


def test_managed_configuration_is_detected_and_refuses_launch(tmp_path):
    home = codex_exec.prepare_home(tmp_path, IDENTITY)
    absent = lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")  # noqa: E731
    present = lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "string", "")  # noqa: E731
    assert codex_exec.managed_codex_config(home, run=absent) == [] or all(
        not p.startswith(str(home)) and not p.startswith("defaults:")
        for p in codex_exec.managed_codex_config(home, run=absent))
    (home / "requirements.toml").write_text("")
    found = codex_exec.managed_codex_config(home, run=absent)
    assert str(home / "requirements.toml") in found
    assert "defaults:config_toml_base64" in codex_exec.managed_codex_config(home, run=present)

    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    adapter = CodexExecAdapter(tmp_path, containment=containment, verify=lambda **kw: pinned(),
                               mode=lambda: "live", managed=lambda h: ["/etc/codex/config.toml"])
    with pytest.raises(ProviderLaunchRefused) as error:
        adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_unavailable"
    assert containment.launches == []


def test_a_run_whose_output_outgrows_its_cap_is_stopped_and_failed(tmp_path, monkeypatch):
    sign_in(tmp_path)
    monkeypatch.setattr(codex_exec, "MAX_STDOUT_BYTES", 50)
    containment = FakeContainment([{"type": "thread.started", "pad": "x" * 100}], exit_status=None, result=None)
    adapter = make_adapter(tmp_path, containment)
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    finished = drain(adapter, run)[-1]
    assert finished.kind == SafeEventKind.PROVIDER_FINISHED
    assert finished.state == JobState.FAILED and finished.diagnostic_code == "provider_unavailable"
    assert finished.execution_stopped is True and containment.stops == 1


def test_old_proven_runs_are_pruned_and_unproven_ones_kept(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setattr(codex_exec, "RUNS_KEPT", 1)
    sign_in(tmp_path)  # creates the private worker directory
    runs = codex_exec.runs_root(tmp_path)
    runs.mkdir(mode=0o700)
    old = _time.time() - codex_exec.RUN_RETENTION_SECONDS - 10
    for name, stopped, age in (("old-proven-1", True, old), ("old-proven-2", True, old - 5),
                               ("old-unproven", False, old), ("new-proven", True, _time.time())):
        (runs / name).mkdir()
        summary = runs / name / "summary.json"
        summary.write_text(json.dumps({"stop_proof": {"stopped": stopped}}))
        os.utime(summary, (age, age))
    adapter = make_adapter(tmp_path, FakeContainment(SUCCESS_SCRIPT))
    adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    left = {entry.name for entry in runs.iterdir()}
    assert "old-unproven" in left and "new-proven" in left
    assert "old-proven-1" not in left and "old-proven-2" not in left


def test_logout_waits_for_the_lease_before_checking(tmp_path):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    AccountLeaseStore(tmp_path, "codex").acquire(
        job_id="login-x", account_identity=IDENTITY, worker_pid=os.getpid(), worker_epoch=1, ttl_s=60,
    )
    with pytest.raises(Exception):
        live_cli.logout(tmp_path, "1", run=_login_run(ACCOUNT_ID), verify=pinned)


def test_a_refused_containment_launch_leaves_no_run_directory(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment()
    containment.launch_error = ContainmentError("launchd_bootstrap_failed")
    adapter = make_adapter(tmp_path, containment)
    with pytest.raises(ProviderLaunchRefused):
        adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert not (codex_exec.runs_root(tmp_path) / ("a" * 32)).exists()


def test_a_partly_read_auth_file_vouches_for_nothing(tmp_path, monkeypatch):
    home = sign_in(tmp_path)
    real_read = os.read
    reads = iter([b'{"tokens"', b""])
    monkeypatch.setattr(codex_exec.os, "read", lambda fd, n: next(reads))
    assert codex_exec.home_identity(home) is None
    monkeypatch.setattr(codex_exec.os, "read", lambda fd, n: real_read(fd, min(n, 7)))
    assert codex_exec.home_identity(home) == IDENTITY


def test_logout_fails_while_any_credentials_file_remains(tmp_path):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    home = sign_in(tmp_path)
    (home / "auth.json").write_text("{not parseable but present")

    def ok(argv, env, check=False, **kwargs):
        return subprocess.CompletedProcess(argv, 0, "", "")

    with pytest.raises(live_cli.AccountPinError):
        live_cli.logout(tmp_path, "1", run=ok, verify=pinned)


def test_a_late_refusal_after_an_abandoned_start_releases_the_lease(tmp_path):
    import threading

    sign_in(tmp_path)
    gate = threading.Event()

    class SlowRefusal(CodexExecAdapter):
        def start(self, job, workspace, *, worker_epoch):
            gate.wait(5)
            raise ProviderLaunchRefused("provider_unavailable")

    update_worker_settings(tmp_path, enabled=True)
    configure_worker_local_policy(
        tmp_path, pinned_account_ref=IDENTITY,
        workspaces=(WorkerWorkspace("research", (tmp_path / "research").resolve()),),
    )
    adapter = SlowRefusal(tmp_path, containment=FakeContainment(), verify=lambda **kw: pinned(),
                          mode=lambda: "live", managed=lambda h: [])
    ticks = iter(range(10_000))
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=IDENTITY,
                            monotonic=lambda: next(ticks) * 1000.0)
    job = _submit(runtime)
    final = runtime.reconcile_once()  # the runtime limit abandons the blocked start
    assert final.state == JobState.INTERRUPTED
    gate.set()
    import time as _time
    for _ in range(100):
        lease = AccountLeaseStore(tmp_path, "codex").read_current()
        if lease.state == "released":
            break
        _time.sleep(0.05)
    assert lease.state == "released" and lease.reason == "unlaunched"
    assert runtime.get(job.job_id).state == JobState.INTERRUPTED


def test_a_recovered_run_records_its_proof_for_retention(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment([], exit_status=None, result=None)
    adapter = make_adapter(tmp_path, containment)
    adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    make_adapter(tmp_path, containment).recover("a" * 32)
    summary = json.loads((codex_exec.runs_root(tmp_path) / ("a" * 32) / "summary.json").read_text())
    assert summary["outcome"] == "recovered" and summary["stop_proof"]["stopped"] is True


def test_a_late_run_interrupted_with_proof_releases_the_lease(tmp_path):
    import threading
    import time as _time

    sign_in(tmp_path)
    gate = threading.Event()
    containment = FakeContainment([], exit_status=None, result=None)

    class SlowStart(CodexExecAdapter):
        def start(self, job, workspace, *, worker_epoch):
            gate.wait(5)
            return super().start(job, workspace, worker_epoch=worker_epoch)

    update_worker_settings(tmp_path, enabled=True)
    configure_worker_local_policy(
        tmp_path, pinned_account_ref=IDENTITY,
        workspaces=(WorkerWorkspace("research", (tmp_path / "research").resolve()),),
    )
    adapter = SlowStart(tmp_path, containment=containment, verify=lambda **kw: pinned(),
                        mode=lambda: "live", managed=lambda h: [])
    ticks = iter(range(10_000))
    runtime = WorkerRuntime(tmp_path, adapter=adapter, account_identity=IDENTITY,
                            monotonic=lambda: next(ticks) * 1000.0)
    _submit(runtime)
    assert runtime.reconcile_once().state == JobState.INTERRUPTED
    gate.set()
    for _ in range(100):
        lease = AccountLeaseStore(tmp_path, "codex").read_current()
        if lease.state == "released":
            break
        _time.sleep(0.05)
    assert lease.state == "released" and lease.reason == "confirmed_stopped"
    assert containment.stops == 1


def test_live_lock_io_errors_are_controlled_refusals(tmp_path, monkeypatch):
    from openswap import locking

    def broken(self, timeout=None):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(locking.FileLock, "acquire", broken)
    with pytest.raises(live.LiveModeError) as error:
        with live.live_lock(tmp_path):
            pass
    assert error.value.code == "live_lock_unavailable"
    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    with pytest.raises(ProviderLaunchRefused):
        make_adapter(tmp_path, containment).start(job_record(), workspace(tmp_path), worker_epoch=1)
    assert containment.launches == []


def test_a_wrong_account_that_will_not_sign_out_is_reported(tmp_path):
    _roster(tmp_path, {"1": ACCOUNT_ID})

    def run(argv, env, check=False, **kwargs):
        if argv[1] == "login":
            (Path(env["CODEX_HOME"]) / "auth.json").write_text(auth_json("acct-other"))
        return subprocess.CompletedProcess(argv, 1 if argv[1] == "logout" else 0, "", "")

    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.login(tmp_path, "1", run=run, verify=pinned)
    assert error.value.code == "login_account_mismatch_still_signed_in"



def test_recent_proven_runs_beyond_the_count_cap_are_pruned(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setattr(codex_exec, "RUNS_KEPT", 2)
    sign_in(tmp_path)
    runs = codex_exec.runs_root(tmp_path)
    runs.mkdir(mode=0o700)
    now = _time.time()
    for index in range(4):
        (runs / f"recent-{index}").mkdir()
        summary = runs / f"recent-{index}" / "summary.json"
        summary.write_text(json.dumps({"stop_proof": {"stopped": True}}))
        os.utime(summary, (now - index, now - index))
    make_adapter(tmp_path, FakeContainment(SUCCESS_SCRIPT)).start(job_record(), workspace(tmp_path), worker_epoch=1)
    left = {entry.name for entry in runs.iterdir()}
    assert {"recent-0", "recent-1"} <= left and not {"recent-2", "recent-3"} & left



def test_an_unproven_finish_is_retried_and_stays_recoverable(tmp_path):
    sign_in(tmp_path)
    containment = FakeContainment(SUCCESS_SCRIPT)
    proofs = iter([StopProof(False, False, 1)] * 3 + [StopProof(True, False, 0)])

    def flaky_stop(handle, timeout=15.0):
        containment.stops += 1
        return next(proofs)

    containment.stop = flaky_stop
    adapter = make_adapter(tmp_path, containment)
    run = adapter.start(job_record(), workspace(tmp_path), worker_epoch=1)
    finished = drain(adapter, run)[-1]
    assert finished.execution_stopped is False and containment.stops == 3
    # Still registered: a later interrupt retries the sweep and can prove it.
    assert adapter.interrupt(run) == InterruptResult(True, True, None)
    assert containment.stops == 4 and adapter._runs == {}



def test_a_failed_login_is_a_failure_even_with_this_accounts_old_credentials(tmp_path):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    home = codex_exec.prepare_home(tmp_path, IDENTITY)
    (home / "auth.json").write_text(auth_json(ACCOUNT_ID))  # stale credentials of this account

    def failing(argv, env, check=False, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "")

    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.login(tmp_path, "1", run=failing, verify=pinned)
    assert error.value.code == "login_failed"



@pytest.mark.parametrize("action", ["login", "logout"])
def test_login_and_logout_refuse_while_a_managed_layer_exists(tmp_path, action):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    home = codex_exec.prepare_home(tmp_path, IDENTITY)
    (home / "auth.json").write_text(auth_json(ACCOUNT_ID))
    run = _login_run(ACCOUNT_ID)
    with pytest.raises(live_cli.AccountPinError) as error:
        getattr(live_cli, action)(tmp_path, "1", run=run, verify=pinned, managed=lambda h: ["/etc/codex"])
    assert error.value.code == "managed_codex_config"
    assert run.calls == []  # Codex never ran
    assert (home / "auth.json").exists()
    assert AccountLeaseStore(tmp_path, "codex").read_current().state == "released"



@pytest.mark.parametrize("action", ["login", "logout"])
def test_the_slot_is_resolved_under_the_codex_mutation_guard(tmp_path, monkeypatch, action):
    _roster(tmp_path, {"1": ACCOUNT_ID})
    original = live_cli._resolve
    held = []

    def spy(root, selector):
        probe = live_cli.AccountLeaseStore(tmp_path, "codex")
        try:
            with probe.mutation_guard(timeout=0):
                held.append(False)  # nobody held it
        except Exception:
            held.append(True)
        return original(root, selector)

    monkeypatch.setattr(live_cli, "_resolve", spy)
    getattr(live_cli, action)(tmp_path, "1", run=_login_run(ACCOUNT_ID), verify=pinned)
    assert held == [True]
