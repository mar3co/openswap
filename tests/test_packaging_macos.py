"""Invariants for the macOS spike freeze (plan 007). No freeze, no Keychain."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging" / "macos"
FORBIDDEN_IDENTITY = "Developer ID Application:"


def test_project_publishes_only_the_openswap_command():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"] == {"openswap": "openswap.cli:main"}


def test_widget_runtime_has_no_legacy_cswap_path_or_entitlement():
    widget = ROOT / "macos" / "OpenSwapWidget" / "Widget"
    for name in ("Snapshot.swift", "Widget.entitlements"):
        assert "cswap" not in (widget / name).read_text(encoding="utf-8").lower()


def _tracked_text_files(*dirs: Path) -> list[Path]:
    skip = {"dist", "build", "__pycache__"}
    out: list[Path] = []
    for directory in dirs:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if skip.intersection(path.parts):
                continue
            out.append(path)
    return out


def test_packaging_files_do_not_embed_signing_identity():
    hits = []
    for path in _tracked_text_files(
        PACKAGING,
        ROOT / "macos",
        ROOT / ".github" / "workflows",
    ):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if FORBIDDEN_IDENTITY in text:
            hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"signing identity string leaked into {hits}"


@pytest.mark.skipif(sys.platform == "win32", reason="ci-import is a bash script for macOS runners")
def test_ci_import_signing_keychain_requires_p12():
    script = PACKAGING / "ci-import-signing-keychain.sh"
    assert script.is_file(), "CI keychain import script is missing"
    env = os.environ.copy()
    env.pop("SIGNING_APPLICATION_P12_BASE64", None)
    env["RUNNER_TEMP"] = str(PACKAGING)
    result = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    combined = result.stderr + result.stdout
    assert "SIGNING_APPLICATION_P12_BASE64" in combined


def test_build_sh_unsigned_path_is_success_not_a_stop():
    """Local Macs without a signing identity still produce a freeze."""
    text = (PACKAGING / "build.sh").read_text(encoding="utf-8")
    assert "OPENSWAP_SIGN_IDENTITY" in text
    # The unsigned freeze used to `exit 2` (plan 007 Step 0 STOP). Signing
    # moved to GitHub Actions; a missing identity must not fail the freeze.
    unsigned_block = text.split("IDENTITY", 1)[-1]
    assert "exit 2" not in unsigned_block
    assert "exit 0" in unsigned_block
