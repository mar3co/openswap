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
# A URL's ``user:password@``, or a user alone as long as a token (``https://<token>@github.com``).
# ASCII whitespace only: a password may hold a space outside ASCII, shown escaped.
_USERINFO = (r"(?i:(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*+://)"
             r"(?:[^/ \t\r\n\f\v@:\"'<>]*+:[^/ \t\r\n\f\v@\"'<>]++|[A-Za-z0-9_-]{20,}+)@)")
# ``user:password`` given to a login flag (``curl -u``, ``psql -U``, ``http --auth``); the user is
# bounded so that flags packed into one long word stay linear. Not a ``uid:gid``, a format
# (``date -u +%H:%M``), a substitution, another flag, or a URL or path after the colon.
_FLAG_USER = (r"(?<![A-Za-z0-9_-])(?:-[uUa]|--(?:(?:proxy-)?user|auth)(?=[=\s]))(?:=|\s*+)[\"']?"
              r"(?!\d{1,10}+:\d{1,10}+(?![^\s\"']))(?!-)[^\s:\"'%$()]{0,128}+:(?![/\"'])")
_CREDENTIAL = re.compile("|".join([*_TOKENS, rf"-----(?:BEGIN|END){_KEY_LABEL}", _USERINFO, rf"{_FLAG_USER}\S"]))

_SHAPES = re.compile("|".join([rf"-----BEGIN{_KEY_LABEL}.*?(?:-----END{_KEY_LABEL}|\Z)", *_TOKENS]), re.DOTALL)
_URL_PASSWORD = re.compile(_USERINFO)
_FLAG_PASSWORD = re.compile(rf"({_FLAG_USER})[^\s\"'`]++")
# An ``Authorization`` or ``Cookie`` header (``HTTP_AUTHORIZATION`` too, and a quoted name, as in
# JSON): a quoted value up to its closing quote, or an unquoted one up to the line's end or a
# quote, quoted parts after ``=`` included (``sessionid="…"``, ``Digest response="…"``).
_HEADER = re.compile(r"(?i)(?<![A-Za-z0-9_])((?:[A-Za-z0-9]++_)*+(?:(?:proxy-)?authorization|(?:set-)?cookie)"
                     r"[\"']?[ \t]*+:[ \t]*+[\"']?)(?:(?<=\")[^\r\n\"]*+|(?<=')[^\r\n']*+"
                     r"|(?:[^\r\n\"']|(?<==)\"[^\r\n\"]*+\"?|(?<==)'[^\r\n']*+'?)++)")
# A scheme's word before a token-long word with a digit, as a header value is written (not a path
# or an identifier in prose: ``basic tests/test_x.py``).
_SCHEME_TOKEN = re.compile(rf"(?i){_START}((?:bearer|basic)[ \t]++)(?=[A-Za-z_.~+/=-]*+\d)[A-Za-z0-9_.~+/=-]{{20,}}+")
# A header-style name before its colon (``X-Auth-Key: …``): read like a flag.
_HEADER_COLON = re.compile(r"[ \t]*+:")
# A name (``DB_PASSWORD``, ``--api-key``, ``apiKey``) that may hold a secret
# word; whether it does is decided by its segments (``_secret_name``).
_NAMED = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]*?(?i:pass|pw|secret|credential|token|key|auth|bearer"
                    r"|cookie)[A-Za-z0-9_-]*+")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_SECRET_SEGMENTS = frozenset({"password", "passwd", "pwd", "secret", "credential", "credentials", "token",
                              "apikey", "authorization", "bearer", "cookie"})
_SECRET_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key")})
# The shell's working directories (``$PWD``, ``$(pwd)``) and the SSH agent's socket.
_PLAIN_NAMES = frozenset({"pwd", "oldpwd", "ssh_auth_sock"})
# Words that make a name secret wherever they sit in it (``PGPASSWORD``,
# ``clientsecret``): a false alarm only costs a preview its Allow.
_SECRET_WORDS = ("password", "passwd", "passphrase", "secret", "credential")
# Segments that make a name secret only in an environment variable's name, a
# command-line flag or a header name (``HTTP_AUTH``, ``DB_PASS``, ``--auth``,
# ``X-Auth-Key:``): in code, prose and paths they are ordinary (``useAuth``,
# ``auth/views.py``, Python's ``pass``).
_FLAG_SEGMENTS = frozenset({"auth", "pw", "pass"})


def holds_credential(value: object) -> bool:
    """Whether a JSON value (a call's input) may hold a credential: any string
    in it, a key included, holds a recognizable token shape (API keys and
    tokens, a private key's BEGIN or END line, a URL's user and password or a
    token-long user, ``user:password`` after a login flag such as ``curl -u``)
    or a secret-sounding name (``password``, ``pwd``, ``secret``, ``token``,
    ``api_key``, ``authorization``, ``bearer``, ``cookie``, ...) as a word or a
    name's segment; in an environment variable's name or a flag also ``auth``,
    ``pw`` or a last segment ``key`` (``HTTP_AUTH``, ``OPENAI_KEY``,
    ``--signing-key``). A value past ``CHECK_BUDGET`` is taken to hold one."""
    strings = _strings(value)
    if strings is None:
        return True
    text = "\n".join(strings)
    return _CREDENTIAL.search(text) is not None or any(
        _secret_name(m[0], header="-" in m[0] and _HEADER_COLON.match(text, m.end()) is not None)
        for m in _NAMED.finditer(text))


def mask_shapes(text: str) -> str:
    """Prose (Claude's own words, a question) with every credential
    recognizable by its shape (API keys and tokens, private-key blocks, a
    URL's user and password or token-long user), every ``Authorization`` or
    ``Cookie`` header's value, and a token-long word after ``Bearer`` or
    ``Basic`` replaced by ``[REDACTED]``. A name before a word says nothing
    about it in prose, so names alone mask nothing."""
    text = _SHAPES.sub(REDACTED, text)
    text = _URL_PASSWORD.sub(rf"\1{REDACTED}@", text)
    text = _HEADER.sub(rf"\1{REDACTED}", text)
    text = _FLAG_PASSWORD.sub(rf"\1{REDACTED}", text)
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
    ``_FLAG_SEGMENTS`` or a last segment ``key``; never a shorter word inside
    one (``bypass``, ``max_tokens``, ``monkey``)."""
    lowered = name.lower()
    if lowered in _PLAIN_NAMES:
        return False
    if any(word in lowered for word in _SECRET_WORDS):
        return True
    segments = [s.lower() for part in re.split("[_-]", name) for s in _CAMEL.findall(part)]
    if (any(s in _SECRET_SEGMENTS or s.endswith("token") for s in segments)
            or any(p in _SECRET_PAIRS for p in zip(segments, segments[1:]))):
        return True
    return (header or name.startswith("-") or name.isupper()) and (
        any(s in _FLAG_SEGMENTS for s in segments) or (len(segments) > 1 and segments[-1] == "key"))
