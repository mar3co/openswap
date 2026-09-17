import json
import io
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from openswap.codex.engine import CodexAuthError, CodexEngine, DuplicateAccountError
from openswap.codex.onboarding import LoginSession
from openswap.exceptions import ConfigError
from tests.test_codex_auth import _auth


def _engine(tmp_path):
    return CodexEngine(
        backup_dir=tmp_path / "backup",
        home=tmp_path / "live",
        codex_bin=lambda: "/bin/echo",
    )


def test_add_oauth_account_is_enabled_and_does_not_touch_live_or_active(tmp_path):
    engine = _engine(tmp_path)
    engine.home.mkdir()
    live = _auth(email="live@example.test", account_id="live-account")
    (engine.home / "auth.json").write_text(live, encoding="utf-8")
    engine.add_account()
    before_live = (engine.home / "auth.json").read_bytes()
    before_active = engine._read_roster()["activeAccountNumber"]

    number = engine.add_oauth_account(
        _auth(email="new@example.test", account_id="new-account"), alias="New"
    )

    record = engine._read_roster()["accounts"][number]
    assert record["disabled"] is False
    assert record["alias"] == "new"
    assert engine._read_roster()["activeAccountNumber"] == before_active
    assert (engine.home / "auth.json").read_bytes() == before_live
    assert number in engine.switchable_account_numbers()


def test_add_oauth_rejects_invalid_and_duplicate_without_overwrite(tmp_path):
    engine = _engine(tmp_path)
    with pytest.raises(CodexAuthError, match="OAuth"):
        engine.add_oauth_account(json.dumps({"tokens": {"access_token": "secret"}}))
    text = _auth(email="same@example.test", account_id="same-account", refresh="first")
    number = engine.add_oauth_account(text)
    original = engine._slot_text(number)
    with pytest.raises(DuplicateAccountError, match="already saved"):
        engine.add_oauth_account(
            _auth(email="same@example.test", account_id="same-account", refresh="second")
        )
    assert engine._slot_text(number) == original
    assert "second" not in engine._slot_text(number)


def test_add_oauth_does_not_adopt_untracked_slot_directory(tmp_path):
    engine = _engine(tmp_path)
    orphan = engine.slots_dir / "1"
    orphan.mkdir(parents=True)
    (orphan / "auth.json").write_text("untouched", encoding="utf-8")
    number = engine.add_oauth_account(
        _auth(email="new@example.test", account_id="new-account")
    )
    assert number == "2"
    assert (orphan / "auth.json").read_text(encoding="utf-8") == "untouched"


def test_add_oauth_refuses_malformed_roster_without_overwriting_it(tmp_path):
    engine = _engine(tmp_path)
    engine.sequence_file.write_text("{broken", encoding="utf-8")
    with pytest.raises(ConfigError, match="roster is unreadable"):
        engine.add_oauth_account(_auth(email="new@example.test", account_id="new-account"))
    assert engine.sequence_file.read_text(encoding="utf-8") == "{broken"


@pytest.mark.parametrize(
    "roster",
    [
        {"accounts": {"1": "not-a-record"}, "sequence": [1]},
        {"accounts": {}, "sequence": "1"},
        {"accounts": {}, "sequence": [{"number": 1}]},
    ],
)
def test_add_oauth_refuses_invalid_roster_shapes_without_slot_write(tmp_path, roster):
    engine = _engine(tmp_path)
    engine.sequence_file.write_text(json.dumps(roster), encoding="utf-8")
    before = engine.sequence_file.read_bytes()
    with pytest.raises(ConfigError, match="roster is malformed"):
        engine.add_oauth_account(_auth(email="new@example.test", account_id="new-account"))
    assert engine.sequence_file.read_bytes() == before
    assert not engine.slots_dir.exists()


def test_automatic_switch_refuses_disabled_target_under_commit_lock(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    number = engine.add_oauth_account(_auth(email="new@example.test", account_id="new-account"))
    engine.set_account_disabled(number, True)
    monkeypatch.setattr("openswap.settings.load_settings", lambda _root: type("S", (), {"codex_enabled": True})())
    result = engine.switch_to(number, automatic=True, json_output=True)
    assert result["reason"] == "account-disabled"
    assert not (engine.home / "auth.json").exists()


class _SuccessfulProcess:
    pid = 123456789
    returncode = 0

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        home = Path(kwargs["env"]["CODEX_HOME"])
        (home / "auth.json").write_text(
            _auth(email="new@example.test", account_id="new-account"), encoding="utf-8"
        )
        self.stdout = io.BytesIO(
            b"Open https://auth.openai.com/oauth/authorize?state=top-secret"
        )

    def poll(self):
        return self.returncode


def test_session_keeps_url_private_sanitizes_environment_and_saves(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-inherit")
    made = []

    def popen(argv, **kwargs):
        proc = _SuccessfulProcess(argv, **kwargs)
        made.append(proc)
        return proc

    session = LoginSession(engine, codex_bin=sys.executable, popen=popen)
    session.run()

    public = session.state()
    assert public == {
        "stage": "ready", "email": "new@example.test", "plan": "plus",
        "account_id": "new-account", "has_url": True, "mode": "browser",
    }
    assert "top-secret" not in repr(public)
    assert session.login_url().startswith("https://auth.openai.com/")
    assert "OPENAI_API_KEY" not in made[0].kwargs["env"]
    assert made[0].kwargs["cwd"] == made[0].kwargs["env"]["CODEX_HOME"]
    assert made[0].kwargs["env"]["CODEX_HOME"] != str(engine.home)
    temp_home = Path(made[0].kwargs["env"]["CODEX_HOME"])
    number = session.save()
    assert engine._read_roster()["accounts"][number]["disabled"] is False
    assert session.state() == {"stage": "saved", "has_url": False, "mode": "browser"}
    assert not temp_home.exists()


class _HangingProcess:
    pid = 123456788
    returncode = None

    def __init__(self):
        self.stdout = io.BytesIO(b"secret-token")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = -15
        return self.returncode


def test_cancelled_session_has_no_secret_and_removes_temp_home(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    made = []

    def popen(*_args, **kwargs):
        made.append((Path(kwargs["env"]["CODEX_HOME"]), _HangingProcess()))
        return made[-1][1]

    # Avoid signalling the synthetic PID while still exercising ownership and cleanup.
    monkeypatch.setattr(LoginSession, "_terminate", lambda _self, proc: setattr(proc, "returncode", -15))
    session = LoginSession(engine, codex_bin=sys.executable, popen=popen, timeout=10)
    thread = threading.Thread(target=session.run)
    thread.start()
    deadline = time.time() + 2
    while session.state()["stage"] == "starting" and time.time() < deadline:
        time.sleep(0.005)
    session.cancel()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert session.state() == {
        "stage": "cancelled", "has_url": False, "mode": "browser"
    }
    assert "secret-token" not in repr(session.state())
    assert not made[0][0].exists()


def test_unofficial_login_url_is_never_exposed(tmp_path):
    session = LoginSession(_engine(tmp_path), codex_bin="/bin/echo", popen=_SuccessfulProcess)
    session._inspect_output("Open https://auth.openai.com.evil.test/oauth?secret=x")
    assert session.login_url() is None


def test_device_output_strips_ansi_and_parses_one_time_code(tmp_path):
    session = LoginSession(_engine(tmp_path), mode="device", codex_bin="/bin/echo")
    session._public = {"stage": "waiting", "has_url": False}
    session._inspect_output(
        "\x1b[32mEnter this one-time code:\x1b[0m\nABCD-EFGH\n"
        "https://auth.openai.com/codex/device"
    )
    assert session.state() == {
        "stage": "waiting", "has_url": True, "device_code": "ABCD-EFGH",
        "mode": "device",
    }


def test_streaming_reader_surfaces_short_flushed_url_before_child_exits(tmp_path):
    # The child and session outlive the generous poll deadline below (loaded CI
    # runners start Python slowly); cancel() ends both at once when the URL shows.
    session = LoginSession(_engine(tmp_path), codex_bin="/bin/echo", timeout=30)
    session._public = {"stage": "waiting", "has_url": False}
    proc = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys,time; "
            "print('https://auth.openai.com/oauth/authorize?state=private', flush=True); "
            "time.sleep(30)"
        )],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    session._process = proc
    thread = threading.Thread(target=session._wait, args=(proc,))
    thread.start()
    deadline = time.time() + 10
    while not session.state()["has_url"] and time.time() < deadline:
        time.sleep(0.01)
    try:
        assert session.state()["has_url"] is True
        assert proc.poll() is None
    finally:  # never leave the long-lived child behind, even on failure
        session.cancel()
        thread.join(timeout=10)
    assert not thread.is_alive()
