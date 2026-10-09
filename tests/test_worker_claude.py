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
    load_worker_settings,
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
def this_mac(monkeypatch):
    # The evidence binding to this Mac (hardware UUID + install), fixed in tests.
    monkeypatch.setattr(live, "host_binding", lambda root, **kw: "4e" * 32)


@pytest.fixture(autouse=True)
def private_copies(monkeypatch, tmp_path):
    # Pinned Claude copies go to a test folder, never the real Application Support.
    monkeypatch.setattr(claude_cli, "pinned_copies_dir", lambda: tmp_path / "pinned-copies")


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
        # Claude Code's file store for the sign-in (a placeholder: jobs reach no Keychain).
        (profile / ".credentials.json").write_text("{}")
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
    assert pin.version == "2.1.285 (Claude Code)"
    # Jobs run a private, read-only copy, identical to the installed binary.
    assert pin.binary != binary.resolve() and pin.binary.read_bytes() == binary.read_bytes()
    assert pin.binary.parent == claude_cli.pinned_copies_dir()
    assert claude_cli.verify(root) == pin
    if os.name == "posix":
        assert (claude_cli.pin_path(root).stat().st_mode & 0o777) == 0o600
        assert (pin.binary.stat().st_mode & 0o777) == 0o500
    binary.write_text("#!/bin/sh\necho '2.1.286 (Claude Code)'\n")  # an update of the installed binary
    assert claude_cli.verify(root) == pin  # never picked up until re-pinned
    pin.binary.chmod(0o700)
    pin.binary.write_text("#!/bin/sh\necho tampered\n")  # the copy itself changed
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
        write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, SHA, "now", (IDENTITY,), "4e" * 32), "claude")
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


def test_argv_follows_the_profiles_own_settings_and_denies_every_prompt(tmp_path):
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
    assert argv[3] == "/fake/claude" and "-p" in argv
    # The mode and the rules come from the profile's settings.json alone (no repo settings)...
    assert argv[argv.index("--setting-sources") + 1] == "user"
    # ...and nobody is at the Mac: anything that would ask is denied.
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv and "--disable-slash-commands" in argv
    assert argv[argv.index("--add-dir") + 1] == str(source.resolve())
    # No tool list or mode of OpenSwap's own (the default per-Mac limit is "follow")...
    for forbidden in ("--restricted", "--tools", "--allowedTools", "--disallowedTools", "--permission-mode",
                      "--model", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
                      "--mcp-config"):
        assert forbidden not in argv
    # ...only deny rules keeping the file tools off the credentials file, in every mode.
    assert json.loads(argv[argv.index("--settings") + 1]) == {"permissions": {"deny": _credential_rules(root)}}
    env = launch["env"]
    assert env["CLAUDE_CONFIG_DIR"] == str(claude_exec.profile_for(root, IDENTITY))
    assert "ANTHROPIC_API_KEY" not in env and env["DISABLE_AUTOUPDATER"] == "1"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"  # no notes for a later task to load
    sb = (Path(launch["run_dir"]) / "claude.sb").read_text()
    assert "(deny file-write*)" in sb and "(allow default)" in sb
    assert str(workspace(root).output_root.resolve()) in sb
    assert f'(subpath "{root.resolve()}")' in sb  # the backup root is hidden...
    profile = claude_exec.profile_for(root, IDENTITY).resolve()
    assert str(profile) in sb  # ...except this profile
    # Whatever the mode: no Keychain, and no write to what configures the next session.
    assert '(deny process-exec (literal "/usr/bin/security"))' in sb
    assert '(global-name "com.apple.SecurityServer")' in sb and '(deny mach-lookup' in sb
    assert f'(literal "{profile / "settings.json"}")' in sb and f'(subpath "{profile / "skills"}")' in sb
    assert "Find it" in launch["stdin"]


def _credential_rules(root):
    profile = claude_exec.profile_for(root, IDENTITY)
    paths = sorted({str(profile / ".credentials.json"), str(profile.resolve() / ".credentials.json")})
    return [rule for path in paths for rule in (f"Read(/{path})", f"Edit(/{path})")]


@pytest.mark.parametrize("override, expected", [
    ("no-shell", ["--disallowedTools", "Bash,PowerShell,Monitor,REPL,BashOutput,KillShell",
                  "--settings", {"disableAllHooks": True}]),
    ("read-only", ["--tools", "Read,Grep,Glob,WebSearch,WebFetch", "--settings", {"disableAllHooks": True}]),
    ("follow", ["--settings", {}]),
    ("unreadable", ["--tools", "Read,Grep,Glob,WebSearch,WebFetch", "--settings", {"disableAllHooks": True}]),
])
def test_the_per_mac_limit_narrows_the_tools(tmp_path, override, expected):
    from openswap.settings import write_permission_override

    root = setup_root(tmp_path)
    if override == "unreadable":
        # A limit that cannot be read is the strictest one, never none.
        raw = json.loads((root / "settings.json").read_text()) if (root / "settings.json").exists() else {}
        raw.setdefault("worker", {})["permissionOverride"] = "everything"
        (root / "settings.json").write_text(json.dumps(raw))
    else:
        write_permission_override(root, override)
    launcher = FakeLaunch(SUCCESS)
    adapter = make_adapter(root, launcher)  # reads the limit from settings at launch
    run = adapter.start(job_record(), workspace(root), worker_epoch=1)
    argv = launcher.launches[0]["argv"]
    tail = argv[argv.index("--no-session-persistence") + 1:]
    settings = {**expected[-1], "permissions": {"deny": _credential_rules(root)}}
    assert tail[:-1] == expected[:-1] and json.loads(tail[-1]) == settings
    drain(adapter, run)
    summary = json.loads((Path(root) / "worker" / "runs" / ("a" * 32) / "summary.json").read_text())
    assert summary["permissions"]["override"] == ("read-only" if override == "unreadable" else override)


def test_the_profiles_permission_settings_are_validated_before_launch(tmp_path):
    root = setup_root(tmp_path)
    profile = claude_exec.profile_for(root, IDENTITY)
    for bad in ("not json", json.dumps({"permissions": {"defaultMode": "yolo"}}),
                json.dumps({"permissions": {"deny": "Bash"}}), json.dumps([1])):
        (profile / "settings.json").write_text(bad)
        launcher = FakeLaunch(SUCCESS)
        with pytest.raises(ProviderLaunchRefused) as error:
            make_adapter(root, launcher).start(job_record(), workspace(root), worker_epoch=1)
        # Claude Code would silently drop such a file (and the owner's deny rules with it).
        assert error.value.diagnostic_code == "provider_unavailable" and launcher.launches == []
    (profile / "settings.json").unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (profile / "settings.json").symlink_to(target)
    with pytest.raises(ProviderLaunchRefused):
        make_adapter(root, FakeLaunch(SUCCESS)).start(job_record(), workspace(root), worker_epoch=1)
    (profile / "settings.json").unlink()
    (profile / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": "bypassPermissions",
                                                                       "deny": ["Bash(rm:*)"]}}))
    launcher = FakeLaunch([{**INIT, "permissionMode": "bypassPermissions"}, *SUCCESS[1:]])
    adapter = make_adapter(root, launcher)
    drain(adapter, adapter.start(job_record(), workspace(root), worker_epoch=1))
    summary = json.loads((Path(root) / "worker" / "runs" / ("a" * 32) / "summary.json").read_text())
    assert summary["permission_mode"] == "bypassPermissions"
    assert summary["permissions"] == {"mode": "bypassPermissions", "allow_rules": 0, "deny_rules": 1, "ask_rules": 0,
                                      "override": "follow", "for_check": False, "profile_settings": True}


def test_a_sign_in_kept_only_in_the_keychain_is_refused_unlaunched(tmp_path):
    root = setup_root(tmp_path)
    (claude_exec.profile_for(root, IDENTITY) / ".credentials.json").unlink()
    launcher = FakeLaunch(SUCCESS)
    with pytest.raises(ProviderLaunchRefused) as error:
        make_adapter(root, launcher).start(job_record(), workspace(root), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_auth_unavailable" and launcher.launches == []


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


def _job_profile(tmp_path, *, exec_deny=True):
    root = tmp_path / "backup"
    # A dot in the profile's name, as in an email slug: the auto-memory rule is a regular expression.
    out, profile, home = tmp_path / "out", root / "sessions" / "4-a_b.com", tmp_path / "home"
    for path in (out, profile, home / "Library" / "Keychains", profile / "skills", profile / "rules",
                 profile / "projects" / "-repo" / "memory", profile / "projects" / "-repo" / "todo"):
        path.mkdir(parents=True, exist_ok=True)
    (profile / "settings.json").write_text("{}\n")
    (profile / "CLAUDE.md").write_text("\n")
    (profile / ".credentials.json").write_text("placeholder\n")
    (home / "Library" / "Keychains" / "login.keychain-db").write_text("k")
    text = claude_exec.seatbelt_profile(output_root=out.resolve(), profile=profile.resolve(),
                                        run_tmp=(tmp_path / "t").resolve(), home=home.resolve(),
                                        backup_root=root.resolve(), user_dirs=[])
    if not exec_deny:
        text = "\n".join(line for line in text.splitlines() if not line.startswith("(deny process-exec"))
    sb = tmp_path / ("p.sb" if exec_deny else "services.sb")
    sb.write_text(text)
    return sb, profile, home


def _sandboxed(sb, script):
    result = subprocess.run(["/usr/bin/sandbox-exec", "-f", str(sb), "/bin/sh", "-c", script],
                            capture_output=True, text=True, timeout=60)
    return dict(line.split() for line in result.stdout.splitlines())


@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"), reason="Seatbelt")
def test_the_seatbelt_profile_takes_the_keychain_and_the_profiles_configuration_away_for_real(tmp_path):
    sb, profile, home = _job_profile(tmp_path)
    codes = _sandboxed(sb, "; ".join([
        "/usr/bin/security find-generic-password -s openswap-test-missing >/dev/null 2>&1; echo security $?",
        f"head -c 1 {home}/Library/Keychains/login.keychain-db >/dev/null 2>&1; echo keychain_file $?",
        f"printf x >> {profile}/settings.json 2>/dev/null; echo settings $?",
        f"printf x > {profile}/new.json 2>/dev/null && mv {profile}/new.json {profile}/settings.json "
        f"2>/dev/null; echo settings_replaced $?",
        f"printf x >> {profile}/CLAUDE.md 2>/dev/null; echo memory $?",
        f"printf x > {profile}/skills/s.md 2>/dev/null; echo skill $?",
        f"printf x > {profile}/rules/r.md 2>/dev/null; echo rule $?",
        f"printf x > {profile}/projects/-repo/memory/MEMORY.md 2>/dev/null; echo auto_memory $?",
        f"mkdir {profile}/projects/-other 2>/dev/null; mkdir {profile}/projects/-other/memory 2>/dev/null; "
        f"echo new_auto_memory $?",
        f"printf x > {profile}/projects/-repo/todo/t.json; echo project_state $?",
        f"printf x > {profile}/state.json; echo state $?",
        f"cat {profile}/.credentials.json >/dev/null; echo own_sign_in $?",
    ]))
    assert codes["rule"] != "0" and codes["auto_memory"] != "0" and codes["new_auto_memory"] != "0"
    assert codes["project_state"] == "0"
    # The `security` tool cannot start at all (126), so Claude Code's Keychain
    # write fails fast and it keeps the sign-in in its credentials file.
    assert codes["security"] == "126"
    assert codes["keychain_file"] == "1"
    assert codes["settings"] != "0" and codes["settings_replaced"] != "0" and codes["memory"] != "0"
    assert codes["skill"] != "0"
    assert (profile / "settings.json").read_text() == "{}\n" and (profile / "CLAUDE.md").read_text() == "\n"
    # Claude Code's own state, and (honestly) the account's own sign-in, stay reachable.
    assert codes["state"] == "0" and codes["own_sign_in"] == "0"


@pytest.mark.skipif(sys.platform != "darwin" or not os.path.exists("/usr/bin/sandbox-exec"), reason="Seatbelt")
def test_nothing_the_session_starts_can_leave_the_sandbox_for_real(tmp_path):
    import socket
    import tempfile

    sb, _profile, _home = _job_profile(tmp_path)
    # A local daemon's socket (Docker's, say) would act outside the sandbox.
    short = Path(tempfile.mkdtemp(prefix="os-", dir="/tmp"))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(short / "s"))
        server.listen(4)
        connect = f"/usr/bin/nc -U -w 1 {short / 's'} </dev/null >/dev/null 2>&1"
        outside = subprocess.run(["/bin/sh", "-c", connect], timeout=30).returncode
        codes = _sandboxed(sb, "; ".join([
            f"{connect}; echo unix_socket $?",
            f"/bin/launchctl print gui/{os.getuid()} >/dev/null 2>&1; echo launchctl $?",
            "/usr/bin/curl -sS -m 10 -o /dev/null https://example.com 2>/dev/null; echo dns_and_tls $?",
        ]))
    finally:
        server.close()
        import shutil

        shutil.rmtree(short, ignore_errors=True)
    assert outside == 0 and codes["unix_socket"] != "0"
    assert codes["launchctl"] == "126"
    # Name resolution (mDNSResponder's socket) and TLS still work, when this Mac is online at all.
    online = subprocess.run(["/usr/bin/curl", "-sS", "-m", "10", "-o", "/dev/null", "https://example.com"],
                            capture_output=True, timeout=30).returncode == 0
    assert codes["dns_and_tls"] == "0" or not online
    sb_text = sb.read_text()
    assert "(deny lsopen)" in sb_text and "(deny appleevent-send)" in sb_text and "(deny job-creation)" in sb_text


@pytest.mark.skipif(sys.platform != "darwin" or not (os.environ.get("OPENSWAP_KEYCHAIN_SANDBOX_TESTS") == "1"
                                                     or os.environ.get("GITHUB_ACTIONS") == "true"),
                    reason="creates a throwaway keychain file; set OPENSWAP_KEYCHAIN_SANDBOX_TESTS=1 on a Mac")
def test_no_keychain_service_is_reachable_from_the_job_for_real(tmp_path):
    # A throwaway keychain file (never the login keychain, never added as default),
    # so only the Keychain-service rule can make the lookup fail.
    keychain = tmp_path / "probe.keychain-db"
    subprocess.run(["/usr/bin/security", "create-keychain", "-p", "probe", str(keychain)], check=True, timeout=60)
    try:
        subprocess.run(["/usr/bin/security", "unlock-keychain", "-p", "probe", str(keychain)], check=True, timeout=60)
        subprocess.run(["/usr/bin/security", "add-generic-password", "-a", "openswap", "-s", "openswap-probe",
                        "-w", "not-a-secret", str(keychain)], check=True, timeout=60)
        query = f"/usr/bin/security find-generic-password -s openswap-probe {keychain} >/dev/null 2>&1"
        outside = subprocess.run(["/bin/sh", "-c", query], timeout=60).returncode
        sb, _profile, _home = _job_profile(tmp_path, exec_deny=False)
        inside = _sandboxed(sb, f"{query}; echo found $?")
        assert outside == 0 and inside["found"] != "0"
    finally:
        subprocess.run(["/usr/bin/security", "delete-keychain", str(keychain)], timeout=60)


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
    write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, SHA, "now", (), "4e" * 32), "claude")
    assert live.execution_mode(root, "claude") == "live" and live.execution_mode(root, "codex") == "disabled"
    assert load_live_execution(root, "claude").binary_sha256 == SHA
    live.disable_live(root, "claude")
    assert live.execution_mode(root, "claude") == "disabled"


def _claude_evidence(**overrides):
    data = {"kind": live.EVIDENCE_KIND, "schema": live.EVIDENCE_SCHEMA, "passed": True, "provider": "claude",
            "cli": {"version": "2.1.285 (Claude Code)", "binary_sha256": SHA},
            "gates": {name: {"passed": True} for name in live.REQUIRED_GATES},
            "account": {"identity": IDENTITY, "slot": "4"}, "host_binding": "4e" * 32}
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


_GIT_ID = {"GIT_AUTHOR_NAME": "Task", "GIT_AUTHOR_EMAIL": "task@localhost",
           "GIT_COMMITTER_NAME": "Task", "GIT_COMMITTER_EMAIL": "task@localhost"}


def run_for_real(command, cwd):
    """Run a work-folder self-test command for real (git in a temp repo)."""
    import os as _os

    result = subprocess.run(command, shell=True, cwd=cwd, env={**_os.environ, **_GIT_ID},
                            capture_output=True, text=True)
    return result.stdout + result.stderr, result.returncode


class SimulatedClaudeMac(FakeLaunch):
    """Claude Code as the live check drives it, with a sandbox that holds."""

    def __init__(self, home):
        super().__init__()
        self.home = home
        self.jobs = {}

    TOOLS = ["Task", "Bash", "Edit", "Read", "Write", "WebFetch", "WebSearch"]

    def _settings(self, argv, env):
        """The mode, tools and allow rules Claude Code would start with for ``argv``."""
        overlay = json.loads(argv[argv.index("--settings") + 1]) if "--settings" in argv else {}
        profile = {}
        if argv[argv.index("--setting-sources") + 1] == "user":
            path = Path(env["CLAUDE_CONFIG_DIR"]) / "settings.json"
            profile = json.loads(path.read_text()) if path.exists() else {}
        mode = (overlay.get("permissions", {}).get("defaultMode")
                or profile.get("permissions", {}).get("defaultMode") or "auto")
        allow = overlay.get("permissions", {}).get("allow", []) + profile.get("permissions", {}).get("allow", [])
        self._deny = overlay.get("permissions", {}).get("deny", []) + profile.get("permissions", {}).get("deny", [])
        tools = list(self.TOOLS)
        if "--tools" in argv:
            tools = argv[argv.index("--tools") + 1].split(",")
        if "--disallowedTools" in argv:
            tools = [t for t in tools if t not in argv[argv.index("--disallowedTools") + 1].split(",")]
        return mode, tools, allow

    def _attempt(self, tool, target, cwd, mode, tools, allow):
        """One tool call: Claude Code's permission check, then a Seatbelt that holds (writes and reads
        only inside the job's folder)."""
        inside = target.startswith(str(cwd) + "/") or (tool == "Bash" and str(cwd) in target)
        if tool not in tools:
            return False, f"No such tool: {tool}"
        if tool == "Read" and f"Read(/{target})" in getattr(self, "_deny", []):
            return False, "Permission to read this file has been denied."  # a deny rule, in any mode
        if tool == "Read":
            return (True, Path(target).read_text()) if inside else (False, "Permission denied")
        permitted = mode == "bypassPermissions" or tool in allow or (
            tool == "Write" and mode == "acceptEdits" and inside)
        if not permitted:
            return False, f"Claude requested permissions to use {tool}, but you haven't granted it yet."
        if tool == "Write":
            if not inside:
                return False, "EPERM: operation not permitted"
            Path(target).write_text("ok")
            return True, "written"
        if not inside:
            return False, "Operation not permitted"  # the sandbox refuses (git update-ref, outside writes)
        output, code = run_for_real(target, cwd)
        return code == 0, output

    def launch(self, *, job_id, run_dir, argv, env, cwd, stdin_text, ready_timeout=15.0):
        mode, tools, allow = self._settings(argv, env)
        steps = [(tool or "Read", named or path) for tool, named, path
                 in re.findall(r"^\d+\. (?:(Bash|Write) tool: (.+)|(/.+))$", stdin_text, flags=re.M)]
        lines = [{**INIT, "tools": tools, "permissionMode": mode}]
        running = "thoroughly" in stdin_text
        if steps or "DONE" in stdin_text:
            # The self-tests: each numbered step with the tool it names (a bare path is a Read).
            for index, (tool, target) in enumerate(steps):
                ok, content = self._attempt(tool, target, Path(cwd), mode, tools, allow)
                tool_input = {"command": target} if tool == "Bash" else {"file_path": target}
                lines.append({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "id": f"t{index}", "name": tool, "input": tool_input}]}})
                lines.append({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{index}", "is_error": not ok, "content": content}]}})
            lines.append({"type": "result", "subtype": "success", "is_error": False, "result": "DONE",
                          "usage": {"output_tokens": 3}})
        elif not running:
            lines += SUCCESS[1:]
        self.script = lines
        self.exit = None if running else 0
        handle = super().launch(job_id=job_id, run_dir=run_dir, argv=argv, env=env, cwd=cwd, stdin_text=stdin_text)
        self.jobs[handle.label] = running
        return handle

    # What a shell command under the job's Seatbelt profile gets (sign_in_isolation and the wrapper):
    # the folder, the profile's own state and its credentials file; no Keychain, no profile settings.
    ALLOWED_PROBES = {"inside_read", "inside_write", "own_sign_in", "state_write"}

    def run(self, argv, **kwargs):
        if argv[0] == "/usr/bin/sandbox-exec":
            script = argv[-1]
            out = []
            for name in re.findall(r'echo "R (\w+) \$\?"', script):
                if name == "inside_write":
                    target = re.search(r"printf ok > '([^']+)'", script).group(1)
                    Path(target).write_text("ok")
                if name == "state_write":
                    target = re.search(r"printf x > (\S+) 2>/dev/null; echo \"R state_write", script).group(1)
                    Path(target.strip("'")).write_text("x")
                out.append(f"R {name} {0 if name in self.ALLOWED_PROBES else 126 if name == 'security_tool' else 1}")
            return subprocess.CompletedProcess(argv, 0, "\n".join(out) + "\n", "")
        if argv[0] == "/usr/bin/security":
            items = self.__dict__.setdefault("keychain", {"Claude Code-credentials"})
            service = argv[argv.index("-s") + 1]
            if argv[1] == "add-generic-password":
                items.add(service)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if argv[1] == "delete-generic-password":
                items.discard(service)
                return subprocess.CompletedProcess(argv, 0, "", "")
            if service not in items:
                return subprocess.CompletedProcess(argv, 44, "", "")
            return subprocess.CompletedProcess(argv, 0, '"mdat"<timedate>=0x1 "20261001"\n', "")
        if argv[0] == "/bin/ps":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "/bin/launchctl":
            # Nothing a sandboxed probe submitted is loaded.
            return subprocess.CompletedProcess(argv, 113, "", "Could not find service")
        if argv[0] == "/usr/bin/pkill":
            self.__dict__.setdefault("killed", []).append(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


class Child:
    def __init__(self, check, payload):
        live_check.child_acquire_lease(check.root, payload)  # as the real child does
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



def _simulated_check(tmp_path, mac_class=None):
    from openswap.worker.live_check_claude import ClaudeLiveCheck

    root = setup_root(tmp_path)
    configure_worker_local_policy(root, pinned_account_ref=IDENTITY,
                                  workspaces=(WorkerWorkspace("research", (root / "research").resolve()),))
    home = tmp_path / "home"
    home.mkdir()
    mac = (mac_class or SimulatedClaudeMac)(home)
    holder = {}
    check = ClaudeLiveCheck(root, out=lambda *a: None, containment=mac, verify=lambda **kw: pinned(), run=mac.run,
                            spawn_child=lambda payload: Child(holder["check"], payload), sleep=lambda s: None,
                            home=home, live_sessions=lambda p: False)
    holder["check"] = check
    return root, mac, check


def test_the_claude_permission_gates_record_what_they_measured(tmp_path):
    root, mac, check = _simulated_check(tmp_path)
    profile = claude_exec.profile_for(root, IDENTITY)
    (profile / "settings.json").write_text(json.dumps({"permissions": {"defaultMode": "dontAsk"}}))
    evidence = check.run()
    permissions = evidence["gates"]["permissions"]
    assert permissions["passed"] is True and permissions["account_mode"] == "dontAsk"
    assert permissions["headless_shell_denied"] and permissions["headless_write_denied"]
    assert permissions["accept_edits_write_allowed"] and permissions["no_shell_leaves_no_shell_tool"]
    isolation = evidence["gates"]["sign_in_isolation"]
    assert isolation["passed"] is True and isolation["security_tool_denied"] and isolation["keychain_services_denied"]
    # Recorded honestly, never required: a shell command can read the account's own sign-in.
    assert isolation["own_sign_in_readable_by_shell"] is True and isolation["shell_allowed_on_this_mac"] is True
    assert mac.keychain == {"Claude Code-credentials"}  # the throwaway item is gone
    # The work folder ran with the shell in bypassPermissions: only the write scope refused.
    worktree = evidence["gates"]["worktree"]
    assert worktree["passed"] is True and worktree["owner_branch_unchanged"] is True


class LeakyKeychainMac(SimulatedClaudeMac):
    ALLOWED_PROBES = SimulatedClaudeMac.ALLOWED_PROBES | {"security_tool", "keychain_services", "settings_write"}


class AppsEscapeMac(SimulatedClaudeMac):
    ALLOWED_PROBES = SimulatedClaudeMac.ALLOWED_PROBES | {"open_app"}


class IgnoresTheModeMac(SimulatedClaudeMac):
    def _attempt(self, tool, target, cwd, mode, tools, allow):
        return super()._attempt(tool, target, cwd, "bypassPermissions", tools, allow)


class IgnoresDenyRulesMac(SimulatedClaudeMac):
    def _attempt(self, tool, target, cwd, mode, tools, allow):
        self._deny = []
        return super()._attempt(tool, target, cwd, mode, tools, allow)


@pytest.mark.parametrize("mac_class, gate, key", [
    (IgnoresDenyRulesMac, "permissions", "credential_rule_holds"),
    (AppsEscapeMac, "sandbox_wrapper", "app_launch_denied"),
    (LeakyKeychainMac, "sign_in_isolation", "keychain_services_denied"),
    (LeakyKeychainMac, "sign_in_isolation", "profile_settings_write_denied"),
    (IgnoresTheModeMac, "permissions", "headless_shell_denied"),
])
def test_a_mac_that_does_not_hold_fails_the_new_gates(tmp_path, mac_class, gate, key):
    _root, _mac, check = _simulated_check(tmp_path, mac_class)
    evidence = check.run()
    assert evidence["gates"][gate]["passed"] is False and evidence["gates"][gate][key] is False
    assert evidence["passed"] is False


def test_a_binary_inside_the_hidden_claude_folder_is_never_pinned(tmp_path, monkeypatch):
    home = tmp_path / "home"
    local = home / ".claude" / "local"
    local.mkdir(parents=True)
    binary = local / "claude"
    binary.write_text("#!/bin/sh\necho '2.1.285 (Claude Code)'\n")
    binary.chmod(0o755)
    monkeypatch.setattr(claude_cli, "_hidden_from_jobs",
                        lambda path, backup_root=None: Path(os.path.realpath(path)).is_relative_to(
                            os.path.realpath(home / ".claude")))
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
    write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, SHA, "now", (), "4e" * 32), "claude")
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
    write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, "00" * 32, "now", (), "4e" * 32), "claude")
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
        # Under the Claude lease: no job can launch on the profile meanwhile.
        assert AccountLeaseStore(root, "claude").read_current().state == "active"
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


def test_claude_never_grants_a_hidden_folder_in_another_case(tmp_path):
    from tests.test_pathid import case_insensitive

    if not case_insensitive(tmp_path):
        pytest.skip("the temp filesystem is case-sensitive")
    root = setup_root(tmp_path)
    home = root.parent / "home"
    for name in (".claude", ".codex"):
        (home / name).mkdir(parents=True, exist_ok=True)
    adapter = make_adapter(root, FakeLaunch(SUCCESS), home=home)
    assert adapter._grant_allowed(home / ".CLAUDE") is False
    assert adapter._grant_allowed(home / ".Codex" / "sessions") is False
    assert adapter._grant_allowed(root.parent / root.name.upper() / "sessions") is False
    assert adapter._grant_allowed(root.parent / "HOME") is False



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


def _native_login(root, email=EMAIL, code=0, file_store=True):
    """A stand-in for `claude auth login`: signs the profile in CLAUDE_CONFIG_DIR in."""
    calls = []

    def run(argv, env, check=False, **kwargs):
        # Claude Code runs with the Keychain out of reach (a Seatbelt wrapper),
        # so it keeps the sign-in in the profile's credentials file.
        assert argv[:2] == ["/usr/bin/sandbox-exec", "-f"]
        sandbox = Path(argv[2]).read_text()
        assert '(deny process-exec (literal "/usr/bin/security"))' in sandbox
        assert '(global-name "com.apple.SecurityServer")' in sandbox
        argv = argv[3:]
        calls.append((list(argv), dict(env), AccountLeaseStore(root, "claude").read_current()))
        profile = Path(env["CLAUDE_CONFIG_DIR"])
        if argv[1:3] == ["auth", "login"] and email is not None:
            (profile / ".claude.json").write_text(json.dumps(
                {"oauthAccount": {"emailAddress": email, "organizationUuid": ORG}}))
            if file_store:
                (profile / ".credentials.json").write_text("{}")  # Claude Code's own write (a placeholder)
        elif argv[1:3] == ["auth", "logout"]:
            (profile / ".claude.json").unlink(missing_ok=True)
            (profile / ".credentials.json").unlink(missing_ok=True)
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
    assert result == {"slot": "4", "account_ref": IDENTITY, "profile_ready": True, "signed_in_now": True,
                      "permissions": {"mode": None, "allow_rules": 0, "deny_rules": 0, "ask_rules": 0}}
    argv, env, lease = run.calls[0]
    profile = claude_exec.profile_for(root, IDENTITY)
    assert argv == ["/fake/claude", "auth", "login", "--claudeai", "--email", EMAIL]
    assert env["CLAUDE_CONFIG_DIR"] == str(profile) and "ANTHROPIC_API_KEY" not in env
    assert lease is not None and lease.state == "active" and lease.account_identity == IDENTITY
    assert AccountLeaseStore(root, "claude").read_current().state == "released"
    assert not (profile / "settings.json").exists()  # no settings unless the owner asks


def test_prepare_refuses_a_sign_in_kept_only_in_the_keychain(tmp_path):
    # Jobs cannot reach the Keychain: a sign-in Claude Code kept there is not usable.
    root = setup_root(tmp_path, prepared=False)
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.claude_prepare(root, "claude:4", run=_native_login(root, file_store=False), verify=lambda: pinned())
    assert error.value.code == "claude_sign_in_not_in_profile"
    assert AccountLeaseStore(root, "claude").read_current().state == "released"


def test_prepare_signs_in_again_when_the_sign_in_is_only_in_the_keychain(tmp_path):
    root = setup_root(tmp_path, prepared=True)
    profile = claude_exec.profile_for(root, IDENTITY)
    (profile / ".credentials.json").unlink()  # an older prepare: the sign-in is in the Keychain
    run = _native_login(root)
    result = live_cli.claude_prepare(root, "claude:4", run=run, verify=lambda: pinned())
    assert result["signed_in_now"] is True and [c[0][1:3] for c in run.calls] == [["auth", "login"]]
    assert claude_exec.credentials_in_file(profile)


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




def test_server_managed_policy_cached_in_the_profile_refuses_the_launch(tmp_path):
    root = setup_root(tmp_path)
    profile = claude_exec.profile_for(root, IDENTITY)
    assert str(profile / "remote-settings.json") not in claude_exec.managed_claude_config(profile, user="nobody")
    (profile / "remote-settings.json").write_text("{}")  # fetched, but no policy
    assert str(profile / "remote-settings.json") not in claude_exec.managed_claude_config(profile, user="nobody")
    (profile / "remote-settings.json").write_text(json.dumps({"hooks": {"PreToolUse": []}}))
    assert str(profile / "remote-settings.json") in claude_exec.managed_claude_config(profile, user="nobody")
    launcher = FakeLaunch(SUCCESS)
    adapter = ClaudeCodeAdapter(
        root, containment=launcher, verify=lambda **kw: pinned(), mode=lambda: "live", sleep=lambda s: None,
        live_sessions=lambda p: False, home=root.parent / "home",
    )
    write_live_execution(root, LiveExecutionSettings(True, "ab" * 32, SHA, "now", (IDENTITY,), "4e" * 32), "claude")
    with pytest.raises(ProviderLaunchRefused) as error:
        adapter.start(job_record(), workspace(root), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_unavailable" and launcher.launches == []


def test_a_managed_settings_fragment_directory_counts(tmp_path, monkeypatch):
    fragments = tmp_path / "managed-settings.d"
    fragments.mkdir()
    monkeypatch.setattr(claude_exec, "MANAGED_SETTINGS_DIR", str(fragments))
    monkeypatch.setattr(claude_exec, "MANAGED_CLAUDE_PATHS", ())
    assert claude_exec.managed_claude_config(tmp_path, user="nobody") == []
    (fragments / "10-policy.json").write_text("{}")
    assert claude_exec.managed_claude_config(tmp_path, user="nobody") == [str(fragments)]



def test_polling_waits_until_every_selectable_account_passed_a_check(tmp_path):
    from openswap.worker.models import ProviderAvailability

    root = setup_root(tmp_path)
    runtime, codex, claude = _runtime(root, IDENTITY)
    claude.probe = lambda: ProviderAvailability(True, None, "v")
    checked = set()
    claude._account_checked = lambda identity: identity in checked
    assert runtime.provider_availability().diagnostic_code == "live_adapter_disabled"
    checked.add(IDENTITY)
    assert runtime.provider_availability().available is True



def test_a_binary_in_any_hidden_folder_is_never_pinned(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = tmp_path / "root"
    root.mkdir()
    for folder in (home / ".codex" / "bin", root / "bin"):
        folder.mkdir(parents=True)
        binary = folder / "claude"
        binary.write_text("#!/bin/sh\necho '2.1.285 (Claude Code)'\n")
        binary.chmod(0o755)
        monkeypatch.setattr(claude_cli.Path, "home", classmethod(lambda cls: home))
        import pwd

        monkeypatch.setattr(pwd, "getpwuid", lambda uid: (_ for _ in ()).throw(KeyError(uid)))
        with pytest.raises(claude_cli.ClaudeCliError) as error:
            claude_cli.pin(root, binary=binary)
        assert error.value.code == "binary_in_claude_config"


def test_a_symlinked_profile_is_never_prepared(tmp_path):
    root = setup_root(tmp_path, prepared=False)
    profile = claude_exec.profile_for(root, IDENTITY)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.symlink_to(elsewhere)
    run = _native_login(root)
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.claude_prepare(root, "claude:4", run=run, verify=lambda: pinned())
    assert error.value.code == "claude_profile_unsafe" and run.calls == []



@pytest.mark.parametrize("code, state", [(44, "absent"), (51, "unreadable"), (36, "unreadable")])
def test_a_keychain_query_failure_is_unreadable_not_absent(tmp_path, code, state):
    from openswap.worker.live_check_claude import ClaudeLiveCheck

    home = tmp_path / "home"
    home.mkdir()
    root = setup_root(tmp_path)
    check = ClaudeLiveCheck(root, out=lambda *a: None, home=home,
                            run=lambda argv, **kw: subprocess.CompletedProcess(argv, code, "", ""))
    assert check._default_login_snapshot()[0] == state


def test_status_does_not_call_a_profile_under_managed_policy_ready(tmp_path):
    root = setup_root(tmp_path)
    claude_cli.pin(root, binary=fake_claude(tmp_path))
    profile = claude_exec.profile_for(root, IDENTITY)
    (profile / "remote-settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))
    ready = {a["slot"]: a["profile_ready"] for a in live_cli.claude_status(root)["accounts"]}
    assert ready == {"4": False}



@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_a_symlinked_profile_ancestor_refuses_the_launch(tmp_path):
    root = setup_root(tmp_path)
    sessions = root / "sessions"
    moved = tmp_path / "moved-sessions"
    sessions.rename(moved)
    sessions.symlink_to(moved)  # the profile itself is a plain directory, its parent a link
    launcher = FakeLaunch(SUCCESS)
    with pytest.raises(ProviderLaunchRefused):
        make_adapter(root, launcher).start(job_record(), workspace(root), worker_epoch=1)
    assert launcher.launches == []


def test_prepare_does_not_call_a_profile_under_managed_policy_ready(tmp_path):
    root = setup_root(tmp_path)
    profile = claude_exec.profile_for(root, IDENTITY)
    (profile / "remote-settings.json").write_text(json.dumps({"env": {"X": "1"}}))
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.claude_prepare(root, "claude:4", run=_native_login(root), verify=lambda: pinned(),
                                unshare=lambda p: None)
    assert error.value.code == "claude_profile_not_ready"


def test_the_readiness_report_follows_the_pinned_provider(tmp_path):
    from types import SimpleNamespace

    from openswap.worker.remote import RemoteClient

    root = setup_root(tmp_path)
    runtime, codex, claude = _runtime(root, IDENTITY)
    claude.execution_mode = "live"
    report = RemoteClient._readiness(SimpleNamespace(runtime=runtime), load_worker_settings(root))
    assert report["execution"] == "live"
    claude.execution_mode = "disabled"
    codex.execution_mode = "live"
    assert RemoteClient._readiness(SimpleNamespace(runtime=runtime), load_worker_settings(root))["execution"] == \
        "disabled"



@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_status_never_calls_a_profile_under_a_symlinked_ancestor_ready(tmp_path):
    root = setup_root(tmp_path)
    claude_cli.pin(root, binary=fake_claude(tmp_path))
    sessions = root / "sessions"
    moved = tmp_path / "moved-sessions"
    sessions.rename(moved)
    sessions.symlink_to(moved)
    ready = {a["slot"]: a["profile_ready"] for a in live_cli.claude_status(root)["accounts"]}
    assert ready == {"4": False}


def _claude_status(**overrides):
    status = {"cli": {"pinned": True, "version": "2.1.285 (Claude Code)"}, "execution_mode": "disabled",
              "checked_accounts": [],
              "accounts": [{"slot": "4", "alias": None, "account_ref": IDENTITY, "pinned": True, "allowed": True,
                            "profile_ready": True},
                           {"slot": "5", "alias": None, "account_ref": "claude:" + "5" * 64, "pinned": False,
                            "allowed": True, "profile_ready": False}]}
    status.update(overrides)
    return status


def test_claude_status_picks_pin_and_prepare_before_the_live_check():
    status = _claude_status()
    status["accounts"][0]["pinned"] = False
    assert "openswap worker account claude:<slot>" in live_cli._claude_next_step(status)
    status = _claude_status()
    assert "openswap worker claude prepare claude:5" in live_cli._claude_next_step(status)
    status["accounts"][1]["profile_ready"] = True
    assert "live-check --provider claude`" in live_cli._claude_next_step(status)
    status["execution_mode"] = "live"
    status["checked_accounts"] = [IDENTITY]
    assert "--account claude:5" in live_cli._claude_next_step(status)
    status["checked_accounts"] = [IDENTITY, "claude:" + "5" * 64]
    assert live_cli._claude_next_step(status).startswith("nothing")



def test_a_claude_pin_made_during_an_unpinned_probe_is_replanned_not_failed(tmp_path):
    from openswap.worker.models import ProviderAvailability

    root = setup_root(tmp_path)
    runtime, codex, claude = _runtime(root, None)
    probes = []

    def codex_probe():
        probes.append("codex")
        # The owner pins a Claude account while the (unpinned) Codex probe runs.
        configure_worker_local_policy(root, pinned_account_ref=IDENTITY,
                                      workspaces=(WorkerWorkspace("research", (root.parent / "research").resolve()),))
        return ProviderAvailability(True, None, "v")

    def claude_probe():
        probes.append("claude")
        return ProviderAvailability(True, None, "v")

    codex.probe, claude.probe = codex_probe, claude_probe
    _submit(runtime)
    final = runtime.reconcile_once()
    assert probes == ["codex", "claude"]
    # Planned again on Claude: the job reached Claude's adapter (which refuses
    # in this test), instead of failing on the unprobed provider.
    assert claude.started == [IDENTITY] and codex.started == []
    assert final.pinned_account_ref == IDENTITY



def test_every_path_takes_provider_locks_in_one_order(tmp_path, monkeypatch):
    from openswap.worker import leases as leases_module

    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    taken = []
    real = leases_module.AccountLeaseStore.mutation_guard

    def recording(self, *args, **kwargs):
        taken.append(self.provider)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(leases_module.AccountLeaseStore, "mutation_guard", recording)
    with ProviderLeases(root).mutation_guard():
        pass
    assert taken == list(leases_module.PROVIDER_LOCK_ORDER) == ["codex", "claude"]
    stores = [AccountLeaseStore(root, "claude"), AccountLeaseStore(root, "codex")]
    stores.sort(key=leases_module.provider_lock_rank)  # what the purge does
    assert [store.provider for store in stores] == ["codex", "claude"]
    # Sorting by lock path (the old purge order) would have put Claude first.
    assert sorted(str(store.provider_lock) for store in stores)[0].endswith(f"{root.name}/.lock")
