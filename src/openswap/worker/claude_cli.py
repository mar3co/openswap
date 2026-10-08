"""The Claude Code CLI build Remote tasks may run: the owner's own install, pinned by hash.

Codex has one official release asset with a published digest, so OpenSwap
installs exactly that. Claude Code is different: the owner installs and
updates it themselves (Homebrew cask or Anthropic's native installer), it
updates often, and the decision memo requires the unmodified binary under the
owner's own login. So OpenSwap never downloads or replaces it. Instead,
``openswap worker claude pin`` records the resolved path, ``--version`` and
SHA-256 of the binary installed now, the live check measures that exact
binary, live execution is bound to its hash, and every launch re-hashes it.
Any update (a new hash) disables Claude jobs until the owner pins and checks
again; jobs also run with the auto-updater off.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openswap.worker.codex_cli import CodexCliError, platform_supported, sha256_file

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)? \(Claude Code\)$")
# ``~/.claude/local`` (the old npm-local install) is deliberately absent: jobs
# run with ``~/.claude`` hidden by Seatbelt, so a binary there could not start.
CANDIDATES = ("~/.local/bin/claude", "/opt/homebrew/bin/claude", "/usr/local/bin/claude")


def _hidden_from_jobs(binary: Path, backup_root: Path | None = None) -> bool:
    """Whether the job's Seatbelt profile hides this path (so the binary could not start).

    The profile hides the default Claude and Codex folders and the backup
    root (see ``claude_exec.seatbelt_profile``).
    """
    try:
        import pwd

        home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, AttributeError):
        home = Path.home()
    hidden = [home / ".claude", home / ".codex"] + ([Path(backup_root)] if backup_root is not None else [])
    target = Path(os.path.realpath(binary))
    return any(target.is_relative_to(Path(os.path.realpath(folder))) for folder in hidden)


class ClaudeCliError(RuntimeError):
    """A safe, path-free reason the pinned Claude Code CLI is unavailable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PinnedClaude:
    binary: Path
    version: str
    binary_sha256: str

    def to_dict(self) -> dict:
        return {"version": self.version, "binary_sha256": self.binary_sha256}


def pinned_version(backup_root: Path) -> str | None:
    """The pinned version string, from the pin file only (no hashing); None if unreadable."""
    try:
        raw = json.loads(pin_path(backup_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    version = raw.get("version") if isinstance(raw, dict) else None
    return version if isinstance(version, str) and _VERSION_RE.fullmatch(version) else None


def pin_path(backup_root: Path) -> Path:
    return Path(backup_root) / "worker" / "claude-cli" / "pin.json"


def find_installed(which=shutil.which) -> Path | None:
    """The installed ``claude``, resolved through symlinks to the real binary."""
    found = which("claude")
    candidates = [found] if found else []
    candidates += [os.path.expanduser(path) for path in CANDIDATES]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and not _hidden_from_jobs(Path(candidate)):
            return Path(os.path.realpath(candidate))
    return None


def _version(binary: Path, run) -> str:
    scratch = tempfile.mkdtemp(prefix="openswap-claude-version-")
    try:
        os.makedirs(os.path.join(scratch, "config"), mode=0o700)
        result = run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=30, check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": scratch, "CLAUDE_CONFIG_DIR": os.path.join(scratch, "config"),
                 "DISABLE_AUTOUPDATER": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        raise ClaudeCliError("version_probe_failed") from None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    version = (result.stdout or "").strip()
    if result.returncode != 0 or not _VERSION_RE.fullmatch(version):
        raise ClaudeCliError("version_unrecognized")
    return version


def _check_binary(binary: Path, backup_root: Path | None = None) -> None:
    if _hidden_from_jobs(binary, backup_root):
        raise ClaudeCliError("binary_in_claude_config")
    try:
        info = binary.lstat()
    except OSError:
        raise ClaudeCliError("not_installed") from None
    if not stat.S_ISREG(info.st_mode) or not os.access(binary, os.X_OK):
        raise ClaudeCliError("binary_unsafe")
    # Writable by others means anyone could change what the pin vouches for.
    if os.name != "nt" and info.st_mode & 0o022:
        raise ClaudeCliError("binary_permissions")


def pinned_copies_dir() -> Path:
    """Where pinned Claude Code binaries are kept: private, and outside every folder
    the job sandbox hides (Application Support, next to the job locks)."""
    from openswap.worker.containment import default_lock_dir

    return default_lock_dir().parent / "claude-cli"


def _private_copy(source: Path, digest: str) -> Path:
    """Copy ``source`` to a read-only file in :func:`pinned_copies_dir` named by its digest.

    Jobs execute this copy, so an update or replacement of the installed
    binary between the hash and the launch can never run unverified.
    """
    from openswap.worker.containment import ensure_private_dir

    store = pinned_copies_dir()
    store.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        ensure_private_dir(store)
    except Exception:
        raise ClaudeCliError("binary_unsafe") from None
    dest = store / f"claude-{digest[:32]}"
    try:
        if dest.exists() and not dest.is_symlink() and sha256_file(dest) == digest:
            return dest
    except (OSError, CodexCliError):
        pass
    fd, tmp = tempfile.mkstemp(dir=str(store), prefix=".claude.")
    try:
        with os.fdopen(fd, "wb") as out, open(source, "rb") as src:
            shutil.copyfileobj(src, out, 1024 * 1024)
        os.chmod(tmp, 0o500)
        os.replace(tmp, dest)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ClaudeCliError("binary_unsafe") from None
    return dest


def pin(backup_root: Path, *, binary: Path | None = None, run=subprocess.run, which=shutil.which,
        supported: bool | None = None) -> PinnedClaude:
    """Pin the installed Claude Code: a private, read-only copy of it, with its version
    and SHA-256 (replacing an older pin). Jobs run that copy, never the installed path."""
    if not (platform_supported() if supported is None else supported):
        raise ClaudeCliError("unsupported_platform")
    source = Path(os.path.realpath(binary)) if binary is not None else find_installed(which)
    if source is None:
        raise ClaudeCliError("not_installed")
    _check_binary(source, backup_root)
    try:
        source_digest = sha256_file(source)
    except (OSError, CodexCliError):
        raise ClaudeCliError("binary_unsafe") from None
    binary = _private_copy(source, source_digest)
    _check_binary(binary, backup_root)
    try:
        digest = sha256_file(binary)
    except (OSError, CodexCliError):
        raise ClaudeCliError("binary_unsafe") from None
    if digest != source_digest:
        raise ClaudeCliError("binary_changed")  # replaced while it was being copied
    version = _version(binary, run)
    target = pin_path(backup_root)
    target.parent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.mkdir(mode=0o700, exist_ok=True)
    document = {"binary": str(binary), "source": str(source), "version": version, "binary_sha256": digest,
                "pinned_at": datetime.now(timezone.utc).isoformat()}
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".pin.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
    return PinnedClaude(binary, version, digest)


def verify(backup_root: Path, *, run=subprocess.run, supported: bool | None = None,
           check_version: bool = True) -> PinnedClaude:
    """The pinned binary, re-hashed (and version-checked); raises if anything changed."""
    if not (platform_supported() if supported is None else supported):
        raise ClaudeCliError("unsupported_platform")
    try:
        raw = json.loads(pin_path(backup_root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ClaudeCliError("not_pinned") from None
    except (OSError, ValueError):
        raise ClaudeCliError("pin_invalid") from None
    if (not isinstance(raw, dict) or not isinstance(raw.get("binary"), str)
            or not isinstance(raw.get("version"), str)
            or not isinstance(raw.get("binary_sha256"), str) or not _HEX64.fullmatch(raw["binary_sha256"])):
        raise ClaudeCliError("pin_invalid")
    binary = Path(raw["binary"])
    _check_binary(binary, backup_root)
    try:
        actual = sha256_file(binary)
    except (OSError, CodexCliError):
        raise ClaudeCliError("binary_unsafe") from None
    if actual != raw["binary_sha256"]:
        # Updated since it was pinned and checked: pin and check again.
        raise ClaudeCliError("binary_changed")
    if check_version and _version(binary, run) != raw["version"]:
        raise ClaudeCliError("version_mismatch")
    return PinnedClaude(binary, raw["version"], actual)
