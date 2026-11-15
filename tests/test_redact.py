"""Recognizing credentials in what a session shows off the Mac (plan 028, R1a;
fix round 2, G3 as amended: a call that may hold one is withheld, prose is masked)."""

from __future__ import annotations

import time

import pytest

from openswap.redact import CHECK_BUDGET, holds_credential, mask_shapes

SHAPES = [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx", "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "github_pat_11ABCDEFG0123456789_abcdefghij",
    "glpat-AbCdEfGhIjKlMnOpQrSt", "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "hf_AbCdEfGhIjKlMnOpQrStUvWxYz01234567",
    "AKIAIOSFODNN7EXAMPLE", "xoxb-1234567890-abcdefghijkl", "https://hooks.slack.com/services/T000/B000/XXXX",
    "sk_live_AbCdEfGhIjKlMnOp", "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "SG.AbCdEfGhIjKlMnOpQrStUv.AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcde",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
]


@pytest.mark.parametrize("shape", SHAPES)
def test_a_token_shape_anywhere_in_a_call_holds_a_credential(shape):
    for value in ({"command": f"deploy --with {shape} now"}, {"a": [{"b": {"c": shape}}]}, {"env": {shape: "x"}}):
        assert holds_credential(value)


@pytest.mark.parametrize("text", [
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk", "b3BlbnNzaC1rZXk\n-----END RSA PRIVATE KEY-----",
    "git clone https://oauth2:Zq9secret@gitlab.test/repo.git",
])
def test_a_key_block_line_or_a_url_password_holds_a_credential(text):
    assert holds_credential({"content": text})


@pytest.mark.parametrize("text", [
    "export DB_PASSWORD=x", "tool --passwd x", "sshpass --pass x", "MYSQL_PWD=x mysql",
    "client_secret=x", "AWS_SECRET_ACCESS_KEY=x", "credentials.json", "aws_credential x", "export GITHUB_TOKEN=x",
    "OPENAI_API_KEY=x", "tool --api-key x", "apiKey=x", "APIKEY=x", "access_key x", "privateKey: x",
    "curl -H 'Authorization: Basic x'", "curl -H 'Bearer x'", "curl --cookie sid=x", "XAuthToken=x",
    "make tests pass",
])
def test_a_secret_sounding_name_holds_a_credential(text):
    assert holds_credential({"command": text})


@pytest.mark.parametrize("value", [{"headers": {"X-Api-Key": "k"}}, {"a": {"b": [{"password": "x"}]}}])
def test_a_secret_sounding_key_at_any_depth_holds_a_credential(value):
    assert holds_credential(value)


@pytest.mark.parametrize("text", [
    "npm test", "git push -u origin main", "cd $PWD && pwd", "PWD=/tmp ./run", "OLDPWD=/x ./run", 'echo "$(pwd)"',
    "src/auth.py", "fix-auth", "git log --author alice", "primary_key=id", "max_tokens=4000",
    "claude --max-tokens 4000", "bypass=yes", "cat secrets.yaml", "monkey keyboard", "https://example.com/a@b",
    "git clone https://ann@x.test/repo.git", "Bearertoken", "sshpass -p x ssh host", "mysql -u root -px app",
])
def test_ordinary_calls_hold_no_credential(text):
    assert not holds_credential({"command": text, "description": "Run it"})


def test_a_call_past_the_check_budget_is_taken_to_hold_one():
    assert not holds_credential({"content": "x" * (CHECK_BUDGET - 100)})
    assert holds_credential({"content": "x" * (CHECK_BUDGET + 1)})
    assert holds_credential({"items": [0] * (CHECK_BUDGET + 1)})


HOSTILE = [
    "token=$( " * 30_000, "-u $( " * 40_000, "mysql -p$( " * 23_000, "TOKEN=$(" * 32_000, "tokens " * 37_000,
    "x" * 260_000, "a-" * 130_000, "s/" * 130_000, "eyJ" + "a" * 260_000, "eyJ-" * 65_000, "a://" + "x" * 260_000,
    "-----BEGIN PRIVATE KEY-----" * 9_000, "AAAAAAAa" * 32_000, "Cookie: " * 32_000, '"' + "\\" * 260_000,
    "monkey " * 37_000,
]


@pytest.mark.parametrize("text", HOSTILE, ids=lambda text: f"{text[:12]}…{len(text)}")
def test_checking_and_masking_a_hostile_input_within_the_budget_is_quick(text):
    text = text[:CHECK_BUDGET - 10]
    started = time.perf_counter()
    holds_credential({"command": text})
    mask_shapes(text)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("shape", SHAPES)
def test_prose_has_token_shapes_masked(shape):
    assert mask_shapes(f"Deploy with {shape} now.") == "Deploy with [REDACTED] now."


@pytest.mark.parametrize(("text", "masked"), [
    ("Key:\n-----BEGIN RSA PRIVATE KEY-----\nb3Bl\n-----END RSA PRIVATE KEY-----\nDone.", "Key:\n[REDACTED]\nDone."),
    ("Cut: -----BEGIN RSA PRIVATE KEY-----\nb3Bl", "Cut: [REDACTED]"),
    ("Cloned https://oauth2:Zq9secret@gitlab.test/r.", "Cloned https://[REDACTED]@gitlab.test/r."),
    ("Sent -H 'Authorization: Bearer Zq9 secret' to it", "Sent -H 'Authorization: [REDACTED]' to it"),
    ("Proxy-Authorization: Basic Zq9\nnext line", "Proxy-Authorization: [REDACTED]\nnext line"),
    ('With "Cookie: sid=Zq9; theme=dark" set', 'With "Cookie: [REDACTED]" set'),
])
def test_prose_has_key_blocks_url_passwords_and_header_values_masked(text, masked):
    assert mask_shapes(text) == masked


@pytest.mark.parametrize("text", [
    "Uses Bearer authentication.", "Set PASSWORD=hunter2 and the token Zq9.", "See https://ann@x.test/a@b.",
    "The Authorization header is missing.", "Masked: [REDACTED] and Authorization: [REDACTED]",
])
def test_prose_keeps_names_and_already_masked_text_readable(text):
    assert mask_shapes(text) == text
