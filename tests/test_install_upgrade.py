"""OpenSwap is not on PyPI: upgrade is git pull + editable reinstall."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from openswap.update_check import INSTALL_COMMAND, check_for_update, run_self_upgrade


class TestNoPypi:
    def test_check_for_update_never_hits_pypi(self, monkeypatch):
        monkeypatch.setattr(
            "openswap.update_check._package_is_git_checkout",
            lambda package_file=None: False,
        )
        with patch("urllib.request.urlopen") as mock_urlopen:
            assert check_for_update("0.3.2") is None
            mock_urlopen.assert_not_called()

    def test_run_self_upgrade_pulls_and_reinstalls(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "openswap"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setattr("openswap.update_check._checkout_root", lambda: repo)
        monkeypatch.setattr("openswap.update_check._refresh_launch_agents", lambda: None)
        runs: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            runs.append(list(cmd))
            return MagicMock(returncode=0)

        monkeypatch.setattr("openswap.update_check.subprocess.run", fake_run)
        assert run_self_upgrade() == 0
        assert runs[0] == ["git", "-C", str(repo), "pull"]
        assert runs[1][:3] == ["uv", "tool", "install"]
        assert "--force" in runs[1]
        assert "--editable" in runs[1]
        assert ".[menubar]" in runs[1]

    def test_run_self_upgrade_missing_checkout_tells_how_to_reinstall(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr("openswap.update_check._checkout_root", lambda: None)
        with patch("openswap.update_check.subprocess.run") as mock_run:
            assert run_self_upgrade() == 1
            mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "not published to PyPI" in err
        assert INSTALL_COMMAND in err

    def test_run_self_upgrade_gone_checkout_path(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "moved-openswap"
        monkeypatch.setattr("openswap.update_check._checkout_root", lambda: missing)
        with patch("openswap.update_check.subprocess.run") as mock_run:
            assert run_self_upgrade() == 1
            mock_run.assert_not_called()
        err = capsys.readouterr().err
        assert "moved" in err.lower() or "gone" in err.lower() or "reinstall" in err.lower()

    def test_run_self_upgrade_missing_uv_is_a_clean_error(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "openswap"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "src" / "openswap").mkdir(parents=True)
        (repo / "src" / "openswap" / "__init__.py").write_text("")
        monkeypatch.setattr("openswap.update_check._checkout_root", lambda: repo)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return MagicMock(returncode=0)
            raise FileNotFoundError("uv")

        monkeypatch.setattr("openswap.update_check.subprocess.run", fake_run)
        assert run_self_upgrade() == 1
        err = capsys.readouterr().err
        assert "uv" in err.lower()
        assert "PATH" in err or "path" in err.lower() or "install" in err.lower()

    def test_refresh_reinstalls_menubar_agent_when_plist_exists(
        self, tmp_path, monkeypatch
    ):
        plist = tmp_path / "Library" / "LaunchAgents" / "com.opensoft.openswap.menubar.plist"
        plist.parent.mkdir(parents=True)
        plist.write_bytes(b"")
        monkeypatch.setattr("openswap.update_check.sys.platform", "darwin")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        called = []

        def fake_install(**kwargs):
            called.append("menubar")
            return {"label": "com.opensoft.openswap.menubar"}

        monkeypatch.setattr("openswap.launch_agent.install", fake_install)
        monkeypatch.setattr("openswap.launch_agent.is_loaded", lambda *a, **k: False)
        from openswap.update_check import _refresh_launch_agents

        _refresh_launch_agents()
        assert called == ["menubar"]

    def test_refresh_migrates_legacy_menubar_plist(self, tmp_path, monkeypatch):
        plist = tmp_path / "Library" / "LaunchAgents" / "com.cswap.menubar.plist"
        plist.parent.mkdir(parents=True)
        plist.write_bytes(b"")
        monkeypatch.setattr("openswap.update_check.sys.platform", "darwin")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        called = []
        monkeypatch.setattr(
            "openswap.launch_agent.install",
            lambda **k: called.append("menubar") or {"label": "x"},
        )
        monkeypatch.setattr("openswap.launch_agent.is_loaded", lambda *a, **k: False)
        from openswap.update_check import _refresh_launch_agents

        _refresh_launch_agents()
        assert called == ["menubar"]

    def test_run_self_upgrade_reports_agent_refresh_failure(
        self, tmp_path, monkeypatch, capsys
    ):
        from openswap.exceptions import ClaudeSwitchError

        repo = tmp_path / "openswap"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "src" / "openswap").mkdir(parents=True)
        (repo / "src" / "openswap" / "__init__.py").write_text("")
        monkeypatch.setattr("openswap.update_check._checkout_root", lambda: repo)
        monkeypatch.setattr(
            "openswap.update_check.subprocess.run",
            lambda *a, **k: MagicMock(returncode=0),
        )
        def boom(**kwargs):
            raise ClaudeSwitchError("bootstrap failed")

        plist = tmp_path / "Library" / "LaunchAgents" / "com.opensoft.openswap.menubar.plist"
        plist.parent.mkdir(parents=True)
        plist.write_bytes(b"")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("openswap.update_check.sys.platform", "darwin")
        monkeypatch.setattr("openswap.launch_agent.is_loaded", lambda *a, **k: False)
        monkeypatch.setattr("openswap.launch_agent.install", boom)
        assert run_self_upgrade() == 1
        err = capsys.readouterr().err
        assert "bootstrap failed" in err
        assert "menubar --install-service" in err


class TestRestartWidgetAgent:
    def _arm(self, monkeypatch, returncode, *, loaded):
        calls: list[tuple[str, ...]] = []

        def fake_launchctl(*args):
            calls.append(args)
            return MagicMock(returncode=returncode, stderr="Could not find service", stdout="")

        monkeypatch.setattr("openswap.launch_agent.is_loaded", lambda label, *a, **k: label in loaded)
        monkeypatch.setattr("openswap.launch_agent._launchctl", fake_launchctl)
        # service_target calls os.getuid, which Windows lacks; the test farm runs there too.
        monkeypatch.setattr("openswap.launch_agent.service_target", lambda label, uid=None: f"gui/501/{label}")
        return calls

    def test_kickstarts_loaded_widget_and_returns_none(self, monkeypatch):
        from openswap.update_check import restart_widget_agent
        from openswap.widget_install import LABEL

        calls = self._arm(monkeypatch, 0, loaded={LABEL})
        assert restart_widget_agent() is None
        assert len(calls) == 1
        assert calls[0][:2] == ("kickstart", "-k")
        assert calls[0][2].endswith(LABEL)

    def test_migrates_loaded_legacy_widget_instead_of_kickstarting_it(
        self, monkeypatch
    ):
        from openswap.update_check import restart_widget_agent
        from openswap.widget_install import LEGACY_LABEL

        calls = self._arm(monkeypatch, 0, loaded={LEGACY_LABEL})
        installed = []
        monkeypatch.setattr(
            "openswap.widget_install.install_launch_agent",
            lambda app: installed.append(app) or {"label": "current"},
        )

        assert restart_widget_agent() is None
        assert len(installed) == 1
        assert installed[0].name == "OpenSwap.app"
        assert calls == []

    def test_reports_legacy_widget_migration_failure(self, monkeypatch):
        from openswap.exceptions import ClaudeSwitchError
        from openswap.update_check import restart_widget_agent
        from openswap.widget_install import LEGACY_LABEL

        self._arm(monkeypatch, 0, loaded={LEGACY_LABEL})

        def fail(app):
            raise ClaudeSwitchError("cannot migrate")

        monkeypatch.setattr("openswap.widget_install.install_launch_agent", fail)
        assert restart_widget_agent() == "legacy widget migration failed: cannot migrate"

    def test_returns_detail_when_kickstart_fails(self, monkeypatch):
        from openswap.update_check import restart_widget_agent
        from openswap.widget_install import LABEL

        self._arm(monkeypatch, 113, loaded={LABEL})
        detail = restart_widget_agent()
        assert detail is not None
        assert "113" in detail
        assert "Could not find service" in detail

    def test_no_loaded_widget_job_means_nothing_to_restart(self, monkeypatch):
        from openswap.update_check import restart_widget_agent

        calls = self._arm(monkeypatch, 113, loaded=set())
        assert restart_widget_agent() is None
        assert calls == []

    def test_upgrade_fails_when_widget_does_not_restart(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "openswap"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setattr("openswap.update_check._checkout_root", lambda: repo)
        monkeypatch.setattr("openswap.update_check.sys.platform", "darwin")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setattr("openswap.update_check.subprocess.run", lambda *a, **k: MagicMock(returncode=0))
        monkeypatch.setattr("openswap.launch_agent.is_loaded", lambda *a, **k: False)
        monkeypatch.setattr("openswap.update_check.restart_widget_agent", lambda: "kickstart exit 113")
        assert run_self_upgrade() == 1
        err = capsys.readouterr().err
        assert "kickstart exit 113" in err
        assert "openswap widget --install" in err
