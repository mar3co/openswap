"""Install helpers for the macOS widget companion (no xcodebuild)."""

from __future__ import annotations

import os

from pathlib import Path
import struct
from unittest.mock import patch

import pytest

from openswap import widget_install as wi
from openswap.exceptions import ClaudeSwitchError


def test_detect_development_team_prefers_env(monkeypatch):
    monkeypatch.setenv("DEVELOPMENT_TEAM", "ABCDE12345")
    assert wi.detect_development_team() == "ABCDE12345"


def test_team_from_codesign_output():
    blob = "Identifier=com.opensoft.openswap.widget\nTeamIdentifier=KJ999FVUJ4\nSigned Time=now\n"
    assert wi._team_from_codesign_output(blob) == "KJ999FVUJ4"
    assert wi._team_from_codesign_output("TeamIdentifier=not set\n") is None
    assert wi._team_from_codesign_output("") is None


def test_build_host_plist_points_at_the_binary(tmp_path: Path):
    app = tmp_path / "OpenSwap.app"
    binary = app / "Contents" / "MacOS" / "OpenSwap"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    data = wi.build_host_plist(app, home=tmp_path)
    import plistlib

    plist = plistlib.loads(data)
    assert plist["Label"] == "com.opensoft.openswap.widget"
    assert plist["ProgramArguments"] == [str(binary)]
    assert plist["RunAtLoad"] is True


def test_project_dir_finds_checkout_sources():
    expected = Path(__file__).resolve().parent.parent / "macos" / "OpenSwapWidget"
    found = wi.project_dir()
    assert found == expected
    assert (found / "OpenSwapWidget.xcodeproj").is_dir()


def test_widget_app_icons_use_the_opensoft_mark_at_every_declared_size():
    root = Path(__file__).resolve().parents[1]
    icon_dir = (
        root / "macos" / "OpenSwapWidget" / "Host" / "Assets.xcassets"
        / "AppIcon.appiconset"
    )
    for size in (16, 32, 64, 128, 256, 512, 1024):
        data = (icon_dir / f"icon_{size}.png").read_bytes()
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        assert struct.unpack(">II", data[16:24]) == (size, size)

    source = (root / "assets" / "opensoft-app-icon.svg").read_text()
    assert 'aria-label="OpenSoft"' in source
    assert '<rect width="32" height="32" rx="6" fill="#0a0a0a"/>' in source


def test_popover_packages_the_opensoft_symbol():
    package_dir = Path(__file__).resolve().parents[1] / "src" / "openswap"
    svg = (package_dir / "assets" / "opensoft-symbol.svg").read_text()
    png = package_dir / "assets" / "opensoft-symbol-64.png"
    panel = (package_dir / "menubar_panel.py").read_text()

    assert 'aria-label="OpenSoft"' in svg
    assert struct.unpack(">II", png.read_bytes()[16:24]) == (64, 64)
    assert "def _brand_mark" in panel
    assert '"OpenSwap"' in panel


def test_require_macos_refuses_other_platforms(monkeypatch):
    monkeypatch.setattr(wi.sys, "platform", "linux")
    with pytest.raises(ClaudeSwitchError, match="macOS"):
        wi._require_macos()


def test_widget_app_path_is_under_home_applications(tmp_path: Path):
    from openswap.widget_snapshot import widget_app_path

    assert widget_app_path(tmp_path) == tmp_path / "Applications" / "OpenSwap.app"


def test_install_launch_agent_reports_an_unwritable_launch_agents_directory(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(wi.sys, "platform", "darwin")
    app = tmp_path / "Host.app"
    binary = wi._host_binary(app)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    with patch.object(Path, "mkdir", side_effect=PermissionError(13, "Permission denied")):
        with pytest.raises(ClaudeSwitchError, match="Could not write the launch agent.*Permission denied"):
            wi.install_launch_agent(app, home=tmp_path, uid=501)


def test_install_launch_agent_removes_unloaded_legacy_plist(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(wi.sys, "platform", "darwin")
    app = tmp_path / "OpenSwap.app"
    binary = wi._host_binary(app)
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"")
    legacy = wi.plist_path(wi.LEGACY_LABEL, tmp_path)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old")
    monkeypatch.setattr(wi, "is_loaded", lambda *a, **k: False)
    monkeypatch.setattr(
        wi,
        "_launchctl",
        lambda *a, **k: type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})(),
    )

    wi.install_launch_agent(app, home=tmp_path, uid=501)

    assert not legacy.exists()


def test_copy_built_app_reports_a_copy_it_cannot_make(tmp_path: Path):
    built = tmp_path / "Build" / "Products" / "Release" / f"{wi.HOST_PRODUCT}.app"
    built.mkdir(parents=True)
    with patch.object(
        wi.shutil, "copytree", side_effect=PermissionError(13, "Permission denied")
    ):
        with pytest.raises(ClaudeSwitchError, match="Could not install the widget app.*Permission denied"):
            wi._copy_built_app(tmp_path, tmp_path / "Applications" / "Host.app")


# --- frozen OpenSwap.app (plan 008): register the bundled appex, never build ---


class _Recorder:
    """Stands in for subprocess.run: records argv, answers pluginkit."""

    def __init__(self, pluginkit_out: str = "", pluginkit_rc: int = 0):
        self.calls: list[list[str]] = []
        self.pluginkit_out = pluginkit_out
        self.pluginkit_rc = pluginkit_rc
        self.remove_rc: int | None = None  # `pluginkit -r` only, when set

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        import subprocess

        if argv[0] == "pluginkit" and argv[1] == "-r" and self.remove_rc is not None:
            return subprocess.CompletedProcess(argv, self.remove_rc, "", "remove failed")
        if argv[0] == "pluginkit":
            return subprocess.CompletedProcess(argv, self.pluginkit_rc, self.pluginkit_out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.fixture
def frozen_darwin(frozen_app, monkeypatch):
    """The frozen app on macOS, with every system call recorded, not run."""
    monkeypatch.setattr(wi.sys, "platform", "darwin")
    runs = _Recorder()
    monkeypatch.setattr(wi.subprocess, "run", runs)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "openswap.widget_snapshot.subprocess.Popen",
        lambda argv, **k: spawned.append(list(argv)),
    )

    def _no_xcode():
        raise AssertionError("the frozen app must not build the widget")

    monkeypatch.setattr(wi, "_xcodebuild", _no_xcode)
    monkeypatch.setattr(wi, "detect_development_team", _no_xcode)
    monkeypatch.setattr(wi, "is_loaded", lambda *a, **k: False)
    trashed: list[Path] = []
    monkeypatch.setattr(wi, "_move_to_trash", trashed.append)
    return {"app": frozen_app, "runs": runs, "spawned": spawned, "trashed": trashed}


def test_frozen_install_registers_the_bundled_appex_and_never_builds(frozen_darwin, tmp_path):
    app = frozen_darwin["app"]
    result = wi.install_widget(home=tmp_path)
    appex = app / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex"
    assert frozen_darwin["runs"].calls == [["pluginkit", "-a", str(appex)]]
    assert not any("xcodebuild" in " ".join(c) for c in frozen_darwin["runs"].calls)
    # The helper reloads timelines once, in place of opening a host app.
    assert frozen_darwin["spawned"] == [[str(app / "Contents" / "MacOS" / "openswap-widget-reload")]]
    assert result["app"] == str(app)
    assert {"app", "label", "plist"} <= set(result)
    assert not (tmp_path / "Applications").exists()


def test_frozen_install_reports_a_pluginkit_refusal(frozen_darwin, tmp_path):
    frozen_darwin["runs"].pluginkit_rc = 1
    with pytest.raises(ClaudeSwitchError, match="pluginkit could not register"):
        wi.install_widget(home=tmp_path)


def test_frozen_install_needs_the_appex_in_the_bundle(frozen_darwin, tmp_path):
    import shutil

    shutil.rmtree(frozen_darwin["app"] / "Contents" / "PlugIns")
    with pytest.raises(ClaudeSwitchError, match="missing"):
        wi.install_widget(home=tmp_path)
    assert frozen_darwin["runs"].calls == []


def test_frozen_uninstall_only_unregisters_and_keeps_the_bundle(frozen_darwin, tmp_path):
    app = frozen_darwin["app"]
    result = wi.uninstall_widget(home=tmp_path)
    appex = app / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex"
    assert frozen_darwin["runs"].calls == [["pluginkit", "-r", str(appex)]]
    assert app.is_dir()
    assert frozen_darwin["trashed"] == []
    assert result["unregistered"] is True and result["removed_app"] is False


def test_frozen_status_reports_each_registered_copy(frozen_darwin, tmp_path):
    """-D lists every physical copy; plain -m hides a duplicate provider."""
    # pluginkit prints macOS paths; literal ones keep this true on Windows CI.
    cask = "/Applications/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex"
    old = "/Users/x/Applications/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex"
    frozen_darwin["runs"].pluginkit_out = (
        "+    com.opensoft.openswap.widget.extension(1.0)\tAAAA\t2026-10-01 10:00:00 +0000\t"
        f"{cask}\n"
        "     com.opensoft.openswap.widget.extension(1.0)\tBBBB\t2026-09-01 10:00:00 +0000\t"
        f"{old}\n"
        "\n"
    )
    state = wi.widget_status(home=tmp_path)
    assert frozen_darwin["runs"].calls == [
        ["pluginkit", "-m", "-D", "-v", "-i", "com.opensoft.openswap.widget.extension"]
    ]
    assert state["bundled"] is True
    assert state["registered"] is True
    assert state["registrations"] == [cask, old]


def test_frozen_status_with_one_copy(frozen_darwin, tmp_path):
    cask = "/Applications/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex"
    frozen_darwin["runs"].pluginkit_out = (
        f"+    com.opensoft.openswap.widget.extension(1.0)\tAAAA\t2026-10-01 10:00:00 +0000\t{cask}\n"
    )
    assert wi.widget_status(home=tmp_path)["registrations"] == [cask]


def test_frozen_status_with_nothing_registered(frozen_darwin, tmp_path):
    state = wi.widget_status(home=tmp_path)
    assert state["registered"] is False
    assert state["registrations"] == []


def test_unfrozen_install_still_builds_with_xcode(tmp_path, monkeypatch):
    """Source installs keep the xcodebuild path: the frozen branch must not leak."""
    monkeypatch.setattr(wi.sys, "platform", "darwin")
    monkeypatch.delattr(wi.sys, "frozen", raising=False)
    monkeypatch.setattr(wi, "_xcodebuild", lambda: "/usr/bin/xcodebuild")
    calls: list[list[str]] = []

    def failed_build(argv, **kwargs):
        calls.append(list(argv))
        return type("Proc", (), {"returncode": 1})()

    monkeypatch.setattr(wi.subprocess, "run", failed_build)
    with pytest.raises(ClaudeSwitchError, match="xcodebuild exit 1"):
        wi.install_widget(home=tmp_path, derived=tmp_path / "derived", team="ABCDE12345")
    assert calls[0][0] == "/usr/bin/xcodebuild"
    assert not any(c[0] == "pluginkit" for c in calls)


def _old_host(home: Path) -> Path:
    import plistlib

    app = home / "Applications" / "OpenSwap.app"
    (app / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.opensoft.openswap.widget"})
    )
    return app


def _built_host(home: Path) -> Path:
    appex = (
        home / "Library" / "Caches" / "openswap-widget" / "Build" / "Products" / "Release"
        / "OpenSwap.app" / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex"
    )
    appex.mkdir(parents=True)
    return appex


def test_frozen_install_registers_before_retiring_the_old_host(frozen_darwin, tmp_path, monkeypatch):
    # install_widget takes no uid, so the old host's bootout asks os.getuid(),
    # which Windows CI does not have.
    monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
    old = _old_host(tmp_path)
    wi.install_widget(home=tmp_path)
    bundled = str(frozen_darwin["app"] / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex")
    calls = [c for c in frozen_darwin["runs"].calls if c[0] == "pluginkit"]
    # Registered once, before the old copy is unregistered.
    assert calls == [
        ["pluginkit", "-a", bundled],
        ["pluginkit", "-r", str(old / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex")],
    ]
    assert frozen_darwin["trashed"] == [old]


def test_a_failed_registration_retires_nothing_so_the_next_run_retries(frozen_darwin, tmp_path):
    old = _old_host(tmp_path)
    built = _built_host(tmp_path)
    frozen_darwin["runs"].pluginkit_rc = 1
    with pytest.raises(ClaudeSwitchError, match="pluginkit could not register"):
        wi.retire_legacy_host(home=tmp_path, uid=501)
    assert frozen_darwin["trashed"] == []
    assert old.is_dir() and built.is_dir()
    assert all(c[:2] != ["pluginkit", "-r"] for c in frozen_darwin["runs"].calls)

    frozen_darwin["runs"].pluginkit_rc = 0
    assert wi.retire_legacy_host(home=tmp_path, uid=501) == old
    assert frozen_darwin["trashed"] == [old]


def test_the_build_product_is_unregistered_and_its_cache_removed(frozen_darwin, tmp_path):
    built = _built_host(tmp_path)
    cache = tmp_path / "Library" / "Caches" / "openswap-widget"
    keep = tmp_path / "Library" / "Caches" / "something-else"
    keep.mkdir()
    assert wi.retire_legacy_host(home=tmp_path, uid=501) == cache
    assert ["pluginkit", "-r", str(built)] in frozen_darwin["runs"].calls
    assert not cache.exists()
    assert keep.is_dir()
    assert frozen_darwin["trashed"] == []
    # Done: a second run has nothing to retire.
    before = list(frozen_darwin["runs"].calls)
    assert wi.retire_legacy_host(home=tmp_path, uid=501) is None
    assert frozen_darwin["runs"].calls == before


def test_old_host_and_build_cache_go_together(frozen_darwin, tmp_path):
    old = _old_host(tmp_path)
    built = _built_host(tmp_path)
    assert wi.retire_legacy_host(home=tmp_path, uid=501) == old
    calls = [c for c in frozen_darwin["runs"].calls if c[0] == "pluginkit"]
    assert calls[0][:2] == ["pluginkit", "-a"]
    assert ["pluginkit", "-r", str(built)] in calls
    assert frozen_darwin["trashed"] == [old]
    assert not (tmp_path / "Library" / "Caches" / "openswap-widget").exists()


def test_old_host_is_kept_when_unregistering_its_appex_fails(frozen_darwin, tmp_path, monkeypatch):
    """A copy left registered beside the bundled one is a second provider."""
    old = _old_host(tmp_path)
    old_appex = old / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex"
    runs = frozen_darwin["runs"]
    runs.remove_rc = 1
    # Still listed after the failed -r (a tmp path, so not via pluginkit's
    # macOS-path output: that keeps this true on Windows CI).
    registered = [str(old_appex)]
    monkeypatch.setattr(wi, "_appex_registrations", lambda: registered)
    with pytest.raises(ClaudeSwitchError, match="could not unregister"):
        wi.retire_legacy_host(home=tmp_path, uid=501)
    assert frozen_darwin["trashed"] == []
    assert old.is_dir()
    # The bundled appex was registered first; the old one was never booted out.
    assert runs.calls[0][:2] == ["pluginkit", "-a"]
    assert not any(c[0] == "launchctl" for c in runs.calls)

    runs.remove_rc = None
    registered.clear()
    assert wi.retire_legacy_host(home=tmp_path, uid=501) == old
    assert frozen_darwin["trashed"] == [old]


def test_a_nonzero_remove_of_an_unregistered_copy_is_not_a_failure(frozen_darwin, tmp_path):
    """pluginkit documents no exit status for an unknown path: check the registry."""
    old = _old_host(tmp_path)
    built = _built_host(tmp_path)
    runs = frozen_darwin["runs"]
    runs.remove_rc = 1
    assert wi.retire_legacy_host(home=tmp_path, uid=501) == old
    assert ["pluginkit", "-m", "-D", "-v", "-i", "com.opensoft.openswap.widget.extension"] in runs.calls
    assert frozen_darwin["trashed"] == [old]
    assert not built.exists()


def test_the_build_cache_is_kept_when_its_appex_stays_registered(frozen_darwin, tmp_path, monkeypatch):
    built = _built_host(tmp_path)
    frozen_darwin["runs"].remove_rc = 1
    monkeypatch.setattr(wi, "_appex_registrations", lambda: [str(built)])
    with pytest.raises(ClaudeSwitchError, match="could not unregister"):
        wi.retire_legacy_host(home=tmp_path, uid=501)
    assert built.is_dir()


# --- failing closed ----------------------------------------------------------


def test_an_unreadable_registry_keeps_the_old_copy(frozen_darwin, tmp_path):
    """`pluginkit -r` failed and `-m` cannot answer: that is not "unregistered"."""
    frozen_darwin["runs"].remove_rc = 1
    frozen_darwin["runs"].pluginkit_rc = 1
    with pytest.raises(ClaudeSwitchError, match="could not list registrations"):
        wi._unregister_appex(_old_host(tmp_path) / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex")


def test_frozen_status_reports_an_unreadable_registry(frozen_darwin, tmp_path):
    frozen_darwin["runs"].pluginkit_rc = 1
    with pytest.raises(ClaudeSwitchError, match="could not list"):
        wi.widget_status(home=tmp_path)


@pytest.fixture
def source_darwin(monkeypatch):
    """A uv install on macOS; the host agent's removal is recorded, not run."""
    monkeypatch.delattr(wi.sys, "frozen", raising=False)
    monkeypatch.setattr(wi.sys, "platform", "darwin")
    monkeypatch.setattr(wi, "uninstall_launch_agent", lambda **k: {"removed_plist": False, "was_loaded": False})


def test_source_uninstall_removes_the_old_host(source_darwin, tmp_path):
    app = _old_host(tmp_path)
    assert wi.uninstall_widget(home=tmp_path)["removed_app"] is True
    assert not app.exists()


def test_source_uninstall_never_deletes_another_app_at_that_path(source_darwin, tmp_path):
    """With the cask's appdir at ~/Applications, that path is the cask's app."""
    import plistlib

    app = tmp_path / "Applications" / "OpenSwap.app"
    (app / "Contents").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.opensoft.openswap"}))
    assert wi.uninstall_widget(home=tmp_path)["removed_app"] is False
    assert app.is_dir()


def test_source_uninstall_is_refused_beside_the_cask(source_darwin, tmp_path, monkeypatch):
    from openswap import bundle

    monkeypatch.setattr(bundle, "cask_prefix", lambda: Path("/opt/homebrew"))
    app = _old_host(tmp_path)
    with pytest.raises(ClaudeSwitchError, match="installed with Homebrew"):
        wi.uninstall_widget(home=tmp_path)
    assert app.is_dir()
