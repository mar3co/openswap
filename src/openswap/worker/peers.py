"""Which process is on the other end of the worker's socket, and what started it (plan 025 S2).

Every process the owner runs can reach the socket, so a connection that
receives anything back (an OpenSwap channel, a waiting question hook) is
served only after its place in the process tree is checked.
"""

from __future__ import annotations

import socket
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "mksh", "tcsh", "csh"})
CHANNEL_NAME = "server:openswap"
CHANNEL_FLAGS = ("--dangerously-load-development-channels", "--channels")
PS_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    started: str  # when this PID's life began (``ps`` lstart, in UTC): a reused PID differs
    args: tuple[str, ...]  # argv split on whitespace: ``ps`` cannot show argument boundaries


def peer_pid(connection: socket.socket) -> int:
    """The PID of the process that connected to this AF_UNIX socket; 0 when unknown."""
    try:
        if sys.platform == "darwin":
            return struct.unpack("i", connection.getsockopt(0, 0x002, 4))[0]  # SOL_LOCAL, LOCAL_PEERPID
        if hasattr(socket, "SO_PEERCRED"):
            size = struct.calcsize("3i")
            return struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size))[0]
    except (OSError, struct.error):
        pass
    return 0


def process(pid: int) -> Process | None:
    """``pid``'s parent, start time and arguments, or None when it cannot be read."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        # The C locale and UTC: ``lstart`` reads the same whatever the owner's
        # language or zone, and as Claude Code writes ``procStart`` (measured: UTC).
        out = subprocess.run(["/bin/ps", "-ww", "-o", "ppid=,lstart=,args=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=PS_TIMEOUT_SECONDS,
                             env={"LC_ALL": "C", "TZ": "UTC0", "PATH": "/usr/bin:/bin"}).stdout.split()
        # ppid, then lstart's five words ("Fri Nov  6 14:03:45 2026"), then argv.
        if len(out) < 7:
            return None
        return Process(pid, int(out[0]), " ".join(out[1:6]), tuple(out[6:]))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def names_channel(args: tuple[str, ...]) -> bool:
    """Whether Claude Code was started with OpenSwap's channel named on
    ``--dangerously-load-development-channels`` or ``--channels``."""
    for index, word in enumerate(args):
        flag, _, value = word.partition("=")
        if flag not in CHANNEL_FLAGS:
            continue
        values = [value] if value else []
        for following in args[index + 1:] if not value else ():
            if following.startswith("-"):
                break
            values.append(following)
        if any(CHANNEL_NAME in v.split(",") for v in values):
            return True
    return False


def is_channel(args: tuple[str, ...]) -> bool:
    """Whether a process was started as OpenSwap's channel (``<openswap> attention
    channel``). Any process can say so: this only keeps other MCP servers out."""
    return len(args) >= 3 and args[-2:] == ("attention", "channel")


def is_shell(proc: Process) -> bool:
    return bool(proc.args) and Path(proc.args[0]).name.lstrip("-") in SHELLS


def hook_claude(pid: int) -> Process | None:
    """The Claude Code process a hook process runs under: its first ancestor
    that is not a shell (the hook's own wrapper, and a shell Claude Code ran
    it through, sit in between). None when unknown."""
    hook = process(pid)
    ancestor = process(hook.ppid) if hook is not None else None
    for _ in range(3):
        if ancestor is None or not is_shell(ancestor):
            return ancestor
        ancestor = process(ancestor.ppid)
    return None
