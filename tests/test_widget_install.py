"""Install helpers for the macOS widget companion (no xcodebuild)."""

from __future__ import annotations

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


def test_copy_built_app_reports_a_copy_it_cannot_make(tmp_path: Path):
    built = tmp_path / "Build" / "Products" / "Release" / f"{wi.HOST_PRODUCT}.app"
    built.mkdir(parents=True)
    with patch.object(
        wi.shutil, "copytree", side_effect=PermissionError(13, "Permission denied")
    ):
        with pytest.raises(ClaudeSwitchError, match="Could not install the widget app.*Permission denied"):
            wi._copy_built_app(tmp_path, tmp_path / "Applications" / "Host.app")
