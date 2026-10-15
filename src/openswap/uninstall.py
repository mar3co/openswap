"""Take OpenSwap off a Mac: ``openswap uninstall`` and the cask's hook.

Outside its data folder OpenSwap sets up the menu bar login item, the Remote
tasks worker's LaunchAgent, the Claude Code status line, and the widget's
registration. :func:`remove_integrations` undoes those and leaves saved
accounts and settings alone. :func:`delete_saved_logins` is the part of a zap
that Homebrew's ``zap`` stanza cannot reach: the Keychain.

Homebrew runs a cask's whole ``uninstall`` stanza on ``brew upgrade`` and
``brew reinstall`` too, before it swaps the app, so the cask cannot just
remove the login item: every upgrade would drop it. Its hook runs
``OpenSwap uninstall --homebrew-hook`` instead, and :func:`homebrew_command`
reads which brew command started it. Only an uninstall removes anything.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from openswap import bundle, paths
from openswap.exceptions import ClaudeSwitchError

# `brew rm` and `brew remove` are aliases Homebrew keeps for uninstall.
UNINSTALL_COMMANDS = frozenset({"uninstall", "remove", "rm"})

_BREW_RB = "/Library/Homebrew/brew.rb"
# Homebrew's Ruby is the hook's parent. A few more levels allow for a wrapper
# (env, sudo) without walking the whole process tree.
_MAX_ANCESTORS = 5


@dataclass
class Removal:
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def homebrew_command(pid: int | None = None) -> tuple[str, list[str]] | None:
    """The brew command that started this process, and its other arguments.

    ``("uninstall", ["--zap", "--cask", "openswap"])`` under ``brew uninstall
    --zap --cask openswap``. None when no near ancestor runs Homebrew's
    ``brew.rb``, or ``ps`` cannot say.
    """
    pid = os.getppid() if pid is None else pid
    for _ in range(_MAX_ANCESTORS):
        if pid <= 1:
            return None
        found = _process(pid)
        if found is None:
            return None
        parent, words = found
        for i, word in enumerate(words):
            if word.endswith(_BREW_RB):
                rest = words[i + 1:]
                for j, arg in enumerate(rest):
                    if not arg.startswith("-"):
                        return arg, rest[:j] + rest[j + 1:]
                return None
        pid = parent
    return None


def _process(pid: int) -> tuple[int, list[str]] | None:
    """``pid``'s parent and command line, split on whitespace."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "ppid=,args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = result.stdout.split()
    if result.returncode != 0 or not fields or not fields[0].isdigit():
        return None
    return int(fields[0]), fields[1:]


def remove_integrations(
    home: Path | None = None,
    uid: int | None = None,
    force: bool = False,
) -> Removal:
    """Remove the login item, the worker, the status line, and the widget.

    Never raises: a part that fails is reported in ``failed`` and the rest
    still run. The app only removes what runs a copy of OpenSwap.app, so
    uninstalling the cask leaves a uv install's login item alone. A uv or
    source install removes its own (it refuses to run beside the cask).

    ``force`` is for when the app is about to be deleted: a worker that will
    not stop cleanly is booted out anyway rather than left pointing at a
    missing app.
    """
    home = home if home is not None else Path.home()
    removal = Removal()
    steps = (
        ("menu bar login item", lambda: _remove_menubar_agent(home, uid)),
        ("Remote tasks worker", lambda: _remove_worker(home, uid, force)),
        ("Claude Code status line", _restore_status_line),
        ("widget", lambda: _remove_widget(home, uid)),
    )
    for name, step in steps:
        try:
            if step():
                removal.removed.append(name)
        except Exception as e:
            removal.failed.append(f"{name}: {e}")
    return removal


def _ours(plist: Path) -> bool:
    """Whether this install is the one ``plist`` runs.

    In the app, only this copy of it (or a program that is gone, which no
    install can still use): an agent on a uv install, a dev build or another
    copy of the app that still exists outlives this one.
    """
    if not bundle.is_frozen():
        return True
    try:
        program = plistlib.loads(plist.read_bytes()).get("ProgramArguments")
    except (OSError, plistlib.InvalidFileException, ValueError, AttributeError):
        return True  # unreadable: no other install can claim it either
    if not (isinstance(program, list) and program and isinstance(program[0], str)):
        return True
    return _runs_this_app(program[0])


def _remove_menubar_agent(home: Path, uid: int | None) -> bool:
    from openswap import launch_agent

    removed = False
    for label in (launch_agent.LABEL, *launch_agent.LEGACY_LABELS):
        plist = launch_agent.plist_path(label, home)
        if plist.is_file() and not _ours(plist):
            continue
        result = launch_agent.uninstall(label, home=home, uid=uid)
        removed = removed or result["was_loaded"] or result["removed_plist"]
    return removed


def _remove_worker(home: Path, uid: int | None, force: bool) -> bool:
    from openswap.worker import launch_agent as worker_agent
    from openswap.worker.cli import disable_worker

    plist = worker_agent.plist_path(home)
    if plist.is_file():
        if not _ours(plist):
            return False
    elif not worker_agent._is_loaded(uid):
        return False
    # disable_worker stops a running task first and turns Remote tasks off, so
    # a reinstall does not find the worker enabled with no LaunchAgent.
    try:
        stopped, _, diagnostic = disable_worker(paths.get_backup_root())
    except Exception as e:
        # A failed migration or lifecycle lock: the same as not stopping.
        if not force:
            raise
        stopped, diagnostic = False, str(e)
    if stopped:
        return True
    if not force:
        raise ClaudeSwitchError(
            f"it did not stop ({diagnostic}); see openswap worker status"
        )
    worker_agent.uninstall(home=home, uid=uid)
    return True


def _restore_status_line() -> bool:
    """Put back the user's own Claude Code status line, if this install's
    hook replaced it. A hook that runs another install's openswap stays.

    The app always writes its own executable into the hook, so in the app a
    bare ``openswap statusline`` is not its own: it came from a uv or source
    install, which may outlive the cask.
    """
    from openswap import statusline

    config_home = paths.get_claude_config_home()
    if statusline.installed_command(config_home) is None:
        return False
    if bundle.is_frozen():
        named = statusline.hook_program(config_home)
        if named is None or not _runs_this_app(named):
            return False
    return bool(statusline.uninstall(config_home, paths.get_backup_root())["restored"])


def _runs_this_app(program: str) -> bool:
    """Whether ``program`` is this app or a link to it (the cask's
    ``bin/openswap``), or is already gone."""
    app = bundle.app_path()
    try:
        resolved = Path(program).resolve(strict=True)
    except FileNotFoundError:
        return True
    except (OSError, RuntimeError):
        return False
    return app is not None and resolved.is_relative_to(app.resolve())


def _remove_widget(home: Path, uid: int | None) -> bool:
    from openswap import widget_install

    if bundle.is_frozen():
        # The app's appex goes with the app. Unregistering it only takes it
        # out of the widget gallery before macOS notices, so a pluginkit
        # failure is no reason to stop the uninstall.
        try:
            widget_install.uninstall_widget(home=home, uid=uid)
        except ClaudeSwitchError:
            pass
        return False
    result = widget_install.uninstall_widget(home=home, uid=uid)
    return bool(result.get("removed_app") or result.get("removed_plist") or result.get("was_loaded"))


def delete_saved_logins() -> int:
    """Delete every Keychain item OpenSwap saved: account backups (also under
    the pre-rename service), the worker's pairing, and each session profile's
    current login. Never Claude Code's own items, which hold the login in use
    now. Returns how many service items were deleted.

    Run before the data folder goes, as ``purge`` does: a session profile's
    item is named from its directory's path, so once that is deleted the
    item can no longer be found.
    """
    from openswap import macos_keychain
    from openswap.credentials import LEGACY_BACKUP_SECURITY_SERVICE, SECURITY_SERVICE
    from openswap.session import delete_macos_keychain_entry, profile_is_quiescent

    for profile in _session_profiles():
        # A Claude still running on the profile keeps its login. Only
        # `brew uninstall --zap` gets here past refuse_live_sessions.
        if profile_is_quiescent(profile):
            delete_macos_keychain_entry(profile)
    deleted = 0
    for service in (SECURITY_SERVICE, LEGACY_BACKUP_SECURITY_SERVICE):
        try:
            deleted += macos_keychain.delete_all(service)
        except macos_keychain.KEYCHAIN_ERRORS as e:
            raise ClaudeSwitchError(f"Could not delete the saved logins from the Keychain: {e}") from e
    return deleted


def _data_roots() -> list[Path]:
    """The backup root and, when it is a separate folder, the pre-XDG one:
    both go in a zap, as in purge."""
    backup = paths.get_backup_root()
    legacy = paths.get_legacy_backup_root()
    if legacy.is_relative_to(backup) or backup.is_relative_to(legacy):
        return [backup]
    return [backup, legacy]


def _session_profiles() -> list[Path]:
    """Session profiles in every root a zap deletes."""
    found = []
    for root in _data_roots():
        sessions = root / "sessions"
        if sessions.is_dir():
            found.extend(sorted(p for p in sessions.iterdir() if p.is_dir()))
    return found


def refuse_live_sessions() -> None:
    """Raise while a session-mode Claude runs on a profile a zap would delete.

    As ``purge`` does: deleting a profile and its Keychain item from under a
    live Claude is unsafe, and a record that cannot be read is no proof that
    nothing runs.
    """
    from openswap.session import scan_live_sessions

    live, unreadable = [], []
    for profile in _session_profiles():
        found, bad = scan_live_sessions(profile)
        if found:
            live.append(f"{profile.name} (PID {', '.join(str(s.pid) for s in found)})")
        elif bad:
            unreadable.append(profile.name)
    if live:
        raise ClaudeSwitchError(
            f"Claude is running on session profile {'; '.join(live)}. Exit it, then retry."
        )
    if unreadable:
        raise ClaudeSwitchError(
            f"Could not read the session records of {', '.join(unreadable)}, so whether "
            "Claude is running there is unknown. Repair or remove them, then retry."
        )


def data_paths(home: Path | None = None, *, homebrew_zap: bool = False) -> list[Path]:
    """The files a zap deletes: the cask's ``zap trash:`` list plus the
    pre-XDG backup root, which purge also removes. With ``homebrew_zap``,
    only what Homebrew's zap leaves behind."""
    home = home if home is not None else Path.home()
    logs = home / "Library" / "Logs"
    backup, *legacy = _data_roots()
    found = [] if homebrew_zap else [
        backup,
        home / "Library" / "Containers" / bundle.APPEX_ID,
        *sorted(logs.glob("com.opensoft.openswap.*.log")),
        *sorted(logs.glob("com.opensoft.openswap.*.err")),
    ]
    found.extend(legacy)
    return [path for path in found if path.exists() or path.is_symlink()]


def delete_data(home: Path | None = None, *, homebrew_zap: bool = False) -> list[Path]:
    """Delete :func:`data_paths`. Returns what was deleted; raises on the
    first path that will not go."""
    deleted = []
    for path in data_paths(home, homebrew_zap=homebrew_zap):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as e:
            raise ClaudeSwitchError(f"Could not delete {path}: {e}") from e
        deleted.append(path)
    return deleted


def homebrew_brew() -> Path | None:
    """The ``brew`` that installed this app, when Homebrew did."""
    if not bundle.installed_by_homebrew():
        return None
    prefix = bundle.cask_prefix()
    brew = None if prefix is None else prefix / "bin" / "brew"
    return brew if brew is not None and os.access(brew, os.X_OK) else None
