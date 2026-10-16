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


def test_installed_by_homebrew_in_slash_applications(tmp_path, monkeypatch):
    """The cask's default appdir is /Applications, not only ~/Applications."""
    room = tmp_path / "Caskroom" / "openswap"
    room.mkdir(parents=True)
    monkeypatch.setattr(bundle, "CASKROOMS", (room,))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    # resolve() so the comparison holds on Windows CI too (C:\Applications).
    monkeypatch.setattr(bundle, "app_path", lambda: Path("/Applications").resolve() / "OpenSwap.app")
    assert bundle.installed_by_homebrew(home=tmp_path) is True
    room.rmdir()
    assert bundle.installed_by_homebrew(home=tmp_path) is False


# --- a uv install beside the cask ---------------------------------------------


def test_uv_beside_the_cask_is_refused_and_told_where_the_app_is(tmp_path, monkeypatch):
    from openswap.exceptions import ClaudeSwitchError

    monkeypatch.delattr(sys, "frozen", raising=False)
    prefix = tmp_path / "homebrew"
    room = prefix / "Caskroom" / "openswap"
    monkeypatch.setattr(bundle, "CASKROOMS", (room,))
    bundle.refuse_uv_beside_cask("set up the menu bar extra")  # no cask: fine
    assert bundle.cask_prefix() is None

    room.mkdir(parents=True)
    assert bundle.cask_prefix() == prefix
    with pytest.raises(ClaudeSwitchError) as exc:
        bundle.refuse_uv_beside_cask("set up the menu bar extra")
    message = str(exc.value)
    assert "installed with Homebrew" in message
    assert str(prefix / "bin" / "openswap") in message
    assert "uv tool uninstall openswap" in message


def test_the_app_itself_is_never_refused(frozen_app, tmp_path, monkeypatch):
    room = tmp_path / "Caskroom" / "openswap"
    room.mkdir(parents=True)
    monkeypatch.setattr(bundle, "CASKROOMS", (room,))
    bundle.refuse_uv_beside_cask("set up the menu bar extra")


def _login_item(home: Path, program) -> None:
    import plistlib

    from openswap import launch_agent

    plist = launch_agent.plist_path(home=home)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({"Label": launch_agent.LABEL, "ProgramArguments": program}))


def test_login_item_runs_this_app_after_setup(frozen_app, tmp_path):
    _login_item(tmp_path, [str(frozen_app / "Contents" / "MacOS" / "OpenSwap")])
    assert bundle.login_item_runs_this_app(home=tmp_path) is True


@pytest.mark.parametrize(
    "program",
    [
        ["/Users/x/.local/bin/openswap", "menubar"],
        ["/Applications/Other/OpenSwap.app/Contents/MacOS/OpenSwap"],
        [],
        "not a list",
    ],
    ids=["uv", "another-copy", "empty", "garbled"],
)
def test_login_item_runs_something_else(frozen_app, tmp_path, program):
    _login_item(tmp_path, program)
    assert bundle.login_item_runs_this_app(home=tmp_path) is False


def test_no_login_item(frozen_app, tmp_path):
    assert bundle.login_item_runs_this_app(home=tmp_path) is False


def test_a_uv_install_is_never_the_apps_login_item(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    _login_item(tmp_path, ["/Users/x/.local/bin/openswap", "menubar"])
    assert bundle.login_item_runs_this_app(home=tmp_path) is False
