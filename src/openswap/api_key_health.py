"""Whether a managed Console API key still works (issue #97).

An API key has no usage endpoint, so OpenSwap could not tell a revoked key or
an empty credit balance from a healthy one: the engine kept landing on it and
Claude Code failed on every request. A free ``POST /v1/messages/count_tokens``
with the key answers the question:

- 200: the key authenticates.
- 401: the key is revoked or invalid.
- An error that names the credit balance ("Your credit balance is too low…"):
  the organization has no credit left. Anthropic does not document whether
  count_tokens checks the balance, so this verdict is only ever taken from
  that explicit error, never inferred.
- Anything else (network, 429, 5xx, a retired probe model): unknown. The
  previous verdict stands and the probe retries later.

What the probe cannot see: a key whose organization holds only the monthly
API credit included with Max and Team plans authenticates and has a balance,
but that credit does not cover Claude Code, which reports "Credit balance too
low" itself. The token page and the docs say so instead.

Verdicts live in ``<backup_root>/cache/api_key_health.json`` keyed by the
key's content fingerprint (``oauth.credential_fingerprint``), never the key:
a replaced key is a new fingerprint and is probed afresh. The file is a cache;
writes are serialized by a file lock next to it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from openswap.exceptions import LockError
from openswap.fsutil import replace_with_retry
from openswap.locking import FileLock
from openswap.oauth import credential_fingerprint

_logger = logging.getLogger(__name__)

VERDICT_OK = "ok"
VERDICT_REVOKED = "revoked"
VERDICT_NO_CREDIT = "no-credit"

COUNT_TOKENS_URL = "https://api.anthropic.com/v1/messages/count_tokens"
# Any active model works; a retired one turns every probe into "unknown",
# never into a false verdict.
PROBE_MODEL = "claude-haiku-5-5"
PROBE_TIMEOUT_S = 5

# A healthy key is rechecked every half hour; a dead one, or one whose last
# probe was inconclusive, every five minutes, so a top-up or a network blip
# clears within one short wait.
OK_TTL_S = 30 * 60
RETRY_TTL_S = 5 * 60
# Entries for keys not seen for this long are dropped on the next write.
PRUNE_AFTER_S = 30 * 24 * 3600

CACHE_NAME = "api_key_health.json"


def probe(key: str) -> str | None:
    """One count_tokens call: a verdict, or None when the answer is unknown."""
    body = json.dumps(
        {"model": PROBE_MODEL, "messages": [{"role": "user", "content": "ping"}]}
    ).encode()
    req = urllib.request.Request(
        COUNT_TOKENS_URL,
        data=body,
        method="POST",
        headers={
            "x-api-key": key.strip(),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": "openswap/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            resp.read()
        return VERDICT_OK
    except urllib.error.HTTPError as e:
        return classify_error(e.code, _error_message(e))
    except Exception as e:  # network, timeout, TLS: unknown, retried later
        _logger.debug("API key probe failed: %s", type(e).__name__)
        return None


def classify_error(status: int, message: str) -> str | None:
    """Map an error response to a verdict; None when it proves nothing."""
    if status == 401:
        return VERDICT_REVOKED
    if status in (400, 402, 403) and "credit balance" in message.lower():
        return VERDICT_NO_CREDIT
    _logger.debug("API key probe inconclusive: HTTP %s", status)
    return None


def _error_message(e: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(e.read().decode())
    except Exception:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else ""


class KeyHealth:
    """Cached verdicts per key fingerprint, probed when due."""

    def __init__(self, cache_dir: Path, *, prober=None, clock=time.time):
        self._path = Path(cache_dir) / CACHE_NAME
        self._prober = prober
        self._clock = clock

    def verdict(self, key: str) -> str | None:
        """The cached verdict for ``key`` (any age), or None."""
        fp = credential_fingerprint(key)
        entry = self._read().get(fp) if fp else None
        verdict = entry.get("verdict") if isinstance(entry, dict) else None
        return verdict if verdict in (VERDICT_OK, VERDICT_REVOKED, VERDICT_NO_CREDIT) else None

    def due(self, key: str) -> bool:
        """Whether ``key`` should be probed now."""
        fp = credential_fingerprint(key)
        if not fp:
            return False
        entry = self._read().get(fp)
        if not isinstance(entry, dict):
            return True
        checked = entry.get("checkedAt")
        if not isinstance(checked, (int, float)):
            return True
        ttl = OK_TTL_S if entry.get("verdict") == VERDICT_OK else RETRY_TTL_S
        return self._clock() - checked >= ttl

    def check(self, key: str) -> str | None:
        """Probe ``key`` and record the outcome; returns the verdict now in force."""
        return self.check_all([key]).get(key)

    def check_all(self, keys: list[str]) -> dict[str, str | None]:
        """Probe ``keys`` in parallel and record every outcome in one write.

        Returns each key's verdict now in force, so the caller never depends
        on rereading a file another collector may be replacing. The
        read-modify-write runs under a file lock: two passes (the extra and
        the auto engine) probing different keys keep both verdicts. An
        inconclusive probe keeps the previous verdict but still stamps the
        attempt, so a dead network is not hammered every pass.
        """
        by_fp = {fp: key for key in keys if (fp := credential_fingerprint(key))}
        if not by_fp:
            return {}
        prober = self._prober or probe
        with ThreadPoolExecutor(max_workers=len(by_fp)) as executor:
            results = dict(zip(by_fp, executor.map(prober, by_fp.values())))
        verdicts: dict[str, str | None] = {}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(self._path.with_name(CACHE_NAME + ".lock"), timeout=5.0):
                data = self._read()
                for fp, result in results.items():
                    previous = data.get(fp) if isinstance(data.get(fp), dict) else {}
                    verdict = result if result is not None else previous.get("verdict")
                    entry = {"checkedAt": self._clock()}
                    if verdict is not None:
                        entry["verdict"] = verdict
                    data[fp] = entry
                    verdicts[by_fp[fp]] = verdict
                self._write(data)
        except (LockError, OSError):
            # Unrecorded this pass (the next one probes again), but this
            # pass still acts on what it just learned.
            verdicts = {by_fp[fp]: result for fp, result in results.items()}
        return verdicts

    def _read(self) -> dict:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        keys = raw.get("keys") if isinstance(raw, dict) else None
        return dict(keys) if isinstance(keys, dict) else {}

    def _write(self, keys: dict) -> None:
        now = self._clock()
        keys = {
            fp: entry
            for fp, entry in keys.items()
            if isinstance(entry, dict)
            and isinstance(entry.get("checkedAt"), (int, float))
            and now - entry["checkedAt"] < PRUNE_AFTER_S
        }
        try:
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".api_key_health.")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"keys": keys}, f)
                replace_with_retry(tmp, self._path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as e:
            _logger.debug("Could not save API key health: %s", type(e).__name__)
