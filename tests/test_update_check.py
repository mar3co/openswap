"""Tests for update_check helpers that still exist after cutting PyPI."""

from __future__ import annotations

import pytest

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


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


@pytest.mark.no_release_fetch_fake
class TestLatestRelease:
    def _serve(self, monkeypatch, body=None, error=None):
        calls = []

        def fake_urlopen(req, timeout):
            calls.append(req.full_url)
            if error is not None:
                raise error
            return _Response(body)

        monkeypatch.setattr("openswap.update_check.urllib.request.urlopen", fake_urlopen)
        return calls

    def _later(self, monkeypatch, seconds):
        import time

        now = time.time() + seconds
        monkeypatch.setattr("openswap.cache.time.time", lambda: now)

    def test_reads_the_tag_without_its_v(self, monkeypatch):
        from openswap.update_check import latest_release

        self._serve(monkeypatch, b'{"tag_name": "v0.3.0"}')
        assert latest_release() == "0.3.0"

    def test_caches_the_answer_for_a_day(self, monkeypatch):
        from openswap.update_check import latest_release

        calls = self._serve(monkeypatch, b'{"tag_name": "v0.3.0"}')
        latest_release()
        self._later(monkeypatch, 23 * 3600)
        assert latest_release() == "0.3.0"
        assert len(calls) == 1
        self._later(monkeypatch, 25 * 3600)
        latest_release()
        assert len(calls) == 2

    def test_a_failed_fetch_keeps_the_last_answer_and_retries_within_the_hour(self, monkeypatch):
        from openswap.update_check import latest_release

        self._serve(monkeypatch, b'{"tag_name": "v0.3.0"}')
        latest_release()
        self._later(monkeypatch, 25 * 3600)
        calls = self._serve(monkeypatch, error=OSError("offline"))
        assert latest_release() == "0.3.0"
        assert latest_release() == "0.3.0"
        assert len(calls) == 1
        self._later(monkeypatch, 26 * 3600 + 1)
        latest_release()
        assert len(calls) == 2

    def test_a_stalled_fetch_has_already_opened_the_retry_window(self, monkeypatch):
        from openswap.cache import read_cache
        from openswap.update_check import _cache_path, latest_release

        self._serve(monkeypatch, b'{"tag_name": "v0.3.0"}')
        latest_release()
        self._later(monkeypatch, 25 * 3600)
        seen = []

        def stalled(req, timeout):
            seen.append(read_cache(_cache_path("update_check.json"), float("inf")))
            raise OSError("killed with the process")

        monkeypatch.setattr("openswap.update_check.urllib.request.urlopen", stalled)
        latest_release()
        assert seen == [{"latest": "0.3.0", "ok": False}]

    def test_a_first_failed_fetch_is_no_release(self, monkeypatch):
        from openswap.update_check import latest_release

        self._serve(monkeypatch, error=OSError("offline"))
        assert latest_release() is None

    def test_a_body_without_a_tag_is_a_failed_fetch(self, monkeypatch):
        from openswap.update_check import latest_release

        calls = self._serve(monkeypatch, b'{"message": "Not Found"}')
        assert latest_release() is None
        latest_release()
        assert len(calls) == 1

    def test_the_old_pypi_cache_is_refetched(self, monkeypatch):
        from openswap.cache import write_cache
        from openswap.update_check import _cache_path, latest_release

        write_cache(_cache_path("update_check.json"), "0.1.0")
        self._serve(monkeypatch, b'{"tag_name": "v0.3.0"}')
        assert latest_release() == "0.3.0"


class TestNewerRelease:
    def _latest(self, monkeypatch, version):
        monkeypatch.setattr("openswap.update_check.latest_release", lambda: version)

    def test_a_newer_release_is_reported(self, monkeypatch):
        from openswap.update_check import newer_release

        self._latest(monkeypatch, "0.3.0")
        assert newer_release("0.2.2") == "0.3.0"

    def test_the_same_or_an_older_release_is_not(self, monkeypatch):
        from openswap.update_check import newer_release

        self._latest(monkeypatch, "0.2.2")
        assert newer_release("0.2.2") is None
        assert newer_release("0.3.0") is None

    def test_a_dev_build_of_that_release_is_behind_it(self, monkeypatch):
        from openswap.update_check import newer_release

        self._latest(monkeypatch, "0.3.0")
        assert newer_release("0.3.0.dev1") == "0.3.0"

    def test_no_release_or_a_tag_that_is_not_a_version(self, monkeypatch):
        from openswap.update_check import newer_release

        self._latest(monkeypatch, None)
        assert newer_release("0.2.2") is None
        self._latest(monkeypatch, "nightly")
        assert newer_release("0.2.2") is None


class TestUpgradeHint:
    def test_cask_app_names_the_brew_command(self, frozen_app, monkeypatch):
        from openswap import bundle
        from openswap.update_check import upgrade_hint

        monkeypatch.setattr(bundle, "installed_by_homebrew", lambda home=None: True)
        assert upgrade_hint() == "Upgrade with: brew upgrade --cask openswap"

    def test_other_app_copy_points_at_the_release_page(self, frozen_app, monkeypatch):
        from openswap import bundle
        from openswap.update_check import RELEASES_URL, upgrade_hint

        monkeypatch.setattr(bundle, "installed_by_homebrew", lambda home=None: False)
        assert upgrade_hint() == f"Download it from {RELEASES_URL}"

    def test_uv_install_names_openswap_upgrade(self, monkeypatch):
        import openswap.update_check as uc

        monkeypatch.delattr(uc.sys, "frozen", raising=False)
        assert uc.upgrade_hint() == "Run `openswap upgrade` to update."


class TestCheckForUpdate:
    def test_names_both_versions_and_the_hint(self, monkeypatch):
        import openswap.update_check as uc

        monkeypatch.setattr(uc, "newer_release", lambda current: "0.3.0")
        monkeypatch.setattr(uc, "upgrade_hint", lambda: "HINT")
        assert check_for_update("0.2.2") == (
            "OpenSwap 0.3.0 is available (you have 0.2.2). HINT"
        )

    def test_quiet_when_up_to_date(self, monkeypatch):
        monkeypatch.setattr("openswap.update_check.newer_release", lambda current: None)
        assert check_for_update("0.2.2") is None

    def test_never_raises(self, monkeypatch):
        def boom(current):
            raise RuntimeError("cache dir unwritable")

        monkeypatch.setattr("openswap.update_check.newer_release", boom)
        assert check_for_update("0.2.2") is None

    def test_a_stalled_check_does_not_hold_the_command(self, monkeypatch):
        import threading

        import openswap.update_check as uc

        release = threading.Event()
        monkeypatch.setattr(uc, "newer_release", lambda current: release.wait(5) and "0.3.0")
        monkeypatch.setattr(uc, "NOTICE_WAIT_S", 0.05)
        try:
            assert check_for_update("0.2.2") is None
        finally:
            release.set()


class TestClaimNotification:
    def test_each_version_is_announced_once(self):
        from openswap.update_check import claim_notification

        assert claim_notification("0.3.0") is True
        assert claim_notification("0.3.0") is False
        assert claim_notification("0.3.1") is True
        assert claim_notification("0.3.0") is True

    def test_unrecordable_claim_stays_silent(self, monkeypatch):
        from openswap.update_check import claim_notification

        def unwritable(path, data):
            raise OSError("read-only")

        monkeypatch.setattr("openswap.cache.write_cache", unwritable)
        assert claim_notification("0.3.0") is False


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


class TestMenuBarUpdateNotice:
    @pytest.fixture
    def app(self, monkeypatch):
        from openswap import menubar
        from openswap.menubar_display import NotificationCopy
        from tests.menubar_harness import extract_class

        app = extract_class(
            menubar.__file__, "MenuBarApp", {"_apply_update_found"},
            {"NotificationCopy": NotificationCopy},
        )()
        app._update_found = app._update_shown = None
        app._dirty = False
        app.notified = []
        app._notify = app.notified.append
        app.claims = {}

        def claim(version):
            first = app.claims.get(version, True)
            app.claims[version] = False
            return first

        monkeypatch.setattr("openswap.update_check.claim_notification", claim)
        monkeypatch.setattr("openswap.update_check.upgrade_hint", lambda: "HINT")
        return app

    def test_nothing_found_changes_nothing(self, app):
        app._apply_update_found()
        assert app._dirty is False and app.notified == []

    def test_a_new_release_rebuilds_the_menu_and_notifies_once(self, app):
        app._update_found = "0.3.0"
        app._apply_update_found()
        assert app._dirty is True and app._update_shown == "0.3.0"
        assert [(n.title, n.body) for n in app.notified] == [("OpenSwap 0.3.0 is available", "HINT")]
        app._dirty = False
        app._apply_update_found()
        assert app._dirty is False and len(app.notified) == 1

    def test_an_already_announced_release_shows_the_item_without_a_notice(self, app):
        app.claims["0.3.0"] = False
        app._update_found = "0.3.0"
        app._apply_update_found()
        assert app._dirty is True and app.notified == []

    def test_upgrading_past_the_release_removes_the_item(self, app):
        app._update_found = app._update_shown = "0.3.0"
        app._update_found = None
        app._apply_update_found()
        assert app._dirty is True and app._update_shown is None and app.notified == []
