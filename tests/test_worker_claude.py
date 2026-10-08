"""Claude Code on Remote tasks (owner decision 2026-10-07): pin, adapter, routing, opt-in, live check.

No real Claude Code login, credential or model call is used: binaries are
fake scripts, launches are simulated, and profiles hold public metadata only.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openswap.settings import (
    LiveExecutionSettings,
    WorkerWorkspace,
    configure_worker_local_policy,
    load_live_execution,
    update_worker_settings,
    write_live_execution,
)
from openswap.worker import claude_cli, claude_exec, codex_cli, live, live_check, live_cli
from openswap.worker import containment as cont
from openswap.worker.adapter import ProviderLaunchRefused
from openswap.worker.claude_exec import ClaudeCodeAdapter
from openswap.worker.codex_exec import CodexExecAdapter
from openswap.worker.containment import JobHandle, StopProof
from openswap.worker.leases import AccountLeaseStore, LeaseConflictError, ProviderLeases, stable_account_identity
from openswap.worker.models import JobRecord, JobState, JobSubmission, ResolvedWorkspace, SafeEventKind
from openswap.worker.runtime import WorkerRuntime

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the live Claude adapter is macOS-only")

EMAIL, ORG = "carol@example.com", "org-1"
IDENTITY = stable_account_identity("claude", EMAIL, ORG)
CODEX_IDENTITY = stable_account_identity("codex", "acct-1")
SHA = "cd" * 32


@pytest.fixture(autouse=True)
def apple_silicon(monkeypatch):
    monkeypatch.setattr(codex_cli, "platform_supported", lambda *a, **k: True)
    monkeypatch.setattr(claude_cli, "platform_supported", lambda *a, **k: True)
    monkeypatch.setattr(live, "platform_supported", lambda *a, **k: True)
    from openswap.worker import live_check_claude

    monkeypatch.setattr(live_check_claude, "platform_supported", lambda *a, **k: True)


def setup_root(tmp_path, *, prepared=True, profile_email=EMAIL):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "sequence.json").write_text(json.dumps({"accounts": {
        "4": {"email": EMAIL, "organizationUuid": ORG, "alias": "claudey"},
    }}))
    (root / "codex").mkdir()
    (root / "codex" / "sequence.json").write_text(json.dumps({
        "schemaVersion": 1, "sequence": [1], "accounts": {"1": {"email": "a@example.com", "accountId": "acct-1"}},
    }))
    if prepared:
        profile = claude_exec.profile_for(root, IDENTITY)
        profile.mkdir(parents=True, mode=0o700)
        (profile / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": profile_email, "organizationUuid": ORG}}))
    return root


def pinned(binary="/fake/claude"):
    return claude_cli.PinnedClaude(Path(binary), "2.1.285 (Claude Code)", SHA)


def fake_claude(tmp_path, version="2.1.285 (Claude Code)"):
    binary = tmp_path / "claude-bin"
    binary.write_text(f"#!/bin/sh\necho '{version}'\n")
    binary.chmod(0o755)
    return binary


# -- pinned binary --------------------------------------------------------------------


def test_pin_records_the_installed_binary_and_verify_rechecks_it(tmp_path):
    binary = fake_claude(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    pin = claude_cli.pin(root, binary=binary)
    assert pin.version == "2.1.285 (Claude Code)" and pin.binary == binary.resolve()
    assert claude_cli.verify(root) == pin
    if os.name == "posix":
        assert (claude_cli.pin_path(root).stat().st_mode & 0o777) == 0o600
    binary.write_text("#!/bin/sh\necho '2.1.286 (Claude Code)'\n")  # an update
    with pytest.raises(claude_cli.ClaudeCliError) as error:
        claude_cli.verify(root)
    assert error.value.code == "binary_changed"


def test_pin_refuses_unknown_versions_writable_binaries_and_absence(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(claude_cli.ClaudeCliError) as error:
        claude_cli.pin(root, binary=fake_claude(tmp_path, "not claude"))
    assert error.value.code == "version_unrecognized"
    writable = fake_claude(tmp_path)
    writable.chmod(0o777)
    with pytest.raises(claude_cli.ClaudeCliError) as error:
        claude_cli.pin(root, binary=writable)
    assert error.value.code == "binary_permissions"
    with pytest.raises(claude_cli.ClaudeCliError) as error:
        claude_cli.verify(root)
    assert error.value.code == "not_pinned"
    monkeypatch.setattr(claude_cli, "CANDIDATES", ())
    with pytest.raises(claude_cli.ClaudeCliError) as error:
        claude_cli.pin(root, which=lambda name: None)
    assert error.value.code == "not_installed"


# -- adapter ---------------------------------------------------------------------------


class FakeLaunch:
    """Records the launch; ``script`` lines are Claude's stream-json output."""

    def __init__(self, script=(), exit_status=0):
        self.script = list(script)
        self.exit = exit_status
        self.launches = []
        self.proof = StopProof(True, False, 0)
        self.running = False

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        self.launches.append({"argv": argv, "env": env, "cwd": cwd, "stdin": stdin_text, "run_dir": run_dir})
        handle = JobHandle(cont.job_label(job_id), "gui/501", Path(run_dir), "boot", 4100 + len(self.launches), 900, True)
        cont._save_handle(handle)
        (Path(run_dir) / "stdout.jsonl").write_text("".join(json.dumps(line) + "\n" for line in self.script))
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
        self.running = False
        return self.proof

    def recover(self, run_dir):
        return self.proof if cont.load_handle(Path(run_dir)) else None

    def members(self, handle):
        return [1] if self.running else []

    def label_loaded(self, handle):
        return self.running


INIT = {"type": "system", "subtype": "init", "session_id": "s", "tools": list(claude_exec.RESEARCH_TOOLS),
        "mcp_servers": [], "apiKeySource": "none"}
SUCCESS = [
    INIT,
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "WebSearch",
                                                    "input": {"query": "python"}}]}},
    {"type": "result", "subtype": "success", "is_error": False,
     "result": "Python 3.x. Source: https://www.python.org/downloads/", "usage": {"output_tokens": 9}},
]


def job_record(identity=IDENTITY, job_id="a" * 32):
    now = datetime.now(timezone.utc)
    return JobRecord(
        job_id=job_id, idempotency_key="k", owner_ref="local-user", provider="codex", task="Find it",
        capability_profile="research", workspace_id="research", state=JobState.STARTING, created_at=now,
        updated_at=now, expires_at=now + timedelta(hours=1), runtime_limit_s=600, pinned_account_ref=identity,
        provider_session_id=None, worker_epoch=1, generation=2, diagnostic_code=None, event_cursor=0,
    )


def workspace(root, *sources):
    out = root.parent / "research" / ("a" * 32)
    out.mkdir(parents=True, exist_ok=True)
    return ResolvedWorkspace("research", out, tuple(sources))


def make_adapter(root, launcher, *, mode="live", live_sessions=False, home=None):
    if mode == "live":
        # The owner's Claude opt-in, bound to the pinned binary.
        write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, SHA, "now"), "claude")
    return ClaudeCodeAdapter(
        root, containment=launcher, verify=lambda **kw: pinned(), mode=lambda: mode, sleep=lambda s: None,
        managed=lambda p: [], live_sessions=lambda p: live_sessions, home=home or root.parent / "home",
    )


def drain(adapter, run):
    events, cursor = [], 0
    for _ in range(20):
        for event in adapter.events(run, after_cursor=cursor):
            cursor = event.cursor
            events.append(event)
        if events and events[-1].kind == SafeEventKind.PROVIDER_FINISHED:
            break
    return events


def test_argv_env_and_seatbelt_are_fixed_research_only(tmp_path):
    root = setup_root(tmp_path)
    launcher = FakeLaunch(SUCCESS)
    source = tmp_path / "src"
    source.mkdir()
    os.environ["ANTHROPIC_API_KEY"] = "sk-must-not-pass"
    try:
        make_adapter(root, launcher).start(job_record(), workspace(root, source), worker_epoch=1)
    finally:
        os.environ.pop("ANTHROPIC_API_KEY")
    launch = launcher.launches[0]
    argv = launch["argv"]
    assert argv[:3] == ["/usr/bin/sandbox-exec", "-f", str(Path(launch["run_dir"]) / "claude.sb")]
    assert argv[3] == "/fake/claude" and "-p" in argv and "--restricted" in argv
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob,WebSearch,WebFetch"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--add-dir") + 1] == str(source.resolve())
    for forbidden in ("--model", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                      "--mcp-config", "--settings", "Bash", "Write", "Edit"):
        assert forbidden not in argv and not any(forbidden in arg.split(",") for arg in argv)
    env = launch["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(claude_exec.profile_for(root, IDENTITY))
    assert "ANTHROPIC_API_KEY" not in env and env["DISABLE_AUTOUPDATER"] == "1"
    sb = (Path(launch["run_dir"]) / "claude.sb").read_text()
    assert "(deny file-write*)" in sb and "(allow default)" in sb
    assert str(workspace(root).output_root.resolve()) in sb
    assert f'(subpath "{root.resolve()}")' in sb  # the backup root is hidden...
    assert str(claude_exec.profile_for(root, IDENTITY).resolve()) in sb  # ...except this profile
    assert "Find it" in launch["stdin"]


def test_a_codex_job_never_runs_on_the_claude_adapter_and_vice_versa(tmp_path):
    root = setup_root(tmp_path)
    with pytest.raises(ProviderLaunchRefused) as error:
        make_adapter(root, FakeLaunch(SUCCESS)).start(job_record(CODEX_IDENTITY), workspace(root), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_auth_unavailable"
    codex = CodexExecAdapter(root, containment=FakeLaunch(), verify=lambda **kw: pinned(), mode=lambda: "live",
                             managed=lambda h: [])
    with pytest.raises(ProviderLaunchRefused):
        codex.start(job_record(IDENTITY), workspace(root), worker_epoch=1)


@pytest.mark.parametrize("problem", ["not_prepared", "other_account", "live_session", "disabled"])
def test_launch_refusals(tmp_path, problem):
    root = setup_root(tmp_path, prepared=problem != "not_prepared",
                      profile_email="someone@example.com" if problem == "other_account" else EMAIL)
    launcher = FakeLaunch(SUCCESS)
    adapter = make_adapter(root, launcher, live_sessions=problem == "live_session",
                           mode="disabled" if problem == "disabled" else "live")
    with pytest.raises(ProviderLaunchRefused) as error:
        adapter.start(job_record(), workspace(root), worker_epoch=1)
    expected = {"not_prepared": "provider_auth_unavailable", "other_account": "provider_auth_unavailable",
                "live_session": "provider_unavailable", "disabled": "live_adapter_disabled"}[problem]
    assert error.value.diagnostic_code == expected and launcher.launches == []


def test_stream_json_maps_to_started_and_succeeded_with_the_result_published(tmp_path):
    root = setup_root(tmp_path)
    adapter = make_adapter(root, FakeLaunch(SUCCESS))
    run = adapter.start(job_record(), workspace(root), worker_epoch=1)
    events = drain(adapter, run)
    assert [e.kind for e in events] == [SafeEventKind.PROVIDER_STARTED, SafeEventKind.PROVIDER_FINISHED]
    assert events[-1].state == JobState.SUCCEEDED and events[-1].execution_stopped is True
    assert (workspace(root).output_root / "result.md").read_text().startswith("Python 3.x.")
    summary = json.loads((Path(root) / "worker" / "runs" / ("a" * 32) / "summary.json").read_text())
    assert summary["provider"] == "claude" and summary["item_counts"] == {"WebSearch": 1}
    assert summary["tools"] == sorted(claude_exec.RESEARCH_TOOLS) and summary["mcp_servers"] == 0
    assert "python.org" not in json.dumps(summary)


@pytest.mark.parametrize("result, code", [
    ({"type": "result", "subtype": "success", "is_error": True, "result": "Claude AI usage limit reached"},
     "provider_rate_limited"),
    ({"type": "result", "subtype": "error_during_execution", "result": "OAuth token has expired; /login"},
     "provider_auth_unavailable"),
    ({"type": "result", "subtype": "error_max_turns"}, "provider_unavailable"),
])
def test_failed_results_map_to_allowlisted_codes(tmp_path, result, code):
    root = setup_root(tmp_path)
    adapter = make_adapter(root, FakeLaunch([INIT, result], exit_status=1))
    run = adapter.start(job_record(), workspace(root), worker_epoch=1)
    finished = drain(adapter, run)[-1]
    assert finished.state == JobState.FAILED and finished.diagnostic_code == code


def test_seatbelt_profile_enforces_the_boundary_for_real(tmp_path):
    if sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"):
        pytest.skip("Seatbelt")
    root = tmp_path / "backup"
    out, profile, home = tmp_path / "out", root / "sessions" / "p", tmp_path / "home"
    for path in (out, profile, home, root / "other"):
        path.mkdir(parents=True)
    (root / "other" / "secret").write_text("s")
    (profile / "ok").write_text("p")
    sb = tmp_path / "p.sb"
    # pytest's tmp_path is in this user's temporary folder, which the profile
    # leaves writable for system frameworks: pass none here, so it is not.
    sb.write_text(claude_exec.seatbelt_profile(output_root=out.resolve(), profile=profile.resolve(),
                                               run_tmp=(tmp_path / "t").resolve(), home=home.resolve(),
                                               backup_root=root.resolve(), user_dirs=[]))
    script = (f"cat {root / 'other' / 'secret'} >/dev/null 2>&1; echo R1 $?; cat {profile / 'ok'} >/dev/null; "
              f"echo R2 $?; echo x > {out / 'w'}; echo R3 $?; echo x > {home / 'w'} 2>/dev/null; echo R4 $?; "
              f"echo x > {root / 'other' / 'w'} 2>/dev/null; echo R5 $?; echo x > {profile / 'w'}; echo R6 $?")
    result = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(sb), "/bin/sh", "-c", script],
                            capture_output=True, text=True)
    codes = dict(line.split() for line in result.stdout.splitlines())
    assert codes == {"R1": "1", "R2": "0", "R3": "0", "R4": "1", "R5": "1", "R6": "0"}


def test_the_seatbelt_write_set_is_this_users_folders_only(tmp_path):
    text = claude_exec.seatbelt_profile(output_root=Path("/o"), profile=Path("/b/sessions/1"), run_tmp=Path("/t"),
                                        home=Path("/Users/u"), backup_root=Path("/b"),
                                        user_dirs=[Path("/private/var/folders/x/y/T")])
    assert '(subpath "/private/var/folders")' not in text
    assert '(subpath "/private/var/folders/x/y/T")' in text
    lines = text.splitlines()
    # The backup root's write-deny comes after the support folders and before the job's own.
    deny = next(i for i, line in enumerate(lines) if line.startswith("(deny file-write* ") and '"/b"' in line)
    assert lines[deny - 1].startswith("(allow file-write*") and '"/o"' in lines[deny + 1]


# -- routing, leases and execution mode -----------------------------------------------------


class RecordingAdapter:
    execution_mode = "disabled"

    def __init__(self, name):
        self.name = name
        self.started = []

    def probe(self):
        from openswap.worker.models import ProviderAvailability
        return ProviderAvailability(True, None, self.name)

    def start(self, job, workspace, *, worker_epoch):
        self.started.append(job.pinned_account_ref)
        raise ProviderLaunchRefused("provider_unavailable")

    def events(self, run, *, after_cursor):
        return ()

    def interrupt(self, run):
        from openswap.worker.models import InterruptResult
        return InterruptResult(False, False, None)


def _runtime(root, pin):
    update_worker_settings(root, enabled=True)
    configure_worker_local_policy(root, pinned_account_ref=pin,
                                  workspaces=(WorkerWorkspace("research", (root.parent / "research").resolve()),))
    codex, claude = RecordingAdapter("codex"), RecordingAdapter("claude")
    runtime = WorkerRuntime(root, adapter=codex, sleeper=lambda s: None)
    runtime.adapters = {"codex": codex, "claude": claude}
    runtime.adapter = codex
    return runtime, codex, claude


def _submit(runtime):
    return runtime.submit(JobSubmission(
        idempotency_key="k1", provider="codex", task="t", capability_profile="research", workspace_id="research",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1), runtime_limit_s=600,
    ))


def test_a_job_runs_on_the_provider_of_its_pinned_account(tmp_path):
    root = setup_root(tmp_path)
    runtime, codex, claude = _runtime(root, IDENTITY)
    _submit(runtime)
    final = runtime.reconcile_once()
    assert claude.started == [IDENTITY] and codex.started == []
    assert final.state == JobState.FAILED and final.pinned_account_ref == IDENTITY
    lease = AccountLeaseStore(root, "claude").read_current()
    assert lease.account_identity == IDENTITY and lease.reason == "unlaunched"
    assert AccountLeaseStore(root, "codex").read_current() is None


def test_execution_mode_follows_the_pinned_provider(tmp_path):
    root = setup_root(tmp_path)
    runtime, codex, claude = _runtime(root, IDENTITY)
    claude.execution_mode = "live"
    assert runtime.execution_mode() == "live"
    configure_worker_local_policy(root, pinned_account_ref=CODEX_IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (root.parent / "research").resolve()),))
    assert runtime.execution_mode() == "disabled"


def test_one_job_per_host_across_providers(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    leases = ProviderLeases(root)
    leases.acquire(job_id="b" * 32, account_identity=CODEX_IDENTITY, worker_pid=os.getpid(), worker_epoch=1, ttl_s=60)
    with pytest.raises(LeaseConflictError):
        leases.acquire(job_id="c" * 32, account_identity=IDENTITY, worker_pid=os.getpid(), worker_epoch=1, ttl_s=60)
    assert leases.for_job("b" * 32).account_identity == CODEX_IDENTITY


# -- per-provider opt-in and evidence ---------------------------------------------------------


def test_each_provider_has_its_own_opt_in(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, SHA, "now"), "claude")
    assert live.execution_mode(root, "claude") == "live" and live.execution_mode(root, "codex") == "disabled"
    assert load_live_execution(root, "claude").binary_sha256 == SHA
    live.disable_live(root, "claude")
    assert live.execution_mode(root, "claude") == "disabled"


def _claude_evidence(**overrides):
    data = {"kind": live.EVIDENCE_KIND, "schema": live.EVIDENCE_SCHEMA, "passed": True, "provider": "claude",
            "cli": {"version": "2.1.285 (Claude Code)", "binary_sha256": SHA},
            "gates": {name: {"passed": True} for name in live.REQUIRED_GATES}}
    data.update(overrides)
    return data


def test_claude_evidence_enables_only_claude_and_only_for_its_binary(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    evidence = tmp_path / "e.json"
    evidence.write_text(json.dumps(_claude_evidence()))
    live.enable_live(root, evidence, pinned(), "claude")
    assert live.execution_mode(root, "claude") == "live" and live.execution_mode(root, "codex") == "disabled"
    assert "evidence_for_another_provider" in live.evidence_problems(_claude_evidence(), provider="codex")
    other = claude_cli.PinnedClaude(Path("/x"), "2.1.285 (Claude Code)", "00" * 32)
    assert "claude_binary_mismatch" in live.evidence_problems(_claude_evidence(), pinned=other, provider="claude")


# -- profile preparation ----------------------------------------------------------------------


# -- the Claude live check, simulated -------------------------------------------------------------


class SimulatedClaudeMac(FakeLaunch):
    """Claude Code as the live check drives it, with a sandbox that holds."""

    def __init__(self, home):
        super().__init__()
        self.home = home
        self.jobs = {}

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        reads = re.findall(r"^\d+\. (/.+)$", stdin_text, flags=re.M)
        lines = [INIT]
        running = "thoroughly" in stdin_text
        if reads:
            for index, path in enumerate(reads):
                allowed = path.startswith(str(cwd) + "/")
                lines.append({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"r{index}", "name": "Read", "input": {"file_path": path}}]}})
                lines.append({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"r{index}", "is_error": not allowed,
                     "content": Path(path).read_text() if allowed else "Permission denied"}]}})
            lines.append({"type": "result", "subtype": "success", "is_error": False, "result": "DONE",
                          "usage": {"output_tokens": 3}})
        elif not running:
            lines += SUCCESS[1:]
        self.script = lines
        self.exit = None if running else 0
        handle = super().launch(job_id=job_id, run_dir=run_dir, argv=argv, env=env, cwd=cwd, stdin_text=stdin_text)
        self.jobs[handle.label] = running
        return handle

    def run(self, argv, **kwargs):
        if argv[0] == "/usr/bin/sandbox-exec":
            script = argv[-1]
            out = []
            for name in re.findall(r'echo "R (\w+) \$\?"', script):
                if name == "inside_write":
                    target = re.search(r"printf ok > '([^']+)'", script).group(1)
                    Path(target).write_text("ok")
                out.append(f"R {name} {0 if name.startswith('inside') else 1}")
            return subprocess.CompletedProcess(argv, 0, "\n".join(out) + "\n", "")
        if argv[0] == "/usr/bin/security":
            return subprocess.CompletedProcess(argv, 0, '"mdat"<timedate>=0x1 "20261001"\n', "")
        if argv[0] == "/bin/ps":
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


class Child:
    def __init__(self, check, payload):
        record = check._job_record(payload["job_id"], payload["identity"], payload["task"])
        check.adapter().start(record, ResolvedWorkspace("live-check", Path(payload["workspace"]), ()), worker_epoch=0)
        self.stdout = io.StringIO("STARTED\n")

    def kill(self):
        pass

    def wait(self):
        return -9


def test_a_simulated_claude_live_check_passes_and_enables_claude_only(tmp_path):
    from openswap.worker.live_check_claude import ClaudeLiveCheck

    root = setup_root(tmp_path)
    configure_worker_local_policy(root, pinned_account_ref=IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (root / "research").resolve()),))
    home = tmp_path / "home"
    home.mkdir()
    mac = SimulatedClaudeMac(home)
    holder = {}
    check = ClaudeLiveCheck(root, out=lambda *a: None, containment=mac, verify=lambda **kw: pinned(), run=mac.run,
                            spawn_child=lambda payload: Child(holder["check"], payload), sleep=lambda s: None,
                            home=home, live_sessions=lambda p: False)
    holder["check"] = check
    evidence = check.run()
    failed = {name: {k: v for k, v in gate.items() if v is False} for name, gate in evidence["gates"].items()
              if not gate["passed"]}
    assert failed == {} and evidence["errors"] == {}
    assert evidence["provider"] == "claude" and evidence["cli"]["binary_sha256"] == SHA
    path = live_check.write_evidence(root, evidence)
    assert path.name.startswith("live-check-claude-")
    assert live.latest_evidence(root, "claude") == path and live.latest_evidence(root, "codex") is None
    live.enable_live(root, path, pinned(), "claude")
    assert live.execution_mode(root, "claude") == "live" and live.execution_mode(root, "codex") == "disabled"



def test_a_binary_inside_the_hidden_claude_folder_is_never_pinned(tmp_path, monkeypatch):
    home = tmp_path / "home"
    local = home / ".claude" / "local"
    local.mkdir(parents=True)
    binary = local / "claude"
    binary.write_text("#!/bin/sh\necho '2.1.285 (Claude Code)'\n")
    binary.chmod(0o755)
    monkeypatch.setattr(claude_cli, "_hidden_from_jobs",
                        lambda path: Path(os.path.realpath(path)).is_relative_to(os.path.realpath(home / ".claude")))
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(claude_cli.ClaudeCliError) as error:
        claude_cli.pin(root, binary=binary)
    assert error.value.code == "binary_in_claude_config"
    monkeypatch.setattr(claude_cli, "CANDIDATES", (str(binary),))
    assert claude_cli.find_installed(which=lambda name: None) is None


def test_status_reports_the_pinned_providers_mode(tmp_path):
    from openswap.worker.runtime import read_worker_snapshot

    root = setup_root(tmp_path)
    configure_worker_local_policy(root, pinned_account_ref=IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (root / "research").resolve()),))
    assert read_worker_snapshot(root).provider.available is False
    write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, SHA, "now"), "claude")
    claude_cli.pin(root, binary=fake_claude(tmp_path))
    provider = read_worker_snapshot(root).provider
    assert provider.available is True and provider.version == "2.1.285 (Claude Code)"
    assert live.pinned_execution_mode(root) == "live"
    # Codex pinned, only Claude opted in: not available.
    configure_worker_local_policy(root, pinned_account_ref=CODEX_IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (root / "research").resolve()),))
    assert read_worker_snapshot(root).provider.available is False
    assert live.pinned_execution_mode(root) == "disabled"



def test_enabling_from_a_claude_check_names_claude_in_the_opt_out(tmp_path, monkeypatch, capsys):
    from openswap.worker.live_check_claude import ClaudeLiveCheck

    root = setup_root(tmp_path)
    configure_worker_local_policy(root, pinned_account_ref=IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (root / "research").resolve()),))
    home = tmp_path / "home"
    home.mkdir()
    mac = SimulatedClaudeMac(home)
    real_init = ClaudeLiveCheck.__init__

    def init(self, backup_root, **kwargs):
        kwargs.update(containment=mac, verify=lambda **kw: pinned(), run=mac.run, sleep=lambda s: None,
                      spawn_child=lambda payload: Child(self, payload), home=home, live_sessions=lambda p: False)
        real_init(self, backup_root, **kwargs)

    monkeypatch.setattr(ClaudeLiveCheck, "__init__", init)
    monkeypatch.setattr(claude_cli, "verify", lambda root, **kw: pinned())
    assert live_check.main(["live-check", "--yes", "--enable"], root) == 0
    out = capsys.readouterr().out
    assert "`openswap worker live disable --provider claude`" in out
    assert live.execution_mode(root, "claude") == "live" and live.execution_mode(root, "codex") == "disabled"



def test_a_replaced_pin_between_probe_and_start_is_an_unlaunched_refusal(tmp_path):
    root = setup_root(tmp_path)
    write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, "00" * 32, "now"), "claude")
    launcher = FakeLaunch(SUCCESS)
    adapter = ClaudeCodeAdapter(
        root, containment=launcher, verify=lambda **kw: pinned(), mode=lambda: "live", sleep=lambda s: None,
        managed=lambda p: [], live_sessions=lambda p: False, home=tmp_path / "home", bind_to_opt_in=True,
    )
    with pytest.raises(ProviderLaunchRefused):
        adapter.start(job_record(), workspace(root), worker_epoch=1)
    assert launcher.launches == []



def test_a_profile_sharing_the_default_customizations_is_refused_and_prepare_cleans_it(tmp_path):
    from openswap.session import SHARE_MANIFEST

    root = setup_root(tmp_path)
    profile = claude_exec.profile_for(root, IDENTITY)
    (profile / SHARE_MANIFEST).write_text(json.dumps(["settings.json"]))
    launcher = FakeLaunch(SUCCESS)
    with pytest.raises(ProviderLaunchRefused) as error:
        make_adapter(root, launcher).start(job_record(), workspace(root), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_unavailable" and launcher.launches == []

    calls = []

    def unshare(path):  # removes the mirrored links and the manifest
        calls.append(path)
        (path / SHARE_MANIFEST).unlink()

    def no_login(argv, **kwargs):
        raise AssertionError("already signed in: no login")

    result = live_cli.claude_prepare(root, "claude:4", run=no_login, verify=lambda: pinned(), unshare=unshare)
    assert result["profile_ready"] is True and calls == [profile]


def test_a_link_into_the_default_profile_counts_as_shared(tmp_path):
    home = tmp_path / "home"
    (home / ".claude" / "skills").mkdir(parents=True)
    profile = tmp_path / "profile"
    profile.mkdir()
    assert claude_exec.profile_shared(profile, home) is False
    (profile / "skills").symlink_to(home / ".claude" / "skills")
    assert claude_exec.profile_shared(profile, home) is True


def test_polling_needs_every_provider_a_claim_could_select(tmp_path):
    from openswap.worker.models import ProviderAvailability

    root = setup_root(tmp_path)
    runtime, codex, claude = _runtime(root, CODEX_IDENTITY)
    on = lambda: ProviderAvailability(True, None, "v")  # noqa: E731
    off = lambda: ProviderAvailability(False, "live_adapter_disabled", None)  # noqa: E731
    codex.probe, claude.probe = off, on
    assert runtime._candidate_providers() == ["codex"]
    assert runtime.provider_availability().available is False  # the pin's provider is off
    codex.probe = on
    assert runtime.provider_availability().available is True
    # An allowlisted Claude account the service may choose: its provider must be on too.
    from openswap.worker.cli import allow_worker_account

    allow_worker_account(root, "claude:4")
    assert runtime._candidate_providers() == ["codex", "claude"]
    claude.probe = off
    assert runtime.provider_availability().diagnostic_code == "live_adapter_disabled"
    claude.probe = on
    assert runtime.provider_availability().available is True



def test_status_does_not_call_a_shared_profile_ready(tmp_path):
    from openswap.session import SHARE_MANIFEST

    root = setup_root(tmp_path)
    claude_cli.pin(root, binary=fake_claude(tmp_path))
    ready = {a["slot"]: a["profile_ready"] for a in live_cli.claude_status(root)["accounts"]}
    assert ready == {"4": True}
    (claude_exec.profile_for(root, IDENTITY) / SHARE_MANIFEST).write_text("[]")
    ready = {a["slot"]: a["profile_ready"] for a in live_cli.claude_status(root)["accounts"]}
    assert ready == {"4": False}


def test_the_claude_default_login_snapshot_never_opens_the_credentials(tmp_path, monkeypatch):
    from openswap.worker.live_check_claude import ClaudeLiveCheck

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    creds = home / ".claude" / ".credentials.json"
    creds.write_text("secret-token")
    root = setup_root(tmp_path)
    check = ClaudeLiveCheck(root, out=lambda *a: None, home=home,
                            run=lambda argv, **kw: subprocess.CompletedProcess(argv, 44, "", ""))
    opened = []
    real_open = open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(AssertionError(f"read {self}")))
    state, fingerprint = check._default_login_snapshot()
    assert state == "present" and "secret" not in fingerprint and str(creds) not in opened



@pytest.mark.parametrize("where", ["claude", "codex", "sessions", "home"])
def test_claude_never_grants_a_hidden_folder(tmp_path, where):
    root = setup_root(tmp_path)
    home = root.parent / "home"
    target = {"claude": home / ".claude", "codex": home / ".codex",
              "sessions": root / "sessions", "home": home}[where]
    target.mkdir(parents=True, exist_ok=True)
    launcher = FakeLaunch(SUCCESS)
    with pytest.raises(ProviderLaunchRefused) as error:
        make_adapter(root, launcher, home=home).start(job_record(), workspace(root, target), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_unavailable" and launcher.launches == []


def test_the_built_in_research_area_stays_grantable(tmp_path):
    root = setup_root(tmp_path)
    adapter = make_adapter(root, FakeLaunch(SUCCESS), home=root.parent / "home")
    assert adapter._grant_allowed(root / "worker" / "research" / "x") is True
    assert adapter._grant_allowed(root / "live-check" / "t" / "sandbox") is True
    assert adapter._grant_allowed(root / "sessions" / "4-carol") is False



def test_a_held_claude_lease_points_at_the_claude_store(tmp_path):
    from openswap.worker.live_check_claude import ClaudeLiveCheck

    root = setup_root(tmp_path)
    configure_worker_local_policy(root, pinned_account_ref=IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (root / "research").resolve()),))
    AccountLeaseStore(root, "claude").acquire(job_id="b" * 32, account_identity=IDENTITY, worker_pid=os.getpid(),
                                              worker_epoch=1, ttl_s=60)
    check = ClaudeLiveCheck(root, out=lambda *a: None, home=tmp_path / "home", verify=lambda **kw: pinned())
    with pytest.raises(live_check.CheckRefused) as error:
        check.run()
    assert error.value.code == "lease_held"
    assert "`openswap worker lease release --provider claude`" in str(error.value)


def _native_login(root, email=EMAIL, code=0):
    """A stand-in for `claude auth login`: signs the profile in CLAUDE_CONFIG_DIR in."""
    calls = []

    def run(argv, env, check=False, **kwargs):
        calls.append((list(argv), dict(env), AccountLeaseStore(root, "claude").read_current()))
        profile = Path(env["CLAUDE_CONFIG_DIR"])
        if argv[1:3] == ["auth", "login"] and email is not None:
            (profile / ".claude.json").write_text(json.dumps(
                {"oauthAccount": {"emailAddress": email, "organizationUuid": ORG}}))
        elif argv[1:3] == ["auth", "logout"]:
            (profile / ".claude.json").unlink(missing_ok=True)
        return subprocess.CompletedProcess(argv, code, "", "")

    run.calls = calls
    return run


def test_prepare_signs_in_with_claudes_own_login_under_the_claude_lease(tmp_path, monkeypatch):
    from openswap.session import SessionManager

    monkeypatch.setattr(SessionManager, "setup_session",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no credential seeding")))
    root = setup_root(tmp_path, prepared=False)
    run = _native_login(root)
    result = live_cli.claude_prepare(root, "claude:4", run=run, verify=lambda: pinned())
    assert result == {"slot": "4", "account_ref": IDENTITY, "profile_ready": True, "signed_in_now": True}
    argv, env, lease = run.calls[0]
    profile = claude_exec.profile_for(root, IDENTITY)
    assert argv == ["/fake/claude", "auth", "login", "--claudeai", "--email", EMAIL]
    assert env["CLAUDE_CONFIG_DIR"] == str(profile) and "ANTHROPIC_API_KEY" not in env
    assert lease is not None and lease.state == "active" and lease.account_identity == IDENTITY
    assert AccountLeaseStore(root, "claude").read_current().state == "released"
    assert not (profile / ".credentials.json").exists()  # nothing seeded by OpenSwap


def test_prepare_signs_a_different_account_back_out(tmp_path):
    root = setup_root(tmp_path, prepared=False)
    run = _native_login(root, email="someone@example.com")
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.claude_prepare(root, "claude:4", run=run, verify=lambda: pinned())
    assert error.value.code == "login_account_mismatch"
    assert [call[0][1:3] for call in run.calls] == [["auth", "login"], ["auth", "logout"]]
    assert AccountLeaseStore(root, "claude").read_current().state == "released"


def test_prepare_reports_a_failed_login(tmp_path):
    root = setup_root(tmp_path, prepared=False)
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.claude_prepare(root, "claude:4", run=_native_login(root, email=None, code=1), verify=lambda: pinned())
    assert error.value.code == "login_failed"


def test_prepare_leaves_a_ready_profile_alone_and_refuses_during_a_job(tmp_path):
    root = setup_root(tmp_path, prepared=True)
    run = _native_login(root)
    result = live_cli.claude_prepare(root, "claude:4", run=run, verify=lambda: pinned())
    assert result["profile_ready"] is True and result["signed_in_now"] is False and run.calls == []
    AccountLeaseStore(root, "claude").acquire(job_id="b" * 32, account_identity=IDENTITY, worker_pid=os.getpid(),
                                              worker_epoch=1, ttl_s=60)
    with pytest.raises(LeaseConflictError):
        live_cli.claude_prepare(root, "claude:4", run=run, verify=lambda: pinned())
    assert run.calls == []

