import json

import pytest

from openswap.codex.desktop import DesktopSwitchError, DesktopSwitcher
from openswap.codex.desktop_app import DesktopAppError
from openswap.codex.engine import CodexEngine
from tests.test_codex_auth import _auth, _jwt


@pytest.fixture(autouse=True)
def isolate_managed_config_paths(monkeypatch):
    monkeypatch.setattr(
        "openswap.codex.desktop._managed_config_paths",
        lambda home: (home / "requirements.toml", home / "managed_config.toml"),
    )


class FakeApp:
    def __init__(self, *, running=True, quit_hook=None, launch_error=None, launch_hook=None):
        self.running = running
        self.quit_hook = quit_hook
        self.launch_error = launch_error
        self.launch_hook = launch_hook
        self.calls = []

    def preflight(self, home):
        self.calls.append(("preflight", home))
        return {"bundleId": "com.openai.codex", "version": "test"}

    def is_running(self):
        self.calls.append(("is_running",))
        return self.running

    def quit(self, timeout=20):
        self.calls.append(("quit", timeout))
        if self.quit_hook:
            self.quit_hook()
        self.running = False

    def assert_stopped(self):
        self.calls.append(("assert_stopped",))
        if self.running:
            raise DesktopAppError("still running")

    def launch(self, home, timeout=20):
        self.calls.append(("launch", home, timeout))
        if self.launch_hook:
            self.launch_hook()
        if self.launch_error:
            raise self.launch_error
        self.running = True


def setup_engine(tmp_path):
    engine = CodexEngine(backup_dir=tmp_path / "backup", home=tmp_path / "home")
    # Desktop operations must be isolated from automatic Codex rotation.
    engine.backup_dir.mkdir(parents=True, exist_ok=True)
    (engine.backup_dir / "settings.json").write_text(
        json.dumps({"autoswitch": {"codexEnabled": False}}), encoding="utf-8"
    )
    engine.home.mkdir(parents=True, exist_ok=True)
    (engine.home / "auth.json").write_text(
        _auth(email="a@x.com", account_id="acc-a", refresh="rt-a"), encoding="utf-8"
    )
    engine.add_account()
    (engine.home / "auth.json").write_text(
        _auth(email="b@x.com", account_id="acc-b", refresh="rt-b"), encoding="utf-8"
    )
    engine.add_account()
    return engine


def test_preflight_is_sanitized_and_requires_managed_oauth(tmp_path):
    engine = setup_engine(tmp_path)
    result = DesktopSwitcher(engine, FakeApp()).preflight("1")
    assert result["target"] == {"number": "1", "email": "a@x.com"}
    assert result["current"]["number"] == "2"
    assert result["experimental"] is True
    assert "rt-a" not in json.dumps(result)


def test_refuses_until_both_acknowledgements_without_app_calls(tmp_path):
    engine = setup_engine(tmp_path)
    app = FakeApp()
    switcher = DesktopSwitcher(engine, app)
    with pytest.raises(DesktopSwitchError, match="restart confirmation"):
        switcher.switch("1")
    with pytest.raises(DesktopSwitchError, match="idle"):
        switcher.switch("1", confirm_restart=True)
    assert app.calls == []


def test_switch_returns_awaiting_verification(tmp_path):
    engine = setup_engine(tmp_path)
    app = FakeApp()
    result = DesktopSwitcher(engine, app).switch(
        "1", confirm_restart=True, confirm_idle=True
    )
    assert result["status"] == "awaiting_verification"
    assert result["to"] == {"number": "1", "email": "a@x.com"}
    assert engine.current_account_number() == "1"
    assert app.running


def test_graceful_quit_preserves_refreshed_outgoing(tmp_path):
    engine = setup_engine(tmp_path)
    refreshed = _auth(
        email="b@x.com", account_id="acc-b", refresh="rt-b-new",
        last_refresh="2026-09-12T00:00:00Z",
    )
    app = FakeApp(quit_hook=lambda: engine._write_live(refreshed))
    DesktopSwitcher(engine, app).switch("1", confirm_restart=True, confirm_idle=True)
    assert engine._slot_text("2") == refreshed


def test_target_identity_mismatch_refused_before_quit(tmp_path):
    engine = setup_engine(tmp_path)
    engine._write_slot("1", _auth(email="x@x.com", account_id="acc-x"))
    app = FakeApp()
    with pytest.raises(DesktopSwitchError, match="does not match"):
        DesktopSwitcher(engine, app).switch("1", confirm_restart=True, confirm_idle=True)
    assert not any(call[0] == "quit" for call in app.calls)


def test_concurrent_target_change_after_quit_is_refused(tmp_path):
    engine = setup_engine(tmp_path)
    changed = _auth(email="a@x.com", account_id="acc-a", refresh="rt-other")
    app = FakeApp(quit_hook=lambda: engine._write_slot("1", changed))
    with pytest.raises(DesktopSwitchError, match="target credentials changed"):
        DesktopSwitcher(engine, app).switch("1", confirm_restart=True, confirm_idle=True)
    assert engine._live_slot() == "2"
    assert not any(call[0] == "launch" for call in app.calls)


def test_failed_launch_rolls_back_live_and_roster(tmp_path):
    engine = setup_engine(tmp_path)
    original = engine._live_text()
    app = FakeApp(launch_error=DesktopAppError("launch failed"))
    with pytest.raises(DesktopAppError, match="launch failed"):
        DesktopSwitcher(engine, app).switch("1", confirm_restart=True, confirm_idle=True)
    assert engine._live_text() == original
    assert engine._read_roster()["activeAccountNumber"] == "2"
    assert not app.running


def test_failed_launch_does_not_clobber_concurrent_refresh(tmp_path):
    engine = setup_engine(tmp_path)
    concurrent = _auth(email="a@x.com", account_id="acc-a", refresh="rt-fresh")
    app = FakeApp(
        launch_hook=lambda: engine._write_live(concurrent),
        launch_error=DesktopAppError("launch failed"),
    )
    with pytest.raises(DesktopSwitchError, match="auth changed"):
        DesktopSwitcher(engine, app).switch("1", confirm_restart=True, confirm_idle=True)
    assert engine._live_text() == concurrent
    assert not app.running


def test_quit_timeout_does_not_write_target(tmp_path):
    engine = setup_engine(tmp_path)
    original = engine._live_text()

    class TimeoutApp(FakeApp):
        def quit(self, timeout=20):
            raise DesktopAppError("Timed out waiting for desktop app to quit")

    with pytest.raises(DesktopAppError, match="Timed out"):
        DesktopSwitcher(engine, TimeoutApp()).switch(
            "1", confirm_restart=True, confirm_idle=True
        )
    assert engine._live_text() == original


def test_rejects_api_key_and_unsupported_store(tmp_path):
    engine = setup_engine(tmp_path)
    (engine.home / "config.toml").write_text(
        'cli_auth_credentials_store = "keyring"\n', encoding="utf-8"
    )
    with pytest.raises(DesktopSwitchError, match="only.*file"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")


def test_allows_ordinary_model_options_but_rejects_custom_provider(tmp_path):
    engine = setup_engine(tmp_path)
    config = engine.home / "config.toml"
    config.write_text(
        'model = "gpt-5"\nmodel_reasoning_effort = "high"\n'
        'model_context_window = 12345\nmodel_provider = "openai"\n'
        'sandbox_workspace_write = true\n[mcp_servers.local]\n'
        'command = "safe-tool"\nenv = { MODE = "test" }\n',
        encoding="utf-8",
    )
    assert DesktopSwitcher(engine, FakeApp()).preflight("1")["target"]["number"] == "1"
    config.write_text('model_provider = "custom"\n', encoding="utf-8")
    with pytest.raises(DesktopSwitchError, match="provider"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")


def test_rejects_auto_and_ephemeral_stores_and_api_key_target(tmp_path):
    engine = setup_engine(tmp_path)
    for mode in ("auto", "ephemeral"):
        (engine.home / "config.toml").write_text(
            f'cli_auth_credentials_store = "{mode}"\n', encoding="utf-8"
        )
        with pytest.raises(DesktopSwitchError, match="only.*file"):
            DesktopSwitcher(engine, FakeApp()).preflight("1")
    (engine.home / "config.toml").unlink()
    engine._write_slot("1", json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk-test"}))
    roster = engine._read_roster()
    roster["accounts"]["1"]["kind"] = "api_key"
    engine._write_roster(roster)
    with pytest.raises(DesktopSwitchError, match="API-key"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")


def test_requires_automatic_codex_switching_disabled(tmp_path):
    engine = setup_engine(tmp_path)
    (engine.backup_dir / "settings.json").write_text("{}", encoding="utf-8")
    with pytest.raises(DesktopSwitchError, match="automatic switching"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")


def test_refuses_unresolved_recovery_before_lifecycle_side_effects(tmp_path):
    engine = setup_engine(tmp_path)
    app = FakeApp()
    switcher = DesktopSwitcher(engine, app)
    switcher.recovery_file.write_text("unresolved", encoding="utf-8")
    with pytest.raises(DesktopSwitchError, match="unresolved desktop recovery"):
        switcher.switch("1", confirm_restart=True, confirm_idle=True)
    assert app.calls == []


def test_same_slot_uses_newer_backup_generation(tmp_path):
    engine = setup_engine(tmp_path)
    older_live = _auth(
        email="b@x.com", account_id="acc-b", refresh="rt-old",
        last_refresh="2026-09-09T00:00:00Z",
    )
    newer_slot = _auth(
        email="b@x.com", account_id="acc-b", refresh="rt-new",
        last_refresh="2026-09-12T00:00:00Z",
    )
    engine._write_live(older_live)
    engine._write_slot("2", newer_slot)
    DesktopSwitcher(engine, FakeApp()).switch(
        "2", confirm_restart=True, confirm_idle=True
    )
    assert engine._live_text() == newer_slot


def test_final_live_compare_detects_post_capture_change(tmp_path, monkeypatch):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    original = switcher._target_locked
    concurrent = _auth(email="b@x.com", account_id="acc-b", refresh="rt-race")
    calls = 0

    def validate_then_race(number):
        nonlocal calls
        calls += 1
        result = original(number)
        if calls == 2:
            engine._write_live(concurrent)
        return result

    monkeypatch.setattr(switcher, "_target_locked", validate_then_race)
    with pytest.raises(DesktopSwitchError, match="live credentials changed"):
        switcher.switch("1", confirm_restart=True, confirm_idle=True)


def test_configuration_change_during_shutdown_is_refused(tmp_path):
    engine = setup_engine(tmp_path)
    config = engine.home / "config.toml"
    config.write_text('model = "gpt-5"\n', encoding="utf-8")
    app = FakeApp(quit_hook=lambda: config.write_text('model = "gpt-6"\n', encoding="utf-8"))
    with pytest.raises(DesktopSwitchError, match="configuration.*changed"):
        DesktopSwitcher(engine, app).switch(
            "1", confirm_restart=True, confirm_idle=True
        )


def test_launch_failure_running_and_unstoppable_keeps_recovery(tmp_path):
    engine = setup_engine(tmp_path)

    class UnstoppableApp(FakeApp):
        def launch(self, home, timeout=20):
            self.running = True
            raise DesktopAppError("launch uncertain")

        def quit(self, timeout=20):
            if self.running and any(c[0] == "launch" for c in self.calls):
                raise DesktopAppError("cannot stop")
            super().quit(timeout)

    app = UnstoppableApp()
    # Preserve a launch marker for the conditional quit failure above.
    def launch(home, timeout=20):
        app.calls.append(("launch", home, timeout))
        app.running = True
        raise DesktopAppError("launch uncertain")
    app.launch = launch
    with pytest.raises(DesktopSwitchError, match="may still be running"):
        DesktopSwitcher(engine, app).switch("1", confirm_restart=True, confirm_idle=True)
    assert DesktopSwitcher(engine, app).recovery_file.exists()


def test_rollback_preserves_concurrent_roster_change(tmp_path):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())

    def roster_race():
        roster = engine._read_roster()
        roster["accounts"]["2"]["alias"] = "concurrent"
        engine._write_roster(roster)

    switcher.app.launch_hook = roster_race
    switcher.app.launch_error = DesktopAppError("failed")
    with pytest.raises(DesktopSwitchError, match="roster changed concurrently"):
        switcher.switch("1", confirm_restart=True, confirm_idle=True)
    assert engine._read_roster()["accounts"]["2"]["alias"] == "concurrent"
    assert engine.current_account_number() == "2"
    assert switcher.recovery_file.exists()


def test_malformed_and_conflicting_identity_claims_are_sanitized(tmp_path):
    engine = setup_engine(tmp_path)
    raw = json.loads(engine._slot_text("1"))
    raw["tokens"]["id_token"] = _jwt({
        "email": "a@x.com", "https://api.openai.com/auth": "not-an-object"
    })
    engine._write_slot("1", json.dumps(raw))
    with pytest.raises(DesktopSwitchError, match="malformed OAuth identity claims"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")

    raw["tokens"]["id_token"] = _jwt({
        "email": "a@x.com", "https://api.openai.com/auth": {
            "chatgpt_account_id": "different"
        }
    })
    engine._write_slot("1", json.dumps(raw))
    with pytest.raises(DesktopSwitchError, match="conflicting account identity"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")


def test_rejects_managed_requirements_and_profile_auth_override(tmp_path):
    engine = setup_engine(tmp_path)
    requirements = engine.home / "requirements.toml"
    requirements.write_text("[policy]\n", encoding="utf-8")
    with pytest.raises(DesktopSwitchError, match="managed Codex requirements"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")
    requirements.unlink()
    (engine.home / "config.toml").write_text(
        '[profiles.work]\nforced_login_method = "chatgpt"\n', encoding="utf-8"
    )
    with pytest.raises(DesktopSwitchError, match="authentication"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")


def test_recovery_status_is_sanitized_and_recover_is_explicit(tmp_path):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    source = engine._live_text()
    target = engine._slot_text("1")
    roster_before = engine.sequence_file.read_text(encoding="utf-8")
    roster = engine._read_roster()
    roster["activeAccountNumber"] = "1"
    roster_written = json.dumps(roster, indent=2)
    from openswap.codex.desktop import _atomic_text, _digest
    journal = {
        "schemaVersion": 2,
        "number": "1",
        "fromNumber": "2",
        "sourceLive": source,
        "sourceFingerprint": _digest(source),
        "targetFingerprint": _digest(target),
        "rosterBefore": roster_before,
        "rosterBeforeFingerprint": _digest(roster_before),
        "rosterWritten": roster_written,
        "rosterWrittenFingerprint": _digest(roster_written),
    }
    _atomic_text(switcher.recovery_file, json.dumps(journal))
    _atomic_text(engine.home / "auth.json", target)
    _atomic_text(engine.sequence_file, roster_written)
    status = switcher.recovery_status()
    assert status["pending"] is True
    assert "rt-" not in json.dumps(status)
    with pytest.raises(DesktopSwitchError, match="restart confirmation"):
        switcher.recover()
    result = switcher.recover(confirm_restart=True, confirm_idle=True)
    assert result["status"] == "recovered_awaiting_verification"
    assert engine._live_text() == source
    assert engine.sequence_file.read_text(encoding="utf-8") == roster_before
    assert switcher.recovery_status() == {
        "status": "none", "pending": False, "experimental": True
    }
