from __future__ import annotations

import json
import os
import plistlib
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from openswap.codex.desktop_app import (
    DesktopApp,
    DesktopAppError,
    _Process,
    _argv0,
    _probe_oauth_credentials,
    _stop_probe,
)
from openswap.exceptions import ClaudeSwitchError


def _app(tmp_path: Path) -> Path:
    app = tmp_path / "ChatGPT.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    with (app / "Contents/Info.plist").open("wb") as stream:
        plistlib.dump({
            "CFBundleIdentifier": "com.openai.codex",
            "CFBundleExecutable": "ChatGPT",
            "CFBundleShortVersionString": "26.908.70816",
            "CFBundleVersion": "9275",
        }, stream)
    for path in (app / "Contents/MacOS/ChatGPT", app / "Contents/Resources/codex"):
        path.write_text("binary")
        path.chmod(0o700)
    return app


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    monkeypatch.setattr("openswap.codex.desktop_app.sys.platform", "darwin")
    monkeypatch.setattr("openswap.codex.desktop_app.Path.home", lambda: tmp_path)
    monkeypatch.setattr("openswap.codex.desktop_app.DesktopApp._verify_signature", lambda self: None)
    monkeypatch.setattr("openswap.codex.desktop_app.DesktopApp._probe_bundled_cli", lambda self: None)
    return DesktopApp(_app(tmp_path))


def _home() -> Path:
    return Path.home() / ".codex"


def _ps(stdout: str):
    return subprocess.CompletedProcess([], 0, stdout, "")


def test_error_is_a_handled_openswap_error():
    assert issubclass(DesktopAppError, ClaudeSwitchError)


def test_argv0_preserves_quoted_windows_paths():
    executable = r"C:\Program Files\ChatGPT\codex-code-mode-host"
    assert _argv0(f'"{executable}" --flag') == executable


def test_preflight_returns_only_sanitized_metadata(desktop, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    data = desktop.preflight(_home())
    assert data["bundle_id"] == "com.openai.codex"
    assert data["version"] == "26.908.70816"
    assert data["compatibility"] == "tested_baseline"
    assert data["compatibility_basis"] == (
        "publisher_signature_and_isolated_oauth_file_store_probe"
    )
    assert data["codex_home"] == str(_home())
    assert "secret" not in repr(data)


def test_preflight_rejects_wrong_platform(desktop, tmp_path, monkeypatch):
    monkeypatch.setattr("openswap.codex.desktop_app.sys.platform", "linux")
    with pytest.raises(DesktopAppError, match="macOS"):
        desktop.preflight(_home())


def test_preflight_rejects_wrong_bundle_and_missing_cli(desktop, tmp_path):
    with desktop._plist_path.open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": "evil.app", "CFBundleExecutable": "ChatGPT"}, stream)
    with pytest.raises(DesktopAppError, match="not the supported"):
        desktop.preflight(_home())


def test_preflight_rejects_custom_backend_override(desktop, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_CLI_PATH", "/tmp/other")
    with pytest.raises(DesktopAppError, match="custom Codex backend"):
        desktop.preflight(_home())


def test_preflight_rejects_custom_backend_url(desktop, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unsupported.invalid")
    with pytest.raises(DesktopAppError, match="OPENAI_BASE_URL"):
        desktop.preflight(_home())


def test_preflight_rejects_custom_home(desktop, tmp_path):
    with pytest.raises(DesktopAppError, match="custom Codex home"):
        desktop.preflight(tmp_path / "elsewhere")


def test_preflight_accepts_future_version_when_capabilities_pass(desktop):
    with desktop._plist_path.open("wb") as stream:
        plistlib.dump({
            "CFBundleIdentifier": "com.openai.codex",
            "CFBundleExecutable": "ChatGPT",
            "CFBundleShortVersionString": "future",
            "CFBundleVersion": "9999",
        }, stream)
    result = desktop.preflight(_home())
    assert result["version"] == "future"
    assert result["build"] == "9999"
    assert result["compatibility"] == "compatible_unvalidated"


def test_preflight_rejects_known_incompatible_build(desktop, monkeypatch):
    monkeypatch.setattr(
        "openswap.codex.desktop_app._BLOCKED_BUILDS",
        frozenset({("26.908.70816", "9275")}),
    )
    with pytest.raises(DesktopAppError, match="known to be incompatible") as exc:
        desktop.preflight(_home())
    assert exc.value.reason == "known_incompatible_build"


def test_preflight_rejects_non_dictionary_plist(desktop):
    with desktop._plist_path.open("wb") as stream:
        plistlib.dump(["not", "a", "bundle"], stream)
    with pytest.raises(DesktopAppError, match="missing or invalid"):
        desktop.preflight(_home())


def test_preflight_rejects_malformed_xml_plist(desktop):
    desktop._plist_path.write_bytes(b'<?xml version="1.0"?><plist><dict>')
    with pytest.raises(DesktopAppError, match="missing or invalid"):
        desktop.preflight(_home())


def test_signature_verification_is_strict_and_cached(tmp_path, monkeypatch):
    monkeypatch.setattr("openswap.codex.desktop_app.sys.platform", "darwin")
    monkeypatch.setattr("openswap.codex.desktop_app.Path.home", lambda: tmp_path)
    app = DesktopApp(_app(tmp_path))
    calls = []
    probes = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        stderr = "Identifier=com.openai.codex\nTeamIdentifier=2DC432GLL2\n" if "-d" in argv else ""
        return subprocess.CompletedProcess(argv, 0, "", stderr)
    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.run", fake_run)
    monkeypatch.setattr(app, "_probe_bundled_cli", lambda: probes.append(True))
    app.preflight(_home())
    app.preflight(_home())
    assert len(calls) == 2
    assert len(probes) == 1
    assert calls[0][0][:5] == ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app.app_path)]
    assert all(call[1]["timeout"] == 15.0 for call in calls)
    app._bundled_cli.write_text("changed")
    app._bundled_cli.chmod(0o700)
    app.preflight(_home())
    assert len(calls) == 4
    assert len(probes) == 2


def test_invalidate_cache_rechecks_signature_after_secondary_bundle_change(tmp_path, monkeypatch):
    monkeypatch.setattr("openswap.codex.desktop_app.sys.platform", "darwin")
    monkeypatch.setattr("openswap.codex.desktop_app.Path.home", lambda: tmp_path)
    app = DesktopApp(_app(tmp_path))
    calls = []
    probes = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        stderr = "Identifier=com.openai.codex\nTeamIdentifier=2DC432GLL2\n" if "-d" in argv else ""
        return subprocess.CompletedProcess(argv, 0, "", stderr)

    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.run", fake_run)
    monkeypatch.setattr(app, "_probe_bundled_cli", lambda: probes.append(True))
    app.preflight(_home())
    assert len(calls) == 2
    assert len(probes) == 1
    helper = app.app_path / "Contents/MacOS" / "ChatGPT Helper"
    helper.write_text("other-signed-resource")
    helper.chmod(0o700)
    app.preflight(_home())
    assert len(calls) == 2
    app.invalidate_validation_cache()
    app.preflight(_home())
    assert len(calls) == 4
    assert len(probes) == 2


def test_signature_rejects_wrong_team(tmp_path, monkeypatch):
    monkeypatch.setattr("openswap.codex.desktop_app.sys.platform", "darwin")
    monkeypatch.setattr("openswap.codex.desktop_app.Path.home", lambda: tmp_path)
    app = DesktopApp(_app(tmp_path))
    probes = []
    def fake_run(argv, **kwargs):
        stderr = "Identifier=com.openai.codex\nTeamIdentifier=NOT-OPENAI\n" if "-d" in argv else ""
        return subprocess.CompletedProcess(argv, 0, "", stderr)
    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.run", fake_run)
    monkeypatch.setattr(app, "_probe_bundled_cli", lambda: probes.append(True))
    with pytest.raises(DesktopAppError, match="expected publisher"):
        app.preflight(_home())
    assert probes == []


@pytest.mark.skipif(sys.platform == "win32", reason="macOS compatibility executable")
def test_bundled_cli_probe_uses_isolated_oauth_file_store(tmp_path):
    app_path = _app(tmp_path)
    cli = app_path / "Contents/Resources/codex"
    cli.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") == 1:
        print(json.dumps({{"id": 1, "result": {{"codexHome": os.environ["CODEX_HOME"]}}}}), flush=True)
    elif message.get("id") == 2:
        with open(os.path.join(os.environ["CODEX_HOME"], "auth.json"), encoding="utf-8") as stream:
            auth = json.load(stream)
        tokens = auth.get("tokens") or {{}}
        valid = (
            auth.get("auth_mode") == "chatgpt"
            and auth.get("OPENAI_API_KEY") is None
            and all(tokens.get(name) for name in ("id_token", "access_token", "refresh_token"))
            and "account_id" not in tokens
        )
        account = {{"type": "chatgpt"}} if valid else None
        print(json.dumps({{"id": 2, "result": {{"account": account}}}}), flush=True)
""",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    DesktopApp(app_path)._probe_bundled_cli()


def test_probe_credentials_match_desktop_switch_oauth_contract():
    from openswap.codex.desktop import _credential_identity

    ident = _credential_identity(
        json.dumps(_probe_oauth_credentials()),
        label="Compatibility probe",
    )

    assert ident.kind == "oauth"
    assert ident.email == "openswap-compatibility@example.invalid"
    assert ident.account_id == "openswap-compatibility-account"


@pytest.mark.skipif(sys.platform == "win32", reason="macOS compatibility executable")
def test_bundled_cli_probe_rejects_api_key_only_backend(tmp_path):
    app_path = _app(tmp_path)
    cli = app_path / "Contents/Resources/codex"
    cli.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") == 1:
        print(json.dumps({{"id": 1, "result": {{"codexHome": os.environ["CODEX_HOME"]}}}}), flush=True)
    elif message.get("id") == 2:
        print(json.dumps({{"id": 2, "result": {{"account": {{"type": "apiKey"}}}}}}), flush=True)
""",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    with pytest.raises(DesktopAppError, match="did not load file-backed OAuth credentials") as exc:
        DesktopApp(app_path)._probe_bundled_cli()
    assert exc.value.reason == "incompatible_backend"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_stop_probe_signals_group_after_leader_exits(monkeypatch):
    calls = []

    class ExitedLeader:
        pid = 4321

        @staticmethod
        def poll():
            return 0

    monkeypatch.setattr(
        "openswap.codex.desktop_app.os.killpg",
        lambda pid, sig: calls.append((pid, sig)),
    )

    _stop_probe(ExitedLeader(), True)

    assert calls == [(4321, signal.SIGKILL)]


def test_signature_timeout_is_retryable_but_rejection_is_terminal(tmp_path, monkeypatch):
    app = DesktopApp(_app(tmp_path))

    def timeout_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.run", timeout_run)
    with pytest.raises(DesktopAppError) as timeout:
        app._verify_signature()
    assert timeout.value.reason == "signature_check_failed"

    def reject_run(argv, **_kwargs):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.run", reject_run)
    with pytest.raises(DesktopAppError) as rejected:
        app._verify_signature()
    assert rejected.value.reason == "signature_unverified"


def test_is_running_matches_exact_executable_not_name(desktop, monkeypatch):
    exe = desktop.app_path / "Contents/MacOS/ChatGPT"
    monkeypatch.setattr("openswap.codex.desktop_app._processes", lambda: [
        _Process(2, 1, "/other/ChatGPT", "/other/ChatGPT"),
        _Process(3, 1, str(exe), str(exe)),
    ])
    assert desktop.is_running()


def test_assert_stopped_refuses_external_codex_without_exposing_args(desktop, monkeypatch):
    monkeypatch.setattr("openswap.codex.desktop_app._processes", lambda: [
        _Process(90, 1, "/usr/local/bin/codex", "codex --some-private-value secret")
    ])
    with pytest.raises(DesktopAppError, match="Another Codex client") as exc:
        desktop.assert_stopped()
    assert "secret" not in str(exc.value)


def test_assert_stopped_recognizes_only_known_helpers_inside_bundle(desktop, monkeypatch):
    inside = desktop.app_path / "Contents/Resources/codex-code-mode-host"
    monkeypatch.setattr("openswap.codex.desktop_app._processes", lambda: [
        _Process(91, 1, "codex-code-mode-host", str(inside)),
    ])
    with pytest.raises(DesktopAppError, match="Another Codex client"):
        desktop.assert_stopped()
    monkeypatch.setattr("openswap.codex.desktop_app._processes", lambda: [
        _Process(92, 1, "codex-code-mode-host", "/tmp/codex-code-mode-host"),
    ])
    desktop.assert_stopped()


def test_quit_uses_graceful_request_and_waits_for_descendants(desktop, monkeypatch):
    exe = str(desktop.app_path / "Contents/MacOS/ChatGPT")
    snapshots = iter([
        [_Process(10, 1, exe, exe), _Process(11, 10, str(desktop._bundled_cli), str(desktop._bundled_cli))],
        [_Process(11, 1, str(desktop._bundled_cli), str(desktop._bundled_cli))],
        [],
    ])
    monkeypatch.setattr("openswap.codex.desktop_app._processes", lambda: next(snapshots))
    requested = []
    monkeypatch.setattr(desktop, "_request_terminate", lambda pids, _executable: requested.append(pids))
    monkeypatch.setattr("openswap.codex.desktop_app.time.sleep", lambda _: None)
    desktop.quit()
    assert requested == [{10}]


def test_quit_timeout_never_force_kills(desktop, monkeypatch):
    exe = str(desktop.app_path / "Contents/MacOS/ChatGPT")
    monkeypatch.setattr("openswap.codex.desktop_app._processes", lambda: [_Process(10, 1, exe, exe)])
    monkeypatch.setattr(desktop, "_request_terminate", lambda _pids, _executable: None)
    monkeypatch.setattr("openswap.codex.desktop_app.time.monotonic", lambda: 1.0)
    with pytest.raises(DesktopAppError, match="still running"):
        desktop.quit(timeout=0)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_timeouts_are_rejected(desktop, value):
    with pytest.raises(DesktopAppError, match="finite"):
        desktop.quit(timeout=value)
    with pytest.raises(DesktopAppError, match="finite"):
        desktop.launch(_home(), timeout=value)


def test_launch_uses_vetted_executable_and_sanitized_environment(desktop, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("CODEX_HOME", "/wrong")
    monkeypatch.setattr(desktop, "assert_stopped", lambda: None)
    states = iter([False, True, True])
    monkeypatch.setattr(desktop, "is_running", lambda: next(states))
    monkeypatch.setattr("openswap.codex.desktop_app.time.sleep", lambda _: None)
    seen = {}
    class Started:
        def poll(self):
            return None
    def fake_popen(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return Started()
    monkeypatch.setenv("CODEX_REFRESH_TOKEN", "also-secret")
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.Popen", fake_popen)
    desktop.launch(_home())
    assert seen["argv"] == [str(desktop.app_path / "Contents/MacOS/ChatGPT")]
    assert seen["env"]["CODEX_HOME"] == str(_home())
    assert "OPENAI_API_KEY" not in seen["env"]
    assert "CODEX_REFRESH_TOKEN" not in seen["env"]
    assert "ELECTRON_RUN_AS_NODE" not in seen["env"]
    assert seen["cwd"] == str(Path.home())
    assert seen["start_new_session"] is True


def test_process_scan_uses_fixed_ps_argv(monkeypatch):
    from openswap.codex.desktop_app import _processes
    seen = {}
    def fake_run(argv, **kwargs):
        seen.setdefault("calls", []).append((argv, kwargs))
        if argv[-1] == "pid=,ppid=,comm=":
            return _ps(" 2 1 ChatGPT Helper\n")
        return _ps(" 2 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT --flag\n")
    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.run", fake_run)
    assert _processes()[0].pid == 2
    assert _processes()[0].comm == "ChatGPT Helper"
    assert seen["calls"][0][0] == ["/bin/ps", "-ww", "-axo", "pid=,ppid=,comm="]
    assert seen["calls"][1][0] == ["/bin/ps", "-ww", "-axo", "pid=,args="]
    assert all(call[1]["timeout"] == 3.0 for call in seen["calls"])


def test_observe_capability_uses_reason_codes_not_exception_text(desktop, monkeypatch):
    from openswap.codex.desktop_app import DesktopCapability

    monkeypatch.setattr("openswap.codex.desktop_app.sys.platform", "linux")
    cap = desktop.observe_capability(_home(), now=10.0)
    assert cap == DesktopCapability(state="unsupported", reason="unsupported_platform")
    assert "linux" not in cap.reason
    assert "exception" not in cap.reason

    monkeypatch.setattr("openswap.codex.desktop_app.sys.platform", "darwin")
    missing = DesktopApp(Path("/no/such/ChatGPT.app"))
    cap = missing.observe_capability(_home(), now=11.0)
    assert cap.state == "missing"
    assert cap.reason == "app_missing"
    assert "/" not in cap.reason
    assert "ChatGPT.app" not in repr(cap)

    with desktop._plist_path.open("wb") as stream:
        plistlib.dump({"CFBundleIdentifier": "evil.app", "CFBundleExecutable": "ChatGPT"}, stream)
    cap = desktop.observe_capability(_home(), now=12.0)
    assert cap.state == "invalid"
    assert cap.reason == "wrong_bundle"
    assert "evil.app" not in cap.reason


def test_observe_capability_applies_transaction_policy(desktop, tmp_path, monkeypatch):
    custom_home = tmp_path / "other-codex-home"
    cap = desktop.observe_capability(custom_home, now=20.0)
    assert cap.state == "invalid"
    assert cap.reason == "custom_home"
    assert cap.observed_at is None

    monkeypatch.setenv("CODEX_CLI_PATH", "/custom/codex")
    cap = desktop.observe_capability(_home(), now=21.0)
    assert cap.state == "invalid"
    assert cap.reason == "custom_backend"
    assert cap.observed_at is None


def test_observe_capability_classifies_by_reason_when_messages_collide(desktop, monkeypatch):
    from openswap.codex.desktop_app import DesktopCapability

    def boom(self):
        raise DesktopAppError(
            "The ChatGPT application bundle is missing or invalid.",
            reason="app_invalid",
        )

    monkeypatch.setattr(DesktopApp, "_validate", boom)
    cap = desktop.observe_capability(_home())
    assert cap == DesktopCapability(state="invalid", reason="app_invalid")


def test_transient_capability_failures_expire_for_retry(desktop, monkeypatch):
    from openswap.codex.desktop_app import capability_is_fresh

    for index, reason in enumerate((
        "process_inspect_failed", "app_changed", "signature_check_failed", "probe_failed",
    )):
        def fail_inspection(reason=reason):
            raise DesktopAppError("Operational probe failure.", reason=reason)

        monkeypatch.setattr(desktop, "is_running", fail_inspection)
        observed_at = 100.0 + index
        cap = desktop.observe_capability(_home(), now=observed_at)

        assert cap.state == "invalid"
        assert cap.reason == reason
        assert cap.observed_at == observed_at
        assert capability_is_fresh(cap, now=observed_at + 4.9) is True
        assert capability_is_fresh(cap, now=observed_at + 5.0) is False


def test_observe_capability_reports_fresh_stopped_and_running(desktop, monkeypatch):
    monkeypatch.setattr(desktop, "is_running", lambda: False)
    stopped = desktop.observe_capability(_home(), now=100.0)
    assert stopped.state == "stopped"
    assert stopped.reason == "stopped"
    assert stopped.observed_at == 100.0

    monkeypatch.setattr(desktop, "is_running", lambda: True)
    running = desktop.observe_capability(_home(), now=101.5)
    assert running.state == "running"
    assert running.reason == "running"
    assert running.observed_at == 101.5


def test_observe_capability_timestamps_after_process_inspection(desktop, monkeypatch):
    import openswap.codex.desktop_app as desktop_app_mod

    ticks = iter([1.0, 9.0])
    monkeypatch.setattr(desktop_app_mod.time, "monotonic", lambda: next(ticks))

    def slow_running():
        desktop_app_mod.time.monotonic()
        return False

    monkeypatch.setattr(desktop, "is_running", slow_running)
    cap = desktop.observe_capability(_home())
    assert cap.state == "stopped"
    assert cap.observed_at == 9.0
