"""Coordinate Squirrel's shared installation without modifying its state or app.

The durable journal also gates CLI/Dock launches. A timeout never releases that
gate: ShipIt may still be waiting to install. Recovery is explicit: Resume
restores sessions only after the requested version passes publisher validation,
and Cancel reopens them on whichever version is installed.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from openswap import claude_desktop as cd

WAIT_SECONDS = 120
SHIPIT_ID = cd.BUNDLE_ID + ".ShipIt"
BLOCKED = "Claude's update is unfinished. Choose Resume update or Cancel update in OpenSwap before opening profiles."


class UnreadableJournal(cd.ClaudeDesktopError):
    pass


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise cd.ClaudeDesktopError("Claude reported an unsupported update version.")
    return tuple(int(part) for part in value.split("."))


class UpdateJournal:
    """One gate per user, including callers with a custom profile root."""
    def __init__(self):
        self.storage = cd.DesktopProfiles(cd.default_root())
        self.path = self.storage.root / "desktop-update.json"

    def lock(self):
        return self.storage.lock("desktop-update.lock", timeout=3)

    def read(self):
        if not self.storage._root() or not cd.plain(self.path):
            return None
        try:
            if self.path.stat().st_size > 32_000:
                raise ValueError
            data = json.loads(self.path.read_text())
            if (not isinstance(data, dict) or type(data.get("schema")) is not int or data["schema"] != 1
                    or data.get("phase") not in {"waiting", "restoring"}
                    or not isinstance(data.get("id"), str) or not re.fullmatch(r"[a-f0-9]{32}", data["id"])
                    or not isinstance(data.get("root"), str) or not Path(data["root"]).is_absolute()
                    or not isinstance(data.get("profiles"), list) or len(data["profiles"]) > 100
                    or any(not isinstance(p, str) or not re.fullmatch(r"[a-f0-9]{32}", p) for p in data["profiles"])
                    or len(set(data["profiles"])) != len(data["profiles"])
                    or not isinstance(data.get("docked"), list) or len(data["docked"]) > 100
                    or any(not isinstance(p, str) or not re.fullmatch(r"[a-f0-9]{32}", p) for p in data["docked"])):
                raise ValueError
            version(data["before"])
            version(data["target"])
            return data
        except (ValueError, KeyError, TypeError) as exc:
            raise UnreadableJournal("Claude update recovery data is unreadable. Choose Cancel update in OpenSwap to unblock profiles.") from exc

    def write(self, data):
        self.storage.write_json(self.path, data)

    def clear(self):
        cd.plain(self.path)
        self.path.unlink(missing_ok=True)


@contextmanager
def launch_guard(installed):
    """Serialize spawning Claude with recording an update.

    The installer cannot start between a successful gate check and the spawn.
    Callers validate the app first; signature checks never hold this lock.
    """
    journal = UpdateJournal()
    with journal.lock():
        record = journal.read()
        if record and (record["phase"] == "waiting" or version(installed) < version(record["target"])):
            raise cd.ClaudeDesktopError(BLOCKED)
        yield


def staged_update(app, *, cache=None, app_factory=cd.ClaudeDesktopApp):
    cache = cache or Path.home() / "Library" / "Caches" / SHIPIT_ID
    state = cache / "ShipItState.plist"  # Squirrel writes JSON despite the suffix.
    try:
        if cache.is_symlink() or state.is_symlink() or state.stat().st_size > 32_000:
            raise ValueError
        data = json.loads(state.read_text())
        if not isinstance(data, dict) or data.get("bundleIdentifier") != cd.BUNDLE_ID:
            raise ValueError

        def local_url(raw):
            if not isinstance(raw, str):
                raise ValueError
            url = urlsplit(raw)
            if url.scheme != "file" or url.netloc or url.query or url.fragment:
                raise ValueError
            return Path(unquote(url.path))

        target = local_url(data["targetBundleURL"])
        staged = local_url(data["updateBundleURL"])
        if (target != app.path or staged.name != "Claude.app"
                or staged.parent.parent != cache or not staged.parent.name.startswith("update.")
                or staged.parent.is_symlink() or staged.is_symlink()):
            raise ValueError
        # No executable from the state file is run. Verify the downloaded app
        # with the same Anthropic signing requirement as the installed one.
        info = app_factory(staged).validate()
        version(info["version"])
        return info["version"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise cd.ClaudeDesktopError("No ready Claude update was found. In Claude, check for updates and wait for the download, then choose Update Claude here.") from exc


class MacUpdateBackend:
    def __init__(self, app_factory=cd.ClaudeDesktopApp):
        self.app_factory = app_factory

    def installed(self):
        app = self.app_factory()
        info = app.validate()
        version(info["version"])
        return info["version"], app.stamp()

    def staged(self):
        return staged_update(self.app_factory())

    def installer_running(self):
        app = self.app_factory()
        command = (f"{app.path}/Contents/Frameworks/Squirrel.framework/Resources/ShipIt "
                   f"{SHIPIT_ID} {Path.home()}/Library/Caches/{SHIPIT_ID}/ShipItState.plist")
        return any(len(parts) == 2 and parts[0].isdigit() and parts[1] == command
                   for line in app.processes().splitlines() if (parts := line.strip().split(None, 1)))

    def snapshot(self, store):
        from AppKit import NSRunningApplication

        app = self.app_factory()
        processes = app.processes()
        profiles = store.list(include_archived=True)
        by_pid = {}
        for profile in profiles:
            desktop, code = store.paths(profile)
            pid = app.running(app.launch_argv(desktop, code), desktop, processes=processes)
            if pid is not None:
                by_pid[pid] = profile
        targets = {}
        for native in NSRunningApplication.runningApplicationsWithBundleIdentifier_(cd.BUNDLE_ID):
            if native.isTerminated():
                continue
            pid = native.processIdentifier()
            profile = by_pid.get(pid)
            if (native.executableURL() is None or native.executableURL().path() != str(app.binary)
                    or native.bundleURL() is None or native.bundleURL().path() != str(app.path)
                    or profile is None or profile.archived):
                raise cd.ClaudeDesktopError("Close Claude instances outside the active OpenSwap profiles, including archived profiles, then try the update again.")
            targets[profile.id] = native
        if set(by_pid) != {native.processIdentifier() for native in targets.values()}:
            raise cd.ClaudeDesktopError("Claude is still starting or closing. Wait a moment and try the update again.")
        return targets

    def quit(self, native):
        # Retain the original NSRunningApplication object, never refetch by PID.
        return native.isTerminated() or bool(native.terminate())

    def exited(self, targets):
        return all(native.isTerminated() for native in targets.values())

    def claude_running(self):
        from AppKit import NSRunningApplication
        return any(not app.isTerminated() for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(cd.BUNDLE_ID))

    def docked(self, store, identifiers):
        """The profiles among ``identifiers`` whose Dock helper is running."""
        from AppKit import NSRunningApplication
        from openswap.claude_desktop_dock import DockShortcut

        return [identifier for identifier in identifiers
                if any(not app.isTerminated() for app in NSRunningApplication.runningApplicationsWithBundleIdentifier_(
                    DockShortcut(Path("unused.app"), identifier, store.root).bundle_id))]

    def restore(self, store, identifier):
        return store.open(identifier, app=self.app_factory())

    def attach(self, store, identifier):
        from openswap.claude_desktop_presence import attach_presence
        return attach_presence(store, identifier)


@dataclass
class UpdatePlan:
    before: str
    target: str
    stamp: tuple
    targets: dict
    names: list[str]
    recovery_id: str | None
    installed: bool


class DesktopUpdate:
    def __init__(self, store, *, backend=None, clock=time.monotonic, sleep=time.sleep):
        self.store = store
        self.backend = backend or MacUpdateBackend()
        self.journal = UpdateJournal()
        self.clock, self.sleep = clock, sleep

    def pending(self):
        return self.journal.read() is not None

    def _owned(self, record):
        if record and record["root"] != str(self.store.root):
            raise cd.ClaudeDesktopError("Resume Claude's update from the OpenSwap profile store that started it.")
        return record

    @contextmanager
    def _worker(self):
        # Only one coordinator (release, DEV or a recovered process) may act.
        lock = self.journal.storage.lock("desktop-update-worker.lock", timeout=0.1)
        if not lock.acquire():
            raise cd.ClaudeDesktopError("OpenSwap is already working on Claude's update. Wait for it to finish, then try again.")
        try:
            yield
        finally:
            lock.release()

    def plan(self):
        self.store.ensure_default()
        record = self._owned(self.journal.read())
        before, stamp = self.backend.installed()
        installed = bool(record and version(before) >= version(record["target"]))
        target = record["target"] if installed else self.backend.staged()
        if not installed:
            # Claude can replace a pending download while the user finishes
            # work. Confirm the newer version rather than strand recovery.
            if record and version(target) < version(record["target"]):
                raise cd.ClaudeDesktopError("Claude's downloaded update is older than the one being installed. Open Claude's update check, then retry here. The saved profiles are kept.")
            if version(target) <= version(before):
                raise cd.ClaudeDesktopError("Claude is already on this update. Check for a newer update in Claude.")
            if not self.backend.installer_running():
                raise cd.ClaudeDesktopError("OpenSwap could not find Claude's installer waiting for this update. In Claude, check for updates again, then retry here. The saved profiles are kept.")
        targets = {} if installed else self.backend.snapshot(self.store)
        identifiers = record["profiles"] if installed else list(targets)
        rows = {p.id: p for p in self.store.list()}
        return UpdatePlan(before, target, stamp, targets,
                          [rows[p].name for p in identifiers if p in rows],
                          record["id"] if record else None, installed)

    def execute(self, plan, *, progress=lambda _: None, enabled=lambda: True):
        with self._worker():
            # Signature checks run before taking the launch gate.
            if self.backend.installed() != (plan.before, plan.stamp):
                raise cd.ClaudeDesktopError("Claude changed while you reviewed the update. Try again.")
            changed = cd.ClaudeDesktopError("The open profiles or downloaded update changed. Review the update again.")
            if not plan.installed and self.backend.staged() != plan.target:
                raise changed
            with self.journal.lock():
                record = self._owned(self.journal.read())
                if (record["id"] if record else None) != plan.recovery_id:
                    raise cd.ClaudeDesktopError("Claude's update changed. Review it again before continuing.")
                if not enabled():
                    raise cd.ClaudeDesktopError("Claude update paused. Enable Desktop features and choose Resume update.")
                if not plan.installed:
                    current = self.backend.snapshot(self.store)
                    identity = lambda targets: {p: n.processIdentifier() for p, n in targets.items()}
                    if (identity(current) != identity(plan.targets)
                            or any(n.isTerminated() for n in plan.targets.values())
                            or not self.backend.installer_running()):
                        raise changed
                docked = [] if plan.installed else self.backend.docked(self.store, list(plan.targets))
                if record is None:
                    record = {"schema": 1, "id": uuid.uuid4().hex, "root": str(self.store.root),
                              "before": plan.before, "target": plan.target,
                              "profiles": list(plan.targets), "docked": docked, "phase": "waiting"}
                elif not plan.installed:
                    record["profiles"] = list(dict.fromkeys([*record["profiles"], *plan.targets]))
                    record["docked"] = list(dict.fromkeys([*record["docked"], *docked]))
                    record["target"] = plan.target
                # Durable before the first quit request, even if it is refused.
                self.journal.write(record)

            if not plan.installed:
                progress("Closing Claude profiles…")
                for native in plan.targets.values():
                    if not enabled() or not self.backend.quit(native):
                        raise cd.ClaudeDesktopError("Claude did not finish quitting. Finish your work, then choose Resume update. No profile was forced to close.")
                progress("Waiting for Claude to install its update…")
                deadline = self.clock() + WAIT_SECONDS
                while True:
                    if not enabled():
                        raise cd.ClaudeDesktopError("Claude update paused. Choose Resume update when ready.")
                    if self.backend.exited(plan.targets):
                        try:
                            installed, stamp = self.backend.installed()
                            if version(installed) >= version(plan.target):
                                break
                        except (cd.ClaudeDesktopError, OSError):
                            pass  # bundle replacement can briefly remove app files
                    if self.clock() >= deadline:
                        raise cd.ClaudeDesktopError("Claude has not finished updating. Check any open Claude windows or quit prompts, then choose Resume update. Profiles remain saved.")
                    self.sleep(1)

            installed, stamp = self.backend.installed()
            if version(installed) < version(plan.target):
                raise cd.ClaudeDesktopError("Claude's update is not installed yet. Choose Resume update.")
            self.sleep(2)
            if self.backend.installed() != (installed, stamp):
                raise cd.ClaudeDesktopError("Claude is still changing. Choose Resume update in a moment.")
            # Never redirect an old numbered helper to a new session.
            stale = self._old_helpers(record["profiles"]) if record["phase"] == "waiting" else set()

            progress("Restoring Claude profiles and Dock icons…")
            with self.journal.lock():
                record["phase"] = "restoring"
                self.journal.write(record)
            restored = []
            for identifier in list(record["profiles"]):
                if not enabled():
                    raise cd.ClaudeDesktopError("Profile restoration paused. Choose Resume update when ready.")
                # An archive during installation must not reopen a profile.
                if identifier in {p.id for p in self.store.list()}:
                    result = self.backend.restore(self.store, identifier)
                    if result.get("state") not in {"launched", "already_running"} or not result.get("pid"):
                        raise cd.ClaudeDesktopError("A Claude profile is still opening. Wait a moment, then choose Resume update to finish restoring it.")
                    restored.append(identifier)
                with self.journal.lock():
                    record["profiles"].remove(identifier)
                    self.journal.write(record)
            with self.journal.lock():
                self.journal.clear()
            return {"state": "update_done", "version": installed,
                    "dock_warnings": self._reattach(restored, record["docked"], stale)}

    def cancel(self):
        """Stop coordinating and reopen the closed profiles on whatever Claude is installed."""
        with self._worker():
            with self.journal.lock():
                try:
                    record = self._owned(self.journal.read())
                except UnreadableJournal:
                    # The saved profile list is lost: unblock launches but reopen nothing.
                    record = {"phase": "waiting", "profiles": [], "docked": [], "unreadable": True}
                if record is None:
                    return {"state": "update_canceled", "failed": [], "dock_warnings": []}
                if record["phase"] == "waiting" and self.backend.installer_running() and not self.backend.claude_running():
                    raise cd.ClaudeDesktopError("Claude is installing its update. Wait a moment, then try again.")
                self.journal.clear()
            rows = {p.id: p for p in self.store.list()}
            launched, failed = [], []
            for identifier in record["profiles"]:
                if identifier not in rows:
                    continue
                try:
                    if self.backend.restore(self.store, identifier).get("state") == "launched":
                        launched.append(identifier)
                except (cd.ClaudeSwitchError, OSError):
                    failed.append(rows[identifier].name)
            return {"state": "update_canceled", "failed": failed, "unreadable": record.get("unreadable", False),
                    "dock_warnings": self._reattach(launched, record["docked"], self._old_helpers(launched))}

    def _old_helpers(self, identifiers):
        """Profiles whose previous Dock helper is still running after a short wait."""
        deadline = self.clock() + 10
        while (running := set(self.backend.docked(self.store, identifiers))) and self.clock() < deadline:
            self.sleep(1)
        return running

    def _reattach(self, identifiers, docked, stale):
        """Names of profiles that had a Dock indicator before the update and lack one now."""
        names = {p.id: p.name for p in self.store.list(include_archived=True)}
        missing = []
        for identifier in identifiers:
            try:
                attached = identifier not in stale and self.backend.attach(self.store, identifier)
            except (cd.ClaudeSwitchError, OSError):
                attached = False
            if identifier in docked and not attached:
                missing.append(names[identifier])
        return missing
