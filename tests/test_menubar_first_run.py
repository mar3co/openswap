"""First run, widget and status line from the app (plan 020, phase 2).

No rumps or AppKit: the Settings and welcome models are pure, and controller
methods are lifted out of ``MenuBarApp`` with ``tests.menubar_harness``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openswap import menubar
from openswap.exceptions import ClaudeSwitchError
from tests.menubar_harness import extract_class


def _general(**kwargs) -> list[dict]:
    return menubar.settings_page_rows(
        menubar.MenuBarSettings(),
        strategy="best",
        threshold=90,
        section=menubar.SETTINGS_SECTION_GENERAL,
        **kwargs,
    )


def _this_mac(rows: list[dict]) -> list[dict]:
    ids = [row["id"] for row in rows]
    return rows[ids.index("group_this_mac"):]


# ---- Settings › General › This Mac -----------------------------------------


@pytest.mark.parametrize("installed", [True, False])
def test_status_line_toggle_shows_what_is_on_disk(installed):
    rows = {row["id"]: row for row in _general(status_line=installed)}
    row = rows["claude_status_line"]
    assert row["kind"] == "toggle"
    assert row["label"] == "Claude Code status line"
    assert row["value"] is installed
    assert row["section"] == menubar.SETTINGS_SECTION_GENERAL


def test_this_mac_ends_general_with_the_status_line_then_the_widget_hint():
    rows = _general(status_line=False, bundled_widget=True)
    tail = _this_mac(rows)
    assert rows[-len(tail):] == tail  # the last group of General
    assert [row["id"] for row in tail][-2:] == ["claude_status_line", "widget_hint"]
    hint = tail[-1]
    assert hint["kind"] == "group" and hint["style"] == "hint"
    assert hint["label"] == "Add the widget: Edit Widgets, search OpenSwap"


def test_widget_hint_is_the_apps_only():
    """A uv install builds its widget with Xcode (`openswap widget --install`),
    and a translocated copy registers nothing."""
    ids = {row["id"] for row in _general(bundled_widget=False)}
    assert "widget_hint" not in ids
    assert "claude_status_line" in ids


def test_this_mac_stays_out_of_the_other_sections():
    for section in (menubar.SETTINGS_SECTION_AUTOMATION, menubar.SETTINGS_SECTION_REMOTE):
        rows = menubar.settings_page_rows(
            menubar.MenuBarSettings(),
            strategy="best",
            threshold=90,
            status_line=True,
            bundled_widget=True,
            section=section,
        )
        ids = {row["id"] for row in rows}
        assert not ids & {"group_this_mac", "claude_status_line", "widget_hint"}


def test_bundled_widget_means_the_app_at_a_stable_location(frozen_app, monkeypatch):
    from openswap import bundle

    assert menubar._bundled_widget() is True
    monkeypatch.setattr(bundle, "runs_from_temporary_location", lambda app=None: True)
    assert menubar._bundled_widget() is False


def test_bundled_widget_is_false_in_a_uv_install(monkeypatch):
    import sys

    monkeypatch.delattr(sys, "frozen", raising=False)
    assert menubar._bundled_widget() is False


# ---- the status line toggle's controller ------------------------------------


def _status_line_app(tmp_path: Path, monkeypatch):
    errors: list[str] = []
    scope = {"ClaudeSwitchError": ClaudeSwitchError}
    app = extract_class(
        menubar.__file__, "MenuBarApp", {"_status_line_installed", "_toggle_status_line"}, scope
    )()
    backup = tmp_path / "OpenSwap"
    backup.mkdir()
    app.switcher = SimpleNamespace(backup_dir=backup, _logger=SimpleNamespace(debug=lambda *a, **k: None))
    app._show_error = errors.append
    claude = tmp_path / ".claude"
    claude.mkdir()
    monkeypatch.setattr("openswap.paths.get_claude_config_home", lambda: claude)
    monkeypatch.setattr(
        "openswap.statusline.paint_command",
        lambda: "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline",
    )
    return app, claude, backup, errors


def test_status_line_toggle_wraps_and_restores_through_the_shared_function(tmp_path, monkeypatch):
    app, claude, backup, errors = _status_line_app(tmp_path, monkeypatch)
    calls: list[tuple] = []
    from openswap import statusline

    real = statusline.set_installed
    monkeypatch.setattr(
        statusline, "set_installed", lambda *a: calls.append(a) or real(*a)
    )
    assert app._status_line_installed() is False

    app._toggle_status_line()
    settings = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert settings["statusLine"]["command"] == "/Applications/OpenSwap.app/Contents/MacOS/OpenSwap statusline"
    assert app._status_line_installed() is True

    app._toggle_status_line()
    assert "statusLine" not in json.loads((claude / "settings.json").read_text(encoding="utf-8"))
    assert app._status_line_installed() is False
    assert calls == [(claude, backup, True), (claude, backup, False)]
    assert errors == []


def test_status_line_toggle_flips_what_is_on_disk_now(tmp_path, monkeypatch):
    """`openswap statusline --install` may have run since the page was built."""
    app, claude, backup, _errors = _status_line_app(tmp_path, monkeypatch)
    from openswap import statusline

    statusline.set_installed(claude, backup, True)
    app._toggle_status_line()
    assert app._status_line_installed() is False


def test_status_line_toggle_reports_an_unreadable_settings_file(tmp_path, monkeypatch):
    app, claude, _backup, errors = _status_line_app(tmp_path, monkeypatch)
    (claude / "settings.json").write_text("{not json", encoding="utf-8")
    app._toggle_status_line()
    assert len(errors) == 1
    assert errors[0].startswith("Couldn't change the Claude Code status line:")
    assert (claude / "settings.json").read_text(encoding="utf-8") == "{not json"
