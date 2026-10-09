"""The Homebrew cask serves each Mac the zip built for its CPU (plan 008)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RENDER = ROOT / "packaging" / "homebrew" / "render-cask.sh"
ARM = "a" * 64
INTEL = "b" * 64

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="render-cask.sh is a bash script for macOS and Linux runners"
)


def _render(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RENDER), *args], capture_output=True, text=True, check=False
    )


def _block(cask: str, name: str) -> str:
    start = cask.index(f"  {name} do\n")
    return cask[start : cask.index("\n  end\n", start)]


def test_each_architecture_gets_its_own_zip_and_checksum():
    result = _render("1.2.3", ARM, INTEL)
    assert result.returncode == 0, result.stderr
    cask = result.stdout
    assert "@" not in cask  # every @PLACEHOLDER@ was filled
    assert 'version "1.2.3"' in cask

    arm = _block(cask, "on_arm")
    assert f'sha256 "{ARM}"' in arm
    assert "OpenSwap-#{version}-arm64.zip" in arm
    assert INTEL not in arm

    intel = _block(cask, "on_intel")
    assert f'sha256 "{INTEL}"' in intel
    assert "OpenSwap-#{version}-x86_64.zip" in intel
    assert ARM not in intel


def test_one_checksum_is_not_enough():
    result = _render("1.2.3", ARM)
    assert result.returncode != 0
    assert "x86_64-sha256" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("1.2.3", ARM, "B" * 64),
        ("1.2.3", ARM[:-1], INTEL),
        ("1.2/3", ARM, INTEL),
    ],
)
def test_malformed_input_is_refused(args):
    result = _render(*args)
    assert result.returncode != 0
    assert result.stdout == ""


def test_build_names_the_zip_for_its_architecture():
    build = (ROOT / "packaging" / "macos" / "build.sh").read_text(encoding="utf-8")
    assert 'RELEASE_ZIP="$DIST/OpenSwap-$VERSION-$ARCH.zip"' in build
    assert "check_arch" in build.split('plutil -lint "$APP/Contents/Info.plist"', 1)[1]


def test_release_workflow_builds_both_architectures():
    workflow = (ROOT / ".github" / "workflows" / "macos-app.yml").read_text(encoding="utf-8")
    assert "- arch: arm64" in workflow
    assert "- arch: x86_64" in workflow
    assert "-arm64.zip" in workflow and "-x86_64.zip" in workflow
