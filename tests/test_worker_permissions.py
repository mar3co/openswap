"""Remote sessions follow the account's own permission settings (owner decision 2026-10-08).

Covers the settings files (Claude profile, Codex isolated home), the per-Mac
limit, the Codex launch, `claude prepare` / `codex settings`, the live opt-in
version, and how status and setup show it. No real Claude or Codex runs.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from openswap import settings as settings_module
from openswap.settings import (
    LiveExecutionSettings,
    load_live_execution,
    load_permission_override,
    write_live_execution,
    write_permission_override,
)
from openswap.worker import cli, codex_exec, live, live_cli, permissions
from openswap.worker.permissions import (
    ClaudePermissions,
    CodexPermissions,
    PermissionSettingsError,
    claude_permission_args,
    default_claude_permissions,
    default_codex_permissions,
    read_claude_permissions,
    read_codex_permissions,
    write_claude_permissions,
    write_codex_permissions,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="remote tasks run on macOS")


# -- Claude: the profile's settings.json --------------------------------------------------


def test_an_absent_settings_file_is_claude_codes_defaults(tmp_path):
    assert read_claude_permissions(tmp_path) == ClaudePermissions()
    assert ClaudePermissions().describe() == "mode Claude Code's default"


def test_copying_takes_the_mode_and_the_rules_and_keeps_everything_else(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "model": "opus", "hooks": {"Stop": []},
        "permissions": {"defaultMode": "acceptEdits", "allow": ["Bash(git status)"], "deny": ["Read(.env)"],
                        "additionalDirectories": ["/elsewhere"]}}))
    copied = default_claude_permissions(home)
    # Only the mode and the rules: never hooks, the model or extra directories.
    assert copied == {"defaultMode": "acceptEdits", "allow": ["Bash(git status)"], "deny": ["Read(.env)"]}
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").write_text(json.dumps({"theme": "dark", "permissions": {"ask": ["WebFetch"]}}))
    summary = write_claude_permissions(profile, copied=copied)
    written = json.loads((profile / "settings.json").read_text())
    assert written["theme"] == "dark"
    # A copy replaces the copied keys: the profile's own ask rule goes, as the default has none.
    assert written["permissions"] == copied
    assert summary == ClaudePermissions("acceptEdits", 1, 1, 0)
    assert summary.describe() == "mode acceptEdits, 1 allow, 1 deny rules"
    assert write_claude_permissions(profile, mode="bypassPermissions").mode == "bypassPermissions"
    assert json.loads((profile / "settings.json").read_text())["permissions"]["allow"] == ["Bash(git status)"]
    assert os.stat(profile / "settings.json").st_mode & 0o777 == 0o600


def test_a_dotfiles_link_for_the_default_settings_is_read(tmp_path):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    real = tmp_path / "dotfiles.json"
    real.write_text(json.dumps({"permissions": {"defaultMode": "plan"}}))
    (home / ".claude" / "settings.json").symlink_to(real)
    assert default_claude_permissions(home) == {"defaultMode": "plan"}
    assert default_claude_permissions(tmp_path / "nobody") is None


@pytest.mark.parametrize("text", [
    "not json", "[]", json.dumps({"permissions": []}), json.dumps({"permissions": {"defaultMode": "yolo"}}),
    json.dumps({"permissions": {"allow": "Bash"}}), json.dumps({"permissions": {"deny": [1]}}),
    json.dumps({"permissions": {"disableBypassPermissionsMode": True}}),
    json.dumps({"permissions": {"additionalDirectories": "/tmp"}}),
])
def test_settings_claude_code_would_ignore_are_refused(tmp_path, text):
    (tmp_path / "settings.json").write_text(text)
    with pytest.raises(PermissionSettingsError):
        read_claude_permissions(tmp_path)
    with pytest.raises(PermissionSettingsError):
        write_claude_permissions(tmp_path, mode="default")


def test_a_symlinked_profile_settings_file_is_refused(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "settings.json").symlink_to(target)
    with pytest.raises(PermissionSettingsError) as error:
        read_claude_permissions(profile)
    assert error.value.code == "settings_unsafe"


def test_the_per_mac_limit_maps_to_claude_arguments():
    assert claude_permission_args("follow") == []
    assert claude_permission_args("follow", {"permissions": {"defaultMode": "default"}}) == [
        "--settings", '{"permissions":{"defaultMode":"default"}}']
    no_shell = claude_permission_args("no-shell")
    assert no_shell[:2] == ["--disallowedTools", "Bash,PowerShell,Monitor,REPL,BashOutput,KillShell"]
    assert json.loads(no_shell[3]) == {"disableAllHooks": True}
    read_only = claude_permission_args("read-only")
    assert read_only[:2] == ["--tools", "Read,Grep,Glob,WebSearch,WebFetch"]
    with pytest.raises(ValueError):
        claude_permission_args("everything")


def test_the_credentials_file_gets_deny_rules_for_the_file_tools_in_every_mode():
    rules = permissions.credential_rules(["/b/sessions/4-a_b.com/.credentials.json"])
    assert rules == ["Read(//b/sessions/4-a_b.com/.credentials.json)",
                     "Edit(//b/sessions/4-a_b.com/.credentials.json)"]
    args = claude_permission_args("read-only", {"permissions": {"deny": ["Bash(rm:*)"], "defaultMode": "plan"}},
                                  ["/b/c.json"])
    settings = json.loads(args[args.index("--settings") + 1])
    # Added to the live check's own rules, never replacing them.
    assert settings["permissions"] == {"defaultMode": "plan",
                                       "deny": ["Bash(rm:*)", "Read(//b/c.json)", "Edit(//b/c.json)"]}
    for bad in ("relative/.credentials.json", "/a(b)/c", "/a\nb"):
        with pytest.raises(ValueError):
            permissions.credential_rules([bad])


# -- Codex: the isolated home's permissions.json ------------------------------------------


def test_codex_defaults_until_the_owner_records_settings(tmp_path):
    assert read_codex_permissions(tmp_path) == CodexPermissions()
    assert CodexPermissions().describe() == "approval on-request, sandbox workspace-write (Codex defaults)"
    write_codex_permissions(tmp_path, CodexPermissions("never", "read-only", "auto_review"))
    assert read_codex_permissions(tmp_path) == CodexPermissions("never", "read-only", "auto_review", True)
    (tmp_path / "permissions.json").write_text(json.dumps({"approval_policy": "always"}))
    with pytest.raises(PermissionSettingsError):
        read_codex_permissions(tmp_path)


def test_the_owners_codex_config_and_its_selected_profile_are_copied(tmp_path):
    (tmp_path / "config.toml").write_text(
        'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\nprofile = "careful"\n'
        'model = "o3"\n[profiles.careful]\napproval_policy = "untrusted"\nsandbox_mode = "read-only"\n')
    assert default_codex_permissions(tmp_path) == CodexPermissions("untrusted", "read-only", "user", True)
    (tmp_path / "config.toml").write_text('approval_policy = { granular = { sandbox_approval = true } }\n')
    assert default_codex_permissions(tmp_path).approval == "on-request"
    (tmp_path / "config.toml").write_text('model = "o3"\n')
    assert default_codex_permissions(tmp_path) is None
    assert default_codex_permissions(tmp_path / "missing") is None


@pytest.mark.parametrize("account, override, expected", [
    (CodexPermissions("never"), "follow", ("never", None, True, True)),
    (CodexPermissions("on-request"), "follow", ("on-request", None, True, True)),
    (CodexPermissions("on-failure"), "follow", ("on-request", None, True, True)),
    # Asks before every command not known safe: never wider than chosen, so no shell at all.
    (CodexPermissions("untrusted"), "follow", ("on-request", None, True, False)),
    (CodexPermissions(reviewer="auto_review"), "follow", ("on-request", "auto_review", True, True)),
    (CodexPermissions(sandbox="read-only"), "follow", ("on-request", None, False, True)),
    # danger-full-access still gets OpenSwap's folder rules.
    (CodexPermissions(sandbox="danger-full-access"), "follow", ("on-request", None, True, True)),
    (CodexPermissions("never"), "no-shell", ("never", None, True, False)),
    (CodexPermissions("never", "danger-full-access"), "read-only", ("never", None, False, True)),
])
def test_codex_launch_settings(account, override, expected):
    launch = account.launch(override)
    assert (launch.approval_policy, launch.reviewer, launch.writable, launch.shell) == expected


def test_the_codex_config_carries_the_accounts_policy_and_keeps_openswaps_rules(tmp_path):
    text = codex_exec.codex_config(launch=CodexPermissions("on-request", reviewer="auto_review").launch("follow"))
    assert 'approval_policy = "on-request"' in text and 'approvals_reviewer = "auto_review"' in text
    assert '":root" = "deny"' in text and "[permissions.openswap-research.network]\nenabled = false" in text
    assert '"." = "write"' in text and "sandbox_mode" not in text
    read_only = codex_exec.codex_config(launch=CodexPermissions(sandbox="read-only").launch("follow"))
    assert '"." = "read"' in read_only and '"write"' not in read_only
    # A sign-in writes the config with no launch: nothing may ask, as before.
    assert 'approval_policy = "never"' in codex_exec.codex_config()
    assert "--disable" in codex_exec.codex_argv(Path("/c"), Path("/o"), Path("/r"), shell=False)
    argv = codex_exec.codex_argv(Path("/c"), Path("/o"), Path("/r"), shell=False)
    assert argv[argv.index("shell_tool") - 1] == "--disable"
    assert "shell_tool" not in codex_exec.codex_argv(Path("/c"), Path("/o"), Path("/r"))


# -- the per-Mac limit ------------------------------------------------------------------------


def test_the_limit_defaults_to_follow_and_an_unreadable_one_is_read_only(tmp_path):
    assert load_permission_override(tmp_path) == "follow"
    assert write_permission_override(tmp_path, "no-shell") == "no-shell"
    assert load_permission_override(tmp_path) == "no-shell"
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw["worker"]["permissionOverride"] == "no-shell"
    assert write_permission_override(tmp_path, "follow") == "follow"
    assert "permissionOverride" not in json.loads((tmp_path / "settings.json").read_text())["worker"]
    raw["worker"]["permissionOverride"] = "everything"
    (tmp_path / "settings.json").write_text(json.dumps(raw))
    assert load_permission_override(tmp_path) == "read-only"
    with pytest.raises(ValueError):
        write_permission_override(tmp_path, "everything")
    # A settings file that cannot be read or parsed is not "no limit".
    for text in ("{not json", "[]", json.dumps({"worker": "x"})):
        (tmp_path / "settings.json").write_text(text)
        assert load_permission_override(tmp_path) == "read-only"
    (tmp_path / "settings.json").write_text(json.dumps({"ui": {}}))
    assert load_permission_override(tmp_path) == "follow"
    (tmp_path / "settings.json").unlink()
    (tmp_path / "settings.json").mkdir()  # unreadable as a file
    assert load_permission_override(tmp_path) == "read-only"


def test_the_permissions_command_shows_and_sets_the_limit(tmp_path, capsys):
    assert cli.main(["permissions"], backup_root=tmp_path) == 0
    assert "follow each account's own Claude or Codex permission settings (the default)" in capsys.readouterr().out
    assert cli.main(["permissions", "no-shell"], backup_root=tmp_path) == 0
    out = capsys.readouterr().out
    assert "This Mac limits every remote task: no shell commands" in out and "next task" in out
    assert cli.main(["permissions", "--json"], backup_root=tmp_path) == 0
    assert json.loads(capsys.readouterr().out) == {"permission_override": "no-shell"}
    with pytest.raises(SystemExit):
        cli.main(["permissions", "everything"], backup_root=tmp_path)


def test_the_limit_is_written_under_the_live_lock(tmp_path, monkeypatch):
    # Every launch holds the live lock from reading the limit until its job is
    # released, so a limit that was set can no longer be overtaken by one.
    from contextlib import contextmanager

    from openswap.worker import live as live_module

    held = []

    @contextmanager
    def lock(root, **kwargs):
        held.append(True)
        try:
            yield
        finally:
            held.append(False)

    def write(root, value):
        assert held == [True]
        return value

    monkeypatch.setattr(live_module, "live_lock", lock)
    monkeypatch.setattr(settings_module, "write_permission_override", write)
    assert cli.set_permission_override(tmp_path, "read-only") == "read-only"
    assert held == [True, False]


def test_status_shows_the_limit_only_when_it_is_not_the_default(tmp_path, monkeypatch, capsys):
    snapshot = {"enabled": True, "process_state": "running"}
    monkeypatch.setattr(cli, "read_status", lambda _root: dict(snapshot))
    assert cli.main(["status"], backup_root=tmp_path) == 0
    assert "Permissions" not in capsys.readouterr().out
    assert cli.main(["status", "--json"], backup_root=tmp_path) == 0
    assert "permission_override" not in json.loads(capsys.readouterr().out)
    write_permission_override(tmp_path, "read-only")
    assert cli.main(["status"], backup_root=tmp_path) == 0
    assert "Permissions" in capsys.readouterr().out
    assert cli.main(["status", "--json"], backup_root=tmp_path) == 0
    assert json.loads(capsys.readouterr().out)["permission_override"] == "read-only"


def test_the_control_service_has_no_way_to_set_the_limit():
    # The limit is local policy: no wire operation, IPC request or remote message names it.
    from openswap.worker import ipc, protocol, remote

    for module in (protocol, ipc, remote):
        source = Path(module.__file__).read_text()
        assert "permissionOverride" not in source and "permission_override" not in source


# -- the live opt-in measures this behaviour ---------------------------------------------------


def test_an_opt_in_from_before_this_change_no_longer_runs_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "platform_supported", lambda *a, **k: True)
    monkeypatch.setattr(live, "host_binding", lambda root, **kw: "4e" * 32)
    monkeypatch.setattr(live, "_HOST_CACHE", {})
    write_live_execution(tmp_path, LiveExecutionSettings(True, "ab" * 32, "cd" * 32, "now", (), "4e" * 32))
    assert live.execution_mode(tmp_path) == "live"
    raw = json.loads((tmp_path / "settings.json").read_text())
    assert raw["worker"]["liveExecution"]["policyVersion"] == settings_module.LIVE_POLICY_VERSION
    # The same opt-in as stored before jobs followed the account's settings.
    del raw["worker"]["liveExecution"]["policyVersion"]
    (tmp_path / "settings.json").write_text(json.dumps(raw))
    assert load_live_execution(tmp_path).policy_version == 1
    assert live.execution_mode(tmp_path) == "disabled"
    status = live.live_status(tmp_path)
    assert status["recheck_needed"] is True and status["execution_mode"] == "disabled"
    text = live_cli._codex_next_step({**status, "cli": {"installed": True}, "accounts": [
        {"pinned": True, "allowed": True, "isolated_sign_in": True, "slot": "1", "account_ref": "x"}]})
    assert "run the live check again" in text
    monkeypatch.setattr(live, "_HOST_CACHE", {})


def test_the_new_gates_are_required_to_enable():
    assert {"permissions", "sign_in_isolation"} <= set(live.REQUIRED_GATES)


# -- claude prepare and codex settings ---------------------------------------------------------


def test_claude_prepare_copies_the_owners_settings_and_sets_a_mode(tmp_path, monkeypatch):
    from tests.test_worker_claude import IDENTITY, _native_login, pinned, setup_root
    from openswap.worker import claude_exec

    root = setup_root(tmp_path, prepared=True)
    home = tmp_path / "owner"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text(json.dumps(
        {"permissions": {"defaultMode": "acceptEdits", "deny": ["Bash(rm:*)"]}}))
    run = _native_login(root)
    result = live_cli.claude_prepare(root, "claude:4", run=run, verify=lambda: pinned(), copy_settings=True,
                                     mode="auto", home=home)
    assert run.calls == []  # already signed in: no new sign-in
    assert result["permissions"] == {"mode": "auto", "allow_rules": 0, "deny_rules": 1, "ask_rules": 0}
    written = json.loads((claude_exec.profile_for(root, IDENTITY) / "settings.json").read_text())
    assert written["permissions"] == {"defaultMode": "auto", "deny": ["Bash(rm:*)"]}


def test_claude_prepare_asks_only_for_a_profile_without_settings(tmp_path):
    from tests.test_worker_claude import IDENTITY, _native_login, pinned, setup_root
    from openswap.worker import claude_exec

    root = setup_root(tmp_path, prepared=True)
    asked = []

    def decide(current, default):
        asked.append(current)
        return False, "dontAsk"

    result = live_cli.claude_prepare(root, "claude:4", run=_native_login(root), verify=lambda: pinned(),
                                     decide=decide, home=tmp_path / "nobody")
    assert asked == [ClaudePermissions()] and result["permissions"]["mode"] == "dontAsk"
    result = live_cli.claude_prepare(root, "claude:4", run=_native_login(root), verify=lambda: pinned(),
                                     decide=decide, home=tmp_path / "nobody")
    assert len(asked) == 1 and result["permissions"]["mode"] == "dontAsk"  # configured: not asked again
    (claude_exec.profile_for(root, IDENTITY) / "settings.json").write_text("not json")
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.claude_prepare(root, "claude:4", run=_native_login(root), verify=lambda: pinned())
    assert error.value.code == "claude_settings_invalid"
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.claude_prepare(root, "claude:4", run=_native_login(root), verify=lambda: pinned(), mode="yolo")
    assert error.value.code == "mode_invalid"


def test_codex_settings_copy_and_set_under_the_accounts_lease(tmp_path):
    from tests.test_worker_codex_exec import IDENTITY, sign_in

    root = tmp_path
    (root / "codex").mkdir()
    (root / "codex" / "sequence.json").write_text(json.dumps({
        "schemaVersion": 1, "sequence": [1], "accounts": {"1": {"email": "o@example.com",
                                                                "accountId": "acct-live-1"}}}))
    sign_in(root)
    owner = tmp_path / "owner-codex"
    owner.mkdir()
    (owner / "config.toml").write_text('approval_policy = "never"\nsandbox_mode = "read-only"\n')
    shown = live_cli.codex_settings(root, "1")
    assert shown["changed"] is False and shown["permissions"]["recorded"] is False
    copied = live_cli.codex_settings(root, "1", copy_settings=True, codex_home=owner)
    assert copied["permissions"] == {"approval_policy": "never", "sandbox_mode": "read-only",
                                     "approvals_reviewer": "user", "recorded": True}
    changed = live_cli.codex_settings(root, "1", sandbox="workspace-write", reviewer="auto_review")
    assert changed["permissions"]["sandbox_mode"] == "workspace-write"
    assert changed["permissions"]["approval_policy"] == "never"  # kept
    home = codex_exec.isolated_home(root, IDENTITY)
    assert read_codex_permissions(home) == CodexPermissions("never", "workspace-write", "auto_review", True)
    from openswap.worker.leases import AccountLeaseStore

    assert AccountLeaseStore(root, "codex").read_current().state == "released"
    with pytest.raises(live_cli.AccountPinError):
        live_cli.codex_settings(root, "1", approval="always")
    with pytest.raises(live_cli.AccountPinError) as error:
        live_cli.codex_settings(root, "1", copy_settings=True, codex_home=tmp_path / "missing")
    assert error.value.code == "codex_default_settings_missing"


def test_a_codex_job_runs_with_its_accounts_settings_and_the_limit(tmp_path, monkeypatch):
    from tests.test_worker_codex_exec import (
        HOST, FakeContainment, IDENTITY, job_record, make_adapter, sign_in, workspace,
    )

    monkeypatch.setattr(live, "host_binding", lambda root, **kw: HOST)
    monkeypatch.setattr(live, "_HOST_CACHE", {})
    home = sign_in(tmp_path)
    write_codex_permissions(home, CodexPermissions("never", "read-only"))
    write_permission_override(tmp_path, "no-shell")
    containment = FakeContainment([{"type": "thread.started"}])
    make_adapter(tmp_path, containment).start(job_record(), workspace(tmp_path), worker_epoch=1)
    argv = containment.launches[0]["argv"]
    assert argv[argv.index("shell_tool") - 1] == "--disable"
    config = (codex_exec.isolated_home(tmp_path, IDENTITY) / "config.toml").read_text()
    assert 'approval_policy = "never"' in config and '"." = "read"' in config
    # Settings that cannot be trusted refuse the launch, unlaunched.
    (home / "permissions.json").write_text("{")
    from openswap.worker.adapter import ProviderLaunchRefused

    with pytest.raises(ProviderLaunchRefused) as error:
        make_adapter(tmp_path, FakeContainment()).start(job_record("b" * 32), workspace(tmp_path), worker_epoch=1)
    assert error.value.diagnostic_code == "provider_unavailable"


# -- setup ---------------------------------------------------------------------------------------


def test_the_setup_summary_says_once_that_shell_can_read_the_claude_sign_in(tmp_path, monkeypatch):
    from openswap.worker import guided_setup

    said = []

    class UI:
        interactive = False

        def say(self, text):
            said.append(text)

    state = guided_setup.Readiness(paired_url=None, worker="off", account="Claude 4", folders=(),
                                   execution="disabled", provider="claude")
    monkeypatch.setattr(guided_setup, "readiness", lambda root: state)
    guided_setup.summary(tmp_path, UI(), start_wait_s=0)
    assert said.count(guided_setup.SIGN_IN_NOTE) == 1
    said.clear()
    from dataclasses import replace

    monkeypatch.setattr(guided_setup, "readiness", lambda root: replace(state, permission_override="no-shell"))
    guided_setup.summary(tmp_path, UI(), start_wait_s=0)
    assert guided_setup.SIGN_IN_NOTE not in said
    assert any("Permissions" in line and "no-shell (this Mac)" in line for line in said)
    said.clear()
    monkeypatch.setattr(guided_setup, "readiness", lambda root: replace(state, provider="codex"))
    guided_setup.summary(tmp_path, UI(), start_wait_s=0)
    assert guided_setup.SIGN_IN_NOTE not in said and not any("Permissions" in line for line in said)


def test_the_override_descriptions_name_every_limit():
    assert set(permissions.OVERRIDE_DESCRIPTIONS) == set(permissions.OVERRIDES) == set(
        settings_module.PERMISSION_OVERRIDES)
