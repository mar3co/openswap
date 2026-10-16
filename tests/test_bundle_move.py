"""Move to Applications (plan 021): plan, copy, relaunch. No real ditto."""

from __future__ import annotations

import plistlib
import shutil
from pathlib import Path

import pytest

from openswap import bundle
from openswap.exceptions import ClaudeSwitchError


def _app(folder: Path, version: str | None = "0.2.0", name: str = "OpenSwap.app") -> Path:
    app = folder / name
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "OpenSwap").write_text("#!/bin/sh\n")
    info = {"CFBundleIdentifier": "com.opensoft.openswap"}
    if version is not None:
        info["CFBundleShortVersionString"] = version
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info))
    return app


@pytest.fixture
def fs(tmp_path, monkeypatch):
    """A disk image copy, an Applications folder, no Homebrew, fake tools."""
    image = tmp_path / "Volumes" / "OpenSwap"
    image.mkdir(parents=True)
    apps = tmp_path / "Applications"
    trash = tmp_path / "Trash"
    trash.mkdir()
    calls = {"quarantine": [], "trash": []}

    def ditto(source, destination):
        shutil.copytree(source, destination, symlinks=True)

    def move_to_trash(path):
        calls["trash"].append(path)
        path.rename(trash / path.name)

    monkeypatch.setattr(bundle, "_ditto", ditto)
    monkeypatch.setattr(bundle, "_drop_quarantine", lambda app: calls["quarantine"].append(app))
    monkeypatch.setattr("openswap.widget_install._move_to_trash", move_to_trash)
    monkeypatch.setattr(bundle, "CASKROOMS", (tmp_path / "no-caskroom",))
    monkeypatch.setattr(bundle, "_disk_image_mounts", lambda: set())
    return {"tmp": tmp_path, "image": image, "apps": apps, "trash": trash, "calls": calls}


# ---- where it runs from -------------------------------------------------


def test_a_mounted_disk_image_is_ejected_afterwards(monkeypatch):
    monkeypatch.setattr(bundle, "_disk_image_mounts", lambda: {Path("/Volumes/OpenSwap")})
    source = bundle.move_source(Path("/Volumes/OpenSwap/OpenSwap.app"))
    assert source == {"source": "disk_image", "eject": Path("/Volumes/OpenSwap")}


def test_a_real_disk_is_never_ejected(monkeypatch):
    monkeypatch.setattr(bundle, "_disk_image_mounts", lambda: set())
    source = bundle.move_source(Path("/Volumes/External/Apps/OpenSwap.app"))
    assert source == {"source": "volume", "eject": None}


def test_a_translocated_copy_is_traced_to_its_disk_image(monkeypatch):
    monkeypatch.setattr(bundle, "_disk_image_mounts", lambda: {Path("/Volumes/OpenSwap")})
    monkeypatch.setattr(
        bundle, "_translocated_original", lambda app: Path("/Volumes/OpenSwap/OpenSwap.app"),
    )
    app = Path("/private/var/folders/x/AppTranslocation/ABC/d/OpenSwap.app")
    assert bundle.move_source(app)["eject"] == Path("/Volumes/OpenSwap")


def test_a_translocated_download_ejects_nothing(monkeypatch):
    monkeypatch.setattr(bundle, "_disk_image_mounts", lambda: set())
    monkeypatch.setattr(
        bundle, "_translocated_original", lambda app: Path("/Users/me/Downloads/OpenSwap.app"),
    )
    app = Path("/private/var/folders/x/AppTranslocation/ABC/d/OpenSwap.app")
    assert bundle.move_source(app) == {"source": "download", "eject": None}


def test_an_unknown_translocated_original_is_a_download(monkeypatch):
    monkeypatch.setattr(bundle, "_translocated_original", lambda app: None)
    app = Path("/private/var/folders/x/AppTranslocation/ABC/d/OpenSwap.app")
    assert bundle.move_source(app)["source"] == "download"


# ---- the plan -----------------------------------------------------------


def test_nothing_in_applications_is_a_move(fs):
    app = _app(fs["image"])
    plan = bundle.move_plan(app, fs["apps"])
    assert plan["action"] == "move"
    assert plan["target"] == str(fs["apps"] / "OpenSwap.app")
    assert plan["destination"] == str(fs["apps"])


def test_an_older_copy_is_replaced(fs):
    _app(fs["apps"], "0.1.9")
    plan = bundle.move_plan(_app(fs["image"], "0.2.0"), fs["apps"])
    assert plan["action"] == "replace"
    assert plan["existing_version"] == "0.1.9"


def test_versions_compare_as_numbers(fs):
    _app(fs["apps"], "0.10.0")
    assert bundle.move_plan(_app(fs["image"], "0.9.0"), fs["apps"])["action"] == "open_existing"


@pytest.mark.parametrize("existing", ["0.2.0", "0.3.0"])
def test_the_same_or_a_newer_copy_is_opened(fs, existing):
    _app(fs["apps"], existing)
    plan = bundle.move_plan(_app(fs["image"], "0.2.0"), fs["apps"])
    assert plan["action"] == "open_existing"
    assert plan["homebrew"] is False


def test_an_unreadable_existing_copy_is_replaced(fs):
    _app(fs["apps"], None)
    assert bundle.move_plan(_app(fs["image"]), fs["apps"])["action"] == "replace"


def test_homebrews_copy_is_never_replaced_even_when_older(fs, monkeypatch):
    room = fs["tmp"] / "Caskroom" / "openswap"
    room.mkdir(parents=True)
    monkeypatch.setattr(bundle, "CASKROOMS", (room,))
    home = fs["tmp"] / "home"
    apps = home / "Applications"
    apps.mkdir(parents=True)
    _app(apps, "0.1.0")
    plan = bundle.move_plan(_app(fs["image"], "0.2.0"), apps, home=home)
    assert plan["action"] == "open_existing"
    assert plan["homebrew"] is True


def test_applications_falls_back_to_the_home_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(bundle.os, "access", lambda path, mode: False)
    assert bundle.applications_dir(home=tmp_path) == tmp_path / "Applications"


# ---- the copy -----------------------------------------------------------


def test_a_move_copies_lifts_quarantine_and_leaves_no_staging(fs):
    app = _app(fs["image"])
    plan = bundle.move_plan(app, fs["apps"])
    target = bundle.copy_to_applications(app, plan)
    assert target == fs["apps"] / "OpenSwap.app"
    assert bundle.bundle_version(target) == "0.2.0"
    assert app.exists()  # the source is left for the eject or the user
    staging = fs["apps"] / ".OpenSwap.app.openswap-move"
    assert fs["calls"]["quarantine"] == [staging]
    assert not staging.exists()


def test_a_replace_trashes_the_old_copy_only_after_the_new_one_is_complete(fs):
    _app(fs["apps"], "0.1.0")
    app = _app(fs["image"], "0.2.0")
    plan = bundle.move_plan(app, fs["apps"])
    target = bundle.copy_to_applications(app, plan)
    assert bundle.bundle_version(target) == "0.2.0"
    assert fs["calls"]["trash"] == [target]
    assert bundle.bundle_version(fs["trash"] / "OpenSwap.app") == "0.1.0"


def test_a_failed_copy_leaves_the_old_app_and_no_half_copy(fs, monkeypatch):
    import subprocess

    _app(fs["apps"], "0.1.0")
    app = _app(fs["image"], "0.2.0")
    plan = bundle.move_plan(app, fs["apps"])

    def broken(source, destination):
        destination.mkdir(parents=True)
        raise subprocess.CalledProcessError(1, ["ditto"])

    monkeypatch.setattr(bundle, "_ditto", broken)
    with pytest.raises(ClaudeSwitchError, match="Could not copy OpenSwap"):
        bundle.copy_to_applications(app, plan)
    assert bundle.bundle_version(fs["apps"] / "OpenSwap.app") == "0.1.0"
    assert not (fs["apps"] / ".OpenSwap.app.openswap-move").exists()
    assert fs["calls"]["trash"] == []


def test_a_failed_trash_keeps_the_old_app(fs, monkeypatch):
    _app(fs["apps"], "0.1.0")
    app = _app(fs["image"], "0.2.0")
    plan = bundle.move_plan(app, fs["apps"])

    def refuse(path):
        raise ClaudeSwitchError(f"Could not move {path} to the Trash: denied")

    monkeypatch.setattr("openswap.widget_install._move_to_trash", refuse)
    with pytest.raises(ClaudeSwitchError, match="denied"):
        bundle.copy_to_applications(app, plan)
    assert bundle.bundle_version(fs["apps"] / "OpenSwap.app") == "0.1.0"
    assert not (fs["apps"] / ".OpenSwap.app.openswap-move").exists()


def test_a_copy_that_appeared_meanwhile_is_not_overwritten(fs):
    app = _app(fs["image"])
    plan = bundle.move_plan(app, fs["apps"])
    _app(fs["apps"], "0.3.0")  # appeared after the plan
    with pytest.raises(ClaudeSwitchError, match="already there"):
        bundle.copy_to_applications(app, plan)
    assert bundle.bundle_version(fs["apps"] / "OpenSwap.app") == "0.3.0"


def test_open_existing_is_not_a_copy(fs):
    with pytest.raises(ClaudeSwitchError):
        bundle.copy_to_applications(_app(fs["image"]), {"action": "open_existing", "target": "x"})


# ---- relaunch -----------------------------------------------------------


def test_relaunch_waits_for_this_process_in_a_detached_shell(monkeypatch):
    started = {}

    def popen(argv, **kwargs):
        started["argv"], started["kwargs"] = argv, kwargs

    monkeypatch.setattr(bundle.subprocess, "Popen", popen)
    app, image = Path("/Applications/OpenSwap.app"), Path("/Volumes/OpenSwap")
    bundle.relaunch_after_exit(app, 4242, image)
    argv = started["argv"]
    assert argv[:3] == ["/bin/sh", "-c", bundle.RELAUNCH_SCRIPT]
    assert argv[4:] == ["4242", str(app), str(image)]
    assert started["kwargs"]["start_new_session"] is True
    assert 'kill -0 "$1"' in bundle.RELAUNCH_SCRIPT
    assert bundle.RELAUNCH_SCRIPT.index("/usr/bin/open") < bundle.RELAUNCH_SCRIPT.index("hdiutil detach")


def test_relaunch_without_a_disk_image_ejects_nothing(monkeypatch):
    started = {}
    monkeypatch.setattr(bundle.subprocess, "Popen", lambda argv, **kw: started.update(argv=argv))
    bundle.relaunch_after_exit(Path("/Applications/OpenSwap.app"), 1)
    assert started["argv"][-1] == ""
