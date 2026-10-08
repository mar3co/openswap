"""A type-to-search folder picker for the terminal, in the style of Claude Code.

Typing filters folders under the home folder (or, once the text looks like a
path, the folders inside the typed parent); arrow keys move the highlight,
Tab completes it into the query and Enter picks it. ``pick_folder`` falls back
to plain ``input()`` whenever stdin/stdout is not a terminal or the platform
has no ``termios`` (Windows), so scripted and piped runs behave as before.

The ranking and key handling live in ``FolderIndex`` and ``PickerState`` so
they are testable without a terminal; ``pick_folder`` only draws and reads.
"""

from __future__ import annotations

import os
import sys
import threading
import time
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
    expanded = os.path.expanduser(query)
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


class PickerState:
    """The query, the highlighted row and what each key does to them."""

    def __init__(self, index: FolderIndex):
        self.index = index
        self.query = ""
        self.selected = -1  # nothing highlighted until the owner types or moves
        self.results: list[Path] = []
        self.refresh()

    def refresh(self) -> None:
        self.results = self.index.search(self.query)
        if not self.results:
            self.selected = -1
        elif self.query and self.selected < 0:
            self.selected = 0
        else:
            self.selected = min(self.selected, len(self.results) - 1)

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


def pick_folder(question: str, *, home: Path | None = None) -> str | None:
    """Pick a folder interactively; "" when the owner finishes, ``None`` on Ctrl-C."""
    if not _tty_available():
        try:
            return input(question).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    return _run_picker(question, FolderIndex(home or Path.home()).start())


def _fit(text: str, width: int) -> str:
    """Keep the end of a long path, where the folder's own name is."""
    return text if len(text) <= width else "…" + text[-(width - 1):]


_HINT = "type to search · ↑↓ choose · Tab complete · Enter pick · Esc or empty Enter to finish"


def _render(out, question: str, state: PickerState) -> None:
    width = max(20, os.get_terminal_size(out.fileno()).columns - 1)
    home = state.index.home
    lines = []
    for row, path in enumerate(state.results):
        marker = "❯ " if row == state.selected else "  "
        text = marker + _fit(display_path(path, home), width - len(marker))
        lines.append(f"\x1b[7m{text}\x1b[0m" if row == state.selected else text)
    if not state.results:
        lines.append("  (no matching folders; Enter uses what you typed)" if state.query
                     else "  (looking for folders…)" if not state.index.done else "  (no folders)")
    lines.append(f"\x1b[2m{_HINT[:width]}\x1b[0m")
    head = (question + state.query)[-width:]
    out.write("\r\x1b[J" + head + "".join("\n" + line for line in lines))
    out.write(f"\x1b[{len(lines)}A\r\x1b[{len(head)}C" if head else f"\x1b[{len(lines)}A\r")
    out.flush()


def _read_key(fd: int) -> str:
    import select

    first = os.read(fd, 1)
    if first == b"\x1b":
        if select.select([fd], [], [], 0.03)[0]:
            rest = os.read(fd, 2)
            return {b"[A": "up", b"[B": "down", b"OA": "up", b"OB": "down"}.get(rest, "")
        return "esc"
    if first and first[0] >= 0xC0:
        extra = 3 if first[0] >= 0xF0 else 2 if first[0] >= 0xE0 else 1
        first += os.read(fd, extra)
    return first.decode("utf-8", "ignore")


def _run_picker(question: str, index: FolderIndex) -> str | None:
    import select
    import termios
    import tty

    fd, out = sys.stdin.fileno(), sys.stdout
    saved = termios.tcgetattr(fd)
    state = PickerState(index)
    result: str | None = None
    scanning = True
    try:
        tty.setcbreak(fd)
        _render(out, question, state)
        while True:
            if not select.select([fd], [], [], 0.15)[0]:
                if scanning:  # show folders as the scan finds them, then once more at the end
                    scanning = not index.done
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
        out.write("\r\x1b[J" + question + (result or "") + "\n")
        out.flush()
    return result
