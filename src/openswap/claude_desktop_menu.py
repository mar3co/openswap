"""Claude Desktop profile menu. All UI calls stay on the main thread."""

from __future__ import annotations

import queue
import threading
from dataclasses import asdict
from datetime import datetime

from openswap.claude_desktop import BADGE_COLORS, BUNDLE_ID, ClaudeDesktopApp, ClaudeDesktopError, DesktopProfiles, account_label, validate_badge
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
                 blocked=None, shortcut_factory=None, reveal=None, confirm=None, detector=None):
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
        self.confirm = confirm
        self.detector = detector
        self.operation = "Opening Claude…"
        self.busy = False
        self._results = queue.SimpleQueue()
        self._checks = queue.SimpleQueue()
        self._app_factory = app_factory or ClaudeDesktopApp
        self.checking = False
        self._app_message = "Not checked yet. Install Claude.app in Applications."
        self._profile_states = {}
        self._profile_checked_at = {}
        self._checked_at = None
        self._check_generation = 0

    def suspend_checks(self):
        self._check_generation += 1
        self.checking = False

    def settings_state(self):
        state = {"checking": self.checking, "busy": self.busy or self.blocked(),
                 "operation": self.operation if self.busy else "Waiting for app switch…",
                 "app_message": "Checking Claude Desktop…" if self.checking else self._app_message}
        try:
            profiles = self.store.list(include_archived=True)
            state.update(profiles=sum(not p.archived for p in profiles),
                         archived=sum(p.archived for p in profiles),
                         items=[{**asdict(p), "status": self._profile_states.get(p.id, ""),
                                 "checked_at": self._profile_checked_at.get(p.id)} for p in profiles])
        except (ClaudeSwitchError, OSError):
            state["storage_error"] = "Could not read profiles. Check folder permissions or view details."
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
        menu.add(rumps.MenuItem("Separate sign-ins", callback=None))
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
            item.add(rumps.MenuItem("Account details…", callback=lambda _, p=profile: self.details(p)))
            item.add(rumps.MenuItem("Archive…", callback=lambda _, p=profile: self.archive(p)))
            item.add(rumps.MenuItem("Create shortcut…", callback=lambda _, p=profile: self.shortcut(p)))
            self._add_status(item, profile, rumps)
            menu.add(item)
        if any(p.archived for p in profiles):
            menu.add(archived)
        return menu

    def _add_status(self, item, profile, rumps):
        state = self._profile_states.get(profile.id)
        checked_at = self._profile_checked_at.get(profile.id)
        label = f"{state} at {checked_at}" if state and checked_at else "Running status not checked"
        item.add(rumps.MenuItem(label, callback=None))
        item.add(rumps.MenuItem("Sign-in: Check inside Claude", callback=None))
        if profile.email or profile.organization:
            item.add(rumps.MenuItem(" · ".join(x for x in (profile.email, profile.organization) if x), callback=None))
            item.add(rumps.MenuItem("Last detected details" if profile.detected_at else "Your account labels", callback=None))

    def storage_error(self, _=None):
        try:
            self.store.list(include_archived=True)
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)

    def _error(self, exc):
        self.show_error(str(exc) if isinstance(exc, ClaudeSwitchError)
                        else "Could not access Claude Desktop profiles. Check folder permissions and retry.")

    def perform(self, action, identifier=None):
        """Resolve the current profile by stable ID for inline settings actions."""
        if self.busy or self.blocked() or not self.enabled():
            return
        if action == "add":
            return self.add()
        if action == "storage_error":
            return self.storage_error()
        if action not in {"open", "rename", "archive", "restore", "shortcut", "details"}:
            return
        try:
            profile = self.store.get(identifier, include_archived=action == "restore")
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)
            return
        if action == "open":
            self.open(profile.id)
        elif action == "restore":
            self.edit(profile, archived=False)
        else:
            getattr(self, action)(profile)

    def archive(self, profile):
        if self.busy or self.blocked() or not self.enabled() or self.confirm is None:
            return
        if self.confirm(title=f"Archive {profile.name}?",
                        message="Keeps sign-ins, data and open windows. Shortcuts are disabled until you restore this profile.",
                        ok="Archive", cancel="Cancel") == 1:
            self.edit(profile, archived=True)

    def add(self, _=None):
        if self.busy or self.blocked() or not self.enabled():
            return
        response = self.prompt(
            title="Add Claude Desktop profile",
            message="Name this profile, such as Personal or Work.\n\nSign in and check your account inside the new Claude window. Claude Code switching stays separate.",
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

    def details(self, profile, *, draft=None, note=""):
        if self.busy or self.blocked() or not self.enabled():
            return
        values = draft or {"email": profile.email, "organization": profile.organization,
                           "detected_at": profile.detected_at}
        while True:
            message = note or "Optional labels. Detect uses this profile's saved sign-in; macOS may ask for Keychain access."
            if values.get("detected_at"):
                when = datetime.fromisoformat(values["detected_at"]).astimezone().strftime("%b %d, %H:%M")
                message += f"\nLast detected {when}. The account may have changed."
            response = self.prompt(title=f"Account details · {profile.name}", message=message,
                                   fields=[("Email (optional)", values["email"]),
                                           ("Organization (optional)", values["organization"])],
                                   ok="Save", cancel="Cancel", other="Detect")
            if response.clicked not in (1, -1):
                return
            if self.busy or self.blocked() or not self.enabled():
                return
            email, organization = response.values
            detected_at = values.get("detected_at", "") if (email, organization) == (values["email"], values["organization"]) else ""
            values = {"email": email, "organization": organization, "detected_at": detected_at}
            if response.clicked == -1:
                self._detect(profile.id, values)
                return
            try:
                values["email"] = account_label(email, email=True)
                values["organization"] = account_label(organization)
            except ClaudeSwitchError as exc:
                note = str(exc)
                continue
            try:
                # Re-resolve: external CLI edits may archive the profile while
                # a modal is open. Labels never unarchive or rename it.
                self.store.get(profile.id)
                self.store.update(profile.id, **values)
            except (ClaudeSwitchError, OSError) as exc:
                self._error(exc)
                return
            self.changed()
            return

    def _detect(self, identifier, draft):
        if self.busy or self.blocked() or not self.enabled():
            return
        from openswap.claude_desktop_identity import DetectionError, detect_account
        self.busy = True
        self.operation = "Detecting account…"
        self.changed()

        def work():
            result = {"state": "account_details", "identifier": identifier, "draft": draft}
            try:
                result["draft"] = (self.detector or detect_account)(self.store, identifier)
            except DetectionError as exc:
                result["note"] = str(exc)
            except Exception:
                result["note"] = "Could not detect the account. Sign in inside this profile and allow Keychain access, or enter the details yourself."
            self._results.put((result, None))

        try:
            threading.Thread(target=work, daemon=True, name="claude-desktop-identity").start()
        except Exception:
            self.busy = False
            self.changed()
            self.show_error("Could not start account detection. Try again.")

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
                self._profile_checked_at = dict.fromkeys(states, checked_at)
                self.checking = False
                self.changed()
        try:
            result, error = self._results.get_nowait()
        except queue.Empty:
            return
        self.busy = False
        if error is None and result.get("pid") and result.get("profile", {}).get("id"):
            self._profile_states[result["profile"]["id"]] = "Running"
            self._profile_checked_at[result["profile"]["id"]] = datetime.now().strftime("%b %d, %H:%M:%S")
        self.changed()
        if error is not None:
            self._error(error)
            return
        if result.get("state") == "account_details":
            if not self.enabled() or self.blocked():
                return
            try:
                profile = self.store.get(result["identifier"])
            except (ClaudeSwitchError, OSError):
                return
            self.details(profile, draft=result["draft"], note=result.get("note", ""))
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
