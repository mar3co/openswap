"""Moving a Mac from the uv install to the cask's OpenSwap.app (plan 008 Step 5).

Runs against a temp home with ``frozen_app`` (conftest) standing in for the
bundle. launchctl, pluginkit, and the Trash are fakes that record what they
were asked to do; nothing here touches a real LaunchAgent, widget, or file
outside ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from openswap import bundle, launch_agent, statusline
from openswap import widget_install as wi
from openswap.worker import launch_agent as worker_agent
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
    # A real uv install on this Mac must not show up as a leftover shim.
    for name in ("UV_TOOL_BIN_DIR", "XDG_BIN_HOME", "UV_INSTALL_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))

    seen: dict[str, list] = {"launchctl": [], "pluginkit": [], "trash": [], "reload": []}

    def fake_install(*args, **kwargs):
        # The migration rewrites plists; bootstrapping the menu bar agent is
        # `openswap setup`'s job.
        raise AssertionError("the migration must not install the menu bar agent")

    worker = {"loaded": False}

    def fake_worker_launchctl(*args):
        seen["launchctl"].append(list(args))
        if args[0] == "print":
            return subprocess.CompletedProcess(["launchctl", *args], 0 if worker["loaded"] else 1, "", "")
        if args[0] == "bootout":
            worker["loaded"] = False
        elif args[0] == "bootstrap":
            worker["loaded"] = worker.get("bootstrap_rc", 0) == 0
            return subprocess.CompletedProcess(["launchctl", *args], worker.get("bootstrap_rc", 0), "", "")
        return subprocess.CompletedProcess(["launchctl", *args], 0, "", "")

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
    monkeypatch.setattr(worker_agent, "_launchctl", fake_worker_launchctl)
    return {
        "home": home,
        "app": frozen_app,
        "exe": frozen_app / "Contents/MacOS/OpenSwap",
        "seen": seen,
        "worker": worker,
    }


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


def _changes(mac) -> list[list[str]]:
    """launchctl calls that change something (``print`` only reads)."""
    return [call for call in mac["seen"]["launchctl"] if call[0] != "print"]


def _bootstraps(mac) -> list[list[str]]:
    return [call for call in mac["seen"]["launchctl"] if call[0] == "bootstrap"]


def _app_copy(folder: Path) -> Path:
    exe = folder / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    return exe


def _migrate(mac, notes: list[str] | None = None) -> list[str]:
    return bundle.migrate_from_uv_install(
        home=mac["home"], uid=UID, notify=None if notes is None else notes.append
    )


# --- 1. menu bar login item --------------------------------------------------


def test_uv_login_item_is_repointed_at_the_bundle(mac):
    _menubar_plist(mac["home"], UV_SCRIPT)
    assert _migrate(mac) == ["menubar plist"]
    assert _program(launch_agent.plist_path(home=mac["home"])) == str(mac["exe"])


def test_opened_from_finder_stops_the_uv_extra_without_starting_another(mac, monkeypatch):
    """This process is already the extra; bootstrapping would run a second one."""
    _menubar_plist(mac["home"], UV_SCRIPT)
    loaded = {"checks_after_bootout": None}

    def is_loaded(label=launch_agent.LABEL, uid=None):
        if label != launch_agent.LABEL:
            return False
        if ["bootout", f"gui/{UID}/{launch_agent.LABEL}"] not in mac["seen"]["launchctl"]:
            return True
        # launchd lists the job for two more looks while it tears it down.
        n = loaded["checks_after_bootout"] = (loaded["checks_after_bootout"] or 0) + 1
        return n <= 2

    monkeypatch.setattr(launch_agent, "is_loaded", is_loaded)
    monkeypatch.setattr(launch_agent.time, "sleep", lambda _s: None)
    assert _migrate(mac) == ["menubar plist"]
    assert ["bootout", f"gui/{UID}/{launch_agent.LABEL}"] in mac["seen"]["launchctl"]
    assert _bootstraps(mac) == []
    assert _program(launch_agent.plist_path(home=mac["home"])) == str(mac["exe"])
    # It waited for the teardown: a hand-over to the login item can follow
    # at once (menubar_instance), and must not race a half-unloaded job.
    assert loaded["checks_after_bootout"] == 3


def test_a_uv_extra_that_will_not_stop_keeps_its_plist(mac, monkeypatch):
    """Rewritten while the uv extra is still running, the plist would let the
    start-up hand-over take that extra's pid for the app's and quit; once
    the bootout landed nothing would be running."""
    plist = _menubar_plist(mac["home"], UV_SCRIPT)
    monkeypatch.setattr(launch_agent, "is_loaded", lambda label=launch_agent.LABEL, uid=None: label == launch_agent.LABEL)
    clock = [0.0]
    monkeypatch.setattr(launch_agent.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(launch_agent.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    notes: list[str] = []
    assert "menubar plist" not in _migrate(mac, notes)
    assert ["bootout", f"gui/{UID}/{launch_agent.LABEL}"] in mac["seen"]["launchctl"]
    assert _program(plist) == UV_SCRIPT
    assert any("menu bar login item" in note and "still stopping" in note for note in notes)
    assert launch_agent.login_item_runs_this(home=mac["home"]) is False


def test_login_item_already_on_the_bundle_is_left_alone(mac):
    plist = _menubar_plist(mac["home"], str(mac["exe"]))
    before = plist.read_bytes()
    assert _migrate(mac) == []
    assert _changes(mac) == []
    assert plist.read_bytes() == before


def test_no_login_item_is_not_created(mac):
    """`openswap setup` installs the login item; the migration only moves one."""
    assert _migrate(mac) == []
    assert _changes(mac) == []
    assert not launch_agent.plist_path(home=mac["home"]).exists()


def test_inside_the_agent_only_the_plist_is_rewritten(mac, monkeypatch):
    """Re-bootstrapping from inside the agent would boot it out mid-start."""
    plist = _menubar_plist(mac["home"], UV_SCRIPT)
    monkeypatch.setenv("XPC_SERVICE_NAME", launch_agent.LABEL)
    assert _migrate(mac) == ["menubar plist"]
    assert _changes(mac) == []
    assert _program(plist) == str(mac["exe"])


def test_unreadable_login_item_is_rewritten(mac):
    plist = launch_agent.plist_path(home=mac["home"])
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b"not a plist")
    assert _migrate(mac) == ["menubar plist"]
    assert _program(plist) == str(mac["exe"])


def test_python_dash_m_login_item_is_repointed(mac):
    plist = launch_agent.plist_path(home=mac["home"])
    plist.parent.mkdir(parents=True)
    plist.write_bytes(
        launch_agent.build_plist(["/Users/x/.venv/bin/python3", "-m", "openswap"], home=mac["home"])
    )
    assert _migrate(mac) == ["menubar plist"]
    assert _program(plist) == str(mac["exe"])


def test_a_login_item_on_another_copy_of_the_app_is_left_alone(mac, tmp_path, monkeypatch):
    """Opening a copy from Downloads (or a dev build) must not take over the cask's agent."""
    cask_exe = _app_copy(tmp_path / "Applications")
    plist = _menubar_plist(mac["home"], str(cask_exe))
    before = plist.read_bytes()
    monkeypatch.setattr(
        launch_agent, "is_loaded", lambda label=launch_agent.LABEL, uid=None: label == launch_agent.LABEL
    )
    assert _migrate(mac) == []
    assert plist.read_bytes() == before
    assert ["bootout", f"gui/{UID}/{launch_agent.LABEL}"] not in mac["seen"]["launchctl"]


def test_a_login_item_on_a_deleted_copy_of_the_app_is_repointed(mac, tmp_path):
    gone = tmp_path / "Downloads" / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
    plist = _menubar_plist(mac["home"], str(gone))
    assert _migrate(mac) == ["menubar plist"]
    assert _program(plist) == str(mac["exe"])


@pytest.mark.parametrize(
    "where",
    [
        "/private/var/folders/xy/T/AppTranslocation/0A1B2C3D/d/OpenSwap.app",
        "/Volumes/OpenSwap 0.2.0/OpenSwap.app",
    ],
    ids=["translocated", "disk-image"],
)
def test_a_temporary_copy_migrates_nothing(mac, monkeypatch, where):
    """Those paths vanish: pinning the login item to one would break it."""
    plist = _menubar_plist(mac["home"], UV_SCRIPT)
    _old_host(mac["home"])
    legacy = launch_agent.plist_path("com.cswap.menubar", mac["home"])
    legacy.write_bytes(b"")
    monkeypatch.setattr(bundle, "app_path", lambda: Path(where))
    assert bundle.runs_from_temporary_location() is True
    assert _migrate(mac) == []
    assert _program(plist) == UV_SCRIPT
    assert legacy.exists()
    assert mac["seen"]["launchctl"] == []
    assert mac["seen"]["pluginkit"] == [] and mac["seen"]["trash"] == []


def test_an_installed_app_is_not_a_temporary_copy(frozen_app):
    assert bundle.runs_from_temporary_location() is False
    assert bundle.runs_from_temporary_location(Path("/Applications/OpenSwap.app")) is False


@pytest.mark.parametrize("in_agent", [False, True], ids=["finder", "agent"])
def test_the_older_label_is_retired_whenever_migrating(mac, monkeypatch, in_agent):
    """Even with no current login item, and even from inside the agent."""
    legacy = launch_agent.plist_path("com.cswap.menubar", mac["home"])
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"")
    if in_agent:
        monkeypatch.setenv("XPC_SERVICE_NAME", launch_agent.LABEL)
    assert _migrate(mac) == ["legacy menubar"]
    assert not legacy.exists()
    assert _migrate(mac) == []


def test_a_loaded_older_label_is_booted_out(mac, monkeypatch):
    monkeypatch.setattr(launch_agent, "is_loaded", lambda label=launch_agent.LABEL, uid=None: label == "com.cswap.menubar")
    assert _migrate(mac) == ["legacy menubar"]
    assert ["bootout", f"gui/{UID}/com.cswap.menubar"] in mac["seen"]["launchctl"]


# --- Remote tasks worker -----------------------------------------------------


def _worker_plist(home: Path, program: list[str]) -> Path:
    plist = worker_agent.plist_path(home)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(worker_agent.build_plist(program, home))
    return plist


def test_uv_worker_is_repointed_keeping_its_arguments_and_keys(mac):
    plist = _worker_plist(mac["home"], ["/Users/x/.venv/bin/python3", "-m", "openswap"])
    assert _migrate(mac) == ["worker plist"]
    data = plistlib.loads(plist.read_bytes())
    assert data["ProgramArguments"] == [str(mac["exe"]), "worker", "run", "--managed"]
    assert data["Label"] == worker_agent.LABEL
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    # launchd's PATH is ':'-joined on macOS; compare the prefix so a Windows
    # tmp path's drive colon does not split it.
    assert data["EnvironmentVariables"]["PATH"].startswith(str(mac["exe"].parent) + ":")
    assert _bootstraps(mac) == []  # not loaded: the next start runs the app
    assert _migrate(mac) == []


def test_a_running_uv_worker_is_restarted_on_the_app(mac):
    plist = _worker_plist(mac["home"], [UV_SCRIPT])
    mac["worker"]["loaded"] = True
    assert _migrate(mac) == ["worker agent"]
    target = f"gui/{UID}/{worker_agent.LABEL}"
    assert ["bootout", target] in mac["seen"]["launchctl"]
    assert _bootstraps(mac) == [["bootstrap", f"gui/{UID}", str(plist)]]
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == str(mac["exe"])


def test_inside_the_worker_only_its_plist_is_rewritten(mac, monkeypatch):
    plist = _worker_plist(mac["home"], [UV_SCRIPT])
    mac["worker"]["loaded"] = True
    monkeypatch.setenv("XPC_SERVICE_NAME", worker_agent.LABEL)
    assert _migrate(mac) == ["worker plist"]
    assert _changes(mac) == []
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"][0] == str(mac["exe"])


@pytest.mark.parametrize("on", ["bundle", "other-copy"])
def test_a_worker_on_the_app_is_left_alone(mac, tmp_path, on):
    program = str(mac["exe"]) if on == "bundle" else str(_app_copy(tmp_path / "Applications"))
    plist = _worker_plist(mac["home"], [program])
    before = plist.read_bytes()
    mac["worker"]["loaded"] = True
    assert _migrate(mac) == []
    assert plist.read_bytes() == before
    assert _changes(mac) == []


def test_a_worker_that_will_not_restart_is_reported(mac):
    _worker_plist(mac["home"], [UV_SCRIPT])
    mac["worker"]["loaded"] = True
    mac["worker"]["bootstrap_rc"] = 5
    notes: list[str] = []
    assert _migrate(mac, notes) == []
    assert any("Remote tasks worker" in n and "openswap worker enable" in n for n in notes)


# --- Claude Code status line -------------------------------------------------


def _hook(home: Path, command: str) -> Path:
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"statusLine": {"type": "command", "command": command}, "model": "x"}))
    return settings


def _hook_command(settings: Path) -> str:
    return json.loads(settings.read_text())["statusLine"]["command"]


def test_a_uv_status_line_hook_is_repointed(mac):
    settings = _hook(mac["home"], f"{UV_SCRIPT} statusline")
    assert _migrate(mac) == ["status line"]
    assert _hook_command(settings) == shlex.join([str(mac["exe"]), "statusline"])
    assert json.loads(settings.read_text())["model"] == "x"
    # The app's hook is still recognised as OpenSwap's, so --install does not wrap it.
    assert statusline.is_our_command(_hook_command(settings))
    assert _migrate(mac) == []


def test_a_status_line_env_prefix_is_kept(mac):
    settings = _hook(mac["home"], f"NO_COLOR=1 {UV_SCRIPT} statusline")
    _migrate(mac)
    assert _hook_command(settings) == shlex.join(["NO_COLOR=1", str(mac["exe"]), "statusline"])


@pytest.mark.parametrize("command", ["openswap statusline", "ccstatusline --compact"])
def test_bare_or_foreign_status_lines_are_left_alone(mac, command):
    settings = _hook(mac["home"], command)
    before = settings.read_text()
    assert _migrate(mac) == []
    assert settings.read_text() == before


def test_a_status_line_through_a_link_to_the_app_is_left_alone(mac):
    link = mac["home"] / "bin" / "openswap"
    link.parent.mkdir()
    link.symlink_to(mac["exe"])
    settings = _hook(mac["home"], f"{link} statusline")
    before = settings.read_text()
    assert _migrate(mac) == []
    assert settings.read_text() == before


def test_uv_notice_is_held_back_while_something_still_runs_uv(mac, monkeypatch):
    """`uv tool uninstall` would break whatever still runs the uv binary."""
    _worker_plist(mac["home"], [UV_SCRIPT])
    _hook(mac["home"], f"{UV_SCRIPT} statusline")
    shim = mac["home"] / ".local" / "bin" / "openswap"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\n")

    def refuse(*args, **kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(bundle, "_repoint_worker_agent", refuse)
    monkeypatch.setattr(statusline, "repoint_command", refuse)
    notes: list[str] = []
    _migrate(mac, notes)
    held = notes[-1]
    assert held != bundle.UV_INSTALL_NOTICE
    assert "Remote tasks worker" in held and "Claude Code status line" in held
    assert "Keep it" in held


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
    # The bundled appex is registered before the old one goes.
    assert mac["seen"]["pluginkit"] == [
        ["-a", str(mac["app"] / "Contents/PlugIns/OpenSwapWidgetExtension.appex")],
        ["-r", str(old / "Contents/PlugIns/OpenSwapWidgetExtension.appex")],
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
    assert _migrate(mac, notes) == ["menubar plist"]
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


@pytest.mark.parametrize("where", ["XDG_BIN_HOME", "UV_TOOL_BIN_DIR", "UV_INSTALL_DIR", "XDG_DATA_HOME", "PATH"])
def test_uv_tool_outside_local_bin_is_reported(mac, monkeypatch, where):
    bin_dir = mac["home"] / "tools" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "openswap").write_text("#!/bin/sh\n")
    if where == "XDG_DATA_HOME":
        monkeypatch.setenv(where, str(bin_dir.parent / "share"))
    elif where == "PATH":
        monkeypatch.setenv(where, os.pathsep.join(["/usr/bin", str(bin_dir)]))
    else:
        monkeypatch.setenv(where, str(bin_dir))
    notes: list[str] = []
    _migrate(mac, notes)
    assert notes == ["An older uv install is still on PATH: run uv tool uninstall openswap"]


def test_one_notice_for_a_shim_found_twice(mac, monkeypatch):
    local_bin = mac["home"] / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (local_bin / "openswap").write_text("#!/bin/sh\n")
    monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), "/usr/bin"]))
    notes: list[str] = []
    _migrate(mac, notes)
    assert len(notes) == 1


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
    assert first == ["menubar plist", "widget host"]
    changes = len(_changes(mac))
    calls = {key: len(value) for key, value in mac["seen"].items() if key != "launchctl"}

    assert _migrate(mac) == []
    assert len(_changes(mac)) == changes
    assert {key: len(value) for key, value in mac["seen"].items() if key != "launchctl"} == calls


def test_not_frozen_does_nothing(tmp_path, monkeypatch):
    monkeypatch.delattr(bundle.sys, "frozen", raising=False)
    _menubar_plist(tmp_path, UV_SCRIPT)
    monkeypatch.setattr(launch_agent, "install", lambda **k: pytest.fail("must not install"))
    assert bundle.migrate_from_uv_install(home=tmp_path) == []


def test_unexpected_errors_are_logged_not_raised(mac, monkeypatch, caplog):
    _menubar_plist(mac["home"], UV_SCRIPT)

    def boom(*args, **kwargs):
        raise RuntimeError("launchd went away")

    monkeypatch.setattr(launch_agent, "is_loaded", boom)
    with caplog.at_level("WARNING", logger="openswap"):
        assert _migrate(mac) == []
    assert "launchd went away" in caplog.text
