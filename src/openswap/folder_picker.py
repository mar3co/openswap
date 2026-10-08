"""A type-to-search folder picker for the terminal, in the style of Claude Code.

Typing filters folders under the home folder (or, once the text looks like a
path, the folders inside the typed parent); arrow keys move the highlight,
Tab completes it into the query and Enter picks it. ``pick_folder`` falls back
to plain ``input()`` whenever stdin/stdout is not a terminal or the platform
has no ``termios`` (Windows), so scripted and piped runs behave as before.

A caller may pin numbered suggestions (the guided setup's detected code
folders): they are what an empty query shows, typing searches them before the
home-folder index, and digits (``1 3``, ``1,3``) select them by number.

The ranking and key handling live in ``FolderIndex`` and ``PickerState`` so
they are testable without a terminal; ``pick_folder`` only draws and reads.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# Never descended into: hidden folders, build output, and the home folders
# whose listing makes macOS ask for Photos/Music/Library access.
_SKIP_NAMES = frozenset({
    "node_modules", "__pycache__", "venv", "site-packages", "Library",
    "Applications", "Pictures", "Music", "Movies",
})
_MAX_DEPTH = 4
_MAX_DIRS = 20000
_SCAN_BUDGET_S = 2.0
_VISIBLE = 8


def display_path(path: Path, home: Path) -> str:
    try:
        rel = Path(path).relative_to(home)
    except ValueError:
        return str(path)
    return "~" if str(rel) == "." else "~/" + rel.as_posix()


def _looks_like_path(query: str) -> bool:
    return query.startswith(("/", "~", ".")) or "/" in query


def _subsequence(needle: str, haystack: str) -> bool:
    it = iter(haystack)
    return all(char in it for char in needle)


def _score(query: str, name: str, shown: str) -> int | None:
    """Lower is better; ``None`` drops the folder. Inputs are lowercased."""
    if name == query:
        return 0
    if name.startswith(query):
        return 1
    if query in name:
        return 2
    if query in shown:
        return 3
    if _subsequence(query, shown):
        return 4
    return None


class FolderIndex:
    """Folders under ``home``, breadth first, collected on a background thread."""

    def __init__(self, home: Path, *, max_depth: int = _MAX_DEPTH, max_dirs: int = _MAX_DIRS,
                 budget_s: float = _SCAN_BUDGET_S):
        self.home = Path(home)
        self._max_depth, self._max_dirs, self._budget_s = max_depth, max_dirs, budget_s
        self._lock = threading.Lock()
        self._found: list[Path] = []
        self.done = False

    def start(self) -> "FolderIndex":
        threading.Thread(target=self.scan, name="folder-picker-scan", daemon=True).start()
        return self

    def scan(self) -> None:
        deadline = time.monotonic() + self._budget_s
        level = [self.home]
        try:
            for _depth in range(self._max_depth):
                below: list[Path] = []
                for parent in level:
                    for child in _subfolders(parent):
                        if child.name in _SKIP_NAMES:
                            continue
                        with self._lock:
                            if len(self._found) >= self._max_dirs:
                                return
                            self._found.append(child)
                        below.append(child)
                    if time.monotonic() > deadline:
                        return
                level = below
        finally:
            self.done = True

    def snapshot(self) -> list[Path]:
        with self._lock:
            return list(self._found)

    def search(self, query: str, limit: int = _VISIBLE) -> list[Path]:
        if _looks_like_path(query):
            return _complete_path(query, self.home, limit)
        found = self.snapshot()
        if not query:
            # Breadth first, so this is the top of the home folder.
            return found[:limit]
        needle = query.lower()
        ranked = []
        for order, path in enumerate(found):
            shown = display_path(path, self.home).lower()
            score = _score(needle, path.name.lower(), shown)
            if score is not None:
                ranked.append((score, shown.count("/"), len(shown), order, path))
        ranked.sort(key=lambda item: item[:4])
        return [item[-1] for item in ranked[:limit]]


def _subfolders(parent: Path, *, hidden: bool = False) -> list[Path]:
    try:
        with os.scandir(parent) as entries:
            out = []
            for entry in entries:
                if not hidden and entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        out.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(out, key=lambda p: p.name.lower())


def _complete_path(query: str, home: Path, limit: int) -> list[Path]:
    """Folders inside the typed parent whose name matches the last segment."""
    # "~" is the index's home, not $HOME/%USERPROFILE%, so both agree.
    expanded = str(home) + query[1:] if query == "~" or query.startswith("~/") else query
    if expanded.endswith("/"):
        parent, prefix = Path(expanded), ""
    else:
        parent, prefix = Path(expanded).parent, Path(expanded).name
    if not parent.is_absolute():
        parent = home / parent
    needle = prefix.lower()
    children = _subfolders(parent, hidden=prefix.startswith("."))
    starts = [p for p in children if p.name.lower().startswith(needle)]
    contains = [p for p in children if needle and needle in p.name.lower() and p not in starts]
    return (starts + contains)[:limit]


@dataclass(frozen=True)
class Suggestion:
    """A pinned, numbered entry: a folder, a note shown after it and whether it is ticked."""

    path: Path
    note: str = ""
    checked: bool = False


_NUMBERS = re.compile(r"[0-9]+(?:[\s,]+[0-9]+)*[\s,]*")


class PickerState:
    """The query, the highlighted row and what each key does to them.

    ``pinned`` suggestions are numbered from 1. With an empty query they are
    the rows (``highlight`` names the one Enter picks); typed text searches
    them first, then the home-folder index; a query of numbers shows the
    suggestions it names and Enter returns the numbers as typed.
    """

    def __init__(self, index: FolderIndex, *, pinned=(), highlight: int | None = None):
        self.index = index
        self.pinned: tuple[Suggestion, ...] = tuple(pinned)
        self.query = ""
        self.results: list[Path] = []
        self.refresh()
        # Nothing highlighted until the owner types or moves, unless a pinned
        # suggestion is the default.
        self.selected = highlight if highlight is not None and 0 <= highlight < len(self.results) else -1

    def number_of(self, path: Path) -> int | None:
        """The pinned number shown for ``path`` (1-based), if it is pinned."""
        for number, suggestion in enumerate(self.pinned, start=1):
            if suggestion.path == path:
                return number
        return None

    def suggestion(self, path: Path) -> Suggestion | None:
        number = self.number_of(path)
        return None if number is None else self.pinned[number - 1]

    def numbers(self) -> list[int] | None:
        """The pinned numbers the query names (1-based, in order, once each), or ``None``."""
        if not self.pinned or not _NUMBERS.fullmatch(self.query.strip()) or not self.query.strip():
            return None
        out = []
        for token in re.split(r"[\s,]+", self.query.strip()):
            if token and int(token) not in out:
                out.append(int(token))
        return out

    def _search(self) -> list[Path]:
        if not self.pinned or _looks_like_path(self.query):
            return self.index.search(self.query)
        if not self.query:
            return [suggestion.path for suggestion in self.pinned]
        numbers = self.numbers()
        if numbers is not None:
            return [self.pinned[n - 1].path for n in numbers if 1 <= n <= len(self.pinned)]
        needle = self.query.lower()
        ranked = []
        for order, suggestion in enumerate(self.pinned):
            shown = display_path(suggestion.path, self.index.home).lower()
            score = _score(needle, suggestion.path.name.lower(), shown)
            if score is not None:
                ranked.append((score, order, suggestion.path))
        found = [path for *_rank, path in sorted(ranked, key=lambda item: item[:2])]
        for path in self.index.search(self.query, limit=_VISIBLE):
            if path not in found:
                found.append(path)
        return found[:_VISIBLE]

    def refresh(self) -> None:
        self.results = self._search()
        if not self.results or self.numbers() is not None:
            self.selected = -1  # numbers are picked as typed, not by the highlight
        elif self.query and getattr(self, "selected", -1) < 0:
            self.selected = 0
        else:
            self.selected = min(getattr(self, "selected", -1), len(self.results) - 1)

    def type(self, text: str) -> None:
        self.query += text
        self.selected = -1
        self.refresh()

    def backspace(self) -> None:
        self.query = self.query[:-1]
        self.selected = -1
        self.refresh()

    def clear(self) -> None:
        self.query = ""
        self.selected = -1
        self.refresh()

    def move(self, step: int) -> None:
        if self.results:
            self.selected = max(0, min(len(self.results) - 1, self.selected + step))

    def complete(self) -> None:
        if 0 <= self.selected < len(self.results):
            self.query = display_path(self.results[self.selected], self.index.home) + "/"
            self.selected = -1
            self.refresh()

    def choice(self) -> str:
        """What Enter returns: the highlighted folder, else the typed text ("" finishes)."""
        if 0 <= self.selected < len(self.results):
            return str(self.results[self.selected])
        return self.query.strip()


def _tty_available() -> bool:
    try:
        import termios  # noqa: F401
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (ImportError, AttributeError, ValueError, OSError):
        return False


def available() -> bool:
    """Whether ``pick_folder`` can run interactively here (a terminal with ``termios``)."""
    return _tty_available()


def pick_folder(question: str, *, home: Path | None = None, pinned=(), highlight: int | None = None,
                index: FolderIndex | None = None) -> str | None:
    """Pick a folder interactively; "" when the owner finishes, ``None`` on Ctrl-C.

    ``pinned`` are numbered ``Suggestion`` rows (see ``PickerState``); a query
    of numbers comes back as typed. ``index`` lets a caller reuse one scan
    across several picks.
    """
    if not _tty_available():
        try:
            return input(question).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    if index is None:
        index = FolderIndex(home or Path.home()).start()
    return _run_picker(question, index, pinned=pinned, highlight=highlight)


def _fit(text: str, width: int) -> str:
    """Keep the end of a long path, where the folder's own name is."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


_HINT = "↑↓ move · Tab complete · Enter pick · Esc skip"
_PINNED_HINT = "type or 1 3 · ↑↓ move · Tab complete · Enter pick · Esc skip"


def row_text(state: PickerState, path: Path, width: int) -> str:
    """One row: a pinned suggestion as ``✓ 1  ~/GitHub  (note)``, any other folder as its path."""
    home = state.index.home
    suggestion = state.suggestion(path)
    if suggestion is None:
        return _fit(display_path(path, home), width)
    shown = [display_path(item.path, home) for item in state.pinned]
    pad = max(len(text) for text in shown)
    number = state.number_of(path)
    digits = len(str(len(state.pinned)))
    text = f"{'✓' if suggestion.checked else '•'} {str(number).rjust(digits)}  "
    text += display_path(path, home).ljust(pad) + (f"  {suggestion.note}" if suggestion.note else "")
    return _fit(text.rstrip(), width)


def _render(out, question: str, state: PickerState) -> None:
    try:
        columns = os.get_terminal_size(out.fileno()).columns
    except (OSError, ValueError):
        columns = 80
    width = max(20, (columns or 80) - 1)  # a terminal with no size set reports 0
    lines = []
    for row, path in enumerate(state.results):
        marker = "❯ " if row == state.selected else "  "
        text = marker + row_text(state, path, width - len(marker))
        lines.append(f"\x1b[7m{text}\x1b[0m" if row == state.selected else text)
    if not state.results:
        lines.append("  (no matching folders; Enter uses what you typed)" if state.query
                     else "  (looking for folders…)" if not state.index.done else "  (no folders)")
    hint = _PINNED_HINT if state.pinned else _HINT
    lines.append(f"\x1b[2m{hint[:width]}\x1b[0m")
    head = (question + state.query)[-width:]
    out.write("\r\x1b[J" + head + "".join("\n" + line for line in lines))
    out.write(f"\x1b[{len(lines)}A\r\x1b[{len(head)}C" if head else f"\x1b[{len(lines)}A\r")
    out.flush()


def _read_key(fd: int) -> str:
    import select

    first = os.read(fd, 1)
    if first == b"\x1b":
        # Drain the whole sequence (e.g. Ctrl+Up is ESC [1;5A) so none of it is typed.
        rest = b""
        while len(rest) < 16 and select.select([fd], [], [], 0.03)[0]:
            rest += os.read(fd, 1)
            if rest[:1] in (b"[", b"O") and len(rest) > 1 and 0x40 <= rest[-1] <= 0x7E:
                break
        if not rest:
            return "esc"
        if rest[:1] in (b"[", b"O"):
            return {b"A": "up", b"B": "down"}.get(rest[-1:], "")
        return ""
    if first and first[0] >= 0xC0:
        extra = 3 if first[0] >= 0xF0 else 2 if first[0] >= 0xE0 else 1
        first += os.read(fd, extra)
    return first.decode("utf-8", "ignore")


def _run_picker(question: str, index: FolderIndex, *, pinned=(), highlight: int | None = None) -> str | None:
    import select
    import termios
    import tty

    fd, out = sys.stdin.fileno(), sys.stdout
    saved = termios.tcgetattr(fd)
    state = PickerState(index, pinned=pinned, highlight=highlight)
    result: str | None = None
    scanning = True
    try:
        tty.setcbreak(fd)
        _render(out, question, state)
        while True:
            if not select.select([fd], [], [], 0.15)[0]:
                if scanning:  # show folders as the scan finds them, then once more at the end
                    scanning = not index.done
                    if state.query or not state.pinned:  # pinned rows do not wait for the scan
                        state.refresh()
                        _render(out, question, state)
                continue
            key = _read_key(fd)
            if key in ("\r", "\n"):
                result = state.choice()
                break
            if key in ("esc", "\x04"):
                result = ""
                break
            if key == "up":
                state.move(-1)
            elif key == "down":
                state.move(1)
            elif key == "\t":
                state.complete()
            elif key in ("\x7f", "\x08"):
                state.backspace()
            elif key == "\x15":
                state.clear()
            elif key and key.isprintable():
                state.type(key)
            _render(out, question, state)
    except KeyboardInterrupt:
        result = None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        # The transcript keeps the short ~/ form of a picked folder.
        shown = display_path(Path(result), index.home) if result and os.path.isabs(result) else (result or "")
        out.write("\r\x1b[J" + question + shown + "\n")
        out.flush()
    return result
