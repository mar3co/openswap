"""Move to Applications repoints a login item that ran another copy (plan 021)."""

from __future__ import annotations

import plistlib
from pathlib import Path

from openswap import launch_agent


def _plist(home: Path, program: list[str]) -> Path:
    path = launch_agent.plist_path(launch_agent.LABEL, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(launch_agent.build_plist(program, launch_agent.LABEL, home))
    return path


def _program(path: Path) -> list[str]:
    return plistlib.loads(path.read_bytes())["ProgramArguments"]


def test_a_login_item_for_another_copy_is_repointed(tmp_path):
    old = "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap"
    new = tmp_path / "Applications" / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
    path = _plist(tmp_path, [old])
    assert launch_agent.repoint_login_item(new, home=tmp_path) is True
    assert _program(path) == [str(new), "menubar"]


def test_a_login_item_already_on_this_copy_is_left_alone(tmp_path):
    new = tmp_path / "Applications" / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
    path = _plist(tmp_path, [str(new)])
    before = path.read_bytes()
    assert launch_agent.repoint_login_item(new, home=tmp_path) is False
    assert path.read_bytes() == before


def test_a_uv_installs_login_item_is_not_taken_over(tmp_path):
    path = _plist(tmp_path, ["/Users/x/.local/bin/openswap"])
    before = path.read_bytes()
    new = tmp_path / "Applications" / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
    assert launch_agent.repoint_login_item(new, home=tmp_path) is False
    assert path.read_bytes() == before


def test_no_login_item_means_nothing_to_repoint(tmp_path):
    new = tmp_path / "Applications" / "OpenSwap.app" / "Contents" / "MacOS" / "OpenSwap"
    assert launch_agent.repoint_login_item(new, home=tmp_path) is False
    assert not launch_agent.plist_path(launch_agent.LABEL, tmp_path).exists()
