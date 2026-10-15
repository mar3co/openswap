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

BREW_STUB = r'''#!/bin/sh
echo "brew $*" >> "$STUB_LOG"
case "$1" in
  --prefix) echo "$STUB_DIR/brew-prefix" ;;
  list) [ -x "$STUB_DIR/brew-prefix/bin/openswap" ] ;;
  upgrade) exit "${FAIL_BREW_UPGRADE:-0}" ;;
  install)
    [ -n "${FAIL_BREW_INSTALL:-}" ] && exit 1
    mkdir -p "$STUB_DIR/brew-prefix/bin"
    printf '#!/bin/sh\necho "brew-openswap $*" >> "$STUB_LOG"\nexit "${FAIL_SETUP:-0}"\n' > "$STUB_DIR/brew-prefix/bin/openswap"
    chmod +x "$STUB_DIR/brew-prefix/bin/openswap"
    ;;
esac
'''

# `defaults read <bundle>/Contents/Info CFBundleIdentifier`, from the plist
# XML plistlib writes; launchctl and pluginkit only record. Stubs come first on
# PATH, so the real tools never run (and Linux CI has none of them).
DEFAULTS_STUB = r'''#!/bin/sh
echo "defaults $*" >> "$STUB_LOG"
[ -f "$2.plist" ] || exit 1
grep -A1 "<key>$3</key>" "$2.plist" | sed -n 's:.*<string>\(.*\)</string>.*:\1:p'
'''

RECORD_STUB = r'''#!/bin/sh
echo "$(basename "$0") $*" >> "$STUB_LOG"
[ "$(basename "$0")" = launchctl ] && [ -n "${FAIL_LAUNCHCTL:-}" ] && exit 3
if [ "$(basename "$0")" = pluginkit ]; then
  [ "$1" = -r ] && [ -n "${FAIL_PLUGINKIT_REMOVE:-}" ] && exit 1
  if [ "$1" = -m ]; then
    [ -n "${FAIL_PLUGINKIT_LIST:-}" ] && exit 1
    printf '%s\n' "${PLUGINKIT_LIST:-}"
  fi
fi
exit 0
'''

UV_INSTALLER = r'''#!/bin/sh
[ -n "${FAIL_UV_INSTALLER:-}" ] && exit 1
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
    brew: bool = False,
    env: dict[str, str] | None = None,
    script_text: str | None = None,
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
    if brew:
        _write(stubs / "brew", BREW_STUB)
    _write(stubs / "defaults", DEFAULTS_STUB)
    _write(stubs / "launchctl", RECORD_STUB)
    _write(stubs / "pluginkit", RECORD_STUB)

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
    # File mode is the documented `bash install.sh`; stdin mode is `curl | bash`.
    proc = subprocess.run(
        ["bash", str(SCRIPT)] if script_text is None else ["bash"],
        input=script_text,
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
        # Once when it happens, once more as the last word after setup's output.
        assert proc.stderr.count("NOT updated") == 2
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
        assert "Installing the openswap tool failed" in proc.stderr

    def test_failed_uv_installer_is_reported(self, tmp_path):
        proc, calls = run_installer(tmp_path, uv=False, bin_on_path=False, env={"FAIL_UV_INSTALLER": "1"})
        assert proc.returncode == 1
        assert "uv installer failed" in proc.stderr
        assert not any(c.startswith("git") for c in calls)

    def test_shell_config_failure_does_not_block_setup(self, tmp_path):
        proc, calls = run_installer(tmp_path, bin_on_path=False, env={"FAIL_UPDATE_SHELL": "1"})
        assert proc.returncode == 0, proc.stderr
        assert "openswap setup" in calls
        last = proc.stdout.strip().splitlines()[-1]
        assert "Add" in last and "PATH" in last
        assert "new terminal" not in proc.stdout

    def test_setup_failure_is_the_exit_status_but_path_hint_still_prints(self, tmp_path):
        proc, calls = run_installer(tmp_path, bin_on_path=False, env={"FAIL_SETUP": "3"})
        assert proc.returncode == 3
        assert "openswap setup" in calls
        assert "new terminal" in proc.stdout

    @pytest.mark.parametrize("cut_before", ['\n{\n  main "$@"', "\n  exit\n}"])
    def test_truncated_download_runs_nothing(self, tmp_path, cut_before):
        """bash executes a piped script as it parses, so every prefix must be inert.

        Cut just before the brace group (a complete, do-nothing script) and
        just before its closing brace (an unfinished group). The bare
        ``main "$@"`` the second cut leaves is exactly what a call outside a
        group would have executed.
        """
        text = SCRIPT.read_text()
        head = text[: text.rindex(cut_before)]
        _, calls = run_installer(tmp_path, script_text=head)
        assert calls == []

    def test_piped_script_installs_like_the_file(self, tmp_path):
        proc, calls = run_installer(tmp_path, script_text=SCRIPT.read_text())
        assert proc.returncode == 0, proc.stderr
        assert "openswap setup" in calls

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


class TestHomebrewPath:
    """With Homebrew the installer installs the app cask instead of cloning."""

    def test_homebrew_installs_the_cask_and_runs_its_setup(self, tmp_path):
        proc, calls = run_installer(tmp_path, brew=True)
        assert proc.returncode == 0, proc.stderr
        assert "brew install --cask mar3co/openswap/openswap" in calls
        # The cask's own command, not whatever `openswap` comes first on PATH.
        assert "brew-openswap setup" in calls
        assert "openswap setup" not in calls
        assert not any(c.startswith(("git", "uv", "curl", "xcode-select")) for c in calls)
        assert "supported" in proc.stdout
        assert "OPENSWAP_DIR" in proc.stdout
        assert not any(c.startswith("brew upgrade") for c in calls)

    def test_rerun_with_the_cask_installed_upgrades_it(self, tmp_path):
        run_installer(tmp_path, brew=True)
        (tmp_path / "calls.log").unlink()
        proc, calls = run_installer(tmp_path, brew=True)
        assert proc.returncode == 0, proc.stderr
        assert "brew list --cask openswap" in calls
        assert "brew upgrade --cask mar3co/openswap/openswap" in calls
        assert not any(c.startswith("brew install") for c in calls)
        assert "brew-openswap setup" in calls

    def test_failed_cask_upgrade_stops_before_setup(self, tmp_path):
        run_installer(tmp_path, brew=True)
        (tmp_path / "calls.log").unlink()
        proc, calls = run_installer(tmp_path, brew=True, env={"FAIL_BREW_UPGRADE": "1"})
        assert proc.returncode == 1
        assert "brew upgrade --cask" in proc.stderr
        assert not any("setup" in c for c in calls)

    def test_openswap_dir_keeps_the_checkout_path_for_developers(self, tmp_path):
        target = _fake_checkout(tmp_path / "mine")
        proc, calls = run_installer(tmp_path, brew=True, env={"OPENSWAP_DIR": str(target)})
        assert proc.returncode == 0, proc.stderr
        assert not any(c.startswith("brew") for c in calls)
        assert "uv tool install --force --editable .[menubar]" in calls
        assert "openswap setup" in calls

    def test_failed_cask_install_stops_before_setup(self, tmp_path):
        proc, calls = run_installer(tmp_path, brew=True, env={"FAIL_BREW_INSTALL": "1"})
        assert proc.returncode == 1
        assert "brew install --cask" in proc.stderr
        assert not any("setup" in c for c in calls)
        assert not any(c.startswith(("git", "uv")) for c in calls)

    def test_setup_failure_is_the_exit_status(self, tmp_path):
        proc, calls = run_installer(tmp_path, brew=True, env={"FAIL_SETUP": "3"})
        assert proc.returncode == 3
        assert "brew-openswap setup" in calls

    def test_piped_script_installs_the_cask(self, tmp_path):
        proc, calls = run_installer(tmp_path, brew=True, script_text=SCRIPT.read_text())
        assert proc.returncode == 0, proc.stderr
        assert "brew-openswap setup" in calls


def _host_app(home: Path, bundle_id: str = "com.opensoft.openswap.widget") -> Path:
    import plistlib

    app = home / "Applications" / "OpenSwap.app"
    (app / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
    return app


class TestOldWidgetHostBeforeTheCask:
    """With appdir ~/Applications the old host would block `brew install --cask`."""

    @pytest.mark.parametrize(
        "env",
        [
            {"FAIL_PLUGINKIT_REMOVE": "1", "PLUGINKIT_LIST": "STILL"},
            {"FAIL_PLUGINKIT_REMOVE": "1", "FAIL_PLUGINKIT_LIST": "1"},
        ],
        ids=["still-registered", "registry-unreadable"],
    )
    def test_old_host_stays_when_its_widget_will_not_unregister(self, tmp_path, env):
        """Once it is in the Trash nothing retries, so a registered copy stops the installer."""
        home = tmp_path / "home"
        app = _host_app(home)
        appex = f"{app}/Contents/PlugIns/OpenSwapWidgetExtension.appex"
        env = {k: (f"+    com.opensoft.openswap.widget.extension(1.0)\t{appex}" if v == "STILL" else v) for k, v in env.items()}
        proc, calls = run_installer(tmp_path, brew=True, env={"HOMEBREW_CASK_OPTS": "--appdir=~/Applications", **env})
        assert proc.returncode != 0
        assert "Could not unregister the old widget" in proc.stderr + proc.stdout
        assert app.is_dir()
        assert not any(c.startswith(("brew install", "launchctl bootout")) for c in calls)

    def test_a_failed_remove_of_an_unlisted_widget_still_proceeds(self, tmp_path):
        home = tmp_path / "home"
        app = _host_app(home)
        proc, calls = run_installer(
            tmp_path, brew=True,
            env={"HOMEBREW_CASK_OPTS": "--appdir=~/Applications", "FAIL_PLUGINKIT_REMOVE": "1", "PLUGINKIT_LIST": ""},
        )
        assert proc.returncode == 0, proc.stderr
        assert not app.exists()
        assert "brew install --cask mar3co/openswap/openswap" in calls

    @pytest.mark.parametrize(
        "opts",
        [
            "--appdir=~/Applications",
            "--appdir=$HOME/Applications/",
            "--no-quarantine --appdir ~/Applications",
            '--appdir="~/Applications"',
        ],
    )
    def test_old_host_is_retired_to_the_trash_before_brew(self, tmp_path, opts):
        home = tmp_path / "home"
        app = _host_app(home)
        agent = home / "Library" / "LaunchAgents" / "com.opensoft.openswap.widget.plist"
        agent.parent.mkdir(parents=True)
        agent.write_text("")
        proc, calls = run_installer(tmp_path, brew=True, env={"HOMEBREW_CASK_OPTS": opts})
        assert proc.returncode == 0, proc.stderr
        assert "launchctl bootout gui/501/com.opensoft.openswap.widget" in calls
        assert f"pluginkit -r {app}/Contents/PlugIns/OpenSwapWidgetExtension.appex" in calls
        assert calls.index("launchctl bootout gui/501/com.opensoft.openswap.widget") < calls.index(
            "brew install --cask mar3co/openswap/openswap"
        )
        assert not agent.exists()
        assert not app.exists()
        assert (home / ".Trash" / "OpenSwap.app" / "Contents" / "Info.plist").is_file()

    def test_the_trash_copy_never_overwrites_one_already_there(self, tmp_path):
        home = tmp_path / "home"
        _host_app(home)
        earlier = home / ".Trash" / "OpenSwap.app"
        earlier.mkdir(parents=True)
        (earlier / "keep").write_text("mine")
        (home / ".Trash" / "OpenSwap 2.app").mkdir()
        proc, _ = run_installer(tmp_path, brew=True, env={"HOMEBREW_CASK_OPTS": "--appdir=~/Applications"})
        assert proc.returncode == 0, proc.stderr
        assert (earlier / "keep").read_text() == "mine"
        assert (home / ".Trash" / "OpenSwap 3.app" / "Contents" / "Info.plist").is_file()

    def test_a_not_loaded_agent_does_not_stop_the_install(self, tmp_path):
        _host_app(tmp_path / "home")
        proc, calls = run_installer(
            tmp_path,
            brew=True,
            env={"HOMEBREW_CASK_OPTS": "--appdir=~/Applications", "FAIL_LAUNCHCTL": "1"},
        )
        assert proc.returncode == 0, proc.stderr
        assert "brew-openswap setup" in calls

    def test_an_app_with_another_bundle_id_is_never_touched(self, tmp_path):
        app = _host_app(tmp_path / "home", bundle_id="com.example.other")
        proc, calls = run_installer(tmp_path, brew=True, env={"HOMEBREW_CASK_OPTS": "--appdir=~/Applications"})
        assert app.is_dir()
        assert not any(c.startswith(("launchctl", "pluginkit")) for c in calls)

    def test_the_default_appdir_leaves_the_old_host_to_the_app(self, tmp_path):
        """In /Applications nothing is in the way; the app's migration retires it."""
        app = _host_app(tmp_path / "home")
        proc, calls = run_installer(tmp_path, brew=True)
        assert proc.returncode == 0, proc.stderr
        assert app.is_dir()
        assert not any(c.startswith(("launchctl", "pluginkit")) for c in calls)

    def test_an_upgrade_does_not_retire_anything(self, tmp_path):
        run_installer(tmp_path, brew=True)
        (tmp_path / "calls.log").unlink()
        app = _host_app(tmp_path / "home")
        proc, calls = run_installer(tmp_path, brew=True, env={"HOMEBREW_CASK_OPTS": "--appdir=~/Applications"})
        assert proc.returncode == 0, proc.stderr
        assert app.is_dir()
        assert "brew upgrade --cask mar3co/openswap/openswap" in calls
