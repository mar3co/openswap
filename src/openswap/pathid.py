"""Path identity that holds on case-insensitive and linked filesystems.

``Path.resolve()`` follows symlinks but keeps the case the caller typed, so on
APFS (case-insensitive by default) ``~/library`` and ``~/Library`` resolve to
two different strings for one folder. Any check written as
``a == b or a.is_relative_to(b)`` can then be dodged with a case variant.
``canonical`` returns the form stored on disk, and ``inside``/``overlap``
also compare filesystem identity (``st_dev``/``st_ino``) along the
ancestors, which covers case, firmlinks and anything else that names one
folder two ways. A leaf module: no openswap imports.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_MAX_LISTED = 10_000


def _on_disk_darwin(path: Path) -> str | None:
    """The kernel's own name for an existing path (``F_GETPATH``), or ``None``."""
    try:
        import fcntl

        getpath = fcntl.F_GETPATH
    except (ImportError, AttributeError):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        raw = fcntl.fcntl(fd, getpath, bytes(1024))
    except OSError:
        return None
    finally:
        os.close(fd)
    name = os.fsdecode(raw.split(b"\0", 1)[0])
    return name if name.startswith("/") else None


def _on_disk_walk(path: Path) -> Path:
    """Each component's on-disk spelling, found by identity in its parent's listing.

    The fallback for a path the process may stat but not open (a folder with
    no read permission for the owner, say): a component whose parent cannot
    be listed keeps its spelling.
    """
    out = Path(path.anchor)
    for part in path.parts[1:]:
        candidate = out / part
        try:
            wanted = os.lstat(candidate)
            with os.scandir(out) as entries:
                names = [entry.name for _, entry in zip(range(_MAX_LISTED + 1), entries)]
        except OSError:
            out = candidate
            continue
        if part in names or len(names) > _MAX_LISTED:
            out = candidate
            continue
        for name in names:
            try:
                if os.path.samestat(os.lstat(out / name), wanted):
                    candidate = out / name
                    break
            except OSError:
                continue
        out = candidate
    return out


def canonical(path: str | os.PathLike) -> Path:
    """``path`` made absolute and resolved, its existing part spelt as stored on disk.

    A missing tail keeps the caller's spelling (there is nothing on disk to
    match yet).
    """
    p = Path(path)
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        p = Path(os.path.abspath(p))
    if sys.platform != "darwin":
        return p
    existing, tail = p, []
    while not os.path.lexists(existing):
        if existing.parent == existing:
            return p
        tail.insert(0, existing.name)
        existing = existing.parent
    found = _on_disk_darwin(existing)
    base = Path(found) if found is not None else _on_disk_walk(existing)
    return base.joinpath(*tail)


def inside(child: str | os.PathLike, parent: str | os.PathLike) -> bool:
    """Whether ``child`` is ``parent`` or lies beneath it, by spelling or by identity."""
    c, p = canonical(child), canonical(parent)
    if c == p or c.is_relative_to(p):
        return True
    try:
        target = os.stat(p)
    except OSError:
        return False
    for ancestor in (c, *c.parents):
        try:
            if os.path.samestat(os.stat(ancestor), target):
                return True
        except OSError:
            continue
    return False


def same(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    """Whether ``a`` and ``b`` name the same path (by spelling or identity)."""
    ca, cb = canonical(a), canonical(b)
    if ca == cb:
        return True
    try:
        return os.path.samestat(os.stat(ca), os.stat(cb))
    except OSError:
        return False


def overlap(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    """Whether either path is the other or contains it."""
    return inside(a, b) or inside(b, a)


def top_component(child: str | os.PathLike, parent: str | os.PathLike) -> str | None:
    """The first component of ``child`` below ``parent`` (on-disk spelling), or ``None``.

    ``None`` when ``child`` is ``parent`` itself or not inside it.
    """
    c, p = canonical(child), canonical(parent)
    if c != p and c.is_relative_to(p):
        return c.relative_to(p).parts[0]
    try:
        target = os.stat(p)
    except OSError:
        return None
    previous = None
    for ancestor in (c, *c.parents):
        try:
            if os.path.samestat(os.stat(ancestor), target):
                return previous
        except OSError:
            pass
        previous = ancestor.name
    return None
