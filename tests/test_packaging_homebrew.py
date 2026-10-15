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


def test_uninstall_runs_the_apps_hook_instead_of_removing_the_login_item():
    """Homebrew runs the uninstall stanza on upgrade too; `launchctl:` there
    would drop the login item on every `brew upgrade`."""
    cask = _render("1.2.3", ARM, INTEL).stdout
    stanza = cask[cask.index("  uninstall script: {") : cask.index("  zap ")]
    assert "uninstall --homebrew-hook" in stanza
    assert '"#{appdir}/OpenSwap.app/Contents/MacOS/OpenSwap"' in stanza
    # A missing app is nothing to clean up, not a failed `brew uninstall`.
    assert '[ -x "$0" ] || exit 0' in stanza
    assert "must_succeed: false" in stanza
    assert "\n  uninstall launchctl:" not in cask


def test_zap_stops_every_agent_and_trashes_their_logs():
    cask = _render("1.2.3", ARM, INTEL).stdout
    zap = cask[cask.index("  zap ") : cask.index("  caveats")]
    for label in ("menubar", "widget", "worker"):
        assert f'"com.opensoft.openswap.{label}"' in zap
        assert f'"~/Library/Logs/com.opensoft.openswap.{label}.err"' in zap
    assert '"~/Library/Application Support/OpenSwap"' in zap


def test_caveats_are_one_step():
    cask = _render("1.2.3", ARM, INTEL).stdout
    caveats = cask[cask.index("  caveats do") :]
    assert caveats.count(" setup") == 1
    assert "uninstall" not in caveats and "uv tool" not in caveats
