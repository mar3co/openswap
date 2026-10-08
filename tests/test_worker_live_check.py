"""The live-check evidence harness, driven by simulated and fake Codex runs (never real Codex)."""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import signal
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


@pytest.fixture(autouse=True)
def this_mac(monkeypatch):
    # The evidence binding to this Mac (hardware UUID + install), fixed in tests.
    monkeypatch.setattr(live, "host_binding", lambda root, **kw: "4e" * 32)


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
    assert live_check.command_items(stdout) == [
        {"command": "cat a", "output": "x", "exit_code": 1, "completed": True}]
    assert live_check.item_types(stdout) == {"command_execution", "mcp_tool_call"}


# -- a simulated Mac: sandboxed or leaky -------------------------------------------------


class SimulatedMac:
    """Containment, process list and command runner for a simulated Codex.

    ``sandboxed`` decides what the simulated research sandbox allows.
    """

    def __init__(self, *, sandboxed=True, contain=True, links=True, curl_blocked=False):
        self.links = links
        self.curl_blocked = curl_blocked
        self.curl_request_code = 7
        self.booted_out = set()
        self.tmpdir_writes = 0
        self.sandboxed = sandboxed
        self.contain = contain
        self.jobs: dict[str, dict] = {}
        self.processes: list[tuple[int, str]] = []
        self.submitted_labels: set[str] = set()
        self.managed_key = False
        self.network_outside = True
        self.curl_envs: list[dict] = []
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
            markers = re.findall(r'"(openswap-live-check-[0-9a-f]+-(?:child|detached))", "(\d+)"', script)
            job.update(running=True, exit=None, helpers=[f"{name} {n}" for name, n in markers])
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
        if "notes.txt" in command:  # the approved read-only source is readable
            return Path(re.search(r"(/[^ ']+notes\.txt)", command).group(1)).read_text(), 0
        if "TMPDIR" in command and not self.sandboxed:
            self.tmpdir_writes += 1
        if "inside-write.txt" in command:
            (cwd / "inside-write.txt").write_text("ok")
            return "", 0
        if command == "/usr/bin/env":
            return "PATH=/usr/bin:/bin\nHOME=/x\n", 0
        if command.startswith("/usr/bin/curl -sS") and self.sandboxed:
            return "curl: (7) Failed to connect to example.com port 80", self.curl_request_code
        if command == "/usr/bin/curl --version":
            return ("", 126) if self.curl_blocked else ("curl 8.7.1 (x86_64-apple-darwin25.0)\n", 0)
        if "ln -s" in command and self.links:
            target = re.search(r"ln -s '?(/[^ ']+link-target\.txt)", command).group(1)
            (cwd / "link.txt").symlink_to(target)  # writing the link inside the folder works
            if not self.sandboxed:
                return Path(target).read_text(), 0
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
        if argv[0] == "/usr/bin/curl":
            env = kwargs.get("env") or {}
            self.curl_envs.append(env)
            if self.network_outside:
                return subprocess.CompletedProcess(argv, 0, "<title>Example Domain</title>", "")
            return subprocess.CompletedProcess(argv, 6, "", "curl: (6) Could not resolve host")
        if argv[0] == "/usr/bin/defaults":
            return subprocess.CompletedProcess(argv, 0 if self.managed_key else 1, "", "")
        if argv[0] == "/bin/launchctl":
            label = argv[2].rsplit("/", 1)[-1]
            if argv[1] == "bootout" and label in self.submitted_labels:
                self.submitted_labels.discard(label)
                self.booted_out.add(label)
                return subprocess.CompletedProcess(argv, 0, "", "")
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
        "passed": False, "default_login_present": True, "readable": True, "byte_identical": False}


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


@pytest.mark.parametrize("state", ["RUNNING", "STALE", "UNAVAILABLE"])
def test_preflight_refuses_while_the_worker_runs_unpaused(tmp_path, monkeypatch, state):
    from openswap.worker import runtime
    from openswap.worker.models import ProviderAvailability, RemoteConnectivity, WorkerProcessState, WorkerSnapshot

    root = setup_root(tmp_path)
    # A stale or unreadable worker may still be running and admit a job mid-check.
    snapshot = WorkerSnapshot(True, False, getattr(WorkerProcessState, state), RemoteConnectivity.DISABLED,
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


class ChainingMac(SimulatedMac):
    """The model chains every probe into one shell command, ending in a failing submit."""

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        commands = re.findall(r"^\d+\. (.+)$", stdin_text, flags=re.M)
        if not commands:
            return super().launch(job_id=job_id, run_dir=run_dir, argv=argv, env=env, cwd=cwd,
                                  stdin_text=stdin_text, ready_timeout=ready_timeout)
        chained = " ; ".join(commands)
        return super().launch(job_id=job_id, run_dir=run_dir, argv=argv, env=env, cwd=cwd,
                              stdin_text=f"1. {chained}\n", ready_timeout=ready_timeout)

    def _simulate(self, command, cwd):
        if " ; " in command:
            for part in command.split(" ; "):
                super()._simulate(part, cwd)
            inside = (cwd / "inside.txt").read_text()
            return inside, 1  # last command (the submit) failed
        return super()._simulate(command, cwd)


def test_chained_probes_prove_nothing(tmp_path):
    root = setup_root(tmp_path)
    evidence = make_check(root, ChainingMac()).run()
    gate = evidence["gates"]["sandbox_exec"]
    assert gate["each_probe_its_own_command"] is False
    assert gate["auth_read_denied"] is False and gate["shell_network_denied"] is False
    assert gate["passed"] is False


def test_command_matching_is_exact_shell_text():
    probe = "/bin/sh -c 'printf x > /tmp/a.txt'"
    assert live_check.command_matches(probe, probe)
    assert live_check.command_matches(f"bash -lc {shlex.quote(probe)}", probe)
    assert live_check.command_matches('/bin/zsh -lc "cat /x/read-me.txt"', "cat /x/read-me.txt")
    assert not live_check.command_matches("cat /x/read-me.txt >/dev/null", "cat /x/read-me.txt")
    auth = "/bin/sh -c 'cat /h/auth.json > /dev/null'"
    assert not live_check.command_matches("/bin/sh -c \"cat /h/auth.json '>' /dev/null\"", auth)
    symlink = "/bin/sh -c 'ln -s /o/link-target.txt link.txt; cat link.txt'"
    assert not live_check.command_matches("/bin/sh -c \"ln -s /o/link-target.txt 'link.txt;' cat link.txt\"", symlink)
    assert not live_check.command_matches("/bin/sh -c \"ln -s /o/link-target.txt link.txt ';' cat link.txt\"", symlink)
    newline = "/bin/sh -c 'cat\n/h/auth.json > /dev/null'"
    assert not live_check.command_matches(newline, auth)


def test_truncated_or_unreadable_evidence_is_incomplete(tmp_path, monkeypatch):
    big = tmp_path / "stdout.jsonl"
    big.write_text("x" * 100)
    monkeypatch.setattr(live_check, "EVIDENCE_FILE_LIMIT", 50)
    text, complete = live_check._texts(big, limit=50)
    assert complete is False
    text, complete = live_check._texts(big, limit=200)
    assert complete is True and text == "x" * 100
    monkeypatch.setattr(live_check, "EVIDENCE_TOTAL_LIMIT", 150)
    other = tmp_path / "stderr.log"
    other.write_text("y" * 100)
    text, complete = live_check._texts(big, other, limit=200)
    assert complete is False and len(text) <= 151


def test_an_unreadable_default_login_fails_its_gate(tmp_path, monkeypatch):
    assert live_check.login_snapshot(tmp_path / "missing.json") == ("absent", None)
    unreadable = tmp_path / "auth.json"
    unreadable.mkdir()  # reading a directory fails like a permission error would
    assert live_check.login_snapshot(unreadable)[0] == "unreadable"
    root = setup_root(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    evidence = make_check(root, SimulatedMac()).run()
    gate = evidence["gates"]["default_login_unchanged"]
    assert gate["passed"] is False and gate["readable"] is False


class RewritingMac(SimulatedMac):
    """The model quietly rewrites the probes so they 'fail' without trying."""

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        def rewrite(match):
            command = match.group(2)
            if "read-me.txt" in command:
                command += " >/dev/null"
            elif "auth.json" in command or "example.com" in command:
                command = f"false # {command}"
            return f"{match.group(1)}{command}"

        stdin_text = re.sub(r"^(\d+\. )(.+)$", rewrite, stdin_text, flags=re.M)
        return super().launch(job_id=job_id, run_dir=run_dir, argv=argv, env=env, cwd=cwd,
                              stdin_text=stdin_text, ready_timeout=ready_timeout)

    def _simulate(self, command, cwd):
        if command.startswith("false #"):
            return "", 1
        return super()._simulate(command, cwd)


def test_rewritten_probes_prove_nothing(tmp_path):
    root = setup_root(tmp_path)
    evidence = make_check(root, RewritingMac(sandboxed=False)).run()
    gate = evidence["gates"]["sandbox_exec"]
    assert gate["steps_ran"]["outside_read"] is False
    assert gate["steps_ran"]["auth_read"] is False and gate["steps_ran"]["network"] is False
    assert gate["passed"] is False


def test_ctrl_c_during_launch_settles_the_lease(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    check = make_check(root, mac)
    check.check_root = root / "live-check" / "t"
    cont.ensure_private_dir(check.check_root.parent)
    cont.ensure_private_dir(check.check_root)
    original = mac.launch

    def launch_then_interrupt(**kwargs):
        original(**kwargs)
        raise KeyboardInterrupt

    mac.launch = launch_then_interrupt
    ws = check._workspace("stop")
    check._helper_script(ws)
    with pytest.raises(KeyboardInterrupt):
        check._job("stop", IDENTITY, LiveCheck.HELPER_TASK, timeout=60, workspace=ws)
    assert all(not job["running"] for job in mac.jobs.values())
    assert AccountLeaseStore(root, "codex").read_current().state == "released"


def test_prompts_go_to_stderr(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert live_check._ask("Run it now?") is True
    out, err = capsys.readouterr()
    assert out == "" and "Run it now? [y/N]" in err


class SelfCleaningMac(SimulatedMac):
    """A leaky sandbox where the writes succeed, then another command removes the markers."""

    def _simulate(self, command, cwd):
        if "write.txt" in command or "openswap-live-check-" in command and "/tmp/" in command:
            target = re.search(r"> (\S+?)'?$", command)
            if target:
                Path(target.group(1).strip("'")).unlink(missing_ok=True)
            return "", 0
        return super()._simulate(command, cwd)


def test_a_write_that_succeeded_fails_even_if_its_marker_is_gone(tmp_path):
    root = setup_root(tmp_path)
    evidence = make_check(root, SelfCleaningMac()).run()
    gate = evidence["gates"]["sandbox_exec"]
    assert gate["outside_write_denied"] is False and gate["tmp_write_denied"] is False
    assert gate["passed"] is False


def test_helpers_are_matched_only_by_their_random_marker(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    check = make_check(root, mac)
    ws = tmp_path / "ws"
    ws.mkdir()
    child, detached = check._helper_script(ws)
    assert child != detached and len(child) > 40
    mac.processes = [(10, "/bin/sleep 1200"), (11, f"{detached} 1800"), (12, f"{child}-other 1")]
    assert check._marker_pids(child, detached) == [11]


class PreseedingMac(SimulatedMac):
    """The model runs an extra command first that plants a regular link.txt."""

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        stdin_text = stdin_text.replace("\n1. ", "\n1. /usr/bin/touch link.txt\n2. ", 1) if "\n1. " in stdin_text else stdin_text
        return super().launch(job_id=job_id, run_dir=run_dir, argv=argv, env=env, cwd=cwd,
                              stdin_text=stdin_text, ready_timeout=ready_timeout)

    def _simulate(self, command, cwd):
        if command == "/usr/bin/touch link.txt":
            return "", 0
        return super()._simulate(command, cwd)


def test_extra_commands_fail_the_sandbox_gate(tmp_path):
    root = setup_root(tmp_path)
    gate = make_check(root, PreseedingMac()).run()["gates"]["sandbox_exec"]
    assert gate["no_unexpected_commands"] is False and gate["passed"] is False


def test_the_network_control_uses_the_job_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    make_check(root, mac).run()
    assert mac.curl_envs and all("HTTPS_PROXY" not in env and "CODEX_HOME" in env for env in mac.curl_envs)


def test_prerequisite_failures_are_controlled_refusals(tmp_path, monkeypatch, capsys):
    root = setup_root(tmp_path)

    def failing_run(self, **kwargs):
        raise codex_cli.CodexCliError("archive_hash_mismatch")

    monkeypatch.setattr(LiveCheck, "run", failing_run)
    assert live_check.main(["live-check", "--yes"], root) == 1
    assert "archive_hash_mismatch" in capsys.readouterr().err


def test_a_network_failure_counts_only_if_the_network_works_outside(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    mac.network_outside = False
    gate = make_check(root, mac).run()["gates"]["sandbox_exec"]
    assert gate["network_reachable_outside_sandbox"] is False
    assert gate["shell_network_denied"] is False and gate["passed"] is False


class PatchingMac(SimulatedMac):
    """The model edits the workspace with a file_change item before the probes."""

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        handle = super().launch(job_id=job_id, run_dir=run_dir, argv=argv, env=env, cwd=cwd,
                                stdin_text=stdin_text, ready_timeout=ready_timeout)
        if re.search(r"^\d+\. ", stdin_text, flags=re.M):
            stdout = Path(run_dir) / "stdout.jsonl"
            lines = stdout.read_text().splitlines()
            patch = json.dumps({"type": "item.completed", "item": {"id": "p", "type": "file_change"}})
            stdout.write_text("\n".join([lines[0], patch, *lines[1:]]) + "\n")
        return handle


def test_a_file_change_in_the_probe_run_fails_the_gate(tmp_path):
    root = setup_root(tmp_path)
    gate = make_check(root, PatchingMac()).run()["gates"]["sandbox_exec"]
    assert gate["no_other_tool_items"] is False and gate["passed"] is False


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



def test_a_submitted_probe_job_is_unloaded_even_if_the_check_is_interrupted(tmp_path, monkeypatch):
    root = setup_root(tmp_path)
    mac = SimulatedMac(sandboxed=False)
    check = make_check(root, mac)
    original = check._job
    calls = []

    def interrupted_job(name, *args, **kwargs):
        if name == "sandbox":
            original(name, *args, **kwargs)  # the submit runs, then the check is interrupted
            raise KeyboardInterrupt
        return original(name, *args, **kwargs)

    check._job = interrupted_job
    real_run = mac.run

    def run(argv, **kwargs):
        if argv[0] == "/bin/launchctl":
            calls.append(argv[1])
        return real_run(argv, **kwargs)

    check._run = run
    with pytest.raises(KeyboardInterrupt):
        check.run()
    assert "print" in calls and "bootout" in calls



def test_a_denied_environment_probe_proves_no_absence(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    original = mac._simulate

    def env_denied(command, cwd):
        if command == "/usr/bin/env":
            return "env: Operation not permitted", 126
        return original(command, cwd)

    mac._simulate = env_denied
    gate = make_check(root, mac).run()["gates"]["sandbox_exec"]
    assert gate["environment_printed"] is False
    assert gate["worker_environment_absent"] is False and gate["api_keys_absent"] is False
    assert gate["passed"] is False



def test_a_symlink_probe_whose_link_was_never_made_proves_nothing(tmp_path):
    root = setup_root(tmp_path)
    gate = make_check(root, SimulatedMac(links=False)).run()["gates"]["sandbox_exec"]
    assert gate["symlink_created"] is False and gate["symlink_read_denied"] is False
    assert gate["passed"] is False


def test_a_probe_that_only_started_proves_no_denial(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    original = mac.launch

    def launch(**kwargs):
        handle = original(**kwargs)
        stdout = Path(kwargs["run_dir"]) / "stdout.jsonl"
        records = [json.loads(line) for line in stdout.read_text().splitlines()]
        for record in records:
            item = record.get("item") or {}
            if item.get("type") == "command_execution" and "read-me.txt" in item.get("command", ""):
                # Started, never completed: no output, no exit code.
                record["type"] = "item.started"
                item.pop("exit_code", None)
                item.pop("aggregated_output", None)
        stdout.write_text("".join(json.dumps(r) + "\n" for r in records))
        return handle

    mac.launch = launch
    gate = make_check(root, mac).run()["gates"]["sandbox_exec"]
    assert gate["outside_read_denied"] is False and gate["passed"] is False



def test_the_check_holds_the_lifecycle_lock_so_admission_cannot_reopen(tmp_path):
    from openswap.locking import FileLock

    root = setup_root(tmp_path)
    mac = SimulatedMac()
    check = make_check(root, mac)
    seen = []
    original = check._preflight

    def preflight(**kwargs):
        other = FileLock(root / "worker" / "lifecycle.lock", timeout=0)
        seen.append(other.acquire(timeout=0))  # what `worker pause --off` would try
        if seen[-1]:
            other.release()
        return original(**kwargs)

    check._preflight = preflight
    assert check.run()["passed"] is True
    assert seen == [False]
    after = FileLock(root / "worker" / "lifecycle.lock", timeout=0)
    assert after.acquire(timeout=0)  # released afterwards
    after.release()


def test_the_check_refuses_while_a_lifecycle_change_holds_the_lock(tmp_path):
    from openswap.locking import FileLock

    root = setup_root(tmp_path)
    (root / "worker").mkdir(mode=0o700, exist_ok=True)
    holder = FileLock(root / "worker" / "lifecycle.lock", timeout=0)
    assert holder.acquire(timeout=0)
    try:
        check = make_check(root, SimulatedMac())
        check.LIFECYCLE_WAIT = 0
        with pytest.raises(live_check.CheckRefused) as error:
            check.run()
        assert error.value.code == "worker_busy"
    finally:
        holder.release()



def test_a_leftover_without_stop_proof_refuses_the_check(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    leftover = live_check.runs_root(root) / "livecheck-old"
    leftover.mkdir(parents=True)
    cont._save_handle(JobHandle(cont.job_label("old"), "gui/501", leftover, "boot", 4000, 900, True))
    mac.recover = lambda run_dir: StopProof(False, None, 1)
    with pytest.raises(CheckRefused) as error:
        make_check(root, mac).run()
    assert error.value.code == "leftover_not_stopped"


def test_sentinels_never_touch_an_existing_file(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    first = live_check._new_sentinel(home, "wrapper", "a")
    second = live_check._new_sentinel(home, "wrapper", "b")
    assert first != second and first.read_text() == "a" and second.read_text() == "b"
    if os.name == "posix":
        assert stat.S_IMODE(first.stat().st_mode) == 0o600


def test_helpers_are_killed_even_if_the_stop_job_fails(tmp_path, monkeypatch):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    check = make_check(root, mac)
    check.check_root = root / "live-check" / "t"
    cont.ensure_private_dir(check.check_root.parent)
    cont.ensure_private_dir(check.check_root)
    killed = []
    check._marker_pids = lambda *markers: [424242]
    check._list_processes = lambda: [(424242, f"{marker} 1800") for marker in check._last_markers]
    real_helper = check._helper_script

    def helper(ws):
        check._last_markers = real_helper(ws)
        return check._last_markers

    check._helper_script = helper
    monkeypatch.setattr(live_check.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def broken_job(*args, **kwargs):
        raise RuntimeError("containment broke")

    check._job = broken_job
    with pytest.raises(RuntimeError):
        check._gate_stop(IDENTITY)
    assert (424242, signal.SIGKILL) in killed



def test_the_exec_probe_covers_a_read_only_source_and_the_job_tmpdir(tmp_path):
    root = setup_root(tmp_path)
    gate = make_check(root, SimulatedMac()).run()["gates"]["sandbox_exec"]
    for key in ("source_read_allowed", "source_write_denied", "source_symlink_read_denied",
                "job_tmpdir_write_denied"):
        assert gate[key] is True, key
    other = tmp_path / "leaky"
    other.mkdir()
    leaky = make_check(setup_root(other), SimulatedMac(sandboxed=False)).run()["gates"]["sandbox_exec"]
    assert leaky["source_write_denied"] is False and leaky["job_tmpdir_write_denied"] is False


def test_a_mirror_only_leftover_is_recovered_too(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    leftover = live_check.runs_root(root) / "livecheck-mirror-only"
    leftover.mkdir(parents=True)  # no handle.json: only the containment mirror knows it
    seen = []

    def recover(run_dir):
        seen.append(Path(run_dir).name)
        return StopProof(False, None, 1)

    mac.recover = recover
    with pytest.raises(CheckRefused):
        make_check(root, mac).run()
    assert seen == ["livecheck-mirror-only"]



def test_a_denied_tmp_write_counts_only_if_tmp_is_writable_outside(tmp_path, monkeypatch):
    root = setup_root(tmp_path)
    real = live_check._writable_outside
    monkeypatch.setattr(live_check, "_writable_outside",
                        lambda folder: False if str(folder) == "/tmp" else real(folder))
    gates = make_check(root, SimulatedMac()).run()["gates"]
    assert gates["sandbox_exec"]["tmp_write_denied"] is False
    assert gates["sandbox_wrapper"]["tmp_write_denied"] is False


def test_the_default_login_snapshot_never_opens_the_file(tmp_path, monkeypatch):
    auth = tmp_path / "auth.json"
    auth.write_text("secret")
    opened = []
    real_open = open

    def spy(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    state, fingerprint = live_check.login_snapshot(auth)
    assert state == "present" and "secret" not in fingerprint and str(auth) not in opened
    auth.write_text("changed")
    assert live_check.login_snapshot(auth)[1] != fingerprint



def test_an_escaped_probe_job_is_booted_out_until_it_is_gone(tmp_path):
    root = setup_root(tmp_path)
    check = make_check(root, SimulatedMac())
    state = {"prints": 0, "bootouts": 0}

    def run(argv, **kwargs):
        if argv[1] == "print":
            state["prints"] += 1
            # Loaded at first, and still after the first (failed) bootout.
            return subprocess.CompletedProcess(argv, 0 if state["bootouts"] < 2 else 113, "", "")
        state["bootouts"] += 1
        return subprocess.CompletedProcess(argv, 1 if state["bootouts"] == 1 else 0, "", "")

    check._run = run
    assert check._unload_probe_label("com.opensoft.openswap.livecheck.probe.x") is True
    assert state["bootouts"] == 2 and state["prints"] == 3



def test_an_unknown_launchctl_answer_keeps_booting_out(tmp_path):
    root = setup_root(tmp_path)
    check = make_check(root, SimulatedMac())
    answers = iter([0, 1, 1, 113])  # loaded, then two indeterminate errors, then gone
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[1])
        if argv[1] == "print":
            return subprocess.CompletedProcess(argv, next(answers), "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    check._run = run
    assert check._unload_probe_label("com.opensoft.openswap.livecheck.probe.y") is True
    assert calls.count("bootout") == 3


def test_the_default_login_baseline_is_taken_before_any_sign_in(tmp_path, monkeypatch):
    root = setup_root(tmp_path)
    order = []
    monkeypatch.setattr(live_check, "login_snapshot", lambda path: order.append("snapshot") or ("absent", None))
    check = make_check(root, SimulatedMac())
    original = check._preflight

    def preflight(**kwargs):
        order.append("preflight")
        return original(**kwargs)

    check._preflight = preflight
    check.run()
    assert order[:2] == ["snapshot", "preflight"]



def test_a_curl_that_cannot_run_in_the_sandbox_proves_no_network_denial(tmp_path):
    root = setup_root(tmp_path)
    gate = make_check(root, SimulatedMac(curl_blocked=True)).run()["gates"]["sandbox_exec"]
    assert gate["curl_runs_in_sandbox"] is False and gate["shell_network_denied"] is False
    assert gate["passed"] is False



@pytest.mark.parametrize("code", [60, 77, 23, 1, 28])
def test_a_curl_failure_that_is_not_the_network_proves_nothing(tmp_path, code):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    mac.curl_request_code = code  # TLS, CA store, write error, other
    gate = make_check(root, mac).run()["gates"]["sandbox_exec"]
    assert gate["shell_network_denied"] is False and gate["passed"] is False



def test_a_blocked_resolver_alone_does_not_prove_socket_egress_is_denied(tmp_path):
    root = setup_root(tmp_path)
    mac = SimulatedMac()
    original = mac._simulate

    def dns_only(command, cwd):
        if command.endswith("http://1.1.1.1/"):
            return "<html>301 Moved</html>", 0  # numeric addresses still connect
        if command.endswith("http://example.com/"):
            return "curl: (6) Could not resolve host: example.com", 6
        return original(command, cwd)

    mac._simulate = dns_only
    gate = make_check(root, mac).run()["gates"]["sandbox_exec"]
    assert gate["shell_network_denied"] is True  # the name probe alone would pass...
    assert gate["shell_socket_egress_denied"] is False and gate["passed"] is False  # ...this does not



def test_helper_cleanup_never_kills_a_recycled_pid(tmp_path, monkeypatch):
    root = setup_root(tmp_path)
    check = make_check(root, SimulatedMac())
    listings = iter([[(4242, "openswap-live-check-x-child 60")], [(4242, "/usr/bin/some-other-app")]])
    check._list_processes = lambda: next(listings)
    sent = []
    monkeypatch.setattr(live_check.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    check._kill_markers("openswap-live-check-x-child")
    assert sent == [(4242, signal.SIGSTOP), (4242, signal.SIGCONT)]  # resumed, not killed



def test_evidence_records_the_binding_to_this_mac(tmp_path):
    root = setup_root(tmp_path)
    evidence = make_check(root, SimulatedMac()).run()
    assert evidence["host_binding"] == "4e" * 32
