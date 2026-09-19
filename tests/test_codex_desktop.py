import json
import os
import stat

import pytest

from openswap.codex import desktop
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


@pytest.mark.parametrize("failed_path", ["auth", "roster"])
def test_post_replace_durability_failure_rolls_back_exact_writes(
    tmp_path, monkeypatch, failed_path
):
    engine = setup_engine(tmp_path)
    original_live = engine._live_text()
    original_roster = engine.sequence_file.read_text(encoding="utf-8")
    switcher = DesktopSwitcher(engine, FakeApp())
    atomic_text = desktop._atomic_text
    failed = False

    def fail_after_replace(path, text):
        nonlocal failed
        atomic_text(path, text)
        if (not failed and (
                (failed_path == "auth" and path == engine.home / "auth.json")
                or (failed_path == "roster" and path == engine.sequence_file))):
            failed = True
            raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(desktop, "_atomic_text", fail_after_replace)
    with pytest.raises(DesktopSwitchError, match="Original credentials were restored"):
        switcher.switch("1", confirm_restart=True, confirm_idle=True)

    assert engine._live_text() == original_live
    assert engine.sequence_file.read_text(encoding="utf-8") == original_roster
    assert not switcher.recovery_file.exists()


def test_failure_before_auth_replace_leaves_credentials_unchanged(tmp_path, monkeypatch):
    engine = setup_engine(tmp_path)
    original_live = engine._live_text()
    original_roster = engine.sequence_file.read_text(encoding="utf-8")
    switcher = DesktopSwitcher(engine, FakeApp())
    atomic_text = desktop._atomic_text

    def fail_before_replace(path, text):
        if path == engine.home / "auth.json":
            raise OSError("synthetic pre-replace failure")
        atomic_text(path, text)

    monkeypatch.setattr(desktop, "_atomic_text", fail_before_replace)
    with pytest.raises(DesktopSwitchError, match="Credentials were not changed"):
        switcher.switch("1", confirm_restart=True, confirm_idle=True)

    assert engine._live_text() == original_live
    assert engine.sequence_file.read_text(encoding="utf-8") == original_roster
    assert not switcher.recovery_file.exists()


@pytest.mark.skipif(os.name == "nt", reason="directory fsync is POSIX-only")
def test_real_directory_fsync_failure_after_auth_replace_is_reconciled(
    tmp_path, monkeypatch
):
    engine = setup_engine(tmp_path)
    original_live = engine._live_text()
    switcher = DesktopSwitcher(engine, FakeApp())
    fsync = os.fsync
    directory_fsyncs = 0

    def fail_auth_directory_fsync(fd):
        nonlocal directory_fsyncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_fsyncs += 1
            if directory_fsyncs == 2:
                raise OSError("synthetic auth directory fsync failure")
        return fsync(fd)

    monkeypatch.setattr(desktop.os, "fsync", fail_auth_directory_fsync)
    with pytest.raises(DesktopSwitchError, match="Original credentials were restored"):
        switcher.switch("1", confirm_restart=True, confirm_idle=True)

    assert engine._live_text() == original_live
    assert engine.current_account_number() == "2"
    assert not switcher.recovery_file.exists()


def test_recovery_journal_durability_failure_retains_actionable_record(
    tmp_path, monkeypatch
):
    engine = setup_engine(tmp_path)
    original_live = engine._live_text()
    switcher = DesktopSwitcher(engine, FakeApp())
    atomic_text = desktop._atomic_text

    def fail_after_journal_replace(path, text):
        atomic_text(path, text)
        if path == switcher.recovery_file:
            raise OSError("synthetic recovery directory fsync failure")

    monkeypatch.setattr(desktop, "_atomic_text", fail_after_journal_replace)
    with pytest.raises(DesktopSwitchError, match="recovery-status"):
        switcher.switch("1", confirm_restart=True, confirm_idle=True)

    assert engine._live_text() == original_live
    assert switcher.recovery_file.exists()
    assert switcher.recovery_status()["pending"] is True


def test_post_replace_auth_failure_preserves_concurrent_login_and_recovery(
    tmp_path, monkeypatch
):
    engine = setup_engine(tmp_path)
    concurrent = _auth(email="a@x.com", account_id="acc-a", refresh="rt-concurrent")
    switcher = DesktopSwitcher(engine, FakeApp())
    atomic_text = desktop._atomic_text

    def race_after_replace(path, text):
        atomic_text(path, text)
        if path == engine.home / "auth.json":
            engine._write_live(concurrent)
            raise OSError("synthetic directory fsync failure")

    monkeypatch.setattr(desktop, "_atomic_text", race_after_replace)
    with pytest.raises(DesktopSwitchError, match="rollback was withheld"):
        switcher.switch("1", confirm_restart=True, confirm_idle=True)

    assert engine._live_text() == concurrent
    assert switcher.recovery_file.exists()


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


@pytest.mark.parametrize("mode", ["keyring", "auto", "ephemeral"])
def test_rejects_non_file_credential_store(tmp_path, mode):
    engine = setup_engine(tmp_path)
    (engine.home / "config.toml").write_text(
        f'cli_auth_credentials_store = "{mode}"\n', encoding="utf-8"
    )
    with pytest.raises(DesktopSwitchError, match="only.*file"):
        DesktopSwitcher(engine, FakeApp()).preflight("1")


def test_rejects_api_key_target(tmp_path):
    engine = setup_engine(tmp_path)
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
            # The launch marker drives the conditional quit failure below.
            self.calls.append(("launch", home, timeout))
            self.running = True
            raise DesktopAppError("launch uncertain")

        def quit(self, timeout=20):
            if self.running and any(c[0] == "launch" for c in self.calls):
                raise DesktopAppError("cannot stop")
            super().quit(timeout)

    app = UnstoppableApp()
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


def _journal(engine, switcher, **overrides):
    """Leave a 2 -> 1 switch interrupted after both writes; return its journal."""
    source = engine._live_text()
    target = engine._slot_text("1")
    roster_before = engine.sequence_file.read_text(encoding="utf-8")
    roster = engine._read_roster()
    roster["activeAccountNumber"] = "1"
    roster_written = json.dumps(roster, indent=2)
    journal = {
        "schemaVersion": 2,
        "number": "1",
        "fromNumber": "2",
        "sourceLive": source,
        "sourceFingerprint": desktop._digest(source),
        "targetFingerprint": desktop._digest(target),
        "rosterBefore": roster_before,
        "rosterBeforeFingerprint": desktop._digest(roster_before),
        "rosterWritten": roster_written,
        "rosterWrittenFingerprint": desktop._digest(roster_written),
        **overrides,
    }
    desktop._atomic_text(switcher.recovery_file, json.dumps(journal))
    desktop._atomic_text(engine.home / "auth.json", target)
    desktop._atomic_text(engine.sequence_file, roster_written)
    return journal


def _recovery_state(engine, switcher):
    return (
        engine._live_text(),
        engine.sequence_file.read_text(encoding="utf-8"),
        switcher.recovery_file.read_text(encoding="utf-8"),
    )


def _assert_recover_refused(engine, switcher, match):
    """A refused recovery leaves live auth, roster, and journal untouched."""
    before = _recovery_state(engine, switcher)
    with pytest.raises(DesktopSwitchError, match=match):
        switcher.recover(confirm_restart=True, confirm_idle=True)
    assert _recovery_state(engine, switcher) == before
    assert not any(call[0] == "launch" for call in switcher.app.calls)


def test_recovery_status_is_sanitized_and_recover_is_explicit(tmp_path):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    journal = _journal(engine, switcher)
    status = switcher.recovery_status()
    assert status["pending"] is True
    assert "rt-" not in json.dumps(status)
    with pytest.raises(DesktopSwitchError, match="restart confirmation"):
        switcher.recover()
    result = switcher.recover(confirm_restart=True, confirm_idle=True)
    assert result["status"] == "recovered_awaiting_verification"
    assert engine._live_text() == journal["sourceLive"]
    assert engine.sequence_file.read_text(encoding="utf-8") == journal["rosterBefore"]
    assert switcher.recovery_status() == {
        "status": "none", "pending": False, "experimental": True
    }


_FOREIGN_SOURCE = _auth(email="a@x.com", account_id="acc-a", refresh="rt-foreign")


@pytest.mark.parametrize("overrides, match", [
    ({"schemaVersion": 1}, "integrity validation"),
    ({"number": "01"}, "integrity validation"),
    ({"number": "\u00b2"}, "integrity validation"),
    ({"targetFingerprint": "not-a-digest"}, "integrity validation"),
    ({"sourceLive": "{}"}, "integrity validation"),
    ({"rosterBefore": "{}"}, "integrity validation"),
    ({"rosterWritten": "{}"}, "integrity validation"),
    # Re-fingerprinted credentials of an account the transaction never left.
    ({"sourceLive": _FOREIGN_SOURCE,
      "sourceFingerprint": desktop._digest(_FOREIGN_SOURCE)}, "identities are inconsistent"),
])
def test_recover_refuses_tampered_journal_before_lifecycle(tmp_path, overrides, match):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    _journal(engine, switcher, **overrides)
    with pytest.raises(DesktopSwitchError, match=match):
        switcher.recovery_status()
    _assert_recover_refused(engine, switcher, match)
    assert switcher.app.calls == []


def test_recover_refuses_malformed_journal_before_lifecycle(tmp_path):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    _journal(engine, switcher)
    switcher.recovery_file.write_text("{truncated", encoding="utf-8")
    _assert_recover_refused(engine, switcher, "recovery record is malformed")
    assert switcher.app.calls == []


@pytest.mark.parametrize("live, match", [
    (_auth(email="c@x.com", account_id="acc-c", refresh="rt-c"), "unrelated to this recovery"),
    (_auth(email="b@x.com", account_id="acc-b", refresh="rt-b-old",
           last_refresh="2026-09-09T00:00:00Z"), "cannot be proven newer"),
    (_auth(email="b@x.com", account_id="acc-b", refresh="rt-b-undated",
           last_refresh=None), "cannot be proven newer"),
], ids=["unrelated", "older", "undated"])
def test_recover_never_overwrites_unproven_live_login(tmp_path, live, match):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    _journal(engine, switcher)
    engine._write_live(live)
    _assert_recover_refused(engine, switcher, match)
    assert not switcher.app.running


def test_recover_keeps_newer_generation_of_source_login(tmp_path):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    journal = _journal(engine, switcher)
    refreshed = _auth(
        email="b@x.com", account_id="acc-b", refresh="rt-b-new",
        last_refresh="2026-09-12T00:00:00Z",
    )
    engine._write_live(refreshed)
    switcher.recover(confirm_restart=True, confirm_idle=True)
    assert engine._live_text() == refreshed
    assert engine.sequence_file.read_text(encoding="utf-8") == journal["rosterBefore"]
    assert not switcher.recovery_file.exists()


def test_recover_preserves_roster_changed_outside_transaction(tmp_path):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    _journal(engine, switcher)
    roster = engine._read_roster()
    roster["accounts"]["2"]["alias"] = "concurrent"
    engine._write_roster(roster)
    _assert_recover_refused(engine, switcher, "roster changed outside")
    assert not switcher.app.running


def test_recover_relaunch_failure_keeps_journal_for_retry(tmp_path):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp(launch_error=DesktopAppError("launch failed")))
    journal = _journal(engine, switcher)
    journal_text = switcher.recovery_file.read_text(encoding="utf-8")
    with pytest.raises(DesktopSwitchError, match="relaunch failed. The app is stopped"):
        switcher.recover(confirm_restart=True, confirm_idle=True)
    restored = (journal["sourceLive"], journal["rosterBefore"], journal_text)
    assert _recovery_state(engine, switcher) == restored
    assert not switcher.app.running

    switcher.app.launch_error = None
    result = switcher.recover(confirm_restart=True, confirm_idle=True)
    assert result["status"] == "recovered_awaiting_verification"
    assert not switcher.recovery_file.exists()
    assert engine._live_text() == journal["sourceLive"]
    assert engine.sequence_file.read_text(encoding="utf-8") == journal["rosterBefore"]


def test_recover_relaunch_failure_with_unstoppable_app_keeps_journal(tmp_path):
    engine = setup_engine(tmp_path)

    def cannot_stop():
        raise DesktopAppError("cannot stop")

    app = FakeApp(running=False, quit_hook=cannot_stop,
                  launch_error=DesktopAppError("launch uncertain"))
    app.launch_hook = lambda: setattr(app, "running", True)
    switcher = DesktopSwitcher(engine, app)
    journal = _journal(engine, switcher)
    with pytest.raises(DesktopSwitchError, match="may still be running"):
        switcher.recover(confirm_restart=True, confirm_idle=True)
    assert engine._live_text() == journal["sourceLive"]
    assert switcher.recovery_status()["pending"] is True


@pytest.mark.parametrize("roster_written", [False, True])
def test_recover_does_not_rewrite_live_already_equal_to_source(
    tmp_path, monkeypatch, roster_written
):
    engine = setup_engine(tmp_path)
    switcher = DesktopSwitcher(engine, FakeApp())
    journal = _journal(engine, switcher)
    engine._write_live(journal["sourceLive"])
    if not roster_written:
        engine.sequence_file.write_text(journal["rosterBefore"], encoding="utf-8")
    atomic_text = desktop._atomic_text
    written = []

    def record_write(path, text):
        written.append(path)
        atomic_text(path, text)

    monkeypatch.setattr(desktop, "_atomic_text", record_write)
    result = switcher.recover(confirm_restart=True, confirm_idle=True)
    assert result["status"] == "recovered_awaiting_verification"
    assert written == ([engine.sequence_file] if roster_written else [])
    assert engine._live_text() == journal["sourceLive"]
    assert engine.sequence_file.read_text(encoding="utf-8") == journal["rosterBefore"]
    assert not switcher.recovery_file.exists()
