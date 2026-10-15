"""Tests for update_check helpers that still exist after cutting PyPI."""

from __future__ import annotations

from openswap.update_check import (
    _checkout_root,
    _detect_install_method,
    _package_is_git_checkout,
    check_for_update,
)


class TestGitCheckoutGuard:
    def test_package_from_git_checkout_is_detected(self, tmp_path):
        (tmp_path / ".git").mkdir()
        package_file = tmp_path / "src" / "openswap" / "__init__.py"
        package_file.parent.mkdir(parents=True)
        package_file.write_text("")

        assert _package_is_git_checkout(package_file) is True
        assert _checkout_root(package_file) == tmp_path

    def test_package_inside_uv_tools_without_git_is_not_a_checkout(self, tmp_path):
        package_file = (
            tmp_path
            / "uv"
            / "tools"
            / "openswap"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "openswap"
            / "__init__.py"
        )
        package_file.parent.mkdir(parents=True)
        package_file.write_text("")

        assert _package_is_git_checkout(package_file) is False

    def test_venv_inside_another_git_repo_is_not_a_checkout(self, tmp_path):
        (tmp_path / ".git").mkdir()
        package_file = (
            tmp_path
            / ".venv"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "openswap"
            / "__init__.py"
        )
        package_file.parent.mkdir(parents=True)
        package_file.write_text("")

        assert _checkout_root(package_file) is None
        assert _package_is_git_checkout(package_file) is False

    def test_direct_url_editable_checkout(self, tmp_path):
        repo = tmp_path / "openswap"
        repo.mkdir()
        (repo / ".git").mkdir()
        (repo / "src" / "openswap").mkdir(parents=True)
        (repo / "src" / "openswap" / "__init__.py").write_text("")
        dist = (
            tmp_path
            / "uv"
            / "tools"
            / "openswap"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "openswap-0.1.0.dist-info"
        )
        dist.mkdir(parents=True)
        (dist / "direct_url.json").write_text(
            '{"url":"' + repo.as_uri() + '","dir_info":{"editable":true}}'
        )
        package_file = dist.parent / "openswap" / "__init__.py"
        package_file.parent.mkdir(parents=True)
        package_file.write_text("")

        assert _checkout_root(package_file) == repo

    def test_direct_url_nested_in_another_git_repo_is_rejected(self, tmp_path):
        vendor = tmp_path / "vendor" / "openswap"
        (vendor / "src" / "openswap").mkdir(parents=True)
        (vendor / "src" / "openswap" / "__init__.py").write_text("")
        (tmp_path / ".git").mkdir()
        dist = (
            tmp_path
            / ".venv"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "openswap-0.1.0.dist-info"
        )
        dist.mkdir(parents=True)
        (dist / "direct_url.json").write_text(
            '{"url":"' + vendor.as_uri() + '","dir_info":{}}'
        )
        package_file = dist.parent / "openswap" / "__init__.py"
        package_file.parent.mkdir(parents=True)
        package_file.write_text("")

        assert _checkout_root(package_file) is None


class TestDetectInstallMethod:
    def _set_prefix(self, monkeypatch, prefix: str) -> None:
        monkeypatch.setattr("openswap.update_check.sys.prefix", prefix)
        monkeypatch.delenv("UV_TOOL_DIR", raising=False)
        monkeypatch.delenv("PIPX_HOME", raising=False)

    def test_uv_tool_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/share/uv/tools/openswap")
        assert _detect_install_method() == "uv"

    def test_pipx_default_path(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/.local/pipx/venvs/openswap")
        assert _detect_install_method() == "pipx"

    def test_source_checkout_returns_none(self, monkeypatch):
        self._set_prefix(monkeypatch, "/home/me/code/openswap/.venv")
        assert _detect_install_method() is None


class TestCheckForUpdate:
    def test_never_nags(self):
        assert check_for_update("0.3.2") is None
        assert check_for_update("9.9.9") is None


class TestFrozenApp:
    """The cask's app upgrades through Homebrew and never runs git or uv."""

    def _no_subprocess(self, monkeypatch):
        def _refuse(*a, **k):
            raise AssertionError(f"frozen upgrade ran a subprocess: {a}")

        import openswap.update_check as uc

        monkeypatch.setattr(uc.subprocess, "run", _refuse)
        monkeypatch.setattr(uc.subprocess, "Popen", _refuse)

    def test_homebrew_install_names_the_brew_command(self, frozen_app, monkeypatch, capsys):
        from openswap import bundle
        from openswap.update_check import run_self_upgrade

        self._no_subprocess(monkeypatch)
        monkeypatch.setattr(bundle, "installed_by_homebrew", lambda home=None: True)
        assert run_self_upgrade() == 0
        out = capsys.readouterr().out
        assert (
            "OpenSwap was installed with Homebrew. Upgrade with: brew upgrade --cask openswap"
            in out
        )

    def test_copy_outside_homebrew_points_at_the_release_page(
        self, frozen_app, monkeypatch, capsys
    ):
        from openswap import bundle
        from openswap.update_check import RELEASES_URL, run_self_upgrade

        self._no_subprocess(monkeypatch)
        monkeypatch.setattr(bundle, "installed_by_homebrew", lambda home=None: False)
        assert run_self_upgrade() == 0
        out = capsys.readouterr().out
        assert RELEASES_URL in out
        assert "brew upgrade" not in out

    def test_restart_widget_agent_is_none_without_a_legacy_host(self, frozen_app, monkeypatch):
        import openswap.launch_agent as la
        from openswap.update_check import restart_widget_agent

        def _refuse(*a, **k):
            raise AssertionError("frozen restart must not drive launchctl")

        monkeypatch.setattr(la, "_launchctl", _refuse)
        assert restart_widget_agent() is None

    def test_restart_widget_agent_reports_a_legacy_host_without_touching_it(
        self, frozen_app, monkeypatch
    ):
        import plistlib
        from pathlib import Path

        import openswap.launch_agent as la
        import openswap.widget_install as wi
        from openswap.update_check import restart_widget_agent

        host = Path.home() / "Applications" / "OpenSwap.app"
        (host / "Contents").mkdir(parents=True)
        (host / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": "com.opensoft.openswap.widget"})
        )

        def _refuse(*a, **k):
            raise AssertionError("frozen restart must not rewrite or kick the host")

        monkeypatch.setattr(la, "_launchctl", _refuse)
        monkeypatch.setattr(wi, "install_launch_agent", _refuse)
        detail = restart_widget_agent()
        assert detail is not None and str(host) in detail


class TestUvBesideTheCask:
    def test_uv_upgrade_refuses_without_pulling_or_repinning(self, tmp_path, monkeypatch, capsys):
        """`_refresh_launch_agents` would point the login item back at uv."""
        import openswap.update_check as uc
        from openswap import bundle

        room = tmp_path / "homebrew" / "Caskroom" / "openswap"
        room.mkdir(parents=True)
        monkeypatch.setattr(bundle, "CASKROOMS", (room,))
        monkeypatch.delattr(uc.sys, "frozen", raising=False)

        def _refuse(*a, **k):
            raise AssertionError(f"refused upgrade still ran: {a}")

        monkeypatch.setattr(uc.subprocess, "run", _refuse)
        monkeypatch.setattr(uc, "_refresh_launch_agents", _refuse)
        assert uc.run_self_upgrade() == 1
        err = capsys.readouterr().err
        assert "installed with Homebrew" in err
        assert "uv tool uninstall openswap" in err
