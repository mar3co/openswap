"""Tests for the API key validity probe and its cache (issue #97)."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from openswap import api_key_health
from openswap.api_key_health import (
    OK_TTL_S,
    PRUNE_AFTER_S,
    RETRY_TTL_S,
    VERDICT_NO_CREDIT,
    VERDICT_OK,
    VERDICT_REVOKED,
    KeyHealth,
    classify_error,
)
from openswap.json_output import (
    USAGE_API_KEY,
    USAGE_API_KEY_NO_CREDIT,
    USAGE_API_KEY_REVOKED,
    usage_fields,
)

KEY = "sk-ant-api03-" + "a1b2c3d4e5" * 4
OTHER_KEY = "sk-ant-api03-" + "z9y8x7w6v5" * 4
CREDIT_MESSAGE = (
    "Your credit balance is too low to access the Anthropic API. "
    "Please go to Plans & Billing to upgrade or purchase credits."
)


class Clock:
    def __init__(self, now: float = 1_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def _http_error(status: int, message: str = "") -> urllib.error.HTTPError:
    body = json.dumps({"type": "error", "error": {"type": "x", "message": message}})
    return urllib.error.HTTPError(
        api_key_health.COUNT_TOKENS_URL, status, "err", {}, io.BytesIO(body.encode())
    )


class TestClassifyError:
    def test_401_is_revoked(self):
        assert classify_error(401, "invalid x-api-key") == VERDICT_REVOKED

    @pytest.mark.parametrize("status", [400, 402, 403])
    def test_credit_message_is_no_credit(self, status):
        assert classify_error(status, CREDIT_MESSAGE) == VERDICT_NO_CREDIT

    @pytest.mark.parametrize(
        "status, message",
        [
            (400, "model: field required"),
            (404, "model not found"),
            (429, "rate limited"),
            (500, "overloaded"),
            (529, CREDIT_MESSAGE),
        ],
    )
    def test_anything_else_proves_nothing(self, status, message):
        assert classify_error(status, message) is None


@pytest.mark.no_api_key_probe_fake
class TestProbe:
    def test_success_is_ok_and_sends_the_key_only_to_count_tokens(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"input_tokens": 9}'

        def urlopen(req, timeout):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["key"] = req.get_header("X-api-key")
            seen["timeout"] = timeout
            return Resp()

        with patch("urllib.request.urlopen", urlopen):
            assert api_key_health.probe(KEY + "\n") == VERDICT_OK
        assert seen == {
            "url": "https://api.anthropic.com/v1/messages/count_tokens",
            "method": "POST",
            "key": KEY,
            "timeout": api_key_health.PROBE_TIMEOUT_S,
        }

    def test_401_is_revoked(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(401, "invalid x-api-key")):
            assert api_key_health.probe(KEY) == VERDICT_REVOKED

    def test_credit_error_is_no_credit(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(400, CREDIT_MESSAGE)):
            assert api_key_health.probe(KEY) == VERDICT_NO_CREDIT

    def test_network_failure_is_unknown(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            assert api_key_health.probe(KEY) is None

    def test_unreadable_error_body_is_unknown(self):
        err = urllib.error.HTTPError(
            api_key_health.COUNT_TOKENS_URL, 400, "err", {}, io.BytesIO(b"<html>")
        )
        with patch("urllib.request.urlopen", side_effect=err):
            assert api_key_health.probe(KEY) is None


class TestKeyHealth:
    def _health(self, tmp_path, results, clock=None):
        calls = []

        def prober(key):
            calls.append(key)
            return results.pop(0)

        return KeyHealth(tmp_path, prober=prober, clock=clock or Clock()), calls

    def test_new_key_is_due_and_unknown(self, tmp_path):
        health, _ = self._health(tmp_path, [])
        assert health.due(KEY)
        assert health.verdict(KEY) is None

    def test_check_records_the_verdict(self, tmp_path):
        health, calls = self._health(tmp_path, [VERDICT_REVOKED])
        assert health.check(KEY) == VERDICT_REVOKED
        assert health.verdict(KEY) == VERDICT_REVOKED
        assert health.verdict(OTHER_KEY) is None
        assert calls == [KEY]

    def test_healthy_key_waits_the_long_ttl(self, tmp_path):
        clock = Clock()
        health, _ = self._health(tmp_path, [VERDICT_OK], clock)
        health.check(KEY)
        clock.now += OK_TTL_S - 1
        assert not health.due(KEY)
        clock.now += 1
        assert health.due(KEY)

    @pytest.mark.parametrize("verdict", [VERDICT_REVOKED, VERDICT_NO_CREDIT, None])
    def test_dead_or_unknown_key_retries_sooner(self, tmp_path, verdict):
        clock = Clock()
        health, _ = self._health(tmp_path, [verdict], clock)
        health.check(KEY)
        clock.now += RETRY_TTL_S - 1
        assert not health.due(KEY)
        clock.now += 1
        assert health.due(KEY)

    def test_inconclusive_probe_keeps_the_previous_verdict(self, tmp_path):
        clock = Clock()
        health, _ = self._health(tmp_path, [VERDICT_NO_CREDIT, None], clock)
        health.check(KEY)
        clock.now += RETRY_TTL_S
        assert health.check(KEY) == VERDICT_NO_CREDIT
        assert health.verdict(KEY) == VERDICT_NO_CREDIT
        assert not health.due(KEY)

    def test_clean_probe_clears_a_dead_verdict(self, tmp_path):
        clock = Clock()
        health, _ = self._health(tmp_path, [VERDICT_NO_CREDIT, VERDICT_OK], clock)
        health.check(KEY)
        clock.now += RETRY_TTL_S
        assert health.check(KEY) == VERDICT_OK
        assert health.verdict(KEY) == VERDICT_OK

    def test_cache_never_holds_the_key(self, tmp_path):
        health, _ = self._health(tmp_path, [VERDICT_REVOKED])
        health.check(KEY)
        text = (tmp_path / api_key_health.CACHE_NAME).read_text()
        assert KEY not in text
        assert KEY[-20:] not in text

    def test_old_entries_are_pruned_on_write(self, tmp_path):
        clock = Clock()
        health, _ = self._health(tmp_path, [VERDICT_OK, VERDICT_OK], clock)
        health.check(OTHER_KEY)
        clock.now += PRUNE_AFTER_S
        health.check(KEY)
        assert health.verdict(OTHER_KEY) is None
        assert health.verdict(KEY) == VERDICT_OK

    def test_corrupt_cache_reads_as_empty(self, tmp_path):
        (tmp_path / api_key_health.CACHE_NAME).write_text("{not json")
        health, _ = self._health(tmp_path, [VERDICT_OK])
        assert health.due(KEY)
        assert health.check(KEY) == VERDICT_OK


class TestSentinels:
    def test_usage_fields_maps_dead_keys(self):
        assert usage_fields(USAGE_API_KEY_REVOKED) == ("api_key_revoked", None)
        assert usage_fields(USAGE_API_KEY_NO_CREDIT) == ("api_key_no_credit", None)

    def test_sentinel_notes_name_the_failure(self):
        from openswap.engine.notes import SENTINEL_NOTES

        assert SENTINEL_NOTES[USAGE_API_KEY_REVOKED].startswith("API key revoked")
        assert SENTINEL_NOTES[USAGE_API_KEY_NO_CREDIT].startswith("API key out of credit")


class TestCollector:
    """The collector overlays a failed probe on the plain "api key" sentinel."""

    def _switcher(self):
        from openswap.models import Platform
        from openswap.switcher import ClaudeAccountSwitcher

        s = ClaudeAccountSwitcher()
        s.platform = Platform.LINUX
        s._setup_directories()
        s._init_sequence_file()
        return s

    def _info(self, key=KEY):
        return [(2, "api-key-2@token.local", "", "", False, key, "")]

    @pytest.mark.parametrize(
        "verdict, sentinel",
        [
            (VERDICT_REVOKED, USAGE_API_KEY_REVOKED),
            (VERDICT_NO_CREDIT, USAGE_API_KEY_NO_CREDIT),
            (VERDICT_OK, USAGE_API_KEY),
            (None, USAGE_API_KEY),
        ],
    )
    def test_verdict_becomes_the_sentinel(self, temp_home, monkeypatch, verdict, sentinel):
        monkeypatch.setattr(api_key_health, "probe", lambda key: verdict)
        s = self._switcher()
        entries = s._collect_usage_entries(self._info())
        assert entries["2"].sentinel == sentinel
        assert entries["2"].decision_value() == sentinel

    def test_no_fetch_pass_reads_the_cache_without_probing(self, temp_home, monkeypatch):
        calls = []

        def probe(key):
            calls.append(key)
            return VERDICT_REVOKED

        monkeypatch.setattr(api_key_health, "probe", probe)
        s = self._switcher()
        assert s._collect_usage_entries(self._info(), fetch=set())["2"].sentinel == USAGE_API_KEY
        assert calls == []
        s._collect_usage_entries(self._info())
        assert calls == [KEY]
        assert (
            s._collect_usage_entries(self._info(), fetch=set())["2"].sentinel
            == USAGE_API_KEY_REVOKED
        )

    def test_probe_waits_for_its_ttl(self, temp_home, monkeypatch):
        calls = []
        monkeypatch.setattr(api_key_health, "probe", lambda key: calls.append(key) or VERDICT_OK)
        s = self._switcher()
        s._collect_usage_entries(self._info())
        s._collect_usage_entries(self._info())
        assert calls == [KEY]

    def test_replaced_key_is_probed_afresh(self, temp_home, monkeypatch):
        verdicts = {KEY: VERDICT_REVOKED, OTHER_KEY: VERDICT_OK}
        monkeypatch.setattr(api_key_health, "probe", lambda key: verdicts[key])
        s = self._switcher()
        assert s._collect_usage_entries(self._info())["2"].sentinel == USAGE_API_KEY_REVOKED
        assert s._collect_usage_entries(self._info(OTHER_KEY))["2"].sentinel == USAGE_API_KEY

    def test_oauth_slot_is_never_probed(self, temp_home, monkeypatch):
        calls = []
        monkeypatch.setattr(api_key_health, "probe", lambda key: calls.append(key))
        s = self._switcher()
        oauth_json = json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": ""}})
        s._collect_usage_entries([(1, "a@example.com", "", "", False, oauth_json, "")])
        assert calls == []
