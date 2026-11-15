"""Recognizing credentials in what a session shows off the Mac (plan 028, R1a).

A call that may hold a credential is withheld whole rather than masked in place:
only the shell itself knows where a value ends. Only a recognizable shape or a
secret-sounding name is caught. Every pattern runs in time linear in the text:
a session's own input can be built to make a careless pattern backtrack.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"
# What a permission item shows in place of its description and preview when its call may hold a credential.
PREVIEW_WITHHELD = "Preview withheld: it may contain a credential."
# A call is checked only up to this many characters of text, each entry of an
# object or a list counting one more; a larger one is taken to hold a credential.
CHECK_BUDGET = 256 * 1024
# Characters past a text's cap still masked, so a credential the cap cuts through is caught whole.
MASK_MARGIN = 1000

# ASCII word edges: ``\b`` would see none between a token and a letter outside ASCII (``密钥ghp_…``).
_START, _END = r"(?<![A-Za-z0-9_])", r"(?![A-Za-z0-9_])"
# Credentials recognizable by their shape alone.
_TOKENS = [rf"{_START}(?:{token})" for token in (
    r"sk-ant-[A-Za-z0-9_-]{10,}",
    r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}",
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
    rf"(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{{16}}{_END}",
    r"xox[abposr]-[A-Za-z0-9-]{10,}",
    r"https://hooks\.slack\.com/services/[A-Za-z0-9/_-]+",
    r"(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}|whsec_[A-Za-z0-9]{10,}",
    r"AIza[0-9A-Za-z_-]{35}|ya29\.[0-9A-Za-z_-]{20,}|GOCSPX-[A-Za-z0-9_-]{20,}",
    r"(?<!-)eyJ[A-Za-z0-9_-]{10,}+\.[A-Za-z0-9_-]{10,}+\.[A-Za-z0-9_-]{10,}",
    r"npm_[A-Za-z0-9]{30,}|glpat-[A-Za-z0-9_-]{20,}|hf_[A-Za-z0-9]{30,}",
    r"SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
)]
_KEY_LABEL = r"[A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"
_ASCII_SPACE = r" \t\r\n\f\v"
# Where a word ends in prose: also the ideographic space (CJK text) and the line and paragraph separators.
_PROSE_SPACE = _ASCII_SPACE + r"\u3000\u2028\u2029"


def _userinfo(space: str) -> str:
    """A URL's ``user:password@``, or a user alone as long as a token
    (``https://<token>@github.com``), ending at a character of ``space``."""
    return (rf"(?i:(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*+://)"
            rf"(?:[^/{space}@:\"'<>]*+:[^/{space}@\"'<>]++|[A-Za-z0-9_-]{{20,}}+)@)")


# In a call only ASCII whitespace ends one: a password may hold any other space.
_USERINFO = _userinfo(_ASCII_SPACE)
# ``user:password`` given to a login flag (``curl -u``, ``psql -U``, ``http --auth``); the user is
# bounded so that flags packed into one long word stay linear.
_FLAG = r"(?<![A-Za-z0-9_-])(?:-[uUa]|--(?:(?:proxy-)?user|auth)(?=[=\s]))(?:=|\s*+)"
_LOGIN_USER = r"[^\s:\"']{0,128}+:"
_CREDENTIAL = re.compile("|".join([*_TOKENS, rf"-----(?:BEGIN|END){_KEY_LABEL}", _USERINFO,
                                   rf"{_FLAG}[\"']?{_LOGIN_USER}[\"']?[^\s\"']"]))

_SHAPES = re.compile("|".join([rf"-----BEGIN{_KEY_LABEL}.*?(?:-----END{_KEY_LABEL}|\Z)", *_TOKENS]), re.DOTALL)
# A literal U+3000 inside a URL's password in prose then shows: accepted, for readable CJK prose.
_URL_PASSWORD = re.compile(_userinfo(_PROSE_SPACE))
# The password after a login flag in prose: to the quote that opened before the user and on to the
# end of the word, or to the end of the word, quoted parts included, read as the shell reads them:
# a backslash escapes the next character outside quotes and inside double quotes. Inside single
# quotes the shell reads none, but a writer may have meant one (``'it\'s secret'``), so a
# backslash there masks the rest of the line: more than either reading.
_DQ_BODY, _SQ_BODY = r"(?:[^\r\n\"\\]|\\[^\r\n]?)*+", r"[^\r\n'\\]*+(?:\\[^\r\n]*+)?"
_PROSE_WORD = rf"(?:[^{_PROSE_SPACE}\"'\\]|\\[^\r\n]?|\"{_DQ_BODY}\"?|'{_SQ_BODY}'?)++"
_FLAG_PASSWORD = re.compile(rf"({_FLAG}\"{_LOGIN_USER}){_DQ_BODY}(?:\"{_PROSE_WORD})?"
                            rf"|({_FLAG}'{_LOGIN_USER}){_SQ_BODY}(?:'{_PROSE_WORD})?"
                            rf"|({_FLAG}{_LOGIN_USER}){_PROSE_WORD}")
# An ``Authorization`` or ``Cookie`` header (``HTTP_AUTHORIZATION`` too, and a quoted name, as in
# JSON): a quoted value up to its closing quote, or an unquoted one up to the line's end or a
# quote, quoted parts after ``=`` included (``sessionid="…"``, ``Digest response="…"``).
_HEADER = re.compile(r"(?i)(?<![A-Za-z0-9_])((?:[A-Za-z0-9]++_)*+(?:(?:proxy-)?authorization|(?:set-)?cookie)"
                     r"[\"']?[ \t]*+:[ \t]*+[\"']?)(?:(?<=\")[^\r\n\"]*+|(?<=')[^\r\n']*+"
                     r"|(?:[^\r\n\"']|(?<==)\"[^\r\n\"]*+\"?|(?<==)'[^\r\n']*+'?)++)")
# A scheme's word before a token-long word, as a header value is written.
_SCHEME_TOKEN = re.compile(rf"(?i){_START}((?:bearer|basic)[ \t]++)[A-Za-z0-9_.~+/=-]{{20,}}+")
# A header's name before its colon and value (``X-Auth-Key: …``, ``X-Auth-Key:…``, or a quoted
# key, as in JSON: ``"X-Auth-Key": …``): read like a flag (``_secret_name``). Not a YAML key or CSS
# selector (``auth-service:``, ``.auth-form:hover``).
_HEADER_NAME = re.compile(r"[Xx]-[A-Za-z0-9-]+|[A-Z][a-z0-9]*+(?:-[A-Z][a-z0-9]*+)++")
_HEADER_COLON = re.compile(r"[\"']?[ \t]*+:[ \t]*+\S")
# A name (``DB_PASSWORD``, ``--api-key``, ``apiKey``) that may hold a secret
# word; whether it does is decided by its segments (``_secret_name``).
_NAMED = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]*?(?i:pass|pw|secret|credential|token|key|auth|bearer"
                    r"|cookie)[A-Za-z0-9_-]*+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SECRET_SEGMENTS = frozenset({"password", "passwd", "pass", "pwd", "secret", "credential", "credentials", "token",
                              "apikey", "authorization", "bearer", "cookie"})
_SECRET_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key")})
# The shell's working directories (``$PWD``, ``$(pwd)``) and the SSH agent's socket.
_PLAIN_NAMES = frozenset({"pwd", "oldpwd", "ssh_auth_sock"})
# Words that make a name secret wherever they sit in it (``PGPASSWORD``,
# ``clientsecret``): a false alarm only costs a preview its Allow.
_SECRET_WORDS = ("password", "passwd", "passphrase", "secret", "credential")
# Segments that make a name secret only in an environment variable's name, a
# command-line flag or a header name (``HTTP_AUTH``, ``DB_PW``, ``--auth``,
# ``X-Auth-Key:``): in code and paths they are ordinary (``useAuth``,
# ``auth/views.py``).
_FLAG_SEGMENTS = frozenset({"auth", "pw"})
# Header names whose last segment ``key`` names no secret (``Idempotency-Key``, ``X-Cache-Key``),
# by the segment before it, and one by its whole name.
_PLAIN_KEY_SEGMENTS = frozenset({"idempotency", "cache", "partition", "sort", "primary", "foreign"})
_PLAIN_KEY_HEADERS = frozenset({"sec-websocket-key"})


def holds_credential(value: object) -> bool:
    """Whether a JSON value (a call's input) may hold a credential: any string
    in it, a key included, holds a recognizable token shape (API keys and
    tokens, a private key's BEGIN or END line, a URL's user and password or a
    token-long user, ``user:password`` after a login flag such as ``curl -u``)
    or a secret-sounding name (``password``, ``pwd``, ``secret``, ``token``,
    ``api_key``, ``authorization``, ``bearer``, ``cookie``, ...) as a word or a
    name's segment; in an environment variable's name, a flag or a header's
    name before its colon also ``auth``, ``pw`` or a last segment ``key``
    (``HTTP_AUTH``, ``OPENAI_KEY``, ``--signing-key``, ``X-Master-Key:``; not
    a header's non-secret key, such as ``Idempotency-Key:``). A value past
    ``CHECK_BUDGET`` is taken to hold one."""
    strings = _strings(value)
    if strings is None:
        return True
    text = "\n".join(strings)
    return _CREDENTIAL.search(text) is not None or any(
        _secret_name(m[0], header=_HEADER_NAME.fullmatch(m[0]) is not None
                     and _HEADER_COLON.match(text, m.end()) is not None)
        for m in _NAMED.finditer(text))


def mask_shapes(text: str) -> str:
    """Prose (Claude's own words, a question) with every credential
    recognizable by its shape (API keys and tokens, private-key blocks, a
    URL's user and password or token-long user), every ``Authorization`` or
    ``Cookie`` header's value, the password after a login flag's user
    (``curl -u admin:…``) and a token-long word after ``Bearer`` or ``Basic``
    replaced by ``[REDACTED]``. A name before a word says nothing
    about it in prose, so names alone mask nothing."""
    text = _SHAPES.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(rf"\1{REDACTED}@", text)
    text = _HEADER.sub(rf"\1{REDACTED}", text)
    text = _FLAG_PASSWORD.sub(lambda m: f"{m[1] or m[2] or m[3]}{REDACTED}", text)
    return _SCHEME_TOKEN.sub(rf"\1{REDACTED}", text)


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
            stack += value[::-1]  # in order, so a command list reads as its words do
    return strings


def _secret_name(name: str, *, header: bool = False) -> bool:
    """Whether ``name`` says it holds a credential: one of ``_SECRET_WORDS``
    anywhere in it, a secret segment (``DB_TOKEN``, ``apiKey``,
    ``--api-key``) or one ending in ``token`` (``accesstoken``), or, in an
    environment variable's name, a flag or a ``header`` name, one of
    ``_FLAG_SEGMENTS`` or a last segment ``key``, except in a header name a
    key ``_PLAIN_KEY_SEGMENTS`` or ``_PLAIN_KEY_HEADERS`` names; never a
    shorter word inside one (``bypass``, ``max_tokens``, ``monkey``)."""
    lowered = name.lower()
    if lowered in _PLAIN_NAMES:
        return False
    if any(word in lowered for word in _SECRET_WORDS):
        return True
    segments = [s.lower() for part in re.split("[_-]", name) for s in _CAMEL.findall(part)]
    if (any(s in _SECRET_SEGMENTS or s.endswith("token") for s in segments)
            or any(p in _SECRET_PAIRS for p in zip(segments, segments[1:]))):
        return True
    flag = name.startswith("-") or name.isupper()
    if not (header or flag):
        return False
    if any(s in _FLAG_SEGMENTS for s in segments):
        return True
    return len(segments) > 1 and segments[-1] == "key" and (
        flag or not (segments[-2] in _PLAIN_KEY_SEGMENTS or lowered in _PLAIN_KEY_HEADERS))
