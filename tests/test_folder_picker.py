"""The terminal folder picker's search, path completion and key handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from openswap import folder_picker
from openswap.folder_picker import FolderIndex, PickerState, display_path


@pytest.fixture
def home(tmp_path):
    for rel in ("GitHub/opensoft/openswap", "GitHub/opensoft/opentag", "GitHub/other",
                "Documents/Research Notes", "Library/Caches", ".hidden/inside",
                "Projects/node_modules/pkg"):
        (tmp_path / rel).mkdir(parents=True)
    (tmp_path / "file.txt").write_text("x")
    return tmp_path


def _index(home, **kwargs):
    index = FolderIndex(home, **kwargs)
    index.scan()
    return index


def _shown(index, paths):
    return [display_path(p, index.home) for p in paths]


def test_scan_skips_hidden_build_and_private_folders(home):
    found = set(_shown(_index(home), _index(home).snapshot()))
    assert "~/GitHub/opensoft/openswap" in found
    assert "~/Documents/Research Notes" in found
    assert not any(p.startswith(("~/Library", "~/.hidden")) for p in found)
    assert not any("node_modules" in p for p in found)
    assert "~/file.txt" not in found


def test_scan_respects_depth_and_count_limits(home):
    shallow = _shown(_index(home, max_depth=1), _index(home, max_depth=1).snapshot())
    assert "~/GitHub" in shallow and "~/GitHub/opensoft" not in shallow
    assert len(_index(home, max_dirs=2).snapshot()) == 2


def test_search_ranks_name_matches_before_path_and_fuzzy_matches(home):
    index = _index(home)
    assert _shown(index, index.search("openswap"))[0] == "~/GitHub/opensoft/openswap"
    # Equal scores: shallower first, then shorter.
    assert _shown(index, index.search("open"))[:3] == [
        "~/GitHub/opensoft", "~/GitHub/opensoft/opentag", "~/GitHub/opensoft/openswap",
    ]
    assert "~/Documents/Research Notes" in _shown(index, index.search("rsnotes"))
    assert index.search("zzz") == []


def test_empty_query_lists_the_top_of_home_first(home):
    index = _index(home)
    assert _shown(index, index.search(""))[:3] == ["~/Documents", "~/GitHub", "~/Projects"]


def test_typed_paths_complete_inside_the_parent(home):
    index = _index(home)
    assert _shown(index, index.search("~/GitHub/opensoft/opent")) == ["~/GitHub/opensoft/opentag"]
    assert _shown(index, index.search(str(home / "GitHub") + "/")) == [
        "~/GitHub/opensoft", "~/GitHub/other",
    ]
    assert _shown(index, index.search("~/.h")) == ["~/.hidden"]
    assert index.search("/no/such/place/") == []


def test_keys_move_complete_and_pick(home):
    state = PickerState(_index(home))
    assert state.selected == -1 and state.choice() == ""  # empty Enter finishes
    for char in "opens":
        state.type(char)
    assert state.choice() == str(home / "GitHub/opensoft")
    state.move(1)
    assert state.choice() == str(home / "GitHub/opensoft/openswap")
    state.move(10)
    assert state.selected == len(state.results) - 1
    state.move(-10)
    state.complete()
    assert state.query == "~/GitHub/opensoft/"
    assert _shown(state.index, state.results) == ["~/GitHub/opensoft/openswap", "~/GitHub/opensoft/opentag"]
    state.clear()
    for char in "~/brand-new":
        state.type(char)
    assert state.results == [] and state.choice() == "~/brand-new"
    state.backspace()
    assert state.query == "~/brand-ne"


@pytest.mark.skipif(not hasattr(__import__("os"), "openpty"), reason="needs a pseudo-terminal")
def test_escape_sequences_are_drained_not_typed():
    import os

    reader, writer = os.openpty()
    try:
        os.write(writer, b"\x1b[1;5Ax\x1b[Bq\x1b")
        keys = [folder_picker._read_key(reader) for _ in range(5)]
    finally:
        os.close(reader)
        os.close(writer)
    assert keys == ["up", "x", "down", "q", "esc"]


def test_without_a_terminal_it_reads_a_typed_line(monkeypatch):
    monkeypatch.setattr(folder_picker, "_tty_available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "  ~/x  ")
    assert folder_picker.pick_folder("Folder? ") == "~/x"

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert folder_picker.pick_folder("Folder? ") is None


# --- pinned suggestions (the setup's detected folders) ------------------------------------------


def _pinned(home):
    from openswap.folder_picker import Suggestion

    return [Suggestion(home / "GitHub", "(recommended)"), Suggestion(home / "GitHub/opensoft/openswap", "(git repo)"),
            Suggestion(home / "Documents", "", checked=True)]


def test_an_empty_query_shows_the_suggestions_with_the_default_highlighted(home):
    state = PickerState(_index(home), pinned=_pinned(home), highlight=0)
    assert state.results == [home / "GitHub", home / "GitHub/opensoft/openswap", home / "Documents"]
    assert state.selected == 0 and state.choice() == str(home / "GitHub")  # Enter picks it
    assert folder_picker.row_text(state, home / "GitHub", 80) == "• 1  ~/GitHub                    (recommended)"
    assert folder_picker.row_text(state, home / "Documents", 80) == "✓ 3  ~/Documents"
    # Without a default nothing is highlighted, and Enter on nothing finishes.
    state = PickerState(_index(home), pinned=_pinned(home))
    assert state.selected == -1 and state.choice() == ""


def test_typing_searches_the_suggestions_first_then_home(home):
    state = PickerState(_index(home), pinned=_pinned(home), highlight=0)
    for char in "opens":
        state.type(char)
    shown = _shown(state.index, state.results)
    assert shown[0] == "~/GitHub/opensoft/openswap"  # the pinned match leads
    assert "~/GitHub/opensoft" in shown and shown.count("~/GitHub/opensoft/openswap") == 1
    assert state.selected == 0 and state.choice() == str(home / "GitHub/opensoft/openswap")
    assert folder_picker.row_text(state, home / "GitHub/opensoft", 80) == "~/GitHub/opensoft"
    state.clear()
    for char in "~/GitHub/o":
        state.type(char)
    assert _shown(state.index, state.results) == ["~/GitHub/opensoft", "~/GitHub/other"]  # paths still complete


@pytest.mark.parametrize(("typed", "rows"), [("1 3", [0, 2]), ("1,3", [0, 2]), ("3, 1 3", [2, 0]), ("2", [1])])
def test_digits_pick_suggestions_by_number(home, typed, rows):
    pinned = _pinned(home)
    state = PickerState(_index(home), pinned=pinned, highlight=0)
    for char in typed:
        state.type(char)
    assert state.results == [pinned[row].path for row in rows]
    assert state.selected == -1 and state.choice() == typed  # the numbers, as typed


def test_digits_out_of_range_show_nothing_and_come_back_as_typed(home):
    state = PickerState(_index(home), pinned=_pinned(home))
    state.type("9")
    assert state.results == [] and state.choice() == "9"
    # Without suggestions digits are an ordinary search.
    plain = PickerState(_index(home))
    plain.type("9")
    assert plain.numbers() is None
