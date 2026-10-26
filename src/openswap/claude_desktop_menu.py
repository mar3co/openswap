"""Claude Desktop profile menu. All UI calls stay on the main thread."""

from __future__ import annotations

import queue
import threading
from datetime import datetime

from openswap.claude_desktop import BADGE_COLORS, BUNDLE_ID, ClaudeDesktopApp, ClaudeDesktopError, DesktopProfiles, validate_badge
from openswap.exceptions import ClaudeSwitchError


def focus_profile(pid: int) -> None:
    # Focus the specific process, not the bundle ID (shared by all profiles).
    # This does not need Accessibility or read anything from the window.
    import AppKit

    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is not None and app.bundleIdentifier() == BUNDLE_ID:
        app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)


def reveal_shortcut(path: str) -> None:
    import AppKit
    from Foundation import NSURL
    AppKit.NSWorkspace.sharedWorkspace().activateFileViewerSelectingURLs_([NSURL.fileURLWithPath_(path)])


def has_saved_profiles() -> bool:
    """Upgrade hint only; do not inspect Claude's data or authenticate a profile."""
    return bool(DesktopProfiles().list(include_archived=True))


class ClaudeDesktopMenu:
    def __init__(self, *, prompt, show_error, changed, notify, store=None, app_factory=None, show_status=None, enabled=None,
                 blocked=None, shortcut_factory=None, reveal=None):
        self.store = store or DesktopProfiles()
        self.prompt = prompt
        self.show_error = show_error
        self.changed = changed
        self.notify = notify
        self.show_status = show_status or show_error
        self.enabled = enabled or (lambda: True)
        self.blocked = blocked or (lambda: False)
        self.shortcut_factory = shortcut_factory
        self.reveal = reveal or reveal_shortcut
        self.operation = "Opening Claude…"
        self.busy = False
        self._results = queue.SimpleQueue()
        self._checks = queue.SimpleQueue()
        self._app_factory = app_factory or ClaudeDesktopApp
        self.checking = False
        self._app_message = "Not checked yet. Install Claude.app in Applications."
        self._profile_states = {}
        self._checked_at = None
        self._check_generation = 0

    def suspend_checks(self):
        self._check_generation += 1
        self.checking = False

    def settings_state(self):
        state = {"checking": self.checking, "busy": self.busy or self.blocked(),
                 "app_message": "Checking Claude Desktop…" if self.checking else self._app_message}
        try:
            profiles = self.store.list(include_archived=True)
            state.update(profiles=sum(not p.archived for p in profiles),
                         archived=sum(p.archived for p in profiles))
        except (ClaudeSwitchError, OSError):
            state["storage_error"] = "Profile storage needs attention. Open Manage profiles for details."
        return state

    def refresh_status(self, _=None):
        if self.checking or not self.enabled():
            return
        self._check_generation += 1
        generation = self._check_generation
        self.checking = True
        self.changed()

        def check():
            try:
                app = self._app_factory()
                info = app.validate()
                message = f"Claude {info['version']} · Publisher verified."
            except Exception as exc:
                message = str(exc) if isinstance(exc, ClaudeSwitchError) else "Could not check Claude Desktop. Try again."
            try:
                states = app.profile_states(self.store)
            except Exception:
                states = {}
            self._checks.put((generation, message, states, datetime.now().strftime("%b %d, %H:%M:%S")))

        try:
            threading.Thread(target=check, daemon=True, name="claude-desktop-check").start()
        except Exception:
            self._checks.put((generation, "Could not check Claude Desktop. Try again.", {}, None))

    def _notify(self, title, body):
        try:
            self.notify(title, body)
        except Exception:
            pass  # notification permissions cannot turn a completed action into a failure

    def menu(self, rumps):
        menu = rumps.MenuItem("Claude Desktop profiles")
        menu.add(rumps.MenuItem("Experimental · Separate sign-ins", callback=None))
        menu.add(rumps.MenuItem("Checking status…" if self.checking else "Refresh status",
                                callback=None if self.checking else self.refresh_status))
        if self._checked_at:
            menu.add(rumps.MenuItem("App check details…", callback=lambda _: self.show_status(self._app_message)))
        if self.busy or self.blocked():
            menu.add(rumps.MenuItem(self.operation, callback=None))
            return menu
        menu.add(rumps.MenuItem("Add profile…", callback=self.add))
        try:
            profiles = self.store.list(include_archived=True)
        except (ClaudeSwitchError, OSError):
            menu.add(rumps.MenuItem("Profile storage needs attention…", callback=self.storage_error))
            return menu
        archived = rumps.MenuItem("Archived profiles")
        for profile in profiles:
            # IDs avoid a renamed profile being mistaken for another account.
            if profile.archived:
                item = rumps.MenuItem(f"Profile: {profile.name}")
                item.add(rumps.MenuItem("Restore profile", callback=lambda _, p=profile: self.edit(p, archived=False)))
                self._add_status(item, profile, rumps)
                archived.add(item)
                continue
            # rumps keys siblings by title; a name such as 'Add profile…'
            # must not overwrite a management action in its menu dictionary.
            item = rumps.MenuItem(f"Profile: {profile.name}")
            item.add(rumps.MenuItem("Open profile",
                                   callback=lambda _, p=profile: self.open(p.id)))
            item.add(rumps.MenuItem("Rename…", callback=lambda _, p=profile: self.rename(p)))
            item.add(rumps.MenuItem("Archive (keep data)", callback=lambda _, p=profile: self.edit(p, archived=True)))
            item.add(rumps.MenuItem("Create shortcut…", callback=lambda _, p=profile: self.shortcut(p)))
            self._add_status(item, profile, rumps)
            menu.add(item)
        if any(p.archived for p in profiles):
            menu.add(archived)
        return menu

    def _add_status(self, item, profile, rumps):
        state = self._profile_states.get(profile.id)
        label = f"{state} at {self._checked_at}" if state and self._checked_at else "Running status not checked"
        item.add(rumps.MenuItem(label, callback=None))
        item.add(rumps.MenuItem("Sign-in: Check inside Claude", callback=None))

    def storage_error(self, _=None):
        try:
            self.store.list(include_archived=True)
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)

    def _error(self, exc):
        self.show_error(str(exc) if isinstance(exc, ClaudeSwitchError)
                        else "Could not access Claude Desktop profiles. Check folder permissions and retry.")

    def add(self, _=None):
        if self.busy or self.blocked() or not self.enabled():
            return
        response = self.prompt(
            title="Add Claude Desktop profile",
            message="Name this profile, such as Personal or Work.\n\nSign in inside Claude Desktop when it opens. Each profile keeps its own sign-in. Your terminal and editor Claude Code login stays unchanged.",
            default_text="", ok="Create and open", cancel="Cancel",
        )
        if response.clicked != 1:
            return
        try:
            profile = self.store.add(response.text)
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)
            return
        self.open(profile.id)

    def rename(self, profile):
        if self.busy or self.blocked() or not self.enabled():
            return
        response = self.prompt(title="Rename Claude Desktop profile", message="Choose a name for this profile.",
                               default_text=profile.name, ok="Save", cancel="Cancel")
        if response.clicked == 1:
            self.edit(profile, name=response.text)

    def edit(self, profile, **changes):
        if self.busy or self.blocked() or not self.enabled():
            return
        try:
            updated = self.store.update(profile.id, **changes)
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)
            return
        self.changed()
        if "archived" in changes:
            self._notify("Profile archived" if updated.archived else "Profile restored",
                        f"{updated.name}. "
                        + ("Sign-in, work and open windows kept. Restore to use its shortcuts again."
                           if updated.archived else "Ready to open. Restoring does not launch Claude Desktop."))

    def open(self, identifier):
        if self.busy or self.blocked() or not self.enabled():
            return
        self.operation = "Opening Claude…"
        self.busy = True
        self.changed()

        def work():
            try:
                self._results.put((self.store.open(identifier), None))
            except Exception as exc:
                self._results.put((None, exc))

        try:
            threading.Thread(target=work, daemon=True, name="claude-desktop-open").start()
        except Exception as exc:
            self._results.put((None, exc))

    def shortcut(self, profile):
        if self.busy or self.blocked() or not self.enabled():
            return
        from openswap.claude_desktop_shortcut import ICON_NOTICE, create_shortcut
        response = self.prompt(title="Shortcut icon", message="Choose a number from 1–999, or leave blank for the standard icon.\n\n" + ICON_NOTICE,
                               default_text=profile.badge, ok="Continue", cancel="Cancel")
        if response.clicked != 1:
            return
        badge, color = response.text.strip(), profile.color
        try:
            validate_badge(badge, color)
        except ClaudeDesktopError as exc:
            self._error(exc)
            return
        if badge:
            colors = list(BADGE_COLORS)
            response = self.prompt(title="Badge color", message="Choose a badge color. The shortcut will appear in Finder; drag it to your Dock.",
                choices=[c.title() for c in colors], selected=colors.index(color), ok="Create shortcut", cancel="Cancel")
            if response.clicked != 1 or not 0 <= response.choice < len(colors):
                return
            color = colors[response.choice]
        if self.busy or self.blocked() or not self.enabled():
            return
        self.busy = True
        self.operation = "Creating shortcut…"
        self.changed()
        def work():
            try:
                result = (self.shortcut_factory or create_shortcut)(self.store, profile.id, badge=badge, color=color)
                self._results.put((result, None))
            except Exception as exc:
                self._results.put((None, exc))
        try:
            threading.Thread(target=work, daemon=True, name="claude-desktop-shortcut").start()
        except Exception as exc:
            self._results.put((None, exc))

    def poll(self):
        try:
            generation, message, states, checked_at = self._checks.get_nowait()
        except queue.Empty:
            pass
        else:
            if generation == self._check_generation and self.enabled():
                self._app_message, self._profile_states, self._checked_at = message, states, checked_at
                self.checking = False
                self.changed()
        try:
            result, error = self._results.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        self.changed()
        if error is not None:
            self._error(error)
            return
        if result.get("state") == "shortcut_created":
            try:
                self.reveal(result["path"])
            except Exception:
                pass
            self.show_status(f"Shortcut created: {result['path']}\n\nDrag it from Finder to your Dock.\n\n{result['notice']}")
            return
        try:
            focus_profile(result["pid"])
        except Exception:
            pass  # opening succeeded; focus failure is not a login failure
        self._notify("Claude profile opened", f"{result['profile']['name']} · Check the account in Claude.")
