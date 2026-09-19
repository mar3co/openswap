from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from openswap.codex.desktop_app import DesktopApp, DesktopAppError, _Process, _argv0
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


def test_preflight_rejects_unvalidated_version(desktop):
    with desktop._plist_path.open("wb") as stream:
        plistlib.dump({
            "CFBundleIdentifier": "com.openai.codex",
            "CFBundleExecutable": "ChatGPT",
            "CFBundleShortVersionString": "future",
            "CFBundleVersion": "9999",
        }, stream)
    with pytest.raises(DesktopAppError, match="has not been validated"):
        desktop.preflight(_home())


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
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        stderr = "Identifier=com.openai.codex\nTeamIdentifier=2DC432GLL2\n" if "-d" in argv else ""
        return subprocess.CompletedProcess(argv, 0, "", stderr)
    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.run", fake_run)
    app.preflight(_home())
    app.preflight(_home())
    assert len(calls) == 2
    assert calls[0][0][:5] == ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app.app_path)]
    assert all(call[1]["timeout"] == 15.0 for call in calls)
    app._bundled_cli.write_text("changed")
    app._bundled_cli.chmod(0o700)
    app.preflight(_home())
    assert len(calls) == 4


def test_signature_rejects_wrong_team(tmp_path, monkeypatch):
    monkeypatch.setattr("openswap.codex.desktop_app.sys.platform", "darwin")
    monkeypatch.setattr("openswap.codex.desktop_app.Path.home", lambda: tmp_path)
    app = DesktopApp(_app(tmp_path))
    def fake_run(argv, **kwargs):
        stderr = "Identifier=com.openai.codex\nTeamIdentifier=NOT-OPENAI\n" if "-d" in argv else ""
        return subprocess.CompletedProcess(argv, 0, "", stderr)
    monkeypatch.setattr("openswap.codex.desktop_app.subprocess.run", fake_run)
    with pytest.raises(DesktopAppError, match="expected publisher"):
        app.preflight(_home())


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
