"""Where the frozen OpenSwap.app is, as seen from inside it (plan 008).

``frozen_app`` (conftest) lays out a fake bundle under ``tmp_path`` and points
``sys.frozen`` / ``sys.executable`` at it; nothing here touches a real app.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openswap import bundle


def test_not_frozen_reports_no_bundle(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert bundle.is_frozen() is False
    assert bundle.app_path() is None
    assert bundle.executable() is None
    assert bundle.reload_helper() is None
    assert bundle.appex() is None
    assert bundle.installed_by_homebrew() is False


def test_frozen_finds_the_enclosing_app(frozen_app):
    assert bundle.is_frozen() is True
    assert bundle.app_path() == frozen_app
    assert bundle.executable() == frozen_app / "Contents" / "MacOS" / "OpenSwap"
    assert bundle.reload_helper() == frozen_app / "Contents" / "MacOS" / "openswap-widget-reload"
    assert bundle.appex() == frozen_app / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex"


def test_frozen_resolves_the_brew_symlink_back_to_the_bundle(frozen_app, tmp_path, monkeypatch):
    """The cask's `binary` stanza is a symlink; the bundle is what it points at."""
    link = tmp_path / "bin" / "openswap"
    link.parent.mkdir()
    link.symlink_to(frozen_app / "Contents" / "MacOS" / "OpenSwap")
    monkeypatch.setattr(sys, "executable", str(link))
    assert bundle.app_path() == frozen_app
    assert bundle.executable() == frozen_app / "Contents" / "MacOS" / "OpenSwap"


def test_frozen_outside_any_app_reports_no_bundle(tmp_path, monkeypatch):
    loose = tmp_path / "dist" / "OpenSwap"
    loose.parent.mkdir()
    loose.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(loose))
    assert bundle.app_path() is None
    assert bundle.executable() is None


def _app_in(folder: Path, monkeypatch) -> Path:
    exe = folder / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    return exe


@pytest.fixture(autouse=True)
def _no_brew_binary(tmp_path, monkeypatch):
    """Keep a real `<prefix>/bin/openswap` on this Mac out of the answer."""
    monkeypatch.setattr(bundle, "BREW_BINARIES", (tmp_path / "brew" / "bin" / "openswap",))


@pytest.mark.parametrize("room", [0, 1], ids=["apple-silicon", "intel"])
def test_installed_by_homebrew_in_the_appdir_with_a_caskroom(tmp_path, monkeypatch, room):
    caskrooms = (tmp_path / "opt" / "Caskroom" / "openswap", tmp_path / "usr" / "Caskroom" / "openswap")
    monkeypatch.setattr(bundle, "CASKROOMS", caskrooms)
    _app_in(tmp_path / "Applications", monkeypatch)
    assert bundle.installed_by_homebrew(home=tmp_path) is False  # no Caskroom yet
    caskrooms[room].mkdir(parents=True)
    assert bundle.installed_by_homebrew(home=tmp_path) is True


def test_installed_by_homebrew_is_false_outside_an_appdir(frozen_app, tmp_path, monkeypatch):
    """A copy dragged somewhere else is not what `brew upgrade` replaces."""
    room = tmp_path / "Caskroom" / "openswap"
    room.mkdir(parents=True)
    monkeypatch.setattr(bundle, "CASKROOMS", (room,))
    assert bundle.installed_by_homebrew(home=tmp_path) is False


def test_installed_by_homebrew_with_a_custom_appdir(tmp_path, monkeypatch):
    """`brew install --cask --appdir=...` still links `<prefix>/bin/openswap` to the app."""
    room = tmp_path / "Caskroom" / "openswap"
    room.mkdir(parents=True)
    monkeypatch.setattr(bundle, "CASKROOMS", (room,))
    exe = _app_in(tmp_path / "Custom Apps", monkeypatch)
    link = tmp_path / "brew" / "bin" / "openswap"
    link.parent.mkdir(parents=True)
    link.symlink_to(exe)
    assert bundle.installed_by_homebrew(home=tmp_path) is True


def test_a_brew_link_to_another_app_does_not_count(tmp_path, monkeypatch):
    room = tmp_path / "Caskroom" / "openswap"
    room.mkdir(parents=True)
    monkeypatch.setattr(bundle, "CASKROOMS", (room,))
    other = tmp_path / "Elsewhere" / "OpenSwap"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    _app_in(tmp_path / "Custom Apps", monkeypatch)
    link = tmp_path / "brew" / "bin" / "openswap"
    link.parent.mkdir(parents=True)
    link.symlink_to(other)
    assert bundle.installed_by_homebrew(home=tmp_path) is False
