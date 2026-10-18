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


def test_tap_workflow_fails_a_release_it_cannot_push():
    """v0.2.0's push was skipped behind a green run because the secret was
    empty; a missing or unusable token must fail the job and say how to
    recover."""
    workflow = (ROOT / ".github" / "workflows" / "homebrew-tap.yml").read_text(encoding="utf-8")
    assert "HAS_TAP_TOKEN: ${{ secrets.HOMEBREW_TAP_TOKEN != '' }}" in workflow
    assert "::warning::" not in workflow
    push = workflow[workflow.index("      - name: Push cask to tap") :]
    assert "\n        if:" not in push.split("run: |", 1)[0]
    fail = push[push.index("fail() {") : push.index("          }\n")]
    assert "::error " in fail and "HOMEBREW_TAP_TOKEN" in fail
    assert "$TAP_REPO/Casks/openswap.rb by hand" in fail
    assert fail.rstrip().endswith("exit 1")
    steps = push.splitlines()
    assert any(
        '[ "$HAS_TAP_TOKEN" = true ] || fail' in line for line in steps
    ), "a missing token must fail before the clone"
    # The tap is public: only a push proves the token can write, so the
    # dry-run must come before the already-matches exit.
    assert push.index("git push --dry-run origin HEAD || fail") < push.index("already matches")
    assert "git push origin HEAD || fail" in push
    # Published prereleases skip the job; a fixed token re-pushes any tag.
    assert "!github.event.release.prerelease" in workflow
    assert "workflow_dispatch:\n    inputs:\n      tag:" in workflow


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


def test_zap_trashes_every_agents_logs_but_leaves_the_shared_worker_label():
    cask = _render("1.2.3", ARM, INTEL).stdout
    zap = cask[cask.index("  zap ") : cask.index("  caveats")]
    for label in ("menubar", "widget", "worker"):
        assert f'"~/Library/Logs/com.opensoft.openswap.{label}.err"' in zap
    assert '"com.opensoft.openswap.menubar"' in zap
    # `launchctl:` removes by label alone; a uv install's worker shares it,
    # and the hook removes the app's own worker.
    assert '"com.opensoft.openswap.worker"' not in zap
    assert '"~/Library/Application Support/OpenSwap"' in zap


def test_zap_trashes_what_openswap_uninstall_zap_deletes(tmp_path, monkeypatch):
    """`brew uninstall --zap` and a zap without Homebrew leave the same Mac."""
    import re

    from openswap import uninstall

    cask = _render("1.2.3", ARM, INTEL).stdout
    trash = cask[cask.index("      trash:") : cask.index("  caveats")]
    home = tmp_path / "home"
    listed = [home / entry[2:] for entry in re.findall(r'"(~/[^"]+)"', trash)]
    for path in listed:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix in {".log", ".err", ".plist"}:
            path.write_text("x")
        else:
            path.mkdir()
    backup = home / "Library" / "Application Support" / "OpenSwap"
    monkeypatch.setattr("openswap.paths.get_backup_root", lambda: backup)
    monkeypatch.setattr("openswap.paths.get_legacy_backup_root", lambda: backup)
    # All but the worker's job state, which the hook deletes once no task runs.
    job_state = uninstall.job_state_path(home)
    job_state.mkdir(parents=True)
    assert job_state not in listed
    assert sorted(uninstall.data_paths(home)) == sorted([*listed, job_state])


def test_caveats_are_one_step():
    cask = _render("1.2.3", ARM, INTEL).stdout
    caveats = cask[cask.index("  caveats do") :]
    assert caveats.count(" setup") == 1
    assert "uninstall" not in caveats and "uv tool" not in caveats


def test_caveats_offer_the_app_and_the_terminal():
    """Both paths finish setting up (plans 020, 021): the app's setup wizard, or
    `openswap setup`."""
    cask = _render("1.2.3", ARM, INTEL).stdout
    caveats = cask[cask.index("  caveats do") :]
    text = caveats[caveats.index("<<~EOS\n") + len("<<~EOS\n") : caveats.index("    EOS\n")]
    assert text.splitlines() == [
        "      Open OpenSwap from Applications to finish setting up, or run:",
        "        #{openswap} setup",
    ]


def test_caveats_name_the_casks_command_when_another_comes_first_on_path():
    """uv can put its shim in UV_TOOL_BIN_DIR, XDG_BIN_HOME or UV_INSTALL_DIR,
    so the caveat walks the user's PATH instead of checking ~/.local/bin."""
    cask = _render("1.2.3", ARM, INTEL).stdout
    caveats = cask[cask.index("  caveats do") :]
    assert 'ENV.fetch("HOMEBREW_PATH"' in caveats
    assert '"#{HOMEBREW_PREFIX}/bin/openswap"' in caveats
    assert ".local/bin" not in caveats
