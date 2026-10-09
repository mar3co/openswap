"""The one Codex CLI build Remote tasks will run: official 0.157.1, hash-pinned.

Plan 017 phase 1 requires a supported, version-pinned CLI. The worker never
runs whatever ``codex`` is on ``PATH`` or inside ChatGPT.app (that one is a
pre-release). ``install`` fetches the official GitHub release asset, refuses it
unless its SHA-256 equals the digest OpenAI published for that asset, and
unpacks its single binary into OpenSwap's private worker directory. The
binary's own SHA-256 is recorded then and re-checked before every use, along
with its ``--version`` output. Anything else is refused.

Only Apple silicon is supported: the published digest is for the
``aarch64-apple-darwin`` asset (spike-stable-cli.md records it).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

CODEX_VERSION = "0.157.1"
CODEX_VERSION_OUTPUT = f"codex-cli {CODEX_VERSION}"
RELEASE_TAG = f"rust-v{CODEX_VERSION}"
ASSET_NAME = "codex-aarch64-apple-darwin.tar.gz"
ASSET_MEMBER = "codex-aarch64-apple-darwin"
ASSET_URL = f"https://github.com/openai/codex/releases/download/{RELEASE_TAG}/{ASSET_NAME}"
# Published on the release's expanded assets page; verified locally on 2026-09-27.
ASSET_SHA256 = "3c45b162b7a76f51325015b1d0a8112c73219b7a9b59cd5762c37c9ba55894fa"
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_BINARY_BYTES = 512 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CodexCliError(RuntimeError):
    """A safe, path-free reason the pinned CLI is unavailable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PinnedCodex:
    binary: Path
    version: str
    archive_sha256: str
    binary_sha256: str

    def to_dict(self) -> dict:
        return {
            "version": self.version, "archive_sha256": self.archive_sha256,
            "binary_sha256": self.binary_sha256, "release": RELEASE_TAG, "asset": ASSET_NAME,
        }


def install_dir(backup_root: Path) -> Path:
    return Path(backup_root) / "worker" / "codex-cli" / CODEX_VERSION


def binary_path(backup_root: Path) -> Path:
    return install_dir(backup_root) / "codex"


def manifest_path(backup_root: Path) -> Path:
    return install_dir(backup_root) / "manifest.json"


def platform_supported(system: str | None = None, machine: str | None = None) -> bool:
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    return system == "darwin" and machine == "arm64"


def _private_dirs(path: Path, stop: Path) -> None:
    """Create ``path`` and its parents under ``stop`` as private (0700) directories."""
    chain = []
    current = path
    while current != stop and stop in current.parents:
        chain.append(current)
        current = current.parent
    for directory in reversed(chain):
        directory.mkdir(mode=0o700, exist_ok=True)
        info = directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CodexCliError("install_dir_unsafe")
        if os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise CodexCliError("install_dir_unsafe")


def sha256_file(path: Path, limit: int = MAX_BINARY_BYTES) -> str:
    digest = hashlib.sha256()
    total = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise CodexCliError("binary_unsafe")
        while chunk := os.read(fd, 1 << 20):
            total += len(chunk)
            if total > limit:
                raise CodexCliError("binary_too_large")
            digest.update(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _download(url: str, opener: Callable | None = None) -> bytes:
    if opener is None:
        handlers = []
        try:
            import ssl

            import truststore

            handlers.append(urllib.request.HTTPSHandler(context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)))
        except Exception:
            pass
        opener = urllib.request.build_opener(*handlers).open
    try:
        with opener(url, timeout=60) as response:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
    except OSError:
        raise CodexCliError("download_failed") from None
    if len(data) > MAX_ARCHIVE_BYTES:
        raise CodexCliError("archive_too_large")
    return data


def _extract_binary(archive: bytes) -> bytes:
    """The single expected regular-file member of the release archive."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            members = tar.getmembers()
            if len(members) != 1:
                raise CodexCliError("archive_unexpected_layout")
            member = members[0]
            if member.name != ASSET_MEMBER or not member.isreg() or member.size > MAX_BINARY_BYTES:
                raise CodexCliError("archive_unexpected_layout")
            stream = tar.extractfile(member)
            if stream is None:
                raise CodexCliError("archive_unexpected_layout")
            return stream.read(MAX_BINARY_BYTES + 1)
    except (tarfile.TarError, OSError, EOFError):
        raise CodexCliError("archive_unreadable") from None


def install(
    backup_root: Path,
    *,
    archive_path: Path | None = None,
    opener: Callable | None = None,
    run=subprocess.run,
    supported: bool | None = None,
) -> PinnedCodex:
    """Fetch (or read ``archive_path``), verify and unpack the pinned CLI.

    Refuses any archive whose SHA-256 is not the published digest; nothing
    is unpacked from it. Reinstalling the same release replaces the files.
    """
    if not (platform_supported() if supported is None else supported):
        raise CodexCliError("unsupported_platform")
    if archive_path is not None:
        try:
            with open(archive_path, "rb") as handle:
                archive = handle.read(MAX_ARCHIVE_BYTES + 1)
        except OSError:
            raise CodexCliError("archive_unreadable") from None
        if len(archive) > MAX_ARCHIVE_BYTES:
            raise CodexCliError("archive_too_large")
    else:
        archive = _download(ASSET_URL, opener)
    archive_sha = hashlib.sha256(archive).hexdigest()
    if archive_sha != ASSET_SHA256:
        raise CodexCliError("archive_hash_mismatch")
    binary = _extract_binary(archive)
    root = Path(backup_root)
    target_dir = install_dir(root)
    _private_dirs(target_dir, root)
    fd, tmp = tempfile.mkstemp(dir=str(target_dir), prefix=".codex.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(binary)
        os.chmod(tmp, 0o700)
        os.replace(tmp, binary_path(root))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    binary_sha = hashlib.sha256(binary).hexdigest()
    manifest = {
        "version": CODEX_VERSION, "release": RELEASE_TAG, "asset": ASSET_NAME,
        "archive_sha256": archive_sha, "binary_sha256": binary_sha,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    fd, tmp = tempfile.mkstemp(dir=str(target_dir), prefix=".manifest.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, manifest_path(root))
    return verify(root, run=run, supported=True)


def verify(backup_root: Path, *, run=subprocess.run, supported: bool | None = None,
           check_version: bool = True) -> PinnedCodex:
    """The installed pinned CLI, re-hashed and version-checked; raises otherwise."""
    if not (platform_supported() if supported is None else supported):
        raise CodexCliError("unsupported_platform")
    root = Path(backup_root)
    binary = binary_path(root)
    try:
        info = install_dir(root).lstat()
    except OSError:
        raise CodexCliError("not_installed") from None
    if stat.S_ISLNK(info.st_mode) or (os.name != "nt" and (info.st_uid != os.getuid() or info.st_mode & 0o077)):
        raise CodexCliError("install_dir_unsafe")
    try:
        raw = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CodexCliError("not_installed") from None
    except (OSError, ValueError):
        raise CodexCliError("manifest_invalid") from None
    if (
        not isinstance(raw, dict) or raw.get("version") != CODEX_VERSION
        or raw.get("archive_sha256") != ASSET_SHA256
        or not isinstance(raw.get("binary_sha256"), str) or not _HEX64.fullmatch(raw["binary_sha256"])
    ):
        raise CodexCliError("manifest_invalid")
    try:
        actual = sha256_file(binary)
    except FileNotFoundError:
        raise CodexCliError("not_installed") from None
    except OSError:
        raise CodexCliError("binary_unsafe") from None
    if actual != raw["binary_sha256"]:
        raise CodexCliError("binary_hash_mismatch")
    if check_version:
        version_home = install_dir(root) / "version-home"
        _private_dirs(version_home, root)
        try:
            result = run(
                [str(binary), "--version"], capture_output=True, text=True, timeout=20,
                env={"PATH": "/usr/bin:/bin", "HOME": str(install_dir(root)),
                     "CODEX_HOME": str(version_home)},
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise CodexCliError("version_probe_failed") from None
        if result.returncode != 0 or (result.stdout or "").strip() != CODEX_VERSION_OUTPUT:
            raise CodexCliError("version_mismatch")
    return PinnedCodex(binary, CODEX_VERSION_OUTPUT, ASSET_SHA256, actual)
