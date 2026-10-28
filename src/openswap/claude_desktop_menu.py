"""Claude Desktop profile menu. All UI calls stay on the main thread."""

from __future__ import annotations

import queue
import threading
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime

from openswap.claude_desktop import BADGE_COLORS, BUNDLE_ID, ClaudeDesktopApp, ClaudeDesktopError, DesktopProfiles, account_label, validate_badge
from openswap.exceptions import ClaudeSwitchError
from openswap.claude_desktop_monitor import ProfileIdentityMonitor


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
                 blocked=None, shortcut_factory=None, reveal=None, confirm=None, detector=None, monitor=None,
                 code_engine=None, code_connector=None, code_login_allowed=None, signin_sender=None, shortcut_setup=None,
                 guide_seen=None, mark_guide_seen=None):
        self.store = store or DesktopProfiles()
        self.prompt = prompt
        self.show_error = show_error
        self.changed = changed
        self.notify = notify
        self.show_status = show_status or show_error
        self.enabled = enabled or (lambda: True)
        self.blocked = blocked or (lambda: False)
        self.shortcut_factory = shortcut_factory
        self.shortcut_setup = shortcut_setup
        self.guide_seen = guide_seen or (lambda: False)
        self.mark_guide_seen = mark_guide_seen or (lambda: None)
        self.reveal = reveal or reveal_shortcut
        self.confirm = confirm
        self.detector = detector
        self.monitor = monitor or ProfileIdentityMonitor(self.store)
        self.code = code_connector
        if self.code is None and code_engine is not None:
            from openswap.claude_desktop_code import DesktopCodeConnector
            from openswap.menubar_display import resolve_claude_bin
            self.code = DesktopCodeConnector(self.store, code_engine, resolve_binary=resolve_claude_bin, notify=self._notify)
        self.code_login_allowed = code_login_allowed or (lambda: True)
        self.signin_sender = signin_sender
        self.signin_links = None
        # The callback under discovery and its freshness check; never queued.
        self._signin = None
        self._editing_details = False
        self._guide_shown = False
        self._default_error = ""
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
        self._signin = None
        self.monitor.suspend()
        if self.code:
            self.code.suspend()
        if self.signin_links:
            self.signin_links.restore()

    @contextmanager
    def _modal(self):
        self._editing_details = True
        try:
            yield
        finally:
            self._editing_details = False

    def _operation_label(self):
        return self.operation if self.busy else "Waiting for app switch…"

    def _profiles(self):
        rows = self.store.list(include_archived=True)
        self._default_error = ""
        if self.enabled() and not any(p.kind == "default" for p in rows):
            # Registering the ordinary profile is the one write a view can
            # cause; while the registry lists it, views only read.
            try:
                if self.store.ensure_default() is not None:
                    rows = self.store.list(include_archived=True)
            except (ClaudeSwitchError, OSError):
                # Discovery cannot prevent managing separate profiles.
                self._default_error = "Could not show the existing Claude profile. Check its folder permissions."
        return sorted(rows, key=lambda p: p.kind != "default")

    def settings_state(self):
        state = {"checking": self.checking, "busy": self.busy or self.blocked(),
                 "operation": self._operation_label(),
                 "app_message": "Checking Claude Desktop…" if self.checking else self._app_message}
        try:
            profiles = self._profiles()
            if self._default_error:
                state["default_error"] = self._default_error
            state.update(profiles=sum(not p.archived for p in profiles),
                         archived=sum(p.archived for p in profiles),
                         items=[{**asdict(p), "status": self._profile_states.get(p.id, ""),
                                 "checked_at": self._profile_checked_at.get(p.id),
                                 **({"account_status": self.monitor.statuses[p.id]} if self.monitor.statuses.get(p.id) else {}),
                                 **(self.code.state(p) if self.code else {})}
                                for p in profiles])
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
            app, validated, states = None, False, {}
            try:
                app = self._app_factory()
                info = app.validate()
                validated = True
                message = f"Claude {info['version']} · Publisher verified."
            except Exception as exc:
                message = str(exc) if isinstance(exc, ClaudeSwitchError) else "Could not check Claude Desktop. Try again."
            if app is not None:
                try:
                    states = app.profile_states(self.store)
                    from openswap.claude_desktop_presence import attach_presence
                    for profile in self.store.list():
                        if validated and generation == self._check_generation and self.enabled() and states.get(profile.id) == "Running":
                            try:
                                attach_presence(self.store, profile.id)
                            except (ClaudeSwitchError, OSError):
                                pass  # passive refresh must not interrupt work with dialogs
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
            menu.add(rumps.MenuItem(self._operation_label(), callback=None))
            return menu
        menu.add(rumps.MenuItem("Add profile…", callback=self.add))
        try:
            profiles = self._profiles()
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
            item.add(rumps.MenuItem("Reconnect sign-in…", callback=lambda _, p=profile: self.reconnect_signin(p)))
            if self.code:
                action = self.code.state(profile).get("code_action")
                if action:
                    item.add(rumps.MenuItem("Cancel Code sign-in" if action == "cancel_code" else "Continue Code setup…",
                                            callback=lambda _, p=profile, a=action: self.perform(a, p.id)))
            item.add(rumps.MenuItem("Archive…", callback=lambda _, p=profile: self.archive(p)))
            item.add(rumps.MenuItem("Create shortcut…", callback=lambda _, p=profile: self.shortcut(p)))
            item.add(rumps.MenuItem("Set up Dock shortcuts", callback=lambda _, p=profile: self.setup_dock(p)))
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
        if action not in {"open", "rename", "archive", "restore", "shortcut", "setup_dock", "details", "reconnect_signin", "connect_code", "cancel_code"}:
            return
        try:
            profile = self.store.get(identifier, include_archived=action == "restore")
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)
            return
        if action in {"connect_code", "cancel_code"}:
            if self.code:
                try:
                    (self.code.retry if action == "connect_code" else self.code.cancel)(profile.id)
                except (ClaudeSwitchError, OSError) as exc:
                    self._error(exc)
                self.changed()
        elif action == "open":
            self.open(profile.id)
        elif action == "restore":
            self.edit(profile, archived=False)
        else:
            getattr(self, action)(profile)

    def archive(self, profile):
        if self.busy or self.blocked() or not self.enabled() or self.confirm is None:
            return
        with self._modal():
            confirmed = self.confirm(title=f"Archive {profile.name}?",
                                     message="Keeps sign-ins, data and open windows. Shortcuts are disabled until you restore this profile.",
                                     ok="Archive", cancel="Cancel") == 1
        if confirmed:
            self.edit(profile, archived=True)

    def show_guide(self, *, for_creation=False):
        if self.busy or self.blocked() or not self.enabled() or self._editing_details:
            return False
        from openswap.claude_desktop_guide import illustrations

        with self._modal():
            response = self.prompt(
                title="Your Claude profiles",
                message="Your current sign-in appears as Default. Add a profile for another sign-in. Your Claude icon stays in place.",
                illustrations=illustrations(),
                default_text=None,
                ok="Continue" if for_creation else "Done",
                cancel="Cancel" if for_creation else None,
            )
            if response.clicked != 1:
                return False
            self.mark_guide_seen()
            # Also remember within this run if saving is unavailable.
            self._guide_shown = True
            return True

    def add(self, _=None):
        if self.busy or self.blocked() or not self.enabled() or self._editing_details:
            return
        if not (self.guide_seen() or self._guide_shown):
            if not self.show_guide(for_creation=True):
                return
        if self.busy or self.blocked() or not self.enabled():
            return
        with self._modal():
            response = self.prompt(
                title="Create Claude Desktop profile",
                message="Sign in to the new Claude window. Allow OpenSwap to open Claude links if asked.\n\nKeychain access lets OpenSwap read the account and add it to Claude Code. Finish browser sign-in if asked.",
                input_label="Profile name", placeholder="e.g. Work",
                checkbox="Add profile icon to Dock", checkbox_checked=True,
                default_text="", ok="Create and open", cancel="Cancel",
            )
        if response.clicked != 1:
            return
        if self.busy or self.blocked() or not self.enabled():
            return
        try:
            profile = self.store.add(response.text)
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)
            return
        self.open(profile.id, setup_shortcuts=response.checked)

    def rename(self, profile):
        if self.busy or self.blocked() or not self.enabled():
            return
        with self._modal():
            response = self.prompt(title="Rename Claude Desktop profile", message="Choose a name for this profile.",
                                   default_text=profile.name, ok="Save", cancel="Cancel")
        if response.clicked == 1:
            self.edit(profile, name=response.text)

    def reconnect_signin(self, profile):
        if self.busy or self.blocked() or not self.enabled():
            return
        if self.signin_links is None:
            self.show_error("Automatic sign-in needs the OpenSwap app. Open the installed app and try again.")
            return
        try:
            self.signin_links.prepare(retry=True)
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)
            return
        self.open(profile.id)

    def receive_signin(self, value, fresh):
        """External email/fallback links have no trustworthy profile binding.

        Native web-auth returns bypass OpenSwap and reach the origin process.
        Only unbound links need this one-click choice; never select by recency,
        inferred email, or the number of running profiles.
        """
        if self.busy or self.blocked() or not self.enabled() or not fresh():
            return
        from openswap.claude_desktop_signin import callback_link
        try:
            value, kind = callback_link(value)
        except ClaudeDesktopError as exc:
            self._error(exc)
            return
        self.busy = True
        self.operation = "Checking open Claude profiles…"
        self.changed()
        self._signin = (value, fresh)

        def work():
            result = {"state": "signin_candidates", "connector": kind == "connector",
                      "candidates": [], "default_unchecked": False, "managed_unchecked": False}
            try:
                app = self._app_factory()
                app.validate()
                try:
                    self.store.ensure_default()
                except (ClaudeSwitchError, OSError):
                    result["default_unchecked"] = True
                profiles = sorted(self.store.list(), key=lambda p: p.kind != "default")
                processes = app.processes()
                for profile in profiles:
                    desktop, code = self.store.paths(profile)
                    try:
                        pid = app.running(app.launch_argv(desktop, code), desktop, processes=processes)
                    except ClaudeDesktopError:
                        result["default_unchecked" if profile.kind == "default" else "managed_unchecked"] = True
                        continue
                    if pid is not None:
                        result["candidates"].append((profile, pid))
            except (ClaudeSwitchError, OSError) as exc:
                self._results.put((None, exc))
                return
            except Exception:
                self._results.put((None, ClaudeDesktopError(
                    "Could not check the open Claude profiles. Start again from the intended Claude profile.")))
                return
            self._results.put((result, None))

        try:
            threading.Thread(target=work, daemon=True, name="claude-profile-discovery").start()
        except Exception:
            self.busy = False
            self._signin = None
            self.changed()
            self.show_error("Could not check the open Claude profiles. Try again.")

    def _choose_signin_profile(self, result, held):
        """One click picks the target; nothing is preselected unless a single candidate is certain."""
        from openswap.claude_desktop_signin import finish_signin
        stopped = self.blocked() or not self.enabled() or (self.signin_links is not None and self.signin_links.stopping)
        if held is None:
            if not stopped:
                self.show_error("Start again from the intended Claude profile.")
            return
        value, fresh = held
        connector, candidates = result["connector"], result["candidates"]
        if not fresh():
            self.show_error("This link expired. Start again from the intended Claude profile.")
            return
        if stopped:
            return
        unchecked = result["default_unchecked"] or result["managed_unchecked"]
        if not candidates:
            self.show_error("Could not check the open Claude profiles. Start again from the intended Claude profile."
                            if unchecked else "Open the intended Claude profile and start again there.")
            return
        # An unchecked profile may be the origin, so a lone candidate is not preselected then either.
        placeholder = ["Choose a profile…"] if len(candidates) > 1 or unchecked else []
        message = "Which Claude profile did you connect this app from?" if connector else "Which profile did you start signing in to?"
        if result["default_unchecked"]:
            message += "\n\nThe existing Claude profile could not be checked."
        if result["managed_unchecked"]:
            message += "\n\nSome open Claude profiles could not be checked."
        while True:
            with self._modal():
                response = self.prompt(title="Finish connecting app" if connector else "Finish Claude sign-in",
                                       message=message,
                                       choices=placeholder + [p.name for p, _ in candidates], selected=0,
                                       ok="Continue", cancel="Cancel")
            if response.clicked != 1:
                return
            choice = response.choice - len(placeholder)
            if 0 <= choice < len(candidates):
                break
            if not (placeholder and response.choice == 0):
                return
        if not fresh():
            self.show_error("This link expired. Start again from the intended Claude profile.")
            return
        if self.busy or self.blocked() or not self.enabled():
            return
        profile, pid = candidates[choice]
        self.busy = True
        self.operation = "Connecting app…" if connector else "Finishing sign-in…"
        self.changed()

        def work():
            try:
                if not fresh() or not self.enabled():
                    raise ClaudeDesktopError("Sign-in interrupted")
                result = finish_signin(self.store, profile.id, value, app=self._app_factory(),
                                       sender=self.signin_sender, expected_pid=pid)
                self._results.put((result, None))
            except Exception:
                # A callback is a credential. Keep native error details and the
                # callback out of UI errors, result queues and logs.
                self._results.put((None, ClaudeDesktopError(
                    "Could not connect the app. Start again from the intended Claude profile." if connector else
                    "Could not finish sign-in. Restart sign-in in the intended Claude profile.")))

        try:
            threading.Thread(target=work, daemon=True, name="claude-profile-signin").start()
        except Exception:
            self.busy = False
            self.changed()
            self.show_error("Could not connect the app. Try again." if connector else "Could not finish sign-in. Try again.")

    def details(self, profile, *, draft=None, note=""):
        if self.busy or self.blocked() or not self.enabled():
            return
        values = draft or {"email": profile.email, "organization": profile.organization,
                           "detected_at": profile.detected_at, "account_uuid": profile.account_uuid,
                           "organization_uuid": profile.organization_uuid, "detected_login": profile.detected_login}
        while True:
            message = note or ("Account detection is paused. Choose Detect to retry Keychain access, or enter the details yourself."
                               if self.monitor.detector.denied else
                               "Account details update after sign-in or account changes. OpenSwap may request Keychain access.")
            if values.get("detected_at"):
                when = datetime.fromisoformat(values["detected_at"]).astimezone().strftime("%b %d, %H:%M")
                message += f"\nLast detected {when}. The account may have changed."
            with self._modal():
                response = self.prompt(title=f"Account details · {profile.name}", message=message,
                                       fields=[("Email (optional)", values["email"]),
                                               ("Organization (optional)", values["organization"])],
                                       checkbox="Update account details automatically", checkbox_checked=values.get("auto_detect", profile.auto_detect),
                                       ok="Save", cancel="Cancel", other="Detect")
            if response.clicked not in (1, -1):
                return
            if self.busy or self.blocked() or not self.enabled():
                return
            email, organization = response.values
            detected_at = values.get("detected_at", "") if (email, organization) == (values["email"], values["organization"]) else ""
            values = {"email": email, "organization": organization, "detected_at": detected_at, "auto_detect": response.checked,
                      "account_uuid": values.get("account_uuid", "") if detected_at else "",
                      "organization_uuid": values.get("organization_uuid", "") if detected_at else "",
                      "detected_login": values.get("detected_login", "") if detected_at else ""}
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
            self.monitor.refresh(profile.id)
            return

    def _detect(self, identifier, draft):
        if self.busy or self.blocked() or not self.enabled():
            return
        from openswap.claude_desktop_identity import DetectionError
        try:
            self.monitor.detector.retry()
        except (ClaudeSwitchError, OSError) as exc:
            self._error(exc)
            return
        self.busy = True
        self.operation = "Detecting account…"
        self.changed()

        def work():
            result = {"state": "account_details", "identifier": identifier, "draft": draft}
            try:
                result["draft"] = {**(self.detector or self.monitor.detector)(self.store, identifier),
                                   "auto_detect": draft.get("auto_detect", True)}
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

    def _prepare_shortcuts(self, identifier, *, pin, refresh=False):
        from openswap.claude_desktop_shortcut import prepare_shortcuts
        return (self.shortcut_setup or prepare_shortcuts)(self.store, identifier, pin=pin, refresh=refresh)

    def setup_dock(self, profile):
        if self.busy or self.blocked() or not self.enabled():
            return
        self.operation = "Setting up Dock shortcuts…"
        self.busy = True
        self.changed()

        def work():
            try:
                # The explicit action is the repair path: the Dock is refreshed even when nothing changed.
                self._results.put((self._prepare_shortcuts(profile.id, pin=True, refresh=True), None))
            except Exception as exc:
                self._results.put((None, exc))
        try:
            threading.Thread(target=work, daemon=True, name="claude-desktop-dock").start()
        except Exception as exc:
            self._results.put((None, exc))

    def open(self, identifier, *, setup_shortcuts=None):
        if self.busy or self.blocked() or not self.enabled():
            return
        self.operation = "Opening Claude…"
        self.busy = True
        self.changed()

        def work():
            warning = ""
            if setup_shortcuts is not None:
                try:
                    self._prepare_shortcuts(identifier, pin=setup_shortcuts)
                except Exception as exc:
                    warning = ((str(exc) + "\n\n") if isinstance(exc, ClaudeSwitchError) else "") + (
                        "Your profile was kept. Choose Set up Dock shortcuts in its menu to retry.")
            try:
                result = self.store.open(identifier)
                from openswap.claude_desktop_presence import attach_presence
                try:
                    attach_presence(self.store, identifier)
                except (ClaudeSwitchError, OSError):
                    warning = warning or "Claude opened, but its running indicator could not start. Choose Set up Dock shortcuts to retry."
                if warning:
                    result["setup_warning"] = warning
                self._results.put((result, None))
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
        with self._modal():
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
            with self._modal():
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
        if self.signin_links:
            self.signin_links.poll(idle=not (self.busy or self.blocked() or self._editing_details))
        if self.monitor.poll(enabled=self.enabled(), idle=not (self.busy or self.blocked() or self._editing_details)):
            self.changed()
        if self.code and self.code.poll(enabled=self.enabled(), idle=not (self.busy or self.blocked() or self._editing_details) and self.code_login_allowed()):
            self.changed()
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
        held, self._signin = self._signin, None
        if error is None and result.get("pid") and result.get("profile", {}).get("id"):
            self._profile_states[result["profile"]["id"]] = "Running"
            self._profile_checked_at[result["profile"]["id"]] = datetime.now().strftime("%b %d, %H:%M:%S")
        self.changed()
        if error is not None:
            self._error(error)
            return
        if result.get("state") == "signin_candidates":
            self._choose_signin_profile(result, held)
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
            self.show_status(f"Shortcut created: {result['path']}\n\nChoose Set up Dock shortcuts in the profile menu to pin it or refresh its Dock icon.\n\n{result['notice']}")
            return
        if result.get("state") in {"dock_ready", "shortcuts_ready"}:
            self._notify("Dock shortcuts ready" if result["state"] == "dock_ready" else "Shortcuts ready",
                         "Use each profile's shortcut to open its Claude window.")
            self.refresh_status()
            return
        if result.get("state") == "already_opening":
            self._notify("Claude is still starting", f"{result['profile']['name']} · Open it again in a moment.")
            return
        try:
            focus_profile(result["pid"])
        except Exception:
            pass  # opening succeeded; focus failure is not a login failure
        if result.get("state") == "connector_sent":
            self._notify("Connection link sent", f"Check the connection in {result['profile']['name']}.")
            return
        if result.get("state") == "signin_sent":
            self.monitor.refresh(result["profile"]["id"])
            self._notify("Sign-in link sent", f"Check sign-in in {result['profile']['name']}.")
            return
        if self.signin_links and self.enabled():
            try:
                self.signin_links.prepare()
            except (ClaudeSwitchError, OSError):
                pass  # the profile opened; the Desktop section reports an unreadable store
        self._notify("Claude profile opened", f"{result['profile']['name']} · Check the account in Claude.")
        if result.get("setup_warning"):
            self.show_status(result["setup_warning"])
