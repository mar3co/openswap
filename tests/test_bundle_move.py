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
    [staging] = fs["calls"]["quarantine"]
    assert staging.parent.parent == fs["apps"] and staging.name == "OpenSwap.app"
    assert staging.parent.name.startswith(".OpenSwap.app.openswap-move-")
    assert _leftovers(fs) == []


def _leftovers(fs):
    return [p.name for p in fs["apps"].glob(".OpenSwap.app.openswap-move*")]


def test_each_attempt_stages_in_its_own_folder_and_touches_no_collision(fs):
    """Two people moving at once never share a staging path; a stray folder
    under the hidden name is nobody's to delete."""
    stray = fs["apps"] / ".OpenSwap.app.openswap-move-stray"
    stray.mkdir(parents=True)
    (stray / "keep").write_text("x")
    app = _app(fs["image"])
    bundle.copy_to_applications(app, bundle.move_plan(app, fs["apps"]))
    assert (stray / "keep").read_text() == "x"
    assert _leftovers(fs) == [stray.name]


@pytest.mark.parametrize("older, newer", [
    ("1.0rc1", "1.0"), ("1.0a1", "1.0b1"), ("1.0b2", "1.0rc1"), ("1.0", "1.0.1"),
    ("0.9.9", "0.10.0"), ("1.0-beta.1", "1.0"), ("v1.0", "1.1"),
])
def test_versions_order_with_pre_releases_below_their_release(older, newer):
    assert bundle._version_key(older) < bundle._version_key(newer)
    assert bundle._version_key(newer) == bundle._version_key(newer)


def test_a_replace_trashes_the_old_copy_only_after_the_new_one_is_complete(fs):
    _app(fs["apps"], "0.1.0")
    app = _app(fs["image"], "0.2.0")
    plan = bundle.move_plan(app, fs["apps"])
    target = bundle.copy_to_applications(app, plan)
    assert bundle.bundle_version(target) == "0.2.0"
    old = fs["apps"] / "OpenSwap (old).app"
    assert fs["calls"]["trash"] == [old]
    assert bundle.bundle_version(fs["trash"] / "OpenSwap (old).app") == "0.1.0"
    assert not old.exists()


def test_an_existing_old_copy_is_kept_not_deleted(fs):
    """A leftover from an earlier update (or the user's own app by that
    name) stays; the set-aside copy takes the next free name."""
    _app(fs["apps"], "0.1.0")
    _app(fs["apps"], "0.0.9", name="OpenSwap (old).app")
    app = _app(fs["image"], "0.2.0")
    target = bundle.copy_to_applications(app, bundle.move_plan(app, fs["apps"]))
    assert bundle.bundle_version(target) == "0.2.0"
    assert bundle.bundle_version(fs["apps"] / "OpenSwap (old).app") == "0.0.9"
    assert fs["calls"]["trash"] == [fs["apps"] / "OpenSwap (old) 2.app"]
    assert bundle.bundle_version(fs["trash"] / "OpenSwap (old) 2.app") == "0.1.0"


def test_a_failed_final_rename_puts_the_old_app_back(fs, monkeypatch):
    _app(fs["apps"], "0.1.0")
    app = _app(fs["image"], "0.2.0")
    plan = bundle.move_plan(app, fs["apps"])
    real_rename = Path.rename

    def rename(self, target):
        if self.parent.name.startswith(".OpenSwap.app.openswap-move-"):
            raise OSError("rename refused")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(ClaudeSwitchError, match="rename refused"):
        bundle.copy_to_applications(app, plan)
    assert bundle.bundle_version(fs["apps"] / "OpenSwap.app") == "0.1.0"
    assert not (fs["apps"] / "OpenSwap (old).app").exists()
    assert _leftovers(fs) == []
    assert fs["calls"]["trash"] == []


def test_a_copy_that_became_newer_meanwhile_is_not_replaced(fs):
    _app(fs["apps"], "0.1.0")
    app = _app(fs["image"], "0.2.0")
    plan = bundle.move_plan(app, fs["apps"])  # replace, at the time
    shutil.rmtree(fs["apps"] / "OpenSwap.app")
    _app(fs["apps"], "0.3.0")  # installed while the window was open
    with pytest.raises(ClaudeSwitchError, match="0.3.0 is already in"):
        bundle.copy_to_applications(app, plan)
    assert bundle.bundle_version(fs["apps"] / "OpenSwap.app") == "0.3.0"
    assert fs["calls"]["trash"] == []


def test_quarantine_that_will_not_clear_fails_the_move(fs, monkeypatch):
    import subprocess

    monkeypatch.undo()  # the fixture's fake; use the real _drop_quarantine
    monkeypatch.setattr(bundle, "_ditto", lambda s, d: shutil.copytree(s, d, symlinks=True))
    monkeypatch.setattr(bundle, "CASKROOMS", (fs["tmp"] / "no-caskroom",))

    def xattr(argv, **kwargs):  # -d "fails", -p still finds it
        return subprocess.CompletedProcess(argv, 0 if argv[1] == "-p" else 1, b"", b"Operation not permitted")

    monkeypatch.setattr(bundle.subprocess, "run", xattr)
    app = _app(fs["image"])
    plan = bundle.move_plan(app, fs["apps"])
    with pytest.raises(ClaudeSwitchError, match="quarantine"):
        bundle.copy_to_applications(app, plan)
    assert not (fs["apps"] / "OpenSwap.app").exists()
    assert _leftovers(fs) == []


def test_quarantine_already_absent_is_fine(fs, monkeypatch):
    import subprocess

    monkeypatch.undo()  # the fixture's fake; use the real _drop_quarantine
    monkeypatch.setattr(bundle, "_ditto", lambda s, d: shutil.copytree(s, d, symlinks=True))
    monkeypatch.setattr(bundle, "CASKROOMS", (fs["tmp"] / "no-caskroom",))
    calls = []

    def xattr(argv, **kwargs):  # -d exits 1 for "no such xattr"; -p agrees
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, 1, b"", b"No such xattr")

    monkeypatch.setattr(bundle.subprocess, "run", xattr)
    app = _app(fs["image"])
    assert bundle.copy_to_applications(app, bundle.move_plan(app, fs["apps"])).exists()
    assert calls == ["-dr", "-p"]


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
    assert _leftovers(fs) == []
    assert fs["calls"]["trash"] == []


def test_a_failed_trash_leaves_the_old_app_beside_the_new_one(fs, monkeypatch):
    """The new app is installed either way; the old one stays visible."""
    _app(fs["apps"], "0.1.0")
    app = _app(fs["image"], "0.2.0")
    plan = bundle.move_plan(app, fs["apps"])

    def refuse(path):
        raise ClaudeSwitchError(f"Could not move {path} to the Trash: denied")

    monkeypatch.setattr("openswap.widget_install._move_to_trash", refuse)
    target = bundle.copy_to_applications(app, plan)
    assert bundle.bundle_version(target) == "0.2.0"
    assert bundle.bundle_version(fs["apps"] / "OpenSwap (old).app") == "0.1.0"
    assert _leftovers(fs) == []


def test_a_copy_that_appeared_meanwhile_is_not_overwritten(fs):
    app = _app(fs["image"])
    plan = bundle.move_plan(app, fs["apps"])
    _app(fs["apps"], "0.3.0")  # appeared after the plan
    with pytest.raises(ClaudeSwitchError, match="0.3.0 is already in"):
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


def test_eject_detaches_a_disk_image_and_ignores_none(monkeypatch):
    calls = []
    monkeypatch.setattr(bundle.subprocess, "run", lambda argv, **k: calls.append(argv))
    volume = Path("/Volumes/OpenSwap")
    bundle.eject(None)
    bundle.eject(volume)
    assert calls == [["/usr/bin/hdiutil", "detach", str(volume), "-quiet"]]


def test_relaunch_without_a_disk_image_ejects_nothing(monkeypatch):
    started = {}
    monkeypatch.setattr(bundle.subprocess, "Popen", lambda argv, **kw: started.update(argv=argv))
    bundle.relaunch_after_exit(Path("/Applications/OpenSwap.app"), 1)
    assert started["argv"][-1] == ""
