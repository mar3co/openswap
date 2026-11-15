"""The masker for text a session shows off the Mac (plan 028, R1a; fix round F2)."""

from __future__ import annotations

import time

import pytest

from openswap.redact import mask_secrets, mask_shapes, mask_value


@pytest.mark.parametrize("secret", [
    "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx", "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz012345",
    "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "github_pat_11ABCDEFG0123456789_abcdefghij",
    "AKIAIOSFODNN7EXAMPLE", "xoxb-1234567890-abcdefghijkl", "sk_live_AbCdEfGhIjKlMnOp",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "npm_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", "glpat-AbCdEfGhIjKlMnOpQrSt",
    "hf_AbCdEfGhIjKlMnOpQrStUvWxYz01234567", "SG.AbCdEfGhIjKlMnOpQrStUv.AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcde",
])
def test_recognizable_keys_and_tokens_are_masked(secret):
    for mask in (mask_secrets, mask_shapes):
        masked = mask(f"deploy --with {secret} now")
        assert secret not in masked and masked == "deploy --with [REDACTED] now"


@pytest.mark.parametrize(("text", "kept"), [
    ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----", ""),
    ("curl -H 'Authorization: Bearer abc.def-123' https://api.test", "Authorization: Bearer"),
    ("git clone https://oauth2:glpat-xyz@gitlab.test/repo.git", "https://[REDACTED]@gitlab.test/repo.git"),
    ("PGPASSWORD=hunter2 psql", "PGPASSWORD="), ('{"api_key": "abc123"}', '"api_key": "'),
    ("export GITHUB_TOKEN=abc123", "GITHUB_TOKEN="), ("tool --client-secret s3cr3t run", "--client-secret"),
])
def test_named_credentials_and_url_passwords_are_masked(text, kept):
    masked = mask_secrets(text)
    assert "[REDACTED]" in masked and kept in masked
    assert not any(s in masked for s in ("b3BlbnNzaC1rZXk", "abc.def-123", "glpat-xyz", "hunter2", "abc123", "s3cr3t"))


@pytest.mark.parametrize(("text", "masked"), [
    # Every kind of name, in every form a value is given to it.
    ("export STRIPE_KEY=Zq9secret", "export STRIPE_KEY=[REDACTED]"),
    ("OPENAI_API_KEY=Zq9secret npm start", "OPENAI_API_KEY=[REDACTED] npm start"),
    ("MYSQL_PWD=Zq9secret mysql", "MYSQL_PWD=[REDACTED] mysql"),
    ("X_AUTH=Zq9secret ./run", "X_AUTH=[REDACTED] ./run"),
    ("tool --api-key Zq9secret", "tool --api-key [REDACTED]"),
    ("tool --api-key=Zq9secret", "tool --api-key=[REDACTED]"),
    ("tool --auth Zq9secret", "tool --auth [REDACTED]"),
    ("tool --cookie Zq9secret", "tool --cookie [REDACTED]"),
    ("tool --credential Zq9secret", "tool --credential [REDACTED]"),
    ("tool --pass Zq9secret", "tool --pass [REDACTED]"),
    ('{"auth_token": "Zq9secret", "user": "ann"}', '{"auth_token": "[REDACTED]", "user": "ann"}'),
    ("{'password': 'Zq9secret'}", "{'password': '[REDACTED]'}"),
    ("password: Zq9secret", "password: [REDACTED]"),
    ("secret = Zq9secret", "secret = [REDACTED]"),
    ("aws configure set aws_secret_access_key Zq9secret", "aws configure set aws_secret_access_key [REDACTED]"),
    ("https://api.test/?access_token=Zq9secret&page=2", "https://api.test/?access_token=[REDACTED]&page=2"),
    # Quoted values are masked whole, up to the closing quote (or the end).
    ('tool --password "hunter two" next', 'tool --password "[REDACTED]" next'),
    ('PGPASSWORD="a b" psql', 'PGPASSWORD="[REDACTED]" psql'),
    ("PGPASSWORD='a b' psql", "PGPASSWORD='[REDACTED]' psql"),
    ('tool --password "never closed', 'tool --password "[REDACTED]'),
    # An unquoted value ends at the shell's own syntax.
    ("TOKEN=Zq9secret; echo done", "TOKEN=[REDACTED]; echo done"),
    ("TOKEN=Zq9secret|sh", "TOKEN=[REDACTED]|sh"),
    ("TOKEN=Zq9secret&&make", "TOKEN=[REDACTED]&&make"),
    ("TOKEN=Zq9secret>out", "TOKEN=[REDACTED]>out"),
    ("(TOKEN=Zq9secret)", "(TOKEN=[REDACTED])"),
    ("TOKEN=Zq9$(date)", "TOKEN=[REDACTED]"),
    ('TOKEN=Zq9"secret part" run', "TOKEN=[REDACTED] run"),
    # Headers, as text and as JSON keys.
    ("curl -H 'Cookie: sid=Zq9secret; theme=dark' x", "curl -H 'Cookie: [REDACTED]' x"),
    ('{"Authorization": "Bearer Zq9secret"}', '{"Authorization": "[REDACTED]"}'),
    ('{"Cookie": "sid=Zq9secret"}', '{"Cookie": "[REDACTED]"}'),
    ("curl -H 'Bearer Zq9secretZq9'", "curl -H 'Bearer [REDACTED]'"),
    # A password given in a command's own form.
    ("curl -u ann:Zq9secret https://x.test", "curl -u ann:[REDACTED] https://x.test"),
    ("curl --user ann:Zq9secret https://x.test", "curl --user ann:[REDACTED] https://x.test"),
    ("curl --user=ann:Zq9secret https://x.test", "curl --user=ann:[REDACTED] https://x.test"),
    ("psql -U ann:Zq9secret", "psql -U ann:[REDACTED]"),
    ("curl --proxy-user ann:Zq9secret x", "curl --proxy-user ann:[REDACTED] x"),
    ("curl -u 'ann:Zq9 secret' x", "curl -u 'ann:[REDACTED]' x"),
    ("mysql -u root -pZq9secret app", "mysql -u root -p[REDACTED] app"),
    ("mysqldump -pZq9secret app > dump.sql", "mysqldump -p[REDACTED] app > dump.sql"),
    ("mariadb -h db -p'Zq9 secret' app", "mariadb -h db -p'[REDACTED]' app"),
    ("docker login -u ann -p Zq9secret ghcr.io", "docker login -u ann -p [REDACTED] ghcr.io"),
    ("podman login -p Zq9secret quay.io", "podman login -p [REDACTED] quay.io"),
    ("helm registry login -p Zq9secret r.test", "helm registry login -p [REDACTED] r.test"),
    ("skopeo login -p Zq9secret r.test", "skopeo login -p [REDACTED] r.test"),
    # sed: the regex side stays readable, a value on the replacement side is masked.
    ("sed -i 's/^DB_PASSWORD=.*/DB_PASSWORD=Sup3r/' .env", "sed -i 's/^DB_PASSWORD=.*/DB_PASSWORD=[REDACTED]/' .env"),
    ("sed -e 's|TOKEN=.*|TOKEN=Sup3r|g' f", "sed -e 's|TOKEN=.*|TOKEN=[REDACTED]|g' f"),
])
def test_named_credentials_are_masked_in_each_form(text, masked):
    assert mask_secrets(text) == masked


@pytest.mark.parametrize("text", [
    '{"max_tokens": 4000}', "max_tokens=4000", "claude --max-tokens 4000", "input_tokens: 12",
    "grep -rn password= src/", "grep -rn 'token' src/", "echo token expired",
    "sed -n 's/token/word/p' notes.txt", "sed -i 's/^DB_PASSWORD=.*/DB_PASSWORD=/' .env",
    "deploy --token $(sh</tmp/p)", "deploy --token `cat ~/.t`", "deploy --token <(cat ~/.t)",
    "TOKEN=$TOKEN_FROM_ENV ./run", "TOKEN=${TOKEN_FROM_ENV} ./run", 'TOKEN="$TOKEN_FROM_ENV" ./run',
    "git log --author alice", "git log --author=alice", "primary_key=id", '{"key": "value"}',
    "bypass=yes", "passed=3", "cd $PWD", "PWD=/tmp ./run", "OLDPWD=/x ./run", 'echo "$(pwd)"',
    "npm test -- --watch", "git push origin main", "git push -u origin main", "ls ~/code && cat README.md",
    "https://example.com/a@b", "pytest -k 'token_refresh'", "mysql -u root -p app", "docker login ghcr.io",
])
def test_ordinary_commands_are_left_as_they_are(text):
    assert mask_secrets(text) == text


def test_names_alone_never_mask_text_where_only_shapes_count():
    text = "Set PASSWORD=hunter2 and use the token Zq9 (see https://ann:pw@x.test)"
    assert mask_shapes(text) == "Set PASSWORD=hunter2 and use the token Zq9 (see https://[REDACTED]@x.test)"


def test_a_value_under_a_credential_name_is_masked_whole():
    value = {"url": "https://api.test", "headers": {"X-Api-Key": "k-1", "Accept": "json"}, "password": "correct horse",
             "primary_key": "id", "max_tokens": 10}
    assert mask_value(value) == {"url": "https://api.test", "headers": {"X-Api-Key": "[REDACTED]", "Accept": "json"},
                                 "password": "[REDACTED]", "primary_key": "id", "max_tokens": 10}


@pytest.mark.parametrize("text", [
    "token" * 8000, "--token " * 5000, "--token x " * 5000, "TOKEN=" * 7000, "s/" * 20000, "$(" * 20000,
    "-----BEGIN PRIVATE KEY-----" * 2000, "a://" + "x" * 40000, '"' + "\\" * 40000, "sed s/" + "\\a" * 20000,
    "mysql " + "-p" * 20000, "login " + "-p " * 15000, "eyJ" + "a" * 40000, "eyJ-" * 10000, "a-" * 20000,
    "Bearer " * 6000,
], ids=lambda text: f"{text[:12]}…{len(text)}")
def test_masking_a_hostile_input_is_bounded(text):
    started = time.perf_counter()
    mask_secrets(text)
    assert time.perf_counter() - started < 1.0
