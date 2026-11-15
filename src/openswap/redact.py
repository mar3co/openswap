"""Masking recognizable credentials in text a session shows off the Mac (plan 028, R1a).

Only credentials with a recognizable shape or name are caught; anything else in
the text goes as it is. Every pattern runs in time linear in the text: a
session's own input can be built to make a careless pattern backtrack.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Credentials recognizable by their shape alone.
_SHAPES = re.compile("|".join([
    r"-----BEGIN[A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----.*?"
    r"(?:-----END[A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----|\Z)",
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
]), re.DOTALL)
_BEARER = re.compile(r"\b(Bearer[ \t]+)[A-Za-z0-9._~+/-]{8,}=*")
_USERINFO = re.compile(r"(?i)(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*+://)[^/\s@\"'<>]++@")

# A name that may say its value is a credential, and how a value is given to it:
# ``name=value``, ``name: value``, ``"name": value``, ``--name value``, ``--name=value``,
# ``name value``. Whether it does is decided by its segments (``_secret_name``).
_NAMED = re.compile(r"""(?<![\w.$-])(?P<dash>--?)?(?P<quote>["']?)
    (?=[\w.-]*?(?i:pass|pwd|secret|token|auth|cookie|credential|key))(?P<name>[A-Za-z_][\w.-]*+)(?P=quote)
    (?P<sep>[ \t]*[:=][ \t]*|[ \t]+)""", re.VERBOSE)
_HEADERS = {
    "authorization": re.compile(r"(?i)(?:(?:bearer|basic|token|digest)[ \t]+)?([^\s\"']+)"),
    "proxy-authorization": re.compile(r"(?i)(?:(?:bearer|basic|token|digest)[ \t]+)?([^\s\"']+)"),
    "cookie": re.compile(r"([^\r\n\"']+)"),
    "set-cookie": re.compile(r"([^\r\n\"']+)"),
}
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SECRET_WORDS = frozenset({"pass", "password", "passwd", "passphrase", "pwd", "secret", "secrets", "token", "auth",
                           "authorization", "cookie", "cookies", "credential", "credentials"})
_SECRET_PARTS = ("password", "passwd", "passphrase", "secret", "credential", "apikey", "privatekey")
# ``<this>_key`` names a column or a lookup, not a credential.
_PLAIN_KEYS = frozenset({"primary", "foreign", "sort", "partition", "hash", "range", "public", "unique", "cache",
                         "composite"})
_PLAIN_NAMES = frozenset({"pwd", "oldpwd"})

# Where an unquoted shell value ends; the rest of the text stays readable.
_STOP = " \t\r\n;&|<>)"
_VARIABLE = re.compile(r"\$\{[^}]*\}|\$[A-Za-z_]\w*")
_PARENS = re.compile(r"[()]")
_SPECIAL: dict[str, re.Pattern] = {}

# A password in a command's own form: ``-u user:password``, mysql's ``-pPASSWORD``,
# a registry's ``login -p PASSWORD``; and sed's ``s/regex/replacement/``.
_USER_FLAG = re.compile(r"(?<![\w-])(?:-u|-U|--user|--proxy-user)(?:[ \t]+|=)")
_MYSQL = re.compile(r"(?<![\w.-])(?:mysql|mariadb)[\w-]*")
_LOGIN = re.compile(r"(?<![\w-])login(?![\w-])")
_SED = re.compile(r"(?<![\w.-])g?sed(?![\w-])")
_ATTACHED_P = re.compile(r"(?<!\S)-p")
_SPACED_P = re.compile(r"(?<!\S)-p[ \t]+")
_COMMAND_END = re.compile(r"[;&|\n\"']")
_SED_EXPRESSION = re.compile(r"(?<![\w\\])s(?P<d>[/|#:,@!%])(?:\\.|(?!(?P=d))[^\\\n])*+(?P=d)"
                             r"(?P<replacement>(?:\\.|(?!(?P=d))[^\\\n])*+)(?P=d)")


def mask_shapes(text: str) -> str:
    """``text`` with every credential recognizable by its shape alone replaced
    by ``[REDACTED]``: API keys and tokens (Anthropic, OpenAI, GitHub, AWS,
    Slack, Stripe, Google, npm, GitLab, Hugging Face, SendGrid, JWTs), private-key
    blocks, a ``Bearer`` token and a URL's user and password. For prose, where
    a name before a word says nothing about it."""
    text = _SHAPES.sub(REDACTED, text)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    return _USERINFO.sub(rf"\1{REDACTED}@", text)


def mask_secrets(text: str) -> str:
    """``mask_shapes``, and the value of anything whose name says it is a
    password, secret, token, key, credential or cookie (``name=value``,
    ``name: value``, ``"name": value``, ``--name value``), ``Authorization`` and
    ``Cookie`` headers, ``-u user:password``, mysql's ``-pPASSWORD`` and a
    registry's ``login -p PASSWORD``. A quoted value is masked whole; an unquoted
    one up to the shell's next ``; & | < > )``. A value that is only a command
    substitution or a variable (``$(…)``, ``$NAME``) stays readable."""
    text = mask_shapes(text)
    text = _apply(text, _command_spans(text))
    return _mask_named_around_sed(text)


def mask_value(value: object) -> object:
    """A JSON value with ``mask_secrets`` applied to every string in it, and
    the whole value of any key whose name says it is a credential masked."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, list):
        return [mask_value(v) for v in value]
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _secret_name(k) and v not in (None, "")
                else mask_value(v) for k, v in value.items()}
    return value


def _segments(name: str) -> list[str]:
    return [s.lower() for part in re.split(r"[_.-]+", name) for s in _CAMEL.findall(part)]


def _secret_name(name: str) -> bool:
    """Whether ``name`` says its value is a credential, by its segments
    (``DB_PASSWORD``, ``apiKey``, ``x-auth-token``), never by a word inside one
    (``--author``, ``bypass``, ``max_tokens``)."""
    if name.lower() in _PLAIN_NAMES:
        return False
    segments = _segments(name)
    if any(s in _SECRET_WORDS or any(p in s for p in _SECRET_PARTS) for s in segments):
        return True
    return len(segments) >= 2 and segments[-1] == "key" and segments[-2] not in _PLAIN_KEYS


def _apply(text: str, spans) -> str:
    """``text`` with each (start, end) span replaced by ``[REDACTED]``; overlapping spans merge."""
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if not merged:
        return text
    out, pos = [], 0
    for start, end in merged:
        out += [text[pos:start], REDACTED]
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _closing(text: str, i: int) -> int:
    """The index of the quote that closes the one at ``text[i]``, or -1."""
    quote, j = text[i], i + 1
    while True:
        j = text.find(quote, j)
        if j < 0 or quote != '"':
            return j
        k = j
        while k > i + 1 and text[k - 1] == "\\":
            k -= 1
        if (j - k) % 2 == 0:
            return j
        j += 1


def _paren_end(text: str, i: int) -> int:
    """The index just past the parenthesis that closes the one at ``text[i]``, or the end."""
    depth = 0
    for match in _PARENS.finditer(text, i):
        depth += 1 if match[0] == "(" else -1
        if depth == 0:
            return match.end()
    return len(text)


def _value_span(text: str, i: int, stop: str = "") -> tuple[int, int] | None:
    """The span to mask of the value that starts at ``text[i]``, or None when
    there is none or it is only a substitution or a variable. A quoted value's
    span is inside its quotes; an unquoted one ends at the shell's next
    unquoted ``; & | < > )``, whitespace or a character in ``stop``."""
    n = len(text)
    if i >= n or text[i] in _STOP or text[i] in stop:
        return None
    if text[i] in "\"'":
        close = _closing(text, i)
        end = n if close < 0 else close
        inner = text[i + 1:end]
        if not inner or (text[i] == '"' and _VARIABLE.fullmatch(inner)):
            return None
        return i + 1, end
    special = _SPECIAL.get(stop) or _SPECIAL.setdefault(stop, re.compile(f"[{re.escape(_STOP + stop)}\"'`$]"))
    j = i
    while True:
        match = special.search(text, j)
        if match is None:
            j = n
            break
        j = match.start()
        char = text[j]
        if char in _STOP or char in stop:
            break
        if char == "$":
            j = _paren_end(text, j + 1) if text.startswith("(", j + 1) else j + 1
        else:
            close = _closing(text, j)
            j = n if close < 0 else close + 1
    if text.startswith("$(", i):
        pure = _paren_end(text, i + 1) == j
    elif text[i] == "`":
        pure = text.find("`", i + 1) + 1 == j
    else:
        pure = _VARIABLE.fullmatch(text, i, j) is not None
    return None if pure else (i, j)


def _named_span(text: str, match: re.Match, stop: str) -> tuple[int, int] | None:
    name, sep, quote = match["name"], match["sep"], match["quote"]
    header = _HEADERS.get(name.lower())
    if header is not None and not quote and ":" in sep:
        value = header.match(text, match.end())
        return value.span(1) if value else None
    if not _secret_name(name):
        return None
    if not sep.strip():
        # ``name value``: a flag, or a name no word of prose would have (``aws_secret_access_key``).
        if quote or (not match["dash"] and len(_segments(name)) < 2) or text.startswith("-", match.end()):
            return None
    elif sep[0] == "=" and sep[-1] in " \t":
        return None  # ``name= next``: an empty value
    return _value_span(text, match.end(), stop)


def _mask_named(text: str, stop: str = "") -> str:
    spans, pos = [], 0
    for match in _NAMED.finditer(text):
        if match.start() < pos:
            continue  # inside a value already masked
        span = _named_span(text, match, stop)
        if span is not None:
            spans.append(span)
            pos = span[1]
    return _apply(text, spans)


def _command_end(text: str, i: int) -> int:
    """Where the command going on at ``text[i]`` ends: the shell's next unquoted ``; & |`` or line."""
    while (match := _COMMAND_END.search(text, i)) is not None:
        if match[0] not in "\"'":
            return match.start()
        close = _closing(text, match.start())
        if close < 0:
            break
        i = close + 1
    return len(text)


def _commands(text: str, command: re.Pattern):
    """Each (start, end) of the rest of a ``command`` command."""
    pos = 0
    while (match := command.search(text, pos)) is not None:
        end = _command_end(text, match.end())
        yield match.end(), end
        pos = max(end, match.end())


def _command_spans(text: str):
    for match in _USER_FLAG.finditer(text):
        span = _value_span(text, match.end())
        if span is not None:
            colon = text.find(":", *span)
            if 0 <= colon < span[1] - 1:
                yield colon + 1, span[1]
    for command, flag in ((_MYSQL, _ATTACHED_P), (_LOGIN, _SPACED_P)):
        for start, end in _commands(text, command):
            for found in flag.finditer(text, start, end):
                span = None if text.startswith("-", found.end()) else _value_span(text, found.end())
                if span is not None:
                    yield span


def _mask_named_around_sed(text: str) -> str:
    """``_mask_named`` over ``text``; in a sed command's ``s/regex/replacement/``,
    over the replacement only, where the delimiter ends a value too."""
    out, pos = [], 0
    for start, end in _commands(text, _SED):
        for expression in _SED_EXPRESSION.finditer(text, start, end):
            replacement = expression.span("replacement")
            out += [_mask_named(text[pos:expression.start()]), text[expression.start():replacement[0]],
                    _mask_named(text[replacement[0]:replacement[1]], expression["d"])]
            pos = replacement[1]
    out.append(_mask_named(text[pos:]))
    return "".join(out)
