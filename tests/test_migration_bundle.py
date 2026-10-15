"""Moving a Mac from the uv install to the cask's OpenSwap.app (plan 008 Step 5).

Runs against a temp home with ``frozen_app`` (conftest) standing in for the
bundle. launchctl, pluginkit, and the Trash are fakes that record what they
were asked to do; nothing here touches a real LaunchAgent, widget, or file
outside ``tmp_path``.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest

from openswap import bundle, launch_agent
from openswap import widget_install as wi
from openswap.exceptions import ClaudeSwitchError

UID = 501
UV_SCRIPT = "/Users/x/.local/bin/openswap"


@pytest.fixture
def mac(frozen_app, tmp_path, monkeypatch):
    """A temp home on "macOS" with every system call recorded, not run."""
    home = tmp_path / "home"
    home.mkdir()
    for module in (bundle, launch_agent, wi):
        monkeypatch.setattr(module.sys, "platform", "darwin")
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)

    seen: dict[str, list] = {"install": [], "launchctl": [], "pluginkit": [], "trash": [], "reload": []}

    def fake_install(label=launch_agent.LABEL, home=None, program=None, uid=None):
        seen["install"].append(program)
        target = launch_agent.plist_path(label, home)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(launch_agent.build_plist(program, label, home))
        return {"label": label}

    def fake_launchctl(*args):
        seen["launchctl"].append(list(args))
        # `print` answers "not loaded"; everything else succeeds.
        return subprocess.CompletedProcess(["launchctl", *args], 1 if args[0] == "print" else 0, "", "")

    def fake_pluginkit(*args):
        seen["pluginkit"].append(list(args))
        return subprocess.CompletedProcess(["pluginkit", *args], 0, "", "")

    def fake_trash(path: Path):
        seen["trash"].append(path)
        shutil.rmtree(path)

    monkeypatch.setattr(launch_agent, "install", fake_install)
    monkeypatch.setattr(launch_agent, "_launchctl", fake_launchctl)
    monkeypatch.setattr(wi, "_launchctl", fake_launchctl)
    monkeypatch.setattr(wi, "is_loaded", lambda *a, **k: False)
    monkeypatch.setattr(wi, "_pluginkit", fake_pluginkit)
    monkeypatch.setattr(wi, "_move_to_trash", fake_trash)
    return {"home": home, "app": frozen_app, "exe": frozen_app / "Contents/MacOS/OpenSwap", "seen": seen}


def _menubar_plist(home: Path, program: str) -> Path:
    plist = launch_agent.plist_path(home=home)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(launch_agent.build_plist([program], home=home))
    return plist


def _program(plist: Path) -> str:
    return plistlib.loads(plist.read_bytes())["ProgramArguments"][0]


def _old_host(home: Path, bundle_id: str = "com.opensoft.openswap.widget") -> Path:
    app = home / "Applications" / "OpenSwap.app"
    (app / "Contents" / "PlugIns" / "OpenSwapWidgetExtension.appex").mkdir(parents=True)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": bundle_id}))
    return app


def _migrate(mac, notes: list[str] | None = None) -> list[str]:
    return bundle.migrate_from_uv_install(
        home=mac["home"], uid=UID, notify=None if notes is None else notes.append
    )


# --- 1. menu bar login item --------------------------------------------------


def test_uv_login_item_is_repointed_at_the_bundle(mac):
    _menubar_plist(mac["home"], UV_SCRIPT)
    assert _migrate(mac) == ["menubar agent"]
    assert mac["seen"]["install"] == [[str(mac["exe"])]]
    assert _program(launch_agent.plist_path(home=mac["home"])) == str(mac["exe"])


def test_login_item_already_on_the_bundle_is_left_alone(mac):
    plist = _menubar_plist(mac["home"], str(mac["exe"]))
    before = plist.read_bytes()
    assert _migrate(mac) == []
    assert mac["seen"]["install"] == []
    assert plist.read_bytes() == before


def test_no_login_item_is_not_created(mac):
    """`openswap setup` installs the login item; the migration only moves one."""
    assert _migrate(mac) == []
    assert mac["seen"]["install"] == []
    assert not launch_agent.plist_path(home=mac["home"]).exists()


def test_inside_the_agent_only_the_plist_is_rewritten(mac, monkeypatch):
    """Re-bootstrapping from inside the agent would boot it out mid-start."""
    plist = _menubar_plist(mac["home"], UV_SCRIPT)
    monkeypatch.setenv("XPC_SERVICE_NAME", launch_agent.LABEL)
    assert _migrate(mac) == ["menubar plist"]
    assert mac["seen"]["install"] == []
    assert mac["seen"]["launchctl"] == []
    assert _program(plist) == str(mac["exe"])


def test_unreadable_login_item_is_rewritten(mac):
    plist = launch_agent.plist_path(home=mac["home"])
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"not a plist")
    assert _migrate(mac) == ["menubar agent"]
    assert _program(plist) == str(mac["exe"])


# --- 2. old widget host ------------------------------------------------------


def test_old_widget_host_is_booted_out_unregistered_and_trashed(mac, monkeypatch):
    home = mac["home"]
    old = _old_host(home)
    host_plist = launch_agent.plist_path(wi.LABEL, home)
    host_plist.parent.mkdir(parents=True, exist_ok=True)
    host_plist.write_bytes(b"")
    monkeypatch.setattr(launch_agent, "is_loaded", lambda label=None, uid=None: label == wi.LABEL)

    assert _migrate(mac) == ["widget host"]

    assert ["bootout", f"gui/{UID}/{wi.LABEL}"] in mac["seen"]["launchctl"]
    assert not host_plist.exists()
    assert mac["seen"]["trash"] == [old]
    assert mac["seen"]["pluginkit"] == [
        ["-r", str(old / "Contents/PlugIns/OpenSwapWidgetExtension.appex")],
        ["-a", str(mac["app"] / "Contents/PlugIns/OpenSwapWidgetExtension.appex")],
    ]


def test_an_app_with_another_bundle_id_is_not_the_old_host(mac):
    """The cask can install into ~/Applications too: never trash that."""
    _old_host(mac["home"], bundle_id="com.opensoft.openswap")
    assert _migrate(mac) == []
    assert mac["seen"]["trash"] == []
    assert mac["seen"]["pluginkit"] == []


def test_the_bundle_itself_is_never_retired(mac, frozen_app, monkeypatch):
    plist = frozen_app / "Contents" / "Info.plist"
    plist.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.opensoft.openswap.widget"}))
    monkeypatch.setattr(wi, "host_app_path", lambda home=None: frozen_app)
    assert _migrate(mac) == []
    assert mac["seen"]["trash"] == []


def test_a_trash_failure_is_reported_and_the_rest_still_runs(mac, monkeypatch):
    _menubar_plist(mac["home"], UV_SCRIPT)
    _old_host(mac["home"])
    shim = mac["home"] / ".local" / "bin" / "openswap"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\n")

    def refuse(path):
        raise ClaudeSwitchError(f"Could not move {path} to the Trash: Operation not permitted")

    monkeypatch.setattr(wi, "_move_to_trash", refuse)
    notes: list[str] = []
    assert _migrate(mac, notes) == ["menubar agent"]
    assert any("widget host" in n and "Operation not permitted" in n for n in notes)
    assert notes[-1] == bundle.UV_INSTALL_NOTICE


# --- 3. leftover uv tool -----------------------------------------------------


def test_leftover_uv_tool_is_reported_once_and_left_alone(mac):
    shim = mac["home"] / ".local" / "bin" / "openswap"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\n")
    checkout = mac["home"] / ".openswap"
    checkout.mkdir()
    notes: list[str] = []
    assert _migrate(mac, notes) == []
    assert notes == ["An older uv install is still on PATH: run uv tool uninstall openswap"]
    assert shim.exists() and checkout.is_dir()


def test_a_link_to_the_bundle_is_not_a_uv_install(mac):
    shim = mac["home"] / ".local" / "bin" / "openswap"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(mac["exe"])
    notes: list[str] = []
    _migrate(mac, notes)
    assert notes == []


# --- the whole migration -----------------------------------------------------


def test_second_run_is_a_no_op(mac):
    home = mac["home"]
    _menubar_plist(home, UV_SCRIPT)
    _old_host(home)
    first = _migrate(mac)
    assert first == ["menubar agent", "widget host"]
    calls = {key: len(value) for key, value in mac["seen"].items()}

    assert _migrate(mac) == []
    assert {key: len(value) for key, value in mac["seen"].items()} == calls


def test_not_frozen_does_nothing(tmp_path, monkeypatch):
    monkeypatch.delattr(bundle.sys, "frozen", raising=False)
    _menubar_plist(tmp_path, UV_SCRIPT)
    monkeypatch.setattr(launch_agent, "install", lambda **k: pytest.fail("must not install"))
    assert bundle.migrate_from_uv_install(home=tmp_path) == []


def test_unexpected_errors_are_logged_not_raised(mac, monkeypatch, caplog):
    _menubar_plist(mac["home"], UV_SCRIPT)

    def boom(**kwargs):
        raise RuntimeError("launchd went away")

    monkeypatch.setattr(launch_agent, "install", boom)
    with caplog.at_level("WARNING", logger="openswap"):
        assert _migrate(mac) == []
    assert "launchd went away" in caplog.text
