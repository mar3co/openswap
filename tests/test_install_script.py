"""install.sh: one command from a bare Mac to a running menu bar extra.

Every external tool is a stub on PATH that appends its argv to a log and
fails on demand through FAIL_* env knobs, so each branch is observable
without network or launchd.
"""

from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "install.sh"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash installer")

UV_STUB = r'''#!/bin/sh
echo "uv $*" >> "$STUB_LOG"
case "$1 $2" in
  "tool dir") [ -n "${FAIL_TOOL_DIR:-}" ] && exit 2; echo "$HOME/.local/bin" ;;
  "tool install")
    [ -n "${FAIL_UV_INSTALL:-}" ] && exit 1
    mkdir -p "$HOME/.local/bin"
    printf '#!/bin/sh\necho "openswap $*" >> "$STUB_LOG"\nexit "${FAIL_SETUP:-0}"\n' > "$HOME/.local/bin/openswap"
    chmod +x "$HOME/.local/bin/openswap"
    ;;
  "tool update-shell") exit "${FAIL_UPDATE_SHELL:-0}" ;;
esac
'''

GIT_STUB = r'''#!/bin/sh
echo "git $*" >> "$STUB_LOG"
if [ "$1" = clone ]; then
  for last; do :; done
  mkdir -p "$last/.git" "$last/src/openswap"
  : > "$last/src/openswap/__init__.py"
fi
[ "$3" = pull ] && exit "${FAIL_PULL:-0}"
exit 0
'''

CURL_STUB = r'''#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
cat "$STUB_DIR/uv-installer.sh"
'''

UV_INSTALLER = r'''#!/bin/sh
[ -n "${UV_INSTALLER_NOOP:-}" ] && exit 0
mkdir -p "$HOME/.local/bin"
cp "$STUB_DIR/uv.stub" "$HOME/.local/bin/uv"
chmod +x "$HOME/.local/bin/uv"
'''


def _fake_checkout(root: Path) -> Path:
    (root / ".git").mkdir(parents=True)
    (root / "src" / "openswap").mkdir(parents=True)
    (root / "src" / "openswap" / "__init__.py").write_text("")
    return root


def _write(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def run_installer(
    tmp_path: Path,
    *,
    uname: str = "Darwin",
    clt: bool = True,
    uv: bool = True,
    bin_on_path: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess, list[str]]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"

    _write(stubs / "uname", f'#!/bin/sh\necho "{uname}"\n')
    _write(stubs / "id", '#!/bin/sh\necho "${FAKE_UID:-501}"\n')
    _write(
        stubs / "xcode-select",
        '#!/bin/sh\necho "xcode-select $*" >> "$STUB_LOG"\n'
        '[ "$1" = "--install" ] && exit "${FAIL_CLT_INSTALL:-0}"\n'
        + ("exit 0\n" if clt else "exit 2\n"),
    )
    _write(stubs / "git", GIT_STUB)
    _write(stubs / "curl", CURL_STUB)
    _write(stubs / "uv-installer.sh", UV_INSTALLER)
    (stubs / "uv.stub").write_text(UV_STUB)
    if uv:
        _write(stubs / "uv", UV_STUB)

    path = [str(stubs)]
    if bin_on_path:
        path.append(str(home / ".local" / "bin"))
    path += ["/usr/bin", "/bin"]
    run_env = {
        "HOME": str(home),
        "PATH": ":".join(path),
        "STUB_LOG": str(log),
        "STUB_DIR": str(stubs),
        **(env or {}),
    }
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


class TestInstallScript:
    def test_fresh_mac_installs_uv_clones_and_runs_setup(self, tmp_path):
        proc, calls = run_installer(tmp_path, uv=False, bin_on_path=False)
        assert proc.returncode == 0, proc.stderr
        assert any(c.startswith("curl") and "astral.sh/uv" in c for c in calls)
        clone = next(c for c in calls if c.startswith("git clone"))
        assert clone.endswith(str(tmp_path / "home" / ".openswap"))
        assert "https://github.com/mar3co/openswap.git" in clone
        assert "uv tool install --force --editable .[menubar]" in calls
        assert "openswap setup" in calls
        assert "uv tool update-shell" in calls
        assert "new terminal" in proc.stdout

    def test_existing_uv_on_path_skips_installer_and_shell_edit(self, tmp_path):
        proc, calls = run_installer(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert not any(c.startswith("curl") for c in calls)
        assert "uv tool update-shell" not in calls
        assert "openswap setup" in calls
        assert "new terminal" not in proc.stdout

    def test_existing_uv_off_path_adds_bin_dir_to_shell(self, tmp_path):
        proc, calls = run_installer(tmp_path, bin_on_path=False)
        assert proc.returncode == 0, proc.stderr
        assert not any(c.startswith("curl") for c in calls)
        assert "uv tool update-shell" in calls
        assert "new terminal" in proc.stdout

    def test_rerun_pulls_existing_checkout(self, tmp_path):
        checkout = _fake_checkout(tmp_path / "home" / ".openswap")
        proc, calls = run_installer(tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert not any(c.startswith("git clone") for c in calls)
        assert f"git -C {checkout} pull --ff-only" in calls
        assert "uv tool install --force --editable .[menubar]" in calls

    def test_failed_pull_still_installs_as_is(self, tmp_path):
        _fake_checkout(tmp_path / "home" / ".openswap")
        proc, calls = run_installer(tmp_path, env={"FAIL_PULL": "1"})
        assert proc.returncode == 0, proc.stderr
        assert "Could not update" in proc.stdout
        assert "uv tool install --force --editable .[menubar]" in calls
        assert "openswap setup" in calls

    def test_openswap_dir_override_clones_when_missing(self, tmp_path):
        target = tmp_path / "elsewhere"
        proc, calls = run_installer(tmp_path, env={"OPENSWAP_DIR": str(target)})
        assert proc.returncode == 0, proc.stderr
        assert next(c for c in calls if c.startswith("git clone")).endswith(str(target))

    def test_openswap_dir_existing_checkout_is_not_pulled(self, tmp_path):
        target = _fake_checkout(tmp_path / "mine")
        proc, calls = run_installer(tmp_path, env={"OPENSWAP_DIR": str(target)})
        assert proc.returncode == 0, proc.stderr
        assert not any(c.startswith("git") for c in calls)
        assert "not pulling" in proc.stdout
        assert "uv tool install --force --editable .[menubar]" in calls

    def test_refuses_non_checkout_at_install_dir(self, tmp_path):
        stray = tmp_path / "home" / ".openswap"
        stray.mkdir(parents=True)
        (stray / "notes.txt").write_text("mine")
        proc, calls = run_installer(tmp_path)
        assert proc.returncode == 1
        assert "OPENSWAP_DIR" in proc.stderr
        assert not any(c.startswith("uv tool install") for c in calls)

    def test_refuses_foreign_git_repo_at_install_dir(self, tmp_path):
        stray = tmp_path / "home" / ".openswap"
        (stray / ".git").mkdir(parents=True)
        proc, calls = run_installer(tmp_path)
        assert proc.returncode == 1
        assert "not an OpenSwap checkout" in proc.stderr
        assert not any(" pull " in c or c.startswith("uv tool install") for c in calls)

    def test_worktree_checkout_has_git_file_not_dir(self, tmp_path):
        target = tmp_path / "worktree"
        (target / "src" / "openswap").mkdir(parents=True)
        (target / "src" / "openswap" / "__init__.py").write_text("")
        (target / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
        proc, calls = run_installer(tmp_path, env={"OPENSWAP_DIR": str(target)})
        assert proc.returncode == 0, proc.stderr
        assert "uv tool install --force --editable .[menubar]" in calls

    def test_uv_installer_that_leaves_no_uv_is_reported(self, tmp_path):
        proc, calls = run_installer(tmp_path, uv=False, bin_on_path=False, env={"UV_INSTALLER_NOOP": "1"})
        assert proc.returncode == 1
        assert "still not on PATH" in proc.stderr
        assert not any(c.startswith("git") for c in calls)

    def test_old_uv_without_tool_dir_is_told_to_update(self, tmp_path):
        proc, calls = run_installer(tmp_path, env={"FAIL_TOOL_DIR": "1"})
        assert proc.returncode == 1
        assert "uv self update" in proc.stderr
        assert not any(c.startswith("git") for c in calls)

    def test_failed_tool_install_stops_before_setup(self, tmp_path):
        proc, calls = run_installer(tmp_path, env={"FAIL_UV_INSTALL": "1"})
        assert proc.returncode == 1
        assert "openswap setup" not in calls

    def test_shell_config_failure_does_not_block_setup(self, tmp_path):
        proc, calls = run_installer(tmp_path, bin_on_path=False, env={"FAIL_UPDATE_SHELL": "1"})
        assert proc.returncode == 0, proc.stderr
        assert "Could not update your shell config" in proc.stdout
        assert "openswap setup" in calls

    def test_setup_failure_is_the_exit_status_but_path_hint_still_prints(self, tmp_path):
        proc, calls = run_installer(tmp_path, bin_on_path=False, env={"FAIL_SETUP": "3"})
        assert proc.returncode == 3
        assert "openswap setup" in calls
        assert "new terminal" in proc.stdout

    def test_refuses_root(self, tmp_path):
        proc, calls = run_installer(tmp_path, env={"FAKE_UID": "0"})
        assert proc.returncode == 1
        assert "sudo" in proc.stderr
        assert calls == []

    def test_refuses_non_macos(self, tmp_path):
        proc, calls = run_installer(tmp_path, uname="Linux")
        assert proc.returncode == 1
        assert "macOS" in proc.stderr
        assert calls == []

    def test_missing_command_line_tools_starts_install_and_stops(self, tmp_path):
        proc, calls = run_installer(tmp_path, clt=False)
        assert proc.returncode == 1
        assert "xcode-select --install" in calls
        assert "Finish the Command Line Tools dialog" in proc.stderr
        assert not any(c.startswith("git") or c.startswith("uv") for c in calls)

    def test_command_line_tools_install_refusing_to_start_names_the_reset(self, tmp_path):
        proc, calls = run_installer(tmp_path, clt=False, env={"FAIL_CLT_INSTALL": "1"})
        assert proc.returncode == 1
        assert "xcode-select --reset" in proc.stderr
        assert "dialog" not in proc.stderr
