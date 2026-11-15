"""Recognizing credentials in what a session shows off the Mac (plan 028, R1a).

A call that may hold a credential is withheld whole rather than masked in place:
only the shell itself knows where a value ends. Only a recognizable shape or a
secret-sounding name is caught. Every pattern runs in time linear in the text:
a session's own input can be built to make a careless pattern backtrack.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"
# A call is checked only up to this many characters of text (each value counts
# one more); a larger one is taken to hold a credential.
CHECK_BUDGET = 256 * 1024
# Characters past a text's cap still masked, so a credential the cap cuts through is caught whole.
MASK_MARGIN = 1000

# Credentials recognizable by their shape alone.
_TOKENS = [
    r"\bsk-ant-[A-Za-z0-9_-]{10,}",
    r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}",
    r"\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b",
    r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
    r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+",
    r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|\bwhsec_[A-Za-z0-9]{10,}",
    r"\bAIza[0-9A-Za-z_-]{35}|\bya29\.[0-9A-Za-z_-]{20,}|\bGOCSPX-[A-Za-z0-9_-]{20,}",
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}+\.[A-Za-z0-9_-]{10,}+\.[A-Za-z0-9_-]{10,}",
    r"\bnpm_[A-Za-z0-9]{30,}|\bglpat-[A-Za-z0-9_-]{20,}|\bhf_[A-Za-z0-9]{30,}",
    r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
]
_KEY_LABEL = r"[A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
# A URL's ``user:password@``.
_USERINFO = r"(?i:(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*+://)[^/\s@:\"'<>]*+:[^/\s@\"'<>]++@)"
_CREDENTIAL = re.compile("|".join([*_TOKENS, rf"-----(?:BEGIN|END){_KEY_LABEL}", _USERINFO]))

_SHAPES = re.compile("|".join([rf"-----BEGIN{_KEY_LABEL}.*?(?:-----END{_KEY_LABEL}|\Z)", *_TOKENS]), re.DOTALL)
_URL_PASSWORD = re.compile(_USERINFO)
_HEADER = re.compile(r"(?i)\b((?:proxy-)?authorization|(?:set-)?cookie)([ \t]*+:[ \t]*+)[^\r\n\"']+")

# A name (``DB_PASSWORD``, ``--api-key``, ``apiKey``) that may hold a secret
# word; whether it does is decided by its segments (``_secret_name``).
_NAMED = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]*?(?i:pass|pwd|secret|credential|token|key|authorization"
                    r"|bearer|cookie)[A-Za-z0-9_-]*+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SECRET_SEGMENTS = frozenset({"password", "passwd", "pass", "pwd", "secret", "credential", "credentials", "token",
                              "apikey", "authorization", "bearer", "cookie"})
_SECRET_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key")})
# The shell's working directories, ``$PWD`` and ``$(pwd)``.
_PLAIN_NAMES = frozenset({"pwd", "oldpwd"})


def holds_credential(value: object) -> bool:
    """Whether a JSON value (a call's input) may hold a credential: any string
    in it, a key included, holds a recognizable token shape (API keys and
    tokens, a private key's BEGIN or END line, a URL's user and password) or a
    secret-sounding name (``password``, ``pwd``, ``secret``, ``token``,
    ``api_key``, ``authorization``, ``bearer``, ``cookie``, ...) as a word or a
    name's segment. A value past ``CHECK_BUDGET`` is taken to hold one."""
    strings = _strings(value)
    if strings is None:
        return True
    text = "\n".join(strings)
    return _CREDENTIAL.search(text) is not None or any(_secret_name(m[0]) for m in _NAMED.finditer(text))


def mask_shapes(text: str) -> str:
    """Prose (Claude's own words, a question) with every credential
    recognizable by its shape (API keys and tokens, private-key blocks, a
    URL's user and password) and every ``Authorization`` or ``Cookie`` header's
    value replaced by ``[REDACTED]``. A name before a word says nothing about
    it in prose, so names alone mask nothing."""
    text = _SHAPES.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(rf"\1{REDACTED}@", text)
    return _HEADER.sub(rf"\1\2{REDACTED}", text)


def _strings(value: object) -> list[str] | None:
    """Every string in ``value``, keys included, or None past ``CHECK_BUDGET``."""
    strings, stack, size = [], [value], 0
    while stack:
        value = stack.pop()
        size += len(value) if isinstance(value, (str, dict, list)) else 0
        if size > CHECK_BUDGET:
            return None
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            stack += [*value, *value.values()]
        elif isinstance(value, list):
            stack += value
    return strings


def _secret_name(name: str) -> bool:
    """Whether ``name`` says it holds a credential, by its segments
    (``DB_PASSWORD``, ``apiKey``, ``--api-key``), never by a word inside one
    (``bypass``, ``max_tokens``, ``monkey``)."""
    if name.lower() in _PLAIN_NAMES:
        return False
    segments = [s.lower() for part in re.split("[_-]", name) for s in _CAMEL.findall(part)]
    return any(s in _SECRET_SEGMENTS for s in segments) or any(p in _SECRET_PAIRS for p in zip(segments, segments[1:]))
