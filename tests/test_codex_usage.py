import io, json
from pathlib import Path
import pytest
from openswap.codex.usage import CodexUsageError, rate_limits_to_usage, read_rate_limits

_NOW = 1_788_000_000.0

def test_rate_limits_to_usage_maps_windows_by_duration():
    payload = {
        "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": 1788265323},
        "secondary": {"usedPercent": 61, "windowDurationMins": 10080, "resetsAt": 1788765541},
    }
    usage = rate_limits_to_usage(payload, _NOW)
    assert usage["five_hour"]["pct"] == 42.0
    assert usage["five_hour"]["resets_at"] == "2026-09-01T12:22:03+00:00"
    assert "countdown" in usage["five_hour"] and "clock" in usage["five_hour"]
    assert usage["seven_day"]["pct"] == 61.0

def test_rate_limits_to_usage_tolerates_missing_secondary():
    usage = rate_limits_to_usage({"primary": {"usedPercent": 5, "windowDurationMins": 300}, "secondary": None}, _NOW)
    assert usage == {"five_hour": {"pct": 5.0}}

def test_rate_limits_to_usage_weekly_only_does_not_invent_five_hour():
    usage = rate_limits_to_usage(
        {
            "primary": {
                "usedPercent": 46,
                "windowDurationMins": 10080,
                "resetsAt": 1790216227,
            },
            "secondary": None,
        },
        _NOW,
    )
    assert usage is not None
    assert "five_hour" not in usage
    assert usage["seven_day"]["pct"] == 46.0

def test_rate_limits_to_usage_none_when_empty():
    assert rate_limits_to_usage({}, _NOW) is None
    assert rate_limits_to_usage({"primary": None, "secondary": None}, _NOW) is None


class FakeProc:
    """Scripted app-server: records stdin lines, replays stdout lines."""
    def __init__(self, replies: list[str]):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line + "\n" for line in replies))
        self.terminated = False
        self.returncode = None
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): self.returncode = 0; return 0
    def kill(self): self.terminated = True

def test_read_rate_limits_handshake_then_request(tmp_path: Path):
    limits = {"primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 1}, "secondary": None}
    proc = FakeProc([
        json.dumps({"method": "serverNotice", "params": {}}),          # noise before the reply
        json.dumps({"id": 1, "result": {"userAgent": "codex"}}),
        json.dumps({"id": 2, "result": {"rateLimits": limits}}),
    ])
    captured = {}
    def popen(argv, **kwargs):
        captured["argv"] = argv; captured["env"] = kwargs["env"]; return proc
    result = read_rate_limits(
        tmp_path,
        codex_bin="/opt/codex",
        popen=popen,
        environ={"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-env"},
    )
    assert result == limits
    assert captured["argv"] == ["/opt/codex", "app-server"]
    assert captured["env"]["CODEX_HOME"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in captured["env"]
    sent = [json.loads(l) for l in proc.stdin.getvalue().splitlines()]
    assert sent[0]["method"] == "initialize" and sent[0]["id"] == 1
    assert sent[1] == {"method": "initialized"}
    assert sent[2]["method"] == "account/rateLimits/read" and sent[2]["id"] == 2
    assert proc.terminated

def test_read_rate_limits_error_reply_raises(tmp_path: Path):
    proc = FakeProc([json.dumps({"id": 1, "result": {}}), json.dumps({"id": 2, "error": {"message": "unauthorized"}})])
    with pytest.raises(CodexUsageError, match="unauthorized"):
        read_rate_limits(tmp_path, codex_bin="/opt/codex", popen=lambda *a, **k: proc)

def test_read_rate_limits_eof_raises(tmp_path: Path):
    proc = FakeProc([])
    with pytest.raises(CodexUsageError):
        read_rate_limits(tmp_path, codex_bin="/opt/codex", popen=lambda *a, **k: proc)
