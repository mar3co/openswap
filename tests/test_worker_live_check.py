"""The live-check evidence harness, driven by simulated and fake Codex runs (never real Codex)."""

from __future__ import annotations

import io
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from openswap.settings import WorkerWorkspace, configure_worker_local_policy, update_worker_settings
from openswap.worker import codex_cli, codex_exec, live, live_check
from openswap.worker import containment as cont
from openswap.worker.containment import JobHandle, LaunchdContainment, StopProof
from openswap.worker.leases import AccountLeaseStore, stable_account_identity
from openswap.worker.live_check import CheckRefused, LiveCheck

ACCOUNT_ID = "acct-check-1"
IDENTITY = stable_account_identity("codex", ACCOUNT_ID)
FIXTURES = Path(__file__).parent / "fixtures"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the live check is macOS-only")


def auth_json(account_id=ACCOUNT_ID):
    import base64

    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    claims = {"email": "o@example.com", "https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    return json.dumps({"tokens": {"id_token": f"{b64({})}.{b64(claims)}.s", "account_id": account_id,
                                  "refresh_token": "r"}})


def setup_root(tmp_path, *, signed_in=True):
    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    (root / "codex").mkdir()
    (root / "codex" / "sequence.json").write_text(json.dumps({
        "schemaVersion": 1, "activeAccountNumber": None, "sequence": ["1"],
        "accounts": {"1": {"email": "o@example.com", "accountId": ACCOUNT_ID}},
    }))
    configure_worker_local_policy(root, pinned_account_ref=IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (root / "research").resolve()),))
    if signed_in:
        home = codex_exec.prepare_home(root, IDENTITY)
        (home / "auth.json").write_text(auth_json())
    return root


def pinned(binary="/fake/codex"):
    return codex_cli.PinnedCodex(Path(binary), codex_cli.CODEX_VERSION_OUTPUT, codex_cli.ASSET_SHA256, "ab" * 32)


@pytest.fixture(autouse=True)
def apple_silicon(monkeypatch):
    monkeypatch.setattr(codex_cli, "platform_supported", lambda *a, **k: True)
    monkeypatch.setattr(live, "platform_supported", lambda *a, **k: True)
    monkeypatch.setattr(live_check.codex_cli, "platform_supported", lambda *a, **k: True)


# -- pure helpers -----------------------------------------------------------------------


def test_parse_features_and_command_items(tmp_path):
    assert live_check.parse_features("apps  stable  false\nshell_tool stable true\nheader\n") == {
        "apps": False, "shell_tool": True}
    stdout = tmp_path / "stdout.jsonl"
    stdout.write_text("\n".join(json.dumps(r) for r in [
        {"type": "item.started", "item": {"id": "1", "type": "command_execution", "command": "cat a"}},
        {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "cat a",
                                            "aggregated_output": "x", "exit_code": 1}},
        {"type": "item.completed", "item": {"id": "2", "type": "mcp_tool_call"}},
    ]) + "\nnot json\n")
    assert live_check.command_items(stdout) == [{"command": "cat a", "output": "x", "exit_code": 1}]
    assert live_check.item_types(stdout) == {"command_execution", "mcp_tool_call"}


# -- a simulated Mac: sandboxed or leaky -------------------------------------------------


class SimulatedMac:
    """Containment, process list and command runner for a simulated Codex.

    ``sandboxed`` decides what the simulated research sandbox allows.
    """

    def __init__(self, *, sandboxed=True, contain=True):
        self.sandboxed = sandboxed
        self.contain = contain
        self.jobs: dict[str, dict] = {}
        self.processes: list[tuple[int, str]] = []
        self.submitted_labels: set[str] = set()
        self.managed_key = False
        self.features_text = None

    # containment
    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        cont.ensure_private_dir(Path(run_dir))
        handle = JobHandle(cont.job_label(job_id), "gui/501", Path(run_dir), "boot", 6000 + len(self.jobs),
                           900 + len(self.jobs), True)
        cont._save_handle(handle)
        job = {"running": False, "exit": 0, "helpers": []}
        lines = [{"type": "thread.started"}]
        cwd = Path(cwd)
        if "sh ./helper.sh" in stdin_text:
            script = (cwd / "helper.sh").read_text()
            sleeps = re.findall(r"(?:sleep\"?, \"|/bin/sleep )(\d+)", script)
            job.update(running=True, exit=None, helpers=[f"/bin/sleep {n}" for n in sleeps])
            self.processes += [(7000 + i, cmd) for i, cmd in enumerate(job["helpers"])]
        else:
            commands = re.findall(r"^\d+\. (.+)$", stdin_text, flags=re.M)
            for index, command in enumerate(commands):
                output, code = self._simulate(command, cwd)
                lines.append({"type": "item.completed", "item": {
                    "id": str(index), "type": "command_execution", "command": command,
                    "aggregated_output": output, "exit_code": code}})
            if not commands:
                lines.append({"type": "item.completed", "item": {"id": "w", "type": "web_search"}})
            (Path(run_dir) / "last-message.md").write_text(
                "DONE\n" if commands else "See https://www.python.org/downloads/\n")
            lines.append({"type": "turn.completed", "usage": {"output_tokens": 9}})
            (Path(run_dir) / "exit").write_text("0\n")
        (Path(run_dir) / "stdout.jsonl").write_text("".join(json.dumps(r) + "\n" for r in lines))
        self.jobs[handle.label] = job
        return handle

    def _simulate(self, command, cwd):
        if "inside.txt" in command:
            return (cwd / "inside.txt").read_text(), 0
        if "inside-write.txt" in command:
            (cwd / "inside-write.txt").write_text("ok")
            return "", 0
        if command == "/usr/bin/env":
            return "PATH=/usr/bin:/bin\nHOME=/x\n", 0
        if "launchctl submit" in command:
            if not self.sandboxed:
                self.submitted_labels.add(command.split()[3])
                return "", 0
            return "launchctl: Operation not permitted", 1
        if self.sandboxed:
            return "Operation not permitted", 1
        # A leaky "sandbox": everything works.
        path = re.search(r"(/[^ '\"]+read-me\.txt)", command)
        if path:
            return Path(path.group(1)).read_text(), 0
        return "", 0

    def exit_status(self, handle):
        return self.jobs[handle.label]["exit"]

    def leader_alive(self, handle):
        return self.jobs[handle.label]["running"]

    def members(self, handle):
        job = self.jobs.get(handle.label)
        return [1] if job and job["running"] else []

    def label_loaded(self, handle):
        return self.jobs.get(handle.label, {}).get("running", False)

    def stop(self, handle, timeout=15.0):
        job = self.jobs[handle.label]
        if self.contain:
            job["running"] = False
            self.processes = [p for p in self.processes if p[1] not in job["helpers"]]
        return StopProof(not job["running"], job["running"], len(self.members(handle)))

    def recover(self, run_dir):
        handle = cont.load_handle(Path(run_dir))
        return None if handle is None else self.stop(handle)

    # processes and commands
    def list_processes(self):
        return list(self.processes)

    def run(self, argv, **kwargs):
        args = [a for a in argv[1:] if a != "--strict-config"]
        while "--disable" in args:
            i = args.index("--disable")
            del args[i:i + 2]
        if argv[0] == "/usr/bin/defaults":
            return subprocess.CompletedProcess(argv, 0 if self.managed_key else 1, "", "")
        if argv[0] == "/bin/launchctl":
            label = argv[2].rsplit("/", 1)[-1]
            return subprocess.CompletedProcess(argv, 0 if label in self.submitted_labels else 113, "", "")
        if args[:2] == ["mcp", "list"]:
            return subprocess.CompletedProcess(argv, 0, "No MCP servers configured yet.\n", "")
        if args[:2] == ["features", "list"]:
            text = "shell_tool stable true\n" + "".join(f"{n} stable false\n" for n in codex_exec.DISABLED_FEATURES)
            if self.features_text is not None:
                text = self.features_text
            return subprocess.CompletedProcess(argv, 0, text, "")
        if args[:1] == ["sandbox"]:
            script = args[-1]
            out = []
            for name in re.findall(r'echo "R (\w+) \$\?"', script):
                allowed = name in {"inside_read", "inside_write"} or not self.sandboxed
                if name == "inside_write" or (not self.sandboxed and name == "outside_write"):
                    target = re.search(r"printf \w+ > (\S+) 2>/dev/null; echo \"R " + name, script)
                    Path(target.group(1).strip("'")).write_text("x")
                out.append(f"R {name} {0 if allowed else 1}")
            return subprocess.CompletedProcess(argv, 0, "\n".join(out) + "\n", "")
        raise AssertionError(argv)


class FakeChild:
    """The stand-in worker process: launches through the same simulated Mac, then gets killed."""

    def __init__(self, check: LiveCheck, payload: dict):
        from openswap.worker.models import ResolvedWorkspace

        record = check._job_record(payload["job_id"], payload["identity"], payload["task"])
        check.adapter().start(record, ResolvedWorkspace("live-check", Path(payload["workspace"]), ()),
                              worker_epoch=0)
        self.stdout = io.StringIO("STARTED\n")
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self):
        return -9


def make_check(root, mac, **kwargs):
    holder = {}

    def spawn(payload):
        return FakeChild(holder["check"], payload)

    check = LiveCheck(
        root, out=lambda *a: None, containment=mac, verify=lambda **kw: pinned(), run=mac.run,
        spawn_child=spawn, list_processes=mac.list_processes, sleep=lambda s: None, **kwargs,
    )
    holder["check"] = check
    return check


def test_a_sandboxed_contained_mac_passes_every_gate_and_can_be_enabled(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    evidence = make_check(root, mac).run()
    failed = {name: gate for name, gate in evidence["gates"].items() if not gate["passed"]}
    assert failed == {} and evidence["errors"] == {}
    assert evidence["passed"] is True
    assert set(evidence["gates"]) == set(live.REQUIRED_GATES)
    assert evidence["account"] == {"identity": IDENTITY, "slot": "1"}
    lease = AccountLeaseStore(root, "codex").read_current()
    assert lease.state == "released"
    path = live_check.write_evidence(root, evidence)
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text()
    assert "Operation not permitted" not in text and "python.org" not in text  # no model output
    settings = live.enable_live(root, path, pinned())
    assert settings.enabled and live.execution_mode(root) == "live"


def test_a_leaky_sandbox_fails_the_sandbox_gates(tmp_path):
    root = setup_root(tmp_path)
    evidence = make_check(root, SimulatedMac(sandboxed=False)).run()
    gates = evidence["gates"]
    assert gates["sandbox_wrapper"]["passed"] is False
    assert gates["sandbox_wrapper"]["outside_read_denied"] is False
    assert gates["sandbox_exec"]["passed"] is False
    assert gates["sandbox_exec"]["outside_read_denied"] is False
    assert gates["sandbox_exec"]["launchd_submit_contained"] is False
    assert evidence["passed"] is False
    path = live_check.write_evidence(root, evidence)
    with pytest.raises(live.LiveModeError):
        live.enable_live(root, path, pinned())


def test_an_uncontainable_stop_fails_and_keeps_the_lease(tmp_path):
    root = setup_root(tmp_path)
    evidence = make_check(root, SimulatedMac(contain=False)).run()
    assert evidence["gates"]["stop"]["passed"] is False
    assert evidence["gates"]["stop"]["execution_stopped"] is False
    assert evidence["passed"] is False
    lease = AccountLeaseStore(root, "codex").read_current()
    assert lease.state == "uncertain"  # never released without proof


def test_default_login_change_is_detected(tmp_path, monkeypatch):
    root = setup_root(tmp_path)
    default_home = tmp_path / "default"
    default_home.mkdir()
    (default_home / "auth.json").write_text("before")
    monkeypatch.setenv("CODEX_HOME", str(default_home))
    mac = SimulatedMac()
    check = make_check(root, mac)
    original = check._gate_research

    def research_that_touches_default(identity):
        original(identity)
        (default_home / "auth.json").write_text("after")

    check._gate_research = research_that_touches_default
    evidence = check.run()
    assert evidence["gates"]["default_login_unchanged"] == {
        "passed": False, "default_login_present": True, "byte_identical": False}


@pytest.mark.parametrize("problem, code", [
    ("not_signed_in", "account_not_signed_in"),
    ("lease", "lease_held"),
    ("no_cli", "cli_not_installed"),
])
def test_preflight_refusals(tmp_path, problem, code):
    root = setup_root(tmp_path, signed_in=problem != "not_signed_in")
    verify = lambda **kw: pinned()  # noqa: E731
    if problem == "lease":
        AccountLeaseStore(root, "codex").acquire(job_id="x" * 8, account_identity=IDENTITY,
                                                 worker_pid=os.getpid(), worker_epoch=1, ttl_s=60)
    if problem == "no_cli":
        def verify(**kw):
            raise codex_cli.CodexCliError("not_installed")
    mac = SimulatedMac()
    check = LiveCheck(root, out=lambda *a: None, containment=mac, verify=verify, run=mac.run)
    with pytest.raises(CheckRefused) as error:
        check.run()
    assert error.value.code == code


def test_preflight_refuses_while_the_worker_runs_unpaused(tmp_path, monkeypatch):
    from openswap.worker import runtime
    from openswap.worker.models import ProviderAvailability, RemoteConnectivity, WorkerProcessState, WorkerSnapshot

    root = setup_root(tmp_path)
    snapshot = WorkerSnapshot(True, False, WorkerProcessState.RUNNING, RemoteConnectivity.DISABLED,
                              ProviderAvailability(False, "live_adapter_disabled"), None, 0)
    monkeypatch.setattr(runtime, "read_worker_snapshot", lambda root: snapshot)
    mac = SimulatedMac()
    with pytest.raises(CheckRefused) as error:
        make_check(root, mac).run()
    assert error.value.code == "worker_running"


def test_main_does_nothing_without_consent(tmp_path, capsys, monkeypatch):
    root = setup_root(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert live_check.main(["live-check"], root) == 1
    assert "Not run" in capsys.readouterr().out
    assert not live.evidence_dir(root).exists()


@pytest.mark.parametrize("problem", ["missing_feature", "feature_on", "managed_key", "managed_file"])
def test_tool_surface_needs_every_feature_off_and_no_managed_layer(tmp_path, problem):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    names = list(codex_exec.DISABLED_FEATURES)
    if problem == "missing_feature":
        mac.features_text = "shell_tool stable true\n" + "".join(f"{n} stable false\n" for n in names[1:])
    elif problem == "feature_on":
        mac.features_text = "shell_tool stable true\n" + "".join(
            f"{n} stable {'true' if n == 'apps' else 'false'}\n" for n in names)
    elif problem == "managed_key":
        mac.managed_key = True
    else:
        (codex_exec.isolated_home(root, IDENTITY) / "managed_config.toml").write_text("")
    evidence = make_check(root, mac).run()
    assert evidence["gates"]["tool_surface"]["passed"] is False
    assert evidence["passed"] is False


class ShortLivedEscapeMac(SimulatedMac):
    """launchctl submit succeeds, but its job is gone before the label check."""

    def _simulate(self, command, cwd):
        if "launchctl submit" in command:
            return "", 0
        return super()._simulate(command, cwd)


def test_a_successful_launchd_submit_fails_even_if_its_job_is_gone(tmp_path):
    root = setup_root(tmp_path)
    evidence = make_check(root, ShortLivedEscapeMac()).run()
    gate = evidence["gates"]["sandbox_exec"]
    assert gate["launchd_submit_contained"] is False and gate["passed"] is False


class SkippingMac(SimulatedMac):
    """A model that skips some probe commands."""

    def __init__(self, skip):
        super().__init__()
        self.skip = skip

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        lines = [line for line in stdin_text.splitlines() if not any(key in line for key in self.skip)]
        return super().launch(job_id=job_id, run_dir=run_dir, argv=argv, env=env, cwd=cwd,
                              stdin_text="\n".join(lines), ready_timeout=ready_timeout)


@pytest.mark.parametrize("skip", ["auth.json", "link-target.txt", "launchctl submit"])
def test_a_skipped_isolation_probe_fails_the_gate(tmp_path, skip):
    root = setup_root(tmp_path)
    evidence = make_check(root, SkippingMac([skip])).run()
    gate = evidence["gates"]["sandbox_exec"]
    assert gate["passed"] is False and gate["all_required_steps_ran"] is False
    assert evidence["passed"] is False


def test_an_interrupted_check_stops_the_running_job_and_settles_the_lease(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    check = make_check(root, mac)
    check.check_root = root / "live-check" / "t"
    cont.ensure_private_dir(check.check_root.parent)
    cont.ensure_private_dir(check.check_root)

    def interrupted(job_id):
        raise KeyboardInterrupt

    ws = check._workspace("stop")
    check._helper_script(ws)
    with pytest.raises(KeyboardInterrupt):
        check._job("stop", IDENTITY, LiveCheck.HELPER_TASK, timeout=60, until=interrupted, workspace=ws)
    assert all(not job["running"] for job in mac.jobs.values())
    assert AccountLeaseStore(root, "codex").read_current().state == "released"


def test_json_mode_prints_only_the_evidence_on_stdout(tmp_path, capsys, monkeypatch):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    real_init = LiveCheck.__init__

    def init(self, backup_root, **kwargs):
        holder = {}
        kwargs.update(containment=mac, verify=lambda **kw: pinned(), run=mac.run,
                      list_processes=mac.list_processes, sleep=lambda s: None,
                      spawn_child=lambda payload: FakeChild(holder["check"], payload))
        real_init(self, backup_root, **kwargs)
        holder["check"] = self

    monkeypatch.setattr(LiveCheck, "__init__", init)
    monkeypatch.setattr(live_check.codex_cli, "verify", lambda root, **kw: pinned())
    assert live_check.main(["live-check", "--yes", "--json", "--no-enable"], root) == 0
    out, err = capsys.readouterr()
    assert json.loads(out)["passed"] is True
    assert "Evidence:" in err


# -- opt-in: the harness end to end through real launchd with a fake codex ---------------


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("OPENSWAP_LAUNCHD_TESTS") != "1",
    reason="real launchd; set OPENSWAP_LAUNCHD_TESTS=1 on a Mac",
)
def test_real_launchd_harness_with_an_unsandboxed_fake_codex(tmp_path):
    root = setup_root(tmp_path)
    binary = tmp_path / "codex"
    binary.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FIXTURES / "fake_codex.py"}" "$@"\n')
    binary.chmod(0o755)
    real = LaunchdContainment()
    child_code = (
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "from openswap.worker.codex_cli import PinnedCodex, CODEX_VERSION_OUTPUT, ASSET_SHA256\n"
        "from openswap.worker.codex_exec import CodexExecAdapter\n"
        "from openswap.worker.live_check import LiveCheck\n"
        "from openswap.worker.models import ResolvedWorkspace\n"
        "p = json.loads(sys.argv[1])\n"
        "pin = PinnedCodex(Path(p['binary']), CODEX_VERSION_OUTPUT, ASSET_SHA256, 'ab' * 32)\n"
        "check = LiveCheck(Path(p['root']), verify=lambda **kw: pin)\n"
        "record = check._job_record(p['job_id'], p['identity'], p['task'])\n"
        "check.adapter().start(record, ResolvedWorkspace('live-check', Path(p['workspace']), ()), worker_epoch=0)\n"
        "print('STARTED', flush=True)\n"
        "time.sleep(600)\n"
    )

    def spawn(payload):
        return subprocess.Popen([sys.executable, "-c", child_code, json.dumps({**payload, "binary": str(binary)})],
                                stdout=subprocess.PIPE, text=True)

    check = LiveCheck(root, out=print, containment=real, verify=lambda **kw: pinned(binary),
                      spawn_child=spawn, research_timeout=120, probe_timeout=120, helper_wait=60)
    evidence = check.run()
    gates = evidence["gates"]
    print(json.dumps(gates, indent=1))
    for name in ("pinned_cli", "tool_surface", "research_run", "stop", "kill_recovery", "account_identity",
                 "default_login_unchanged"):
        assert gates[name]["passed"] is True, name
    # The fake runs every command without a sandbox, and the harness sees it.
    assert gates["sandbox_wrapper"]["passed"] is False
    assert gates["sandbox_exec"]["passed"] is False
    assert gates["sandbox_exec"]["outside_read_denied"] is False
    assert evidence["passed"] is False
    assert AccountLeaseStore(root, "codex").read_current().state == "released"
