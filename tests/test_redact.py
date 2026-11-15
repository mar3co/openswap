"""Recognizing credentials in what a session shows off the Mac (plan 028, R1a;
fix round 2, G3 as amended: a call that may hold one is withheld, prose is masked)."""

from __future__ import annotations

import time

import pytest

from openswap.redact import CHECK_BUDGET, MASK_MARGIN, REDACTED, holds_credential, mask_shapes

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
    "make tests pass", "PGPASSWORD=hunter2 psql", "clientsecret=x", "MYSQLPASSWD=x", "cat secrets.yaml",
    "GPG_PASSPHRASE=x", "gpg --passphrase x", "DB_PW=x", "HTTP_AUTH=x", "http --auth x", "OPENAI_KEY=x",
    "MASTER_KEY=x", "RAILS_MASTER_KEY=x", "JWT_SIGNING_KEY=x", "cosign --signing-key x", "accesstoken=x",
    "githubtoken x", "ACCESSTOKEN=x",
    'curl -H "X-Auth-Key: 0123abc" x.test', "Ocp-Apim-Subscription-Key: 0123", "x-functions-key: 0123",
    "X-Api-Key: 0123", "X-Master-Key: 0123", "X-CACHE-KEY: 0123",
])
def test_a_secret_sounding_name_holds_a_credential(text):
    assert holds_credential({"command": text})


@pytest.mark.parametrize("value", [{"headers": {"X-Api-Key": "k"}}, {"a": {"b": [{"password": "x"}]}}])
def test_a_secret_sounding_key_at_any_depth_holds_a_credential(value):
    assert holds_credential(value)


@pytest.mark.parametrize("text", [
    "npm test", "git push -u origin main", "cd $PWD && pwd", "PWD=/tmp ./run", "OLDPWD=/x ./run", 'echo "$(pwd)"',
    "src/auth.py", "fix-auth", "git log --author alice", "primary_key=id", "max_tokens=4000",
    "claude --max-tokens 4000", "bypass=yes", "monkey keyboard", "https://example.com/a@b",
    "git clone https://ann@x.test/repo.git", "sshpass -p x ssh host", "mysql -u root -px app",
    "ForeignKey(User)", "cache_key=x", "encryption_key=x", "useAuth()", "cat auth/views.py", "KEY=x", "--key x",
    "npm config set _auth x", "git commit -a -m 'fix: x'", "sort -u a.txt", "git log --author=ann:x",
    "echo $SSH_AUTH_SOCK", "auth-service:\n  image: x", "auth-test:\n\tpytest", ".auth-form:hover { color: red }",
    "bypass-auth: true", 'git commit -a -m "fix: x"', "sort -u -t: -k2 f",
])
def test_ordinary_calls_hold_no_credential(text):
    assert not holds_credential({"command": text, "description": "Run it"})


@pytest.mark.parametrize("text", [
    "curl -H 'Idempotency-Key: 123' x.test", "X-Cache-Key: abc", "Partition-Key: pk", "Sort-Key: sk",
    "Primary-Key: id", "Foreign-Key: id", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
])
def test_a_header_name_ending_in_a_non_secret_key_holds_no_credential(text):
    assert not holds_credential({"command": text})


@pytest.mark.parametrize("text", [
    "X-Auth-Key:0123abc", "X-Master-Key:0123", 'curl -d \'{"X-Auth-Key": "0123abc"}\' x.test',
    "{'X-Master-Key': '0123'}",
])
def test_a_header_name_with_no_space_after_its_colon_or_quoted_as_a_json_key_holds_a_credential(text):
    assert holds_credential({"command": text})


def test_a_url_password_holding_a_space_outside_ascii_holds_a_credential():
    assert holds_credential({"command": "curl https://user:pa\u3000ss@x.test"})


@pytest.mark.parametrize("text", [
    "curl -u admin:hunter2 x.test", "curl -uadmin:hunter2 x.test", "curl --user=admin:hunter2 x.test",
    "curl\n-u\nadmin:hunter2", "psql -U 'admin:hunter2'", "x -a admin:hunter2", "curl --proxy-user a:b x.test",
    "http --auth alice:pw x.test", f"git clone https://{'0123456789abcdef' * 2}@github.com/o/r.git",
    "curl -u admin:'p@ss!' x", 'curl -u admin:"hunter2" x', 'curl -u admin:"pw"tail y',
])
def test_a_user_and_password_after_a_flag_or_a_long_url_user_holds_a_credential(text):
    assert holds_credential({"command": text}) and holds_credential({"command": text.split()})


@pytest.mark.parametrize("prefix", ["\u5bc6\u94a5", "\u00e9", "\u3164", "\u00a0"])
def test_a_token_shape_right_after_a_letter_outside_ascii_is_caught(prefix):
    assert holds_credential({"command": f"{prefix}{SHAPES[2]}"})
    assert mask_shapes(f"{prefix}{SHAPES[2]} x") == f"{prefix}[REDACTED] x"


def test_a_call_past_the_check_budget_is_taken_to_hold_one():
    assert not holds_credential({"content": "x" * (CHECK_BUDGET - 100)})
    assert holds_credential({"content": "x" * (CHECK_BUDGET + 1)})
    assert holds_credential({"items": [0] * (CHECK_BUDGET + 1)})


HOSTILE = [
    "token=$( " * 30_000, "-u $( " * 40_000, "mysql -p$( " * 23_000, "TOKEN=$(" * 32_000, "tokens " * 37_000,
    "x" * 260_000, "a-" * 130_000, "s/" * 130_000, "eyJ" + "a" * 260_000, "eyJ-" * 65_000, "a://" + "x" * 260_000,
    "-----BEGIN PRIVATE KEY-----" * 9_000, "AAAAAAAa" * 32_000, "Cookie: " * 32_000, '"' + "\\" * 260_000,
    "monkey " * 37_000, "-u." * 87_000, "-ua" * 87_000, "x_" * 130_000, "Bearer " * 37_000,
    "https://" + "a" * 260_000, "'Cookie': '" * 23_000, '-u a:"\\' * 87_000, "-u a:'x'" * 32_000,
    '-u "a:x"' * 32_000, 'X-Auth-Key"' * 23_000,
]


@pytest.mark.parametrize("text", HOSTILE, ids=lambda text: f"{text[:12]}…{len(text)}")
def test_checking_and_masking_a_hostile_input_within_the_budget_is_quick(text):
    # Each at the most it is ever given: a call up to the check budget, prose up to its cap plus the margin.
    call, prose = text[:CHECK_BUDGET - 10], text[:1000 + MASK_MARGIN]
    for check in (lambda: holds_credential({"command": call}), lambda: mask_shapes(prose)):
        started = time.perf_counter()
        check()
        assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize("shape", SHAPES)
def test_prose_has_token_shapes_masked(shape):
    assert mask_shapes(f"Deploy with {shape} now.") == "Deploy with [REDACTED] now."


@pytest.mark.parametrize(("text", "hidden"), [
    ("Cloned https://user:p'ass@example.com/x", ("p'ass", "ass")),
    ('{"Authorization": "Bearer abc\\"supersecret"}', ("supersecret",)),
    ("{'Cookie': 'sid=a\\'supersecret'}", ("supersecret",)),
    ('Send Cookie: sid=\\"supersecret\\" now', ("supersecret",)),
])
def test_prose_shows_no_part_of_a_credential_holding_a_quote(text, hidden):
    shown = mask_shapes(text)
    assert REDACTED in shown and not any(part in shown for part in hidden)


def test_a_url_password_holding_an_apostrophe_holds_a_credential():
    assert holds_credential({"command": "curl https://user:p'ass@example.com/x"})


@pytest.mark.parametrize(("text", "hidden"), [
    ("Run curl -u admin:'p\\'q'r st'uv then", ("st", "uv")),
    ('Run -u a:\\"\\"SE" CR ok', ("SE", "CR")),
    ("Run curl -u admin:'it'\\''s secret' x", ("secret",)),
    ("Run curl -u 'admin:it\\'s secret' now", ("secret",)),
    ("Run curl -u admin:'pa\\'ss word' now", ("ss", "word")),
    ("Use -u admin:'a\\b' -U\nbob:pw now", ("pw",)),
    ("Try curl -u admin:'it\\'s secret' first; if that fails use -u\nroot:toor instead.", ("secret", "toor")),
])
def test_prose_shows_no_part_of_a_login_password_quoted_as_the_shell_reads_it(text, hidden):
    shown = mask_shapes(text)
    assert REDACTED in shown and not any(part in shown for part in hidden)


@pytest.mark.parametrize(("text", "masked"), [
    ("Key:\n-----BEGIN RSA PRIVATE KEY-----\nb3Bl\n-----END RSA PRIVATE KEY-----\nDone.", "Key:\n[REDACTED]\nDone."),
    ("Cut: -----BEGIN RSA PRIVATE KEY-----\nb3Bl", "Cut: [REDACTED]"),
    ("Cloned https://oauth2:Zq9secret@gitlab.test/r.", "Cloned https://[REDACTED]@gitlab.test/r."),
    ("Sent -H 'Authorization: Bearer Zq9 secret' to it", "Sent -H 'Authorization: [REDACTED]' to it"),
    ("Proxy-Authorization: Basic Zq9\nnext line", "Proxy-Authorization: [REDACTED]\nnext line"),
    ('With "Cookie: sid=Zq9; theme=dark" set', 'With "Cookie: [REDACTED]" set'),
    ('{"Authorization": "Bearer Zq9 secret", "x": 1}', '{"Authorization": "[REDACTED]", "x": 1}'),
    ("{'Cookie': 'session=Zq9; theme=dark', 'x': 1}", "{'Cookie': '[REDACTED]', 'x': 1}"),
    ("Set HTTP_AUTHORIZATION: Basic Zq9 now", "Set HTTP_AUTHORIZATION: [REDACTED]"),
    ('Authorization: "Bearer Zq9 secret" sent', 'Authorization: "[REDACTED]" sent'),
    ("Authorization: 'Bearer Zq9 secret\nnext", "Authorization: '[REDACTED]\nnext"),
    (f"Use Bearer {'Zq9Xw7Vu5T' * 2} for it", "Use Bearer [REDACTED] for it"),
    (f"Then basic {'Zq9Xw7Vu5T' * 3}", "Then basic [REDACTED]"),
    (f"See https://{'0123456789abcdef' * 2}@github.com/o", "See https://[REDACTED]@github.com/o"),
    ("Send Cookie: sessionid=\"s3cr3tValue123\" now\nnext", "Send Cookie: [REDACTED]\nnext"),
    ('Authorization: Digest username="bob", response="6629fae"\nnext', "Authorization: [REDACTED]\nnext"),
    ('{"Authorization": "Basic it\'s-Zq9Xw7"}', '{"Authorization": "[REDACTED]"}'),
    ("Run `curl -u admin:changeme http://localhost`", "Run `curl -u admin:[REDACTED] http://localhost`"),
    ('Run `curl -u "admin:correct horse" https://x.test`', 'Run `curl -u "admin:[REDACTED]" https://x.test`'),
    ('Run curl -u admin:"correct horse battery" x', "Run curl -u admin:[REDACTED] x"),
    ('Run curl -u "$JIRA_EMAIL:hunter2" x', 'Run curl -u "$JIRA_EMAIL:[REDACTED]" x'),
    ("Clone https://bob:pa\u00a0ssw0rd@example.com/r", "Clone https://[REDACTED]@example.com/r"),
    ('Run curl -u "admin:pa\\"ss" x', 'Run curl -u "admin:[REDACTED]" x'),
    ('Run curl -u "admin:x"tail y', 'Run curl -u "admin:[REDACTED] y'),
    ("Run curl -u 'admin:x'tail y", "Run curl -u 'admin:[REDACTED] y"),
    ("Run ``curl -u admin:pa`ss`` now", "Run ``curl -u admin:[REDACTED] now"),
    ("Run curl -u admin:p@ss\u00a0word now", "Run curl -u admin:[REDACTED] now"),
    ("Run curl -u admin:pw\u3000then", "Run curl -u admin:[REDACTED]\u3000then"),
])
def test_prose_has_key_blocks_url_passwords_and_header_values_masked(text, masked):
    assert mask_shapes(text) == masked


@pytest.mark.parametrize("text", [
    "Uses Bearer authentication.", "Set PASSWORD=hunter2 and the token Zq9.", "See https://ann@x.test/a@b.",
    "The Authorization header is missing.", "Masked: [REDACTED] and Authorization: [REDACTED]",
    "Bearer authentication works.", "Use Basic auth here.",
    "\u670d\u52a1 http://localhost:8080\u3000\u8fde\u63a5\u3000admin@example.com",
    "http://localhost:8080\u2028admin@example.com", "http://localhost:8080\u2029admin@example.com",
])
def test_prose_keeps_names_and_already_masked_text_readable(text):
    assert mask_shapes(text) == text
