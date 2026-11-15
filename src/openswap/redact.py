"""Masking recognizable credentials in text a session shows off the Mac (plan 028, R1a).

Only credentials with a recognizable shape or name are caught; anything else in
the text goes as it is.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"
# A name that says its value is a credential (``DB_PASSWORD``, ``api-key``, ``--token``).
_SECRET_NAME = (r"[\w.-]*(?:password|passwd|passphrase|secret|token|api[_-]?key|apikey|access[_-]?key"
                r"|private[_-]?key|credential)[\w.-]*")
_NAMED = re.compile(_SECRET_NAME, re.IGNORECASE)

_PATTERNS = [
    re.compile(r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----.*?"
               r"(?:-----END[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----|\Z)", re.DOTALL),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b"),
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+"),
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|\bwhsec_[A-Za-z0-9]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}|\bya29\.[0-9A-Za-z_-]{20,}|\bGOCSPX-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]
_AUTHORIZATION = re.compile(r"(?i)\b((?:proxy-)?authorization\s*:\s*)((?:bearer|basic|token|digest)\s+)?[^\s\"']+")
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@\"'<>]+@")
_ASSIGNMENT = re.compile(rf"(?i)(?<![\w.-])({_SECRET_NAME})([\"']?\s*[:=]\s*[\"']?)([^\s\"'&;,)}}]+)")
_FLAG = re.compile(rf"(?i)(?<![\w.-])(--?{_SECRET_NAME})(\s+)([^\s\"'-][^\s\"']*)")


def mask_secrets(text: str) -> str:
    """``text`` with every recognizable credential replaced by ``[REDACTED]``:
    API keys and tokens by their shape (Anthropic, OpenAI, GitHub, AWS, Slack,
    Stripe, Google, JWTs), private-key blocks, ``Authorization:`` headers, a
    URL's user and password, and the value of any ``name=value``, ``name: value``
    or ``--name value`` whose name says it is a password, secret, token or key."""
    for pattern in _PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _AUTHORIZATION.sub(lambda m: f"{m[1]}{m[2] or ''}{REDACTED}", text)
    text = _USERINFO.sub(rf"\1{REDACTED}@", text)
    text = _ASSIGNMENT.sub(rf"\1\2{REDACTED}", text)
    return _FLAG.sub(rf"\1\2{REDACTED}", text)


def mask_value(value: object) -> object:
    """A JSON value with ``mask_secrets`` applied to every string in it, and
    the whole value of any key whose name says it is a credential masked."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _NAMED.fullmatch(k) and v not in (None, "")
                else mask_value(v) for k, v in value.items()}
    return value
