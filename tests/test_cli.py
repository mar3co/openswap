"""Tests for the CLI module."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from openswap import __version__
from openswap import cli
from openswap.credentials import ActiveCredentials
from openswap.switcher import ClaudeAccountSwitcher

# src layout: ensure subprocess can find openswap
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")

# A throwaway HOME for subprocesses. The in-process autouse Keychain/HOME guards
# do NOT reach child processes, so a spawned ``python -m openswap`` would
# otherwise resolve to the developer's real ``~/.claude-swap-backup`` and run the
# data migration against real accounts (touching the real Keychain on macOS). An empty,
# isolated HOME has no ``sequence.json`` → the migration skips before any Keychain
# access, and no ``.claude.json`` → no account to read.
# Allocated under pytest's basetemp so its own retention reclaims it, rather
# than a sweep at exit that a signal-killed worker never reaches.
_ISOLATED_HOME: str | None = None


@pytest.fixture(autouse=True, scope="session")
def _isolated_subprocess_home(tmp_path_factory):
    global _ISOLATED_HOME
    _ISOLATED_HOME = str(tmp_path_factory.mktemp("subproc-home"))


def _subprocess_env(**extra: str) -> dict[str, str]:
    """Build env dict with PYTHONPATH pointing at src/ and an isolated HOME.

    HOME/USERPROFILE default to a throwaway dir so the spawned CLI never touches
    the developer's real backup dir or Keychain; callers may still override HOME
    explicitly (e.g. ``_subprocess_env(HOME=str(temp_home))``), in which case
    USERPROFILE mirrors it unless the caller set USERPROFILE too.
    """
    env = {**os.environ, **extra}
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    if "HOME" not in extra:
        # Falling through here would point the child at the real HOME.
        assert _ISOLATED_HOME is not None, "_isolated_subprocess_home did not run"
        env["HOME"] = _ISOLATED_HOME
        env["USERPROFILE"] = _ISOLATED_HOME
    elif "USERPROFILE" not in extra:
        env["USERPROFILE"] = extra["HOME"]
    # CLAUDE_CONFIG_DIR / XDG_DATA_HOME bypass HOME in path resolution, so a
    # developer with either exported would otherwise point the spawned CLI back
    # at real config/backup paths (and on macOS, the real Keychain). Drop them
    # unless a caller set them deliberately.
    for var in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
        if var not in extra:
            env.pop(var, None)
    return env


_CORE_HELP_VERBS = (
    "list",
    "switch",
    "add",
    "add-token",
    "remove",
    "menubar",
    "widget",
    "auto",
    "statusline",
)


def _assert_advertised_help(stdout: str) -> None:
    """Advertised help is the OpenSwap macOS CLI keep-list, not upstream/Windows/PyPI."""
    assert "OpenSwap" in stdout
    assert "Multi-Account Switcher" not in stdout
    assert "Windows" not in stdout
    commands = stdout.split("Flags combine with subcommands:")[0]
    for verb in _CORE_HELP_VERBS:
        assert f" {verb} " in commands or f" {verb}\n" in commands, verb
    # Trailing space so "running" in "keep the menu bar running" does not match.
    assert " tui" not in stdout
    assert " watch" not in stdout
    assert " run " not in stdout
    assert " map " not in stdout
    assert " unmap " not in stdout
    # Do not pitch a PyPI / uv-tool self-upgrade. Hidden --upgrade may still exist.
    assert "self-upgrade" not in stdout.lower()
    assert "uv tool upgrade openswap" not in stdout
    assert "upgrade " not in commands


class TestCLI:
    """Test CLI argument parsing and execution."""

    def test_version_flag(self):
        """Test --version flag."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--version"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        assert __version__ in result.stdout

    def test_help_flag(self):
        """Test --help flag."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        _assert_advertised_help(result.stdout)
        assert "switch <num|email>" in result.stdout
        assert "status " in result.stdout
        # The legacy `--flag` spellings still work but are hidden from the
        # options section; only the "keep working" note may mention them.
        options_section = result.stdout.split("Flags combine with subcommands:")[0]
        assert "--add-account" not in options_section
        assert "--switch " not in options_section
        assert "--list" not in options_section
        assert "--status" not in options_section
        # ...and the note that they keep working is still present.
        assert "keep working" in result.stdout

    def test_no_args_prints_help(self):
        """Bare `openswap` prints help (the terminal dashboard used to take this)."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        _assert_advertised_help(result.stdout)
        # Hidden legacy flags must not leak into help options.
        options_section = result.stdout.split("Flags combine with subcommands:")[0]
        assert "--add-account" not in options_section

    def test_stale_cswap_launcher_is_rejected(self, capsys):
        with patch.object(sys, "argv", ["/usr/local/bin/cswap", "list"]), patch(
            "openswap.cli._migrate_legacy_cswap_state"
        ) as migrate:
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "Use 'openswap' instead" in capsys.readouterr().err
        migrate.assert_called_once_with()

    def test_tui_and_watch_are_gone(self):
        for verb in ("tui", "watch"):
            result = subprocess.run(
                [sys.executable, "-m", "openswap", verb],
                capture_output=True,
                text=True,
                env=_subprocess_env(),
            )
            assert result.returncode == 2
            assert "terminal dashboard is gone" in result.stderr
            assert "openswap list" in result.stderr

    def test_run_map_unmap_are_gone(self):
        cases = (
            ["run"],
            ["run", "2"],
            ["run", "--help"],
            ["map"],
            ["map", "2"],
            ["map", "--help"],
            ["unmap"],
            ["unmap", "/tmp/x"],
            ["unmap", "--help"],
        )
        for argv in cases:
            result = subprocess.run(
                [sys.executable, "-m", "openswap", *argv],
                capture_output=True,
                text=True,
                env=_subprocess_env(),
            )
            assert result.returncode == 2, argv
            err = result.stderr
            assert "gone" in err.lower(), argv
            assert "extra" in err.lower() or "openswap list" in err or "openswap switch" in err, argv
            # Must not look like a session launch or argparse help for the old verbs.
            assert "this terminal only" not in err.lower()
            assert "this terminal only" not in result.stdout.lower()
            assert "--no-share" not in result.stdout
            assert "Directory mappings" not in result.stdout

    def test_mutually_exclusive_args(self):
        """Test that mutually exclusive args are enforced."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--list", "--status"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode != 0
        assert "not allowed" in result.stderr.lower()

    def test_debug_flag_accepted(self):
        """Test that --debug flag is accepted."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--debug", "--status"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        # Should run (may fail due to no config, but flag should be accepted)
        assert "--debug" not in result.stderr or "unrecognized" not in result.stderr

    def test_token_status_flag_requires_list(self, capsys):
        """--token-status should only be accepted alongside --list."""
        with patch.object(sys, "argv", ["openswap", "--token-status", "--status"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--token-status can only be used with 'list'" in capsys.readouterr().err

    def test_token_status_flag_is_forwarded_to_list(self):
        """--list --token-status should call list_accounts(show_token_status=True)."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--list", "--token-status"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=True,
            json_output=False,
        )

    def test_strategy_best_requires_switch(self, capsys):
        """--strategy should only be accepted alongside --switch."""
        with patch.object(sys, "argv", ["openswap", "--strategy", "best", "--list"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--strategy can only be used with bare 'switch'" in capsys.readouterr().err

    def test_strategy_next_available_requires_switch(self, capsys):
        """--strategy next-available should only be accepted alongside --switch."""
        with patch.object(sys, "argv", ["openswap", "--strategy", "next-available", "--list"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--strategy can only be used with bare 'switch'" in capsys.readouterr().err

    def test_strategy_rejects_unknown_value(self, capsys):
        """argparse rejects strategies outside the known choices."""
        with patch.object(sys, "argv", ["openswap", "--switch", "--strategy", "bogus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2

    def test_switch_strategy_forwarded(self):
        """--switch --strategy best forwards the strategy to switch()."""
        from openswap.settings import AutoSwitchSettings

        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--switch", "--strategy", "best"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.settings.load_settings",
                   return_value=AutoSwitchSettings()), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy="best", json_output=False, models=(), model_source=None
        )

    def test_switch_strategy_falls_back_to_configured_model(self):
        """Without --model, the persistent autoswitch.model steers the
        strategy — reported as coming from the setting, not the CLI."""
        from openswap.settings import AutoSwitchSettings

        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--switch", "--strategy", "best"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.settings.load_settings",
                   return_value=AutoSwitchSettings(model="Fable")), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy="best", json_output=False,
            models=("Fable",), model_source="autoswitch.model",
        )

    def test_switch_model_flag_overrides_setting(self):
        """--model beats autoswitch.model, is deduped, and reports 'cli'."""
        from openswap.settings import AutoSwitchSettings

        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", [
                 "openswap", "--switch", "--strategy", "next-available",
                 "--model", "Opus, opus,Fable",
             ]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.settings.load_settings",
                   return_value=AutoSwitchSettings(model="Sonnet")), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy="next-available", json_output=False,
            models=("Opus", "Fable"), model_source="cli",
        )

    def test_switch_model_without_strategy_is_rejected(self, capsys):
        """--model is meaningless without a usage-aware strategy."""
        with patch.object(sys, "argv", ["openswap", "--switch", "--model", "Fable"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--model can only be used with" in capsys.readouterr().err

    def test_plain_switch_passes_no_strategy(self):
        """Bare --switch forwards strategy=None."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--switch"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=False, models=(), model_source=None
        )

    def test_slot_flag_requires_add_account(self, capsys):
        """--slot should only be accepted alongside --add-account or --add-token."""
        with patch.object(sys, "argv", ["openswap", "--list", "--slot", "3"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 2
        assert "--slot can only be used with 'add' or 'add-token'" in capsys.readouterr().err

    def test_slot_flag_in_help(self):
        """--slot should appear in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "--slot" in result.stdout

    def test_account_flag_requires_export(self, capsys):
        """--account should only be accepted alongside --export."""
        with patch.object(
            sys, "argv", ["openswap", "--list", "--account", "1"]
        ):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--account can only be used with 'export'" in capsys.readouterr().err

    def test_force_flag_requires_import_or_switch_to(self, capsys):
        """--force should only be accepted alongside --import or --switch-to."""
        with patch.object(sys, "argv", ["openswap", "--list", "--force"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert (
            "--force can only be used with 'import' or 'switch <num|email>'"
            in capsys.readouterr().err
        )

    def test_switch_to_force_forwarded(self):
        """--switch-to 2 --force forwards force=True to switch_to()."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--switch-to", "2", "--force"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch_to.assert_called_once_with(
            "2", json_output=False, force=True
        )

    def test_switch_to_without_force_forwards_false(self):
        """Plain --switch-to forwards force=False."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--switch-to", "2"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()

        switcher_cls.return_value.switch_to.assert_called_once_with(
            "2", json_output=False, force=False
        )

    def test_export_and_import_are_mutually_exclusive(self):
        """--export and --import cannot be combined."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "openswap",
                "--export",
                "/tmp/x",
                "--import",
                "/tmp/x",
            ],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode != 0
        assert "not allowed" in result.stderr.lower()

    def test_export_in_help(self):
        """The export/import subcommands should appear in help output."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "export <path>" in result.stdout
        assert "import <path>" in result.stdout

    def test_export_dispatch_calls_transfer(self):
        """--export dispatches into transfer.export_accounts."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("openswap.transfer.export_accounts") as export_fn, \
             patch.object(
                 sys, "argv", ["openswap", "--export", "/tmp/x", "--account", "2"]
             ), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()
        export_fn.assert_called_once_with(
            switcher_cls.return_value, "/tmp/x", account="2", full=False
        )

    def test_full_flag_requires_export(self, capsys):
        """--full should only be accepted alongside --export."""
        with patch.object(sys, "argv", ["openswap", "--list", "--full"]):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()
        assert exc_info.value.code == 2
        assert "--full can only be used with 'export'" in capsys.readouterr().err

    def test_full_flag_dispatches_with_full_true(self):
        """--export --full should pass full=True into export_accounts."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("openswap.transfer.export_accounts") as export_fn, \
             patch.object(
                 sys, "argv", ["openswap", "--export", "/tmp/x", "--full"]
             ), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()
        export_fn.assert_called_once_with(
            switcher_cls.return_value, "/tmp/x", account=None, full=True
        )

    def test_import_dispatch_calls_transfer(self):
        """--import dispatches into transfer.import_accounts."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch("openswap.transfer.import_accounts") as import_fn, \
             patch.object(
                 sys, "argv", ["openswap", "--import", "/tmp/x", "--force"]
             ), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()
        import_fn.assert_called_once_with(
            switcher_cls.return_value, "/tmp/x", force=True
        )

    def test_upgrade_in_help(self):
        """Help must not advertise upgrade as a PyPI / uv-tool self-upgrade."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        _assert_advertised_help(result.stdout)

    def test_upgrade_dispatches_without_constructing_switcher(self):
        """--upgrade should call run_self_upgrade and skip switcher init."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch(
                 "openswap.update_check.run_self_upgrade", return_value=0
             ) as upgrade_fn, \
             patch.object(sys, "argv", ["openswap", "--upgrade"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 0
        upgrade_fn.assert_called_once_with()
        switcher_cls.assert_not_called()

    def test_menubar_flag_dispatches(self, monkeypatch):
        called = {}

        class _FakeSwitcher:
            def __init__(self, *a, **k):
                pass
            def _is_running_in_container(self):
                return False

        def _fake_run(switcher, codex=None):
            called["ran"] = True
            return 0

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "argv", ["openswap", "--menubar"])
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("openswap.menubar.run", _fake_run, raising=False)
        # geteuid only exists on POSIX; ensure non-root path
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert called.get("ran") is True

    def test_menubar_subcommand_dispatches(self, monkeypatch):
        """Bare `openswap menubar` should route exactly like `openswap --menubar`."""
        called = {}

        class _FakeSwitcher:
            def __init__(self, *a, **k):
                pass
            def _is_running_in_container(self):
                return False

        def _fake_run(switcher, codex=None):
            called["ran"] = True
            return 0

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "argv", ["openswap", "menubar"])
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("openswap.menubar.run", _fake_run, raising=False)
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert called.get("ran") is True

    def test_frozen_no_args_without_terminal_starts_menubar(self, monkeypatch):
        called = {}

        class _FakeSwitcher:
            def __init__(self, *a, **k):
                pass
            def _is_running_in_container(self):
                return False

        def _fake_run(switcher, codex=None):
            called["ran"] = True
            return 0

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "argv", ["openswap"])
        monkeypatch.setattr(sys, "stdin", io.StringIO())
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("openswap.menubar.run", _fake_run, raising=False)
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert called.get("ran") is True

    def test_frozen_no_args_with_terminal_prints_help(self, monkeypatch, capsys):
        called = {}

        class _FakeSwitcher:
            def __init__(self, *a, **k):
                pass
            def _is_running_in_container(self):
                return False

        def _fake_run(switcher, codex=None):
            called["ran"] = True
            return 0

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "argv", ["openswap"])
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("openswap.menubar.run", _fake_run, raising=False)
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)

        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 0
        assert "Commands:" in capsys.readouterr().out
        assert called.get("ran") is not True

    def _service_harness(self, monkeypatch, argv):
        """Drive `openswap menubar <service flag>` with launch_agent stubbed out."""
        seen = {"menubar_ran": False}

        class _FakeSwitcher:
            def __init__(self, *a, **k):
                pass

            def _is_running_in_container(self):
                return False

        def _fake_menubar(switcher, codex=None):
            seen["menubar_ran"] = True
            return 0

        def _record(name, payload):
            def _call(*a, **k):
                seen["called"] = name
                return payload

            return _call

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", _FakeSwitcher)
        monkeypatch.setattr(sys, "argv", argv)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr("openswap.menubar.run", _fake_menubar, raising=False)
        monkeypatch.setattr(cli.os, "geteuid", lambda: 1000, raising=False)
        monkeypatch.setattr(
            "openswap.launch_agent.install",
            _record(
                "install",
                {
                    "label": "com.opensoft.openswap.menubar",
                    "plist": "/tmp/p.plist",
                    "program": ["/tmp/openswap", "menubar"],
                    "stdout_log": "/tmp/o.log",
                    "stderr_log": "/tmp/e.log",
                },
            ),
        )
        monkeypatch.setattr(
            "openswap.launch_agent.uninstall",
            _record("uninstall", {"label": "com.opensoft.openswap.menubar", "was_loaded": True, "removed_plist": True}),
        )
        monkeypatch.setattr(
            "openswap.launch_agent.status",
            _record(
                "status",
                {
                    "label": "com.opensoft.openswap.menubar",
                    "installed": True,
                    "loaded": True,
                    "state": "running",
                    "pid": 4242,
                    "plist": "/tmp/p.plist",
                },
            ),
        )
        return seen

    def test_menubar_install_service_routes_to_launch_agent(self, monkeypatch, capsys):
        seen = self._service_harness(monkeypatch, ["openswap", "menubar", "--install-service"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["called"] == "install"
        # The service flags must not also start a foreground menu bar.
        assert seen["menubar_ran"] is False
        assert "installed" in capsys.readouterr().out

    def test_menubar_uninstall_service_routes_to_launch_agent(self, monkeypatch, capsys):
        seen = self._service_harness(monkeypatch, ["openswap", "menubar", "--uninstall-service"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["called"] == "uninstall"
        assert seen["menubar_ran"] is False
        assert "removed" in capsys.readouterr().out

    def test_menubar_service_status_reports_state_and_pid(self, monkeypatch, capsys):
        seen = self._service_harness(monkeypatch, ["openswap", "menubar", "--service-status"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["called"] == "status"
        out = capsys.readouterr().out
        assert "running" in out and "4242" in out

    def test_menubar_service_flags_still_refuse_off_macos(self, monkeypatch):
        self._service_harness(monkeypatch, ["openswap", "menubar", "--install-service"])
        monkeypatch.setattr(sys, "platform", "linux")

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 1

    def test_service_flags_are_rejected_outside_menubar(self, monkeypatch, capsys):
        # `--full` already guards this way; without a matching check
        # `openswap list --install-service` would be accepted and silently ignored.
        monkeypatch.setattr(sys, "argv", ["openswap", "list", "--install-service"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 2
        assert "can only be used with 'menubar'" in capsys.readouterr().err

    def test_plain_menubar_does_not_touch_the_service(self, monkeypatch):
        seen = self._service_harness(monkeypatch, ["openswap", "menubar"])

        with pytest.raises(SystemExit) as exc:
            cli.main()

        assert exc.value.code == 0
        assert seen["menubar_ran"] is True
        assert "called" not in seen


class TestCLICommands:
    """Test individual CLI commands."""

    def test_status_no_account(self, temp_home: Path):
        """Test status command with no account."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--status"],
            capture_output=True,
            text=True,
            env=_subprocess_env(HOME=str(temp_home)),
        )
        # Should succeed even with no account
        assert "No active Claude account" in result.stdout or result.returncode == 0

    def test_list_no_accounts(self, temp_home: Path):
        """Test list command with no accounts."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--list"],
            capture_output=True,
            text=True,
            input="n\n",  # Answer 'n' to first-run prompt
            env=_subprocess_env(HOME=str(temp_home)),
        )
        assert "No accounts" in result.stdout or "managed" in result.stdout.lower()

    def test_add_token_without_email_dispatches_with_none(self, temp_home: Path, capsys):
        """--add-token without --email should dispatch with email=None (defaulted by switcher)."""
        from openswap.switcher import ClaudeAccountSwitcher

        with patch.object(
            sys, "argv", ["openswap", "--add-token", "sk-ant-oat01-abc"],
        ), patch.object(
            ClaudeAccountSwitcher, "add_account_from_token"
        ) as mock_add:
            cli.main()

        mock_add.assert_called_once_with(
            token="sk-ant-oat01-abc", email=None, slot=None
        )

    def test_email_without_add_token_errors(self, capsys):
        """--email without --add-token should exit with a clear error."""
        with patch.object(sys, "argv", ["openswap", "--list", "--email", "u@x.com"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--email can only be used with 'add-token'" in capsys.readouterr().err

    def test_add_token_dispatches_to_switcher(self, temp_home: Path, capsys):
        """--add-token with --email should call add_account_from_token."""
        from openswap.switcher import ClaudeAccountSwitcher

        with patch.object(
            sys, "argv",
            ["openswap", "--add-token", "mytoken", "--email", "u@example.com"],
        ), patch.object(
            ClaudeAccountSwitcher, "add_account_from_token"
        ) as mock_add:
            cli.main()

        mock_add.assert_called_once_with(
            token="mytoken", email="u@example.com", slot=None
        )

    def test_add_token_with_slot(self, temp_home: Path, capsys):
        """--add-token --slot should forward slot to add_account_from_token."""
        from openswap.switcher import ClaudeAccountSwitcher

        with patch.object(
            sys, "argv",
            ["openswap", "--add-token", "tok", "--email", "u@example.com", "--slot", "3"],
        ), patch.object(
            ClaudeAccountSwitcher, "add_account_from_token"
        ) as mock_add:
            cli.main()

        mock_add.assert_called_once_with(
            token="tok", email="u@example.com", slot=3
        )

    def test_add_token_in_help(self):
        """The add-token subcommand and the still-visible --email modifier appear."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "add-token [TOKEN|-]" in result.stdout
        assert "--email" in result.stdout  # modifier flag stays visible


class TestSessionModeGone:
    """`openswap run` / `map` / `unmap` are cut; they must not launch a session."""

    def test_main_help_omits_run_map_unmap(self):
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        # Trailing space so "running" in "keep the menu bar running" does not match.
        assert " run " not in result.stdout
        assert " map " not in result.stdout
        assert " unmap " not in result.stdout
        assert "this terminal only" not in result.stdout.lower()
        assert "directory mapping" not in result.stdout.lower()

    def test_main_help_mentions_alias(self):
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "alias <num|email>" in result.stdout


class TestSubcommandAliases:
    """Memorable subcommands (`openswap switch`, `openswap list`, ...) → classic flags."""

    def test_translate_is_noop_for_flags(self):
        """argv that already uses --flags is passed through untouched."""
        assert cli._translate_subcommand(["--list"]) == ["--list"]
        assert cli._translate_subcommand(["--switch", "--json"]) == ["--switch", "--json"]
        assert cli._translate_subcommand([]) == []

    def test_translate_bare_switch_rotates(self):
        assert cli._translate_subcommand(["switch"]) == ["--switch"]
        assert cli._translate_subcommand(["switch", "--strategy", "best"]) == [
            "--switch", "--strategy", "best",
        ]

    def test_translate_switch_with_target(self):
        assert cli._translate_subcommand(["switch", "2"]) == ["--switch-to", "2"]
        assert cli._translate_subcommand(["switch", "u@x.com", "--json"]) == [
            "--switch-to", "u@x.com", "--json",
        ]

    def test_translate_simple_verbs_and_aliases(self):
        assert cli._translate_subcommand(["list"]) == ["--list"]
        assert cli._translate_subcommand(["ls"]) == ["--list"]
        assert cli._translate_subcommand(["status"]) == ["--status"]
        assert cli._translate_subcommand(["add"]) == ["--add-account"]
        assert cli._translate_subcommand(["rm", "2"]) == ["--remove-account", "2"]
        assert cli._translate_subcommand(["upgrade"]) == ["--upgrade"]
        assert cli._translate_subcommand(["update"]) == ["--upgrade"]
        assert cli._translate_subcommand(["menubar"]) == ["--menubar"]

    def test_translate_value_verbs_pass_through_extra_flags(self):
        assert cli._translate_subcommand(["export", "b.openswap", "--full"]) == [
            "--export", "b.openswap", "--full",
        ]
        assert cli._translate_subcommand(["add-token", "sk-tok", "--slot", "3"]) == [
            "--add-token", "sk-tok", "--slot", "3",
        ]

    def test_translate_unknown_verb_unchanged(self):
        """An unrecognized first token is left for the parser to reject."""
        assert cli._translate_subcommand(["bogus"]) == ["bogus"]

    def test_switch_subcommand_dispatches_switch_to(self):
        """`openswap switch 2` reaches switch_to("2")."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "switch", "2"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()
        switcher_cls.return_value.switch_to.assert_called_once_with(
            "2", json_output=False, force=False
        )

    def test_bare_switch_subcommand_dispatches_switch(self):
        """`openswap switch` reaches switch() (rotate)."""
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "switch"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()
        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=False, models=(), model_source=None
        )

    def test_list_subcommand_with_json(self):
        """`openswap list --json` reaches list_accounts(json_output=True)."""
        payload = {"schemaVersion": 1, "accounts": []}
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "list", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.list_accounts.return_value = payload
            cli.main()
        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=False, json_output=True,
        )

    def test_run_is_not_translated_and_does_not_launch(self, capsys):
        """`run` is not rewritten to a flag; it exits 2 without a session manager."""
        assert cli._translate_subcommand(["run", "2"]) == ["run", "2"]
        with patch("openswap.session.SessionManager") as mgr, \
             patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "run", "2"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        mgr.assert_not_called()
        switcher_cls.assert_not_called()
        err = capsys.readouterr().err
        assert "gone" in err.lower()
        assert "extra" in err.lower() or "openswap list" in err or "openswap switch" in err

    def test_help_subcommand_prints_help(self):
        """`openswap help` exits 0 and prints help (with subcommand docs)."""
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert result.returncode == 0
        _assert_advertised_help(result.stdout)
        assert "Commands:" in result.stdout
        assert "keep working" in result.stdout


class TestJsonOutputCli:
    """CLI wiring for ``--json``: validation, single serialization, error envelope."""

    def test_json_rejected_without_supported_command(self, capsys):
        """--purge --json is rejected (bare --json instead hits the required-group error)."""
        with patch.object(sys, "argv", ["openswap", "--purge", "--json"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--json can only be used with" in capsys.readouterr().err

    def test_token_status_with_json_rejected(self, capsys):
        with patch.object(sys, "argv", ["openswap", "--list", "--token-status", "--json"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2
        assert "--token-status cannot be combined with --json" in capsys.readouterr().err

    def test_list_json_serialized_to_stdout(self, capsys):
        payload = {"schemaVersion": 1, "activeAccountNumber": None, "accounts": []}
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--list", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.list_accounts.return_value = payload
            cli.main()

        switcher_cls.return_value.list_accounts.assert_called_once_with(
            show_token_status=False, json_output=True,
        )
        out = capsys.readouterr().out
        assert json.loads(out) == payload  # exactly one JSON object, no extra text

    def test_switch_json_forwarded_and_serialized(self, capsys):
        payload = {"schemaVersion": 1, "switched": True}
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--switch", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.switch.return_value = payload
            cli.main()

        switcher_cls.return_value.switch.assert_called_once_with(
            strategy=None, json_output=True, models=(), model_source=None,
        )
        assert json.loads(capsys.readouterr().out) == payload

    def test_switch_json_carries_model_fields_when_in_effect(self, capsys):
        """Additive models/modelSource fields make a model-steered pick
        auditable from scripts too."""
        payload = {"schemaVersion": 1, "switched": True}
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", [
                 "openswap", "--switch", "--strategy", "best",
                 "--model", "Fable", "--json",
             ]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.switch.return_value = payload
            cli.main()

        out = json.loads(capsys.readouterr().out)
        assert out["models"] == ["Fable"]
        assert out["modelSource"] == "cli"
        assert out["switched"] is True

    def test_error_envelope_on_stdout_with_exit_1(self, capsys):
        from openswap.exceptions import ConfigError

        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", "--status", "--json"]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            switcher_cls.return_value.status.side_effect = ConfigError("nope")
            with pytest.raises(SystemExit) as excinfo:
                cli.main()

        assert excinfo.value.code == 1
        captured = capsys.readouterr()
        envelope = json.loads(captured.out)  # error went to stdout as JSON
        assert envelope["error"] == {"type": "ConfigError", "message": "nope"}
        assert captured.err == ""  # nothing on stderr in JSON mode


class TestAutoCommand:
    """`openswap auto` pre-dispatch: parsing, settings merge, exit codes, JSONL."""

    class FakeEngine:
        instances: list = []
        tick_outcome = None  # set per test (TickOutcome)

        def __init__(self, switcher, settings, on_event, *, dry_run=False,
                     state_path=None, clock=None):
            self.switcher = switcher
            self.settings = settings
            self.on_event = on_event
            self.dry_run = dry_run
            type(self).instances.append(self)

        def tick(self):
            from openswap.autoswitch import TickOutcome

            return type(self).tick_outcome or TickOutcome.NO_ACTION

        def run_loop(self):
            return 0

        def stop(self):
            pass

    @pytest.fixture(autouse=True)
    def _fresh_fake(self):
        self.FakeEngine.instances = []
        self.FakeEngine.tick_outcome = None

    def _run(self, argv: list[str], temp_home):
        with patch("openswap.autoswitch.AutoSwitchEngine", self.FakeEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["openswap", "auto", *argv]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        return excinfo.value.code

    def test_once_exit_code_switched(self, temp_home):
        from openswap.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.SWITCHED
        assert self._run(["--once"], temp_home) == 0

    def test_once_exit_code_no_action(self, temp_home):
        from openswap.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.NO_ACTION
        assert self._run(["--once"], temp_home) == 2

    def test_once_exit_code_blocked(self, temp_home):
        from openswap.autoswitch import TickOutcome

        self.FakeEngine.tick_outcome = TickOutcome.BLOCKED
        assert self._run(["--once"], temp_home) == 3

    def test_loop_mode_returns_loop_exit(self, temp_home):
        assert self._run([], temp_home) == 0
        assert self.FakeEngine.instances  # loop path constructed the engine

    def test_flags_override_settings_json(self, temp_home):
        from openswap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"threshold": 80.0, "cooldownSeconds": 42.0},
        }))
        self._run(["--once", "--threshold", "60"], temp_home)
        engine = self.FakeEngine.instances[-1]
        assert engine.settings.threshold == 60.0     # CLI wins
        assert engine.settings.cooldown_seconds == 42.0  # settings.json kept

    def test_dry_run_forwarded(self, temp_home):
        self._run(["--once", "--dry-run"], temp_home)
        assert self.FakeEngine.instances[-1].dry_run is True

    def _fake_codex(self, temp_home):
        class FakeCodex:
            def __init__(self, *a, **k):
                self.state_dir = Path(temp_home) / "codex-state"
                self.state_dir.mkdir(parents=True, exist_ok=True)

            def switchable_account_numbers(self):
                return ["1", "2"]

        return FakeCodex

    def test_codex_enabled_false_skips_codex_autoswitch_engine(self, temp_home):
        from openswap.paths import get_backup_root

        backup = get_backup_root()
        backup.mkdir(parents=True, exist_ok=True)
        (backup / "settings.json").write_text(json.dumps({
            "schemaVersion": 1,
            "autoswitch": {"codexEnabled": False},
        }))
        with patch("openswap.codex.engine.CodexEngine", self._fake_codex(temp_home)):
            self._run(["--once"], temp_home)
        assert len(self.FakeEngine.instances) == 1
        assert type(self.FakeEngine.instances[0].switcher).__name__ != "FakeCodex"

    def test_codex_enabled_true_starts_codex_autoswitch_engine(self, temp_home):
        with patch("openswap.codex.engine.CodexEngine", self._fake_codex(temp_home)):
            self._run(["--once"], temp_home)
        assert len(self.FakeEngine.instances) == 2
        assert type(self.FakeEngine.instances[1].switcher).__name__ == "FakeCodex"

    def test_auto_command_gates_codex_engine_on_codex_enabled(self):
        import inspect

        src = inspect.getsource(cli._auto_command)
        assert "codex_enabled" in src
        assert "switchable_account_numbers" in src

    def test_json_stdout_is_pure_jsonl(self, temp_home, capsys):
        from openswap.autoswitch import NoSwitchEvent, TickOutcome

        class EmittingEngine(self.FakeEngine):
            def tick(self):
                self.on_event(NoSwitchEvent(reason="below-threshold"))
                self.on_event(NoSwitchEvent(reason="cooldown"))
                return TickOutcome.NO_ACTION

        with patch("openswap.autoswitch.AutoSwitchEngine", EmittingEngine), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["openswap", "auto", "--once", "--json"]):
            with pytest.raises(SystemExit):
                cli.main()
        lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
        assert len(lines) == 2
        for line in lines:
            payload = json.loads(line)
            assert payload["event"] == "no-switch"
            assert payload["schemaVersion"] == 1

    def test_unknown_flag_errors(self, temp_home, capsys):
        with patch.object(sys, "argv", ["openswap", "auto", "--bogus"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_auto_help(self, capsys):
        with patch.object(sys, "argv", ["openswap", "auto", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--once" in out
        assert "Exit codes" in out

    def test_main_help_mentions_auto(self):
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "auto" in result.stdout

    def test_main_help_mentions_widget(self):
        result = subprocess.run(
            [sys.executable, "-m", "openswap", "--help"],
            capture_output=True,
            text=True,
            env=_subprocess_env(),
        )
        assert "widget --install" in result.stdout

    def test_widget_help(self, capsys):
        with patch.object(sys, "argv", ["openswap", "widget", "--help"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "--install" in out
        assert "Edit Widgets" in out

    def test_widget_requires_a_flag(self):
        with patch.object(sys, "argv", ["openswap", "widget"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2

    def test_widget_install_dispatches(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "openswap.widget_install.install_widget",
            lambda: {
                "app": "/tmp/OpenSwap.app",
                "team": "ABC",
                "log": "/tmp/x.log",
                "label": "com.opensoft.openswap.widget",
                "plist": "/tmp/p.plist",
            },
        )
        with patch.object(sys, "argv", ["openswap", "widget", "--install"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Widget installed" in out
        assert "Edit Widgets" in out

    def test_widget_uninstall_dispatches(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "openswap.widget_install.uninstall_widget",
            lambda: {"removed_app": True, "removed_plist": True, "was_loaded": True},
        )
        with patch.object(sys, "argv", ["openswap", "widget", "--uninstall"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 0
        assert "Widget removed" in capsys.readouterr().out

    def test_widget_status_dispatches(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "openswap.widget_install.widget_status",
            lambda: {
                "app": "/tmp/OpenSwap.app",
                "app_installed": True,
                "installed": True,
                "loaded": True,
                "state": "running",
                "pid": 99,
            },
        )
        with patch.object(sys, "argv", ["openswap", "widget", "--status"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "present" in out
        assert "99" in out

    def test_switcher_error_exits_1(self, temp_home, capsys):
        from openswap.exceptions import ConfigError

        with patch("openswap.cli.ClaudeAccountSwitcher",
                   side_effect=ConfigError("nope")), \
             patch.object(sys, "argv", ["openswap", "auto", "--once"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 1
        assert "nope" in capsys.readouterr().err  # printer.error -> stderr


class TestUnclaimedCommand:
    """Minor 2: the operator escape hatch for a stash row.

    ``--json`` emits bare entry ids, and the two conditions this branch
    introduces (a stranded row, a permanently unreadable one) both leave a row
    an operator must be able to see the slot/reason of, and drop.
    """

    def _stashed(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        entry_id = switcher._store._write_unclaimed_credential(
            "creds-bytes",
            {"reason": "consume-gate-persist-lock-failed",
             "configSlot": "2",
             "consumedFp": "fp-old"},
        )
        return switcher, entry_id

    def test_list_shows_slot_and_reason_not_just_the_id(self, temp_home, capsys):
        _, entry_id = self._stashed(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._unclaimed_command([])
        out = capsys.readouterr().out
        assert entry_id in out
        assert "consume-gate-persist-lock-failed" in out
        assert "2" in out

    def test_purge_removes_bytes_and_row(self, temp_home, capsys):
        switcher, entry_id = self._stashed(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._unclaimed_command(["--purge", entry_id])
        assert switcher.list_unclaimed_credentials() == {}
        assert not switcher._store._stash_entry_path(entry_id).exists()

    def test_purging_an_unknown_id_fails_loudly(self, temp_home):
        self._stashed(temp_home)
        with patch("os.geteuid", return_value=1000, create=True), \
             pytest.raises(SystemExit) as exc:
            cli._unclaimed_command(["--purge", "no-such-entry"])
        assert exc.value.code == 1

    def test_dispatched_from_main(self, temp_home):
        with patch("openswap.cli._unclaimed_command") as fn, \
             patch.object(sys, "argv", ["openswap", "unclaimed", "--purge", "x"]):
            cli.main()
        fn.assert_called_once_with(["--purge", "x"])


class TestAliasCommand:
    """`openswap alias` — set/unset/list a short display alias for an account."""

    def _seeded_switcher_env(self, temp_home):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        data = switcher._get_sequence_data()
        data["accounts"]["2"] = {
            "email": "work@co.com",
            "uuid": "u2",
            "organizationUuid": "",
            "organizationName": "",
            "added": "2024-01-01T00:00:00Z",
        }
        data["sequence"] = [2]
        switcher._write_json(switcher.sequence_file, data)
        return switcher

    def test_set_alias_by_number(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._alias_command(["2", "dev"])

        data = ClaudeAccountSwitcher()._get_sequence_data()
        assert data["accounts"]["2"]["alias"] == "dev"
        assert "dev" in capsys.readouterr().out

    def test_set_alias_by_email(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            cli._alias_command(["work@co.com", "dev"])

        data = ClaudeAccountSwitcher()._get_sequence_data()
        assert data["accounts"]["2"]["alias"] == "dev"

    def test_unset_alias(self, temp_home, capsys):
        switcher = self._seeded_switcher_env(temp_home)
        data = switcher._get_sequence_data()
        data["accounts"]["2"]["alias"] = "dev"
        switcher._write_json(switcher.sequence_file, data)

        with patch("os.geteuid", return_value=1000, create=True):
            cli._alias_command(["2", "--unset"])

        data = ClaudeAccountSwitcher()._get_sequence_data()
        assert "alias" not in data["accounts"]["2"]

    def test_list_aliases(self, temp_home, capsys):
        switcher = self._seeded_switcher_env(temp_home)
        data = switcher._get_sequence_data()
        data["accounts"]["2"]["alias"] = "dev"
        switcher._write_json(switcher.sequence_file, data)

        with patch("os.geteuid", return_value=1000, create=True):
            cli._alias_command([])

        out = capsys.readouterr().out
        assert "dev" in out

    def test_missing_name_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit):
                cli._alias_command(["2"])

    def test_unset_without_account_errors(self, temp_home, capsys):
        """`openswap alias --unset` with no target must error, not silently list."""
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit):
                cli._alias_command(["--unset"])

    def test_unset_with_name_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit):
                cli._alias_command(["2", "dev", "--unset"])

    def test_invalid_alias_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit) as exc:
                cli._alias_command(["2", "123"])
        assert exc.value.code == 1
        assert "Error" in capsys.readouterr().err

    def test_unknown_account_errors(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=1000, create=True):
            with pytest.raises(SystemExit) as exc:
                cli._alias_command(["999", "dev"])
        assert exc.value.code == 1

    def test_dispatched_from_main(self, temp_home):
        with patch("openswap.cli._alias_command") as alias_fn, \
             patch.object(sys, "argv", ["openswap", "alias", "2", "dev"]):
            cli.main()
        alias_fn.assert_called_once_with(["2", "dev"])

    @pytest.mark.skipif(sys.platform == "win32", reason="root guard is POSIX-only")
    def test_alias_refuses_root(self, temp_home, capsys):
        self._seeded_switcher_env(temp_home)
        with patch("os.geteuid", return_value=0, create=True), \
             patch.object(ClaudeAccountSwitcher, "_is_running_in_container", return_value=False):
            with pytest.raises(SystemExit) as exc:
                cli._alias_command(["2", "dev"])
        assert exc.value.code == 1
        assert "root" in capsys.readouterr().err

    def test_add_with_alias_flag(self, temp_home, mock_claude_config, capsys):
        fake_creds = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(ClaudeAccountSwitcher, "_read_active_credentials",
                          return_value=ActiveCredentials(fake_creds, False)), \
             patch.object(ClaudeAccountSwitcher, "_write_account_credentials"), \
             patch.object(sys, "argv", ["openswap", "add", "--alias", "dev"]):
            cli.main()

        data = ClaudeAccountSwitcher()._get_sequence_data()
        assert data["accounts"]["1"]["alias"] == "dev"

    def test_alias_flag_without_add_errors(self, temp_home, capsys):
        with patch("os.geteuid", return_value=1000, create=True), \
             patch.object(sys, "argv", ["openswap", "list", "--alias", "dev"]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 2
        assert "--alias can only be used with 'add'" in capsys.readouterr().err



class TestDisableEnableDispatch:
    """`openswap disable`/`openswap enable` (and the legacy --disable-account /
    --enable-account flags) forward to switcher.set_account_disabled."""

    def _run(self, argv):
        with patch("openswap.cli.ClaudeAccountSwitcher") as switcher_cls, \
             patch.object(sys, "argv", ["openswap", *argv]), \
             patch("os.geteuid", return_value=1000, create=True), \
             patch("openswap.update_check.check_for_update", return_value=None):
            cli.main()
        return switcher_cls.return_value

    def test_disable_subcommand_forwards(self):
        switcher = self._run(["disable", "2"])
        switcher.set_account_disabled.assert_called_once_with("2", True)

    def test_enable_subcommand_forwards(self):
        switcher = self._run(["enable", "user@example.com"])
        switcher.set_account_disabled.assert_called_once_with("user@example.com", False)

    def test_legacy_disable_flag_forwards(self):
        switcher = self._run(["--disable-account", "3"])
        switcher.set_account_disabled.assert_called_once_with("3", True)

    def test_legacy_enable_flag_forwards(self):
        switcher = self._run(["--enable-account", "3"])
        switcher.set_account_disabled.assert_called_once_with("3", False)

    def test_disable_without_target_errors(self, capsys):
        with patch.object(sys, "argv", ["openswap", "disable"]):
            with pytest.raises(SystemExit) as excinfo:
                cli.main()
        assert excinfo.value.code == 2


def test_importing_the_module_allocates_no_temp_dir(tmp_path, tmp_path_factory):
    """Import must allocate nothing; the fixture must allocate inside basetemp.

    The child gets a private TMPDIR of its own rather than watching the shared
    system one, which several checkouts write to concurrently. Both halves are
    needed: re-adding the module-level ``mkdtemp`` is caught only by the empty
    private tmp, and a fixture allocating outside basetemp only by containment.
    """
    child_tmp = tmp_path / "childtmp"
    child_tmp.mkdir()
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import tests.test_cli",
         str(Path(__file__).resolve().parent.parent)],
        capture_output=True, text=True,
        env=_subprocess_env(TMPDIR=str(child_tmp)), timeout=120,
    )
    assert child.returncode == 0, child.stderr
    allocated = list(child_tmp.iterdir())
    assert not allocated, f"import allocated {[a.name for a in allocated]}"

    home = Path(_subprocess_env()["HOME"])
    assert home.is_dir(), f"the isolated HOME is not a real directory: {home}"
    assert home.is_relative_to(tmp_path_factory.getbasetemp()), f"{home} escapes basetemp"


class TestSetupCommand:
    """``openswap setup``: capture the live login, then start the extra."""

    def _harness(
        self, monkeypatch, argv, *, add_raises=None, install_raises=None, widget_detail=None, pid=4242
    ):
        seen: dict = {"add": 0, "install": 0, "widget": 0}

        class FakeSwitcher:
            def __init__(self, debug=False):
                pass

            def _is_running_in_container(self):
                return True  # keep _guard_root quiet if the suite runs as root

            def add_account(self, slot=None, assume_yes=False, alias=None):
                seen["add"] += 1
                if add_raises is not None:
                    raise add_raises

        def fake_install():
            seen["install"] += 1
            if install_raises is not None:
                raise install_raises
            return {
                "label": "com.opensoft.openswap.menubar",
                "plist": "/tmp/p.plist",
                "program": ["/tmp/openswap", "menubar"],
                "stdout_log": "/tmp/o.log",
                "stderr_log": "/tmp/e.log",
            }

        def fake_widget_restart():
            seen["widget"] += 1
            return widget_detail

        monkeypatch.setattr(cli, "ClaudeAccountSwitcher", FakeSwitcher)
        monkeypatch.setattr("openswap.launch_agent.install", fake_install)
        monkeypatch.setattr(
            "openswap.launch_agent.status",
            lambda label=None, *a, **k: {"installed": True, "loaded": True, "state": "running", "pid": pid},
        )
        monkeypatch.setattr(cli, "_wait_for_pid", lambda agent, label, timeout=3.0: agent.status(label)["pid"])
        monkeypatch.setattr("openswap.update_check.restart_widget_agent", fake_widget_restart)
        monkeypatch.setattr(sys, "argv", argv)
        return seen

    def _run(self):
        with pytest.raises(SystemExit) as exc:
            cli.main()
        return exc.value.code

    def test_help(self, capsys):
        with patch.object(sys, "argv", ["openswap", "setup", "--help"]):
            assert self._run() == 0
        assert "usage: openswap setup" in capsys.readouterr().out

    def test_saves_login_and_installs_service(self, monkeypatch, capsys):
        seen = self._harness(monkeypatch, ["openswap", "setup"])
        assert self._run() == 0
        assert seen == {"add": 1, "install": 1, "widget": 1}
        out = capsys.readouterr().out
        assert "Menu bar extra running (pid 4242)" in out
        assert "/tmp/e.log" in out
        assert "Log into another Claude account, then run: openswap add" in out

    def test_not_logged_in_still_installs_service_and_says_so_last(self, monkeypatch, capsys):
        from openswap.exceptions import NotLoggedInError

        seen = self._harness(
            monkeypatch,
            ["openswap", "setup"],
            add_raises=NotLoggedInError("No active Claude account found. Please log in first."),
        )
        assert self._run() == 0
        assert seen == {"add": 1, "install": 1, "widget": 1}
        out = capsys.readouterr().out
        assert "Menu bar extra running (pid 4242)" in out
        assert "another Claude account" not in out
        tail = out.strip().splitlines()[-2:]
        assert "No active Claude account" in tail[0]
        assert "Log into Claude Code, then run: openswap add" in tail[1]

    @pytest.mark.parametrize(
        "exc, message",
        [
            ("CredentialReadError", "Keychain access denied"),
            ("ConfigError", "Permission denied reading Claude config"),
            (OSError, "[Errno 30] Read-only file system"),
        ],
    )
    def test_other_capture_failures_are_not_login_problems(self, monkeypatch, capsys, exc, message):
        from openswap import exceptions

        exc_type = getattr(exceptions, exc) if isinstance(exc, str) else exc
        seen = self._harness(
            monkeypatch,
            ["openswap", "setup"],
            add_raises=exc_type(message),
        )
        assert self._run() == 1
        assert seen == {"add": 1, "install": 1, "widget": 1}
        out = capsys.readouterr().out
        assert "Menu bar extra running (pid 4242)" in out
        assert "Log into Claude Code" not in out
        tail = out.strip().splitlines()[-2:]
        assert message in tail[0]
        assert "openswap add" in tail[1]

    def test_extra_that_did_not_come_up_is_not_called_running(self, monkeypatch, capsys):
        self._harness(monkeypatch, ["openswap", "setup"], pid=None)
        assert self._run() == 0
        out = capsys.readouterr().out
        assert "Menu bar extra running" not in out
        assert "not running yet" in out
        assert "/tmp/e.log" in out

    def test_ctrl_c_after_capture_says_the_account_was_saved(self, monkeypatch, capsys):
        self._harness(monkeypatch, ["openswap", "setup"], install_raises=KeyboardInterrupt())
        assert self._run() == 130
        assert "account was saved" in capsys.readouterr().out

    def test_wait_for_pid_returns_pid_once_launchd_spawned_it(self):
        answers = iter([None, None, 77])
        agent = type("A", (), {"status": staticmethod(lambda label: {"pid": next(answers)})})
        assert cli._wait_for_pid(agent, "x", timeout=5.0) == 77

    def test_wait_for_pid_gives_up_at_the_deadline(self):
        agent = type("A", (), {"status": staticmethod(lambda label: {"pid": None})})
        assert cli._wait_for_pid(agent, "x", timeout=0.0) is None

    def test_ctrl_c_after_failed_capture_reports_the_reason(self, monkeypatch, capsys):
        from openswap.exceptions import CredentialReadError

        self._harness(
            monkeypatch,
            ["openswap", "setup"],
            add_raises=CredentialReadError("Keychain access denied"),
            install_raises=KeyboardInterrupt(),
        )
        assert self._run() == 130
        out = capsys.readouterr().out
        assert "Keychain access denied" in out
        assert "openswap add" in out

    def test_widget_restart_failure_is_a_warning_with_its_own_fix(self, monkeypatch, capsys):
        self._harness(monkeypatch, ["openswap", "setup"], widget_detail="kickstart exit 113")
        assert self._run() == 0
        out = capsys.readouterr().out
        assert "Menu bar extra running (pid 4242)" in out
        assert "kickstart exit 113" in out
        assert out.count("openswap widget --install") == 1

    def test_service_failure_after_failed_capture_reports_both(self, monkeypatch, capsys):
        from openswap.exceptions import ClaudeSwitchError, CredentialReadError

        self._harness(
            monkeypatch,
            ["openswap", "setup"],
            add_raises=CredentialReadError("Keychain access denied"),
            install_raises=ClaudeSwitchError("launchctl bootstrap failed (exit 5)"),
        )
        assert self._run() == 1
        err = capsys.readouterr().err
        assert "Keychain access denied" in err
        assert "When that is fixed, run: openswap add" in err
        assert "launchctl bootstrap failed" in err

    def test_service_failure_exits_nonzero_with_retry_and_account_state(self, monkeypatch, capsys):
        from openswap.exceptions import ClaudeSwitchError

        seen = self._harness(
            monkeypatch,
            ["openswap", "setup"],
            install_raises=ClaudeSwitchError("launchctl bootstrap failed (exit 5)"),
        )
        assert self._run() == 1
        assert seen == {"add": 1, "install": 1, "widget": 0}
        err = capsys.readouterr().err
        assert "launchctl bootstrap failed" in err
        assert "account was saved" in err
        assert "openswap menubar --install-service" in err

    def test_ctrl_c_exits_130(self, monkeypatch, capsys):
        self._harness(monkeypatch, ["openswap", "setup"], add_raises=KeyboardInterrupt())
        assert self._run() == 130
        assert "cancelled" in capsys.readouterr().out.lower()
