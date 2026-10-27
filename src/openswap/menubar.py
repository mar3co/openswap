"""macOS menu bar extra for OpenSwap (``openswap menubar``).

A thin rumps shell over ``openswap.engine.Engine`` and ``openswap.autoswitch``.
It never re-implements account, usage, or auto-switch math. Pure helpers live
in ``openswap.menubar_display`` and are re-exported here so tests and the
popover keep importing from this module. ``rumps`` is imported only in
``run()``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from openswap.claude_login import (
    ClaudeLoginRun,
    login_outcome,
    new_login_outcome,
    remove_legacy_login_command,
)
from openswap.exceptions import ClaudeSwitchError, CredentialMissingError, CredentialReadError
from openswap.menubar_display import *  # noqa: F403
from openswap.codex.desktop_app import DesktopApp, DesktopCapability, capability_is_fresh
from openswap.menubar_display import (  # noqa: F401  tests poke these
    EMPTY_SNAPSHOT,
    MenuBarSettings,
    _account_display_usage,
    _adapt_snapshot,
    _live_countdown,
    _resets_at_ts,
    _rolled_weekly_window,
    _usage_log_key,
    chatgpt_manual_activation_allowed,
    chatgpt_suggestions_effective,
    desktop_capability_status_copy,
    ensure_notification_identity,
    codex_live_slot_changed,
    codex_restart_hint,
)


def desktop_switch_choices(snapshot):
    """Only offer managed Codex OAuth rows in the experimental submenu."""
    from openswap.codex import split_provider_num
    from openswap.json_output import USAGE_API_KEY

    kinds = snapshot.get("kinds") or {}
    choices = []
    for row in snapshot.get("accounts") or []:
        num, email, _active, display, _usage, alias, _org, disabled, _at = row
        provider, slot = split_provider_num(num)
        if provider != "codex" or disabled:
            continue
        if display == USAGE_API_KEY or kinds.get(str(num)) == "api_key":
            continue
        label = f"{slot}  {alias} ({email})" if alias else f"{slot}  {email}"
        choices.append((slot, label))
    return choices


def chatgpt_desktop_config_check(home):
    """Apply the desktop transaction's existing global configuration policy."""
    from openswap.codex.desktop import _config_check

    try:
        _config_check(home)
    except ClaudeSwitchError:
        return False
    return True


def desktop_switch_confirm_ok(process_state="running"):
    if process_state == "stopped":
        return "Switch and open ChatGPT"
    return "Restart ChatGPT"


def desktop_switch_confirm_copy(name, *, pause_auto=False, process_state="running"):
    pause_copy = (
        "Codex auto-switching turns off for all OpenSwap instances using this "
        "store until re-enabled. Claude Code stays unchanged."
        if pause_auto else "Keep Codex auto-switching off during desktop switching."
    )
    if process_state == "stopped":
        title = "Switch and open ChatGPT?"
        action = "Opens ChatGPT and changes the shared Codex login. "
    else:
        title = "Restart ChatGPT?"
        action = "Restarts the entire app and changes the shared Codex login. "
    return (
        title,
        f"Switch to {name}.\n\n"
        + action
        + "Save and stop all local/remote work; close other Codex clients before continuing.\n\n"
        "After restart, check the account in Chat, Work, and Codex.\n\n"
        + pause_copy,
    )


def _bundled_widget() -> bool:
    """Whether this is the app at a stable location, which registers its
    widget at start-up (``widget_install.register_on_start``)."""
    from openswap import bundle

    return bundle.is_frozen() and not bundle.runs_from_temporary_location()


def run(switcher, codex=None) -> int:
    """Entry point for ``openswap menubar``. Blocks until the user quits."""
    ensure_notification_identity()
    try:
        import rumps  # lazy: optional dependency, imported only when launching
        import AppKit  # ships with rumps (pyobjc-framework-Cocoa), never fails alone
    except ImportError as e:
        # This module is import-safe without rumps by design, so the CLI's
        # guard around ``from openswap.menubar import run`` can never see a
        # missing extra — the failure lands here at call time. Raise the
        # error type the CLI already renders cleanly instead of a traceback.
        raise ClaudeSwitchError(
            "Menu bar mode requires 'rumps'. "
            "Install with: uv tool install --force --editable '.[menubar]'"
        ) from e

    # One extra at a time, decided before the status item exists. Held for
    # the life of this process: the local keeps the file open until run()
    # returns or the process exits.
    from openswap import launch_agent
    from openswap.menubar_instance import AlreadyRunning, claim, hand_over_to_login_item

    from openswap import bundle as _bundle

    as_login_item = launch_agent.runs_as_login_item()
    try:
        instance_lock = claim(
            switcher.backup_dir, as_login_item=as_login_item, logger=switcher._logger,
            move_from=_bundle.move_source_app() if _bundle.runs_from_temporary_location() else None,
        )
    except AlreadyRunning as e:
        return e.exit_code
    if as_login_item:
        # launchd started this copy, so the last failed start is history.
        launch_agent.clear_start_failure(switcher.backup_dir)
    # The login item is the copy that should run: start it and step aside.
    # A registered URL handler must first receive its pending Apple event;
    # exiting here would lose a browser callback after a crash/relaunch.
    from openswap.claude_desktop_links import owns_system_handler
    if not as_login_item and not owns_system_handler() and hand_over_to_login_item(
        switcher.backup_dir, logger=switcher._logger
    ):
        if instance_lock is not None:
            instance_lock.release()
        return 0

    # rumps never sets an activation policy, so under a framework Python the
    # process launches as a regular app and parks a "Python" icon in the Dock
    # for as long as the menu bar runs. Accessory keeps the status item and
    # dialog windows but stays out of the Dock and the Cmd-Tab switcher.
    nsapp = AppKit.NSApplication.sharedApplication()
    nsapp.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    # Inherit System Settings → Appearance. A pinned Aqua appearance would
    # keep the popover light after Dark Mode turns on.
    nsapp.setAppearance_(None)

    from openswap.autoswitch import AutoSwitchEngine
    from openswap.settings import load_settings, set_setting
    from openswap.snapshot_source import SnapshotSource
    from openswap.claude_desktop_menu import ClaudeDesktopMenu, has_saved_profiles

    settings_path = menubar_settings_path(switcher.backup_dir)
    log_path = switcher.backup_dir / "openswap.log"

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__("openswap", quit_button=None)
            self.switcher = switcher
            self.codex = codex
            self.settings = MenuBarSettings.load(settings_path, has_desktop_profiles=has_saved_profiles)
            # The supported paced read path: per refresh it fetches only the
            # active account plus (at most once per freshness window) one stale
            # alternate, so an open menu costs O(1) requests per window instead
            # of a full pass per tick — which kept every token at its per-account
            # rate-limit edge. Reused across refreshes to hold its pacing state.
            self._snapshot_source = SnapshotSource(switcher)
            self._codex_source = SnapshotSource(codex) if codex is not None else None
            self.snapshot = dict(EMPTY_SNAPSHOT)
            self._account_states = {
                "claude": "loading", "chatgpt": "loading" if codex is not None else "unavailable"
            }
            self._dirty = False
            self._update_found = None  # written by the release-check thread
            self._update_shown = None
            self._snapshot_at = 0.0
            self._refreshing = False
            self._config_path = switcher._get_claude_config_path()
            self._config_mtime = 0.0
            self._identity_watch = IdentityDriftWatch()
            from openswap.codex.auth import auth_path
            self._codex_auth_path = auth_path(codex.home) if codex is not None else None
            self._codex_auth_mtime = 0.0
            self._store_paths = [switcher.sequence_file]
            if codex is not None:
                self._store_paths.append(codex.sequence_file)
            self._store_seen: dict = {}
            store_roster_changed(self._store_paths, self._store_seen)
            self._last_usage_log: dict = {}  # account num -> last-logged (5h, 7d) key
            # Auto-switch engine (the same one `openswap auto` runs), hosted in a
            # background thread while enabled.
            self._engine = None
            self._codex_engine = None
            self._chatgpt_auto_engine = None
            self._chatgpt_auto_generation = 0
            self._pending_chatgpt_switch = None
            self._chatgpt_monitor_notice = ""
            self._chatgpt_auto_retry_at = 0.0
            self._engine_events: list = []
            self._hold_event = None
            self._hold_slot = None
            self._tick_slot = None
            self._hold_reload_pending = False
            # Set by card-menu edits (rename, disable/enable): the next refresh
            # that lands reloads the open main page even though the slot list
            # and provider states are unchanged.
            self._reload_after_refresh = False
            self._event_lock = threading.Lock()
            self._panel = None
            self._status_icon_animator = None
            self._kickoff_running = False
            self._desktop_switching = False
            self._desktop_result = None
            self._desktop_status = "Switching reopens ChatGPT"
            from openswap.settings import load_worker_settings
            self._worker_policy = load_worker_settings(switcher.backup_dir)
            self._worker_status_cache = {
                "process_state": "unavailable",
                "provider": {"available": False, "diagnostic_code": "status_checking"},
                "active_job": None,
                "queue_depth": 0,
                "lease_quarantined": True,
                "operation": "worker_status_checking",
            }
            self._worker_status_inflight = False
            self._worker_last_refresh_at = 0.0
            self._worker_operation = None
            self._worker_result = None
            self._worker_generation = 0
            self._worker_result_lock = threading.Lock()
            self._desktop_app = DesktopApp()
            self._claude_desktop_profiles = ClaudeDesktopMenu(
                prompt=self._prompt, show_error=self._show_error, changed=self._claude_desktop_changed,
                confirm=self._alert,
                show_status=lambda message: self._alert(title="Claude Desktop", message=message),
                notify=lambda title, body: self._notify(NotificationCopy(title=title, body=body)),
                enabled=lambda: self.settings.desktop_features_enabled,
                blocked=lambda: self._desktop_switching,
                code_engine=self.switcher,
                code_login_allowed=lambda: getattr(self, "_claude_login", None) is None,
                guide_seen=lambda: self.settings.claude_desktop_guide_seen,
                mark_guide_seen=self._mark_claude_desktop_guide_seen,
            )
            try:
                from openswap.claude_desktop_links import DesktopSignInLinks
                self._claude_desktop_profiles.signin_links = DesktopSignInLinks(
                    enabled=lambda: self.settings.desktop_features_enabled,
                    needed=lambda: any(p.kind == "managed" for p in self._claude_desktop_profiles.store.list()),
                    receive_signin=self._claude_desktop_profiles.receive_signin,
                    show_error=self._show_error,
                )
                self._claude_desktop_profiles.signin_links.prepare()
            except Exception:
                # Source/CLI runs do not have a registered application bundle.
                # Never log native exception details, which may contain URLs.
                pass
            self._desktop_app_lock = threading.Lock()
            self._chatgpt_capability = DesktopCapability("checking", "checking")
            self._chatgpt_capability_generation = 0
            self._chatgpt_probe_inflight = False
            self._desktop_consent_generation = None
            self._login_session = None
            self._login_ui_state = {"stage": "idle"}
            self._login_save_result = None
            self._claude_login: ClaudeLoginRun | None = None
            self._claude_login_num: str | None = None
            self._claude_login_results: list = []
            self._refresh_pending = False
            self._login_canceled_session = None
            self._kickoff_results = None
            self._kickoff_retry_after: float | None = None
            self._kickoff_notified_failure = None
            self._kickoff_action_required: dict[str, float] = {}
            self._kickoff_succeeded_nums: set[str] = set()
            self._kickoff_success_date = ""
            self._relogin_notified: set[str] = set()
            self._pending_relogin_notifies: set[str] = set()
            self._auto_capturing = False
            self._auto_captured_nums: set[str] = set()
            self._auto_capture_failed_for: tuple[str, str] | None = None
            self._show_popover_pending = False
            self._show_requests = None
            self._handoff_requests = None
            self._login_item_busy = False
            self._login_item_result = None
            self._login_item_last: dict = {}
            # Opening the app again, or another copy starting, shows the
            # popover instead of a second status item (menubar_instance). Each
            # hook is set up on its own, so one failing leaves the others.
            from openswap.menubar_instance import HANDOFF_NOTIFICATION, SHOW_NOTIFICATION

            try:
                from openswap.menubar_panel import handle_reopen, observe_show_requests
            except Exception:
                self.switcher._logger.warning("show-popover hooks unavailable", exc_info=True)
                hooks = ()
            else:
                hooks = (
                    # rumps' app delegate class (`App.run` installs an instance).
                    ("reopen", lambda: handle_reopen(rumps.rumps.NSApp, self._on_reopen)),
                    ("show request", lambda: setattr(
                        self, "_show_requests",
                        observe_show_requests(SHOW_NOTIFICATION, self._on_show_request),
                    )),
                    ("hand-over request", lambda: setattr(
                        self, "_handoff_requests",
                        observe_show_requests(HANDOFF_NOTIFICATION, self._on_handoff_request),
                    )),
                )
            for name, install in hooks:
                try:
                    install()
                except Exception:
                    self.switcher._logger.warning("%s hook failed", name, exc_info=True)
            # The setup wizard (plan 021): its window once opened, the step it
            # shows, the picks it applies at Finish, the Move plan for a copy
            # at a temporary location (read once per opening), and what the
            # account steps last showed, so the tick redraws only on change.
            self._wizard = None
            self._wizard_step = None
            self._wizard_choices = None
            self._wizard_move = None
            self._wizard_seen = None
            # Another copy at a temporary location (a newer DMG opened while
            # this one runs) asked to be moved: the wizard's Move source.
            self._wizard_source = None
            # A login item change started by the wizard's Finish: its failure
            # is shown as an alert, since the wizard has closed; a hand-over
            # leaves a show request so the login item's copy opens the popover.
            self._login_item_from_welcome = False
            self.rebuild_menu()
            # Background display refresh on the user's interval, plus a fast
            # UI-sync tick that applies snapshots + engine events on the main thread.
            self.refresh_timer = rumps.Timer(self.on_refresh_tick, self.settings.refresh_interval)
            self.refresh_timer.start()
            self.sync_timer = rumps.Timer(self.on_sync_tick, 1)
            self.sync_timer.start()
            # rumps attaches an NSMenu in initializeStatusBar (after __init__).
            # Steal the click for the popover once the status item exists.
            self._attach_timer = rumps.Timer(self._attach_panel_once, 0.15)
            self._attach_timer.start()
            # Cheap cache reads, ticked well inside the cache's day and retry hour.
            self.update_timer = rumps.Timer(self.on_update_tick, 900)
            self.update_timer.start()
            self.on_update_tick(None)
            self.refresh_async()  # first display fetch
            from openswap.widget_snapshot import wake_widget_host
            wake_widget_host()
            remove_legacy_login_command()
            if self.settings.auto_switch_enabled:
                self._start_engine()
            if chatgpt_suggestions_effective(self.settings):
                self._start_chatgpt_auto_monitor()

        # ---- display refresh plumbing ----------------------------------------
        def refresh_async(self, full=False):
            if self._refreshing:
                # One worker at a time (SnapshotSource pacing state is only
                # touched by this single worker). A request made while one is
                # in flight may describe a store change that worker read past,
                # so the tick runs it again once the worker lands.
                self._refresh_pending = True
                return
            if self._desktop_switching:
                return
            self._refreshing = True
            populated = {
                "chatgpt" if str(row[0]).startswith("codex:") else "claude"
                for row in self.snapshot.get("accounts", [])
            }
            next_states = {
                provider: (
                    "loading"
                    if state != "unavailable"
                    and (state == "error" or provider not in populated)
                    else state
                )
                for provider, state in self._account_states.items()
            }
            if next_states != self._account_states:
                self._account_states = next_states
                self._hold_reload_pending = True
            threading.Thread(target=self._worker, args=(full,), daemon=True).start()

        def _worker(self, full):
            # Lock-free handoff: worker only rebinds plain attributes (atomic in
            # CPython); the main-thread sync tick reads them. While the engine
            # runs it already paces all fetching, so the display reads store-only.
            previous_states = self._account_states
            previous_slots = tuple(row[0] for row in self.snapshot.get("accounts", []))
            try:
                self._check_identity_drift()
                try:
                    raw = self._snapshot_source.take(
                        full=full, store_only=self._engine is not None
                    )
                except Exception:
                    # Keep the last good snapshot rather than blanking the menu.
                    self.switcher._logger.debug("menubar snapshot failed", exc_info=True)
                    self._account_states = {
                        "claude": "error", "chatgpt": "error" if self.codex is not None else "unavailable"
                    }
                    return
                codex_raw = None
                if self._codex_source is not None:
                    try:
                        codex_raw = self._codex_source.take(
                            full=full, store_only=(self._codex_engine is not None or
                                                   self._chatgpt_auto_engine is not None)
                        )
                    except Exception:
                        self.switcher._logger.debug(
                            "codex snapshot failed", exc_info=True
                        )
                        codex_raw = None
                snap = _adapt_snapshot(raw, codex_raw)
                self._log_usage(snap)
                now = time.time()
                snap["hold_line"] = self._hold_line_for(snap)
                try:
                    from openswap.process_detection import get_running_instances

                    sessions, ides = get_running_instances()
                except Exception:
                    snap["running_line"] = None
                    snap["claude_running"] = True
                else:
                    snap["running_line"] = format_running_line(sessions, ides)
                    snap["claude_running"] = bool(sessions or ides)
                try:
                    from openswap.process_detection import get_running_codex_instances

                    codex_procs = get_running_codex_instances()
                except Exception:
                    snap["codex_running_line"] = None
                    snap["codex_running"] = True
                else:
                    snap["codex_running_line"] = format_codex_running_line(codex_procs)
                    snap["codex_running"] = bool(codex_procs)
                # Keep reconciliation and publication under the same lock as
                # kickoff result draining. Otherwise a snapshot built just
                # before an OAuth failure can overwrite the new warning.
                with self._event_lock:
                    self._kickoff_action_required = (
                        reconcile_kickoff_action_required(
                            self._kickoff_action_required,
                            snap.get("accounts") or [],
                        )
                    )
                    snap["kickoff_action_required"] = sorted(
                        self._kickoff_action_required
                    )
                    self.snapshot = snap
                self._account_states = {
                    "claude": "ready",
                    "chatgpt": ("ready" if codex_raw is not None else "error")
                    if self._codex_source is not None else "unavailable",
                }
                self._snapshot_at = now
                self._dirty = True  # picked up by on_sync_tick on the main thread
                curr_relogin = relogin_slot_nums(snap)
                with self._event_lock:
                    if self._engine is None:
                        self._pending_relogin_notifies |= newly_relogin_slots(
                            self._relogin_notified, curr_relogin
                        )
                    self._relogin_notified = curr_relogin
                from openswap.widget_snapshot import publish_widget_snapshot
                publish_widget_snapshot(snap, now=self._snapshot_at)
                self._ensure_codex_engine()
            except Exception:
                self.switcher._logger.debug("account display refresh failed", exc_info=True)
                self._account_states = {
                    provider: "error" if state == "loading" else state
                    for provider, state in self._account_states.items()
                }
            finally:
                self._refreshing = False
                # A card edit made while this pass was already running is only
                # visible to the queued pass (_refresh_pending), so leave its
                # reload request for that pass rather than spending it here.
                edit_landed = self._reload_after_refresh and not self._refresh_pending
                if edit_landed:
                    self._reload_after_refresh = False
                if (previous_states != self._account_states or
                        previous_slots != tuple(row[0] for row in self.snapshot.get("accounts", []))
                        or edit_landed):
                    self._hold_reload_pending = True

        def _config_mtime_or_none(self):
            try:
                return self._config_path.stat().st_mtime
            except OSError:
                return None

        def _check_identity_drift(self):
            watch = self._identity_watch
            log = self.switcher._logger
            now = time.time()
            state = watch.begin(now)
            if state == "exhausted":
                log.warning(
                    "Identity check budget for this hour is spent; next check "
                    "when it rolls over (#46)"
                )
            if state != "run":
                return
            try:
                # Both sampled BEFORE the check reads the config. A rewrite
                # landing during its Keychain reads then leaves the watch
                # holding the OLD identity, so the next tick re-arms it;
                # sampled after, the new identity would be marked checked
                # without ever having been looked at.
                sampled = self.switcher.live_identity()
                config_mtime = self._config_mtime_or_none()
                drift = self.switcher.identity_drift()
                line = None
                if drift is not None and drift.outcome != "unattributed":
                    try:
                        from openswap.process_detection import scan_sessions

                        sessions, unreadable = scan_sessions()
                    except OSError:
                        sessions, unreadable = None, 0
                    line = format_identity_drift_log(
                        drift,
                        sessions,
                        unreadable,
                        config_mtime,
                        self._config_mtime_or_none(),
                    )
                # The identity the check actually judged, when it says; the
                # earlier sample only stands in for a clean pass, where being
                # older can over-arm but never miss.
                checked = (
                    drift.config_identity
                    if drift is not None and drift.config_identity is not None
                    else sampled
                )
                report = watch.record(time.time(), checked, drift, line)
                if report:
                    log.warning(report)
            except Exception:
                # KeychainError is a plain Exception; nothing here may take
                # out the refresh pass this runs at the head of.
                watch.failed(time.time())
                log.warning("identity drift check failed", exc_info=True)

        def _log_usage(self, snap):
            """Log each account's session/weekly limits when they change.

            Runs on every refresh (background thread; the logger is thread-safe)
            but de-dupes per account on the (5h, 7d) percentages so an idle
            machine doesn't churn the rotating log with identical lines.
            """
            for num, email, _is_active, _display, last_good, _alias, _org, _disabled, _fetched_at in snap["accounts"]:
                key = _usage_log_key(last_good)
                if key == (None, None) or self._last_usage_log.get(num) == key:
                    continue
                line = format_usage_log(email, last_good)
                if line:
                    self.switcher._logger.info(line)
                    self._last_usage_log[num] = key

        def on_refresh_tick(self, _timer):
            self.refresh_async()

        def on_update_tick(self, _timer):
            threading.Thread(target=self._update_worker, name="openswap-update-check", daemon=True).start()

        def _update_worker(self):
            from openswap import __version__
            from openswap.update_check import newer_release

            try:
                self._update_found = newer_release(__version__)
            except Exception:
                self.switcher._logger.debug("release check failed", exc_info=True)

        def _apply_update_found(self):
            found = self._update_found
            if found == self._update_shown:
                return
            self._update_shown = found
            self._dirty = True
            from openswap.update_check import claim_notification, upgrade_hint

            if found is not None and claim_notification(found):
                self._notify(NotificationCopy(title=f"OpenSwap {found} is available", body=upgrade_hint()))

        def on_update(self, _sender):
            from openswap import __version__
            from openswap.update_check import RELEASES_URL, upgrade_hint

            found = self._update_shown
            if found is None:
                return
            if self._alert(
                title=f"OpenSwap {found} is available",
                message=f"You have {__version__}. {upgrade_hint()}",
                ok="Release Notes", cancel="Close",
            ) == 1:
                import webbrowser
                webbrowser.open(RELEASES_URL)

        def on_sync_tick(self, _timer):
            if self._refresh_pending and not self._refreshing:
                self._refresh_pending = False
                self.refresh_async()
            self._poll_login()
            self._drain_desktop_result()
            if getattr(self, "_claude_desktop_profiles", None) is not None:
                self._claude_desktop_profiles.poll()
            if (getattr(self, "_claude_desktop_update_pending", False)
                    and not getattr(self._panel, "_menu_open", False)):
                self._claude_desktop_changed()
            self._drain_worker_result()
            self._drain_guided_setup()
            self._drain_login_item_result()
            panel = self._panel
            if (
                panel is not None
                and panel.is_shown()
                and getattr(panel, "_page", None) == SETTINGS_PAGE
                and getattr(panel, "_settings_section", None) == SETTINGS_SECTION_REMOTE
                and time.monotonic() - self._worker_last_refresh_at >= 4.0
            ):
                self._worker_view_active()
            if self._desktop_switching:
                return
            self._consume_widget_command()
            self._apply_update_found()
            if self._dirty:
                self._dirty = False
                self.rebuild_menu()
                # Dirty path must not reload the popover: Settings would lose
                # controls, and a main-page reload mid-click swallows mouseUp.
                # Hold copy is applied below and reloads the open main page
                # only when it changes, deferred while the left button is down.
            self._detect_active_change()
            self._reconcile_chatgpt_auto_mode()
            self._validate_pending_chatgpt_switch()
            self._detect_store_change()
            self._drain_engine_events()
            self._apply_hold_line()
            self._drain_relogin_notifies()
            self._drain_claude_login_result()
            self._wizard_tick()
            self._maybe_auto_capture_relogin()
            self._drain_kickoff_results()
            self._maybe_kickoff()

        def _consume_widget_command(self):
            # switch_to and rumps.alert must run on the UI thread.
            from openswap.widget_snapshot import consume_switch_command

            num = consume_switch_command()
            if num is not None:
                self._switch_from_widget(num)

        def _detect_store_change(self):
            # CLI add / remove / alias / disable write only OpenSwap's own
            # index, never Claude's config, so _detect_active_change cannot
            # see them. Skipped while a worker is in flight so a change that
            # landed after it started is still caught next tick. The extra's
            # own roster edits therefore cost one redundant pass a tick later;
            # re-priming after the worker would lose writes it raced.
            if self._refreshing:
                return
            if store_roster_changed(self._store_paths, self._store_seen):
                self.refresh_async()

        def _detect_active_change(self):
            # Reflect account switches from any source (menu, CLI, auto engine)
            # within ~1s. Detecting *which* account is active is a cheap local
            # read of ~/.claude.json -- no Keychain or usage API -- so we can do
            # it on every tick. We gate the read on the file's mtime (a cheap
            # stat) so a large config isn't parsed each second, and only kick a
            # refresh when the active *slot* changed (Claude Code rewrites this
            # file often for unrelated reasons). Slot, not email: two orgs can
            # share an address.
            if self._refreshing:
                return  # a worker is already in-flight; it refreshes the marker
            changed = False
            try:
                mtime = self._config_path.stat().st_mtime
            except OSError:
                mtime = None
            if mtime is not None and mtime != self._config_mtime:
                self._config_mtime = mtime
                live_num = self.switcher.current_account_number()
                if live_slot_changed(self.snapshot, live_num):
                    changed = True
                # The watch compares against the identity ITS last pass saw,
                # not the snapshot: a worker already in flight can absorb an
                # external rewrite into the snapshot before this tick runs.
                if self._identity_watch.slot_seen(
                    self.switcher.live_identity(), time.time()
                ):
                    changed = True
            if self.codex is not None and self._codex_auth_path is not None:
                try:
                    cmtime = self._codex_auth_path.stat().st_mtime
                except OSError:
                    cmtime = None
                else:
                    if cmtime != self._codex_auth_mtime:
                        self._codex_auth_mtime = cmtime
                        if codex_live_slot_changed(
                            self.snapshot, self.codex.current_account_number()
                        ):
                            changed = True
            if self._identity_watch.ready(time.time()):
                changed = True
            if changed:
                # Do not clear the hold cache here: the engine may already have
                # recorded a reason for the new slot. Display is gated by
                # hold_event_for_snapshot so an old hold cannot caption a new card.
                self.refresh_async()

        # ---- auto-switch engine ----------------------------------------------
        def _ensure_codex_engine(self):
            if self._desktop_switching or chatgpt_suggestions_effective(self.settings):
                return
            if self._codex_engine is not None or self._engine is None:
                return
            if self.codex is None or not self.codex.switchable_account_numbers():
                return
            settings = load_settings(self.switcher.backup_dir)
            if not settings.codex_enabled:
                return
            try:
                from openswap.autoswitch import STATE_FILENAME
                ceng = AutoSwitchEngine(
                    self.codex,
                    settings,
                    self._on_engine_event,
                    dry_run=False,
                    state_path=self.codex.state_dir / STATE_FILENAME,
                )
                ceng.on_event = lambda event, e=ceng: self._on_engine_event(event, e)
                self._codex_engine = ceng
                threading.Thread(
                    target=self._run_engine, args=(ceng,), daemon=True
                ).start()
            except Exception as e:
                self.switcher._logger.debug(
                    "codex auto-switch engine failed to start: %s", e
                )

        def _start_engine(self):
            """Run the core AutoSwitchEngine (live) in a background thread."""
            if self._engine is not None:
                self._ensure_codex_engine()
                return
            try:
                engine = AutoSwitchEngine(
                    self.switcher,
                    load_settings(self.switcher.backup_dir),
                    self._on_engine_event,
                    dry_run=False,
                )
            except Exception as e:  # never let a bad start crash the menu bar
                self.switcher._logger.warning("auto-switch engine failed to start: %s", e)
                self._notify(notification_copy_for_engine_start_failure(str(e)))
                return
            engine.on_event = lambda event, e=engine: self._on_engine_event(event, e)
            with self._event_lock:
                self._engine = engine
                self._hold_event = None
                self._hold_slot = None
                self._tick_slot = None
            threading.Thread(target=self._run_engine, args=(engine,), daemon=True).start()
            self._ensure_codex_engine()

        def _run_engine(self, engine):
            try:
                engine.run_loop()
            except Exception:
                self.switcher._logger.debug("auto-switch engine crashed", exc_info=True)

        def _clear_hold_event(self):
            with self._event_lock:
                self._hold_event = None
                self._hold_slot = None
                self._tick_slot = None

        def _reload_main_panel_if_shown(self):
            panel = self._panel
            if (
                panel is not None
                and panel.is_shown()
                and getattr(panel, "_page", None) == MAIN_PAGE
            ):
                if getattr(panel, "_selected_provider", None) == "chatgpt":
                    self._on_chatgpt_view_active()
                panel.reload()

        def _worker_view_active(self):
            """Refresh only the local, redacted worker snapshot off the UI thread."""
            if self._worker_status_inflight or self._worker_operation is not None:
                return
            self._worker_status_inflight = True
            self._worker_last_refresh_at = time.monotonic()
            self._worker_generation += 1
            generation = self._worker_generation
            threading.Thread(
                target=self._worker_status_worker,
                args=(generation,),
                daemon=True,
            ).start()

        def _worker_status_worker(self, generation):
            policy = None
            try:
                from openswap.settings import load_worker_settings
                from openswap.worker.cli import read_status

                root = self.switcher.backup_dir
                snapshot = read_status(root)
                policy = load_worker_settings(root)
            except Exception:
                snapshot = {
                    "process_state": "unavailable",
                    "provider": {"available": False, "diagnostic_code": "status_unavailable"},
                    "active_job": None,
                    "queue_depth": 0,
                    "lease_quarantined": True,
                }
                try:
                    from openswap.settings import load_worker_settings

                    policy = load_worker_settings(self.switcher.backup_dir)
                except Exception:
                    pass
            snapshot = self._with_account_picker(snapshot)
            with self._worker_result_lock:
                if generation == self._worker_generation:
                    self._worker_result = (generation, snapshot, policy, None)

        def _with_account_picker(self, snapshot):
            """Attach roster metadata for the Remote tasks account popup.

            Runs on the worker thread: slot numbers, emails, aliases and the
            opaque account references only, never credentials.
            """
            snapshot = dict(snapshot)
            try:
                from openswap.worker.cli import worker_account_choices

                snapshot["account_picker"] = worker_account_choices(
                    self.switcher.backup_dir
                ).to_dict()
            except Exception:
                snapshot.pop("account_picker", None)
            return snapshot

        def _run_guided_setup(self):
            """Set up Remote tasks…: the steps of `openswap worker pair`, in dialogs.

            Pair from the pasted pairing command when this Mac is not paired,
            then start the worker, confirm the account, approve a research
            folder and show the summary, through the same functions as the
            CLI. The steps run on a worker thread (pairing, launchctl, locks);
            ``on_sync_tick`` shows each dialog on the UI thread as it is asked.
            """
            if self._worker_operation is not None or getattr(self, "_guided_setup", None) is not None:
                return
            from openswap.worker import guided_setup

            root = self.switcher.backup_dir
            ui = guided_setup.ThreadedPrompts(guided_setup.DialogPrompts(
                self._alert, self._prompt, choose_folder=self._choose_folder,
            ))
            self._guided_setup = ui

            def steps():
                from openswap.settings import load_worker_settings

                try:
                    paired = load_worker_settings(root).control_service_url is not None
                    if paired or guided_setup.pair_interactively(root, ui):
                        guided_setup.run(root, ui)
                except guided_setup.SetupClosed:
                    pass
                except Exception:
                    ui.say("Setup stopped unexpectedly. Run `openswap worker setup` in Terminal to finish.")
                finally:
                    try:
                        ui.flush()
                    finally:
                        ui.finished = True

            threading.Thread(target=steps, name="openswap-guided-setup", daemon=True).start()

        def _run_connect(self, url=None):
            """Connect to OpenTag… / Reconnect: pair this Mac by approving it in the browser.

            The flow (``openswap.worker.connect``) runs on a worker thread, as the
            pasted-command setup does. While the browser is open the owner sees one
            dialog, "Finish in your browser…", whose Cancel closes the listener.
            A new pairing then says "Paired with <workspace>" and runs the guided
            setup; a reconnect only says so.
            """
            if self._worker_operation is not None or getattr(self, "_guided_setup", None) is not None:
                return
            from openswap.worker import connect, guided_setup

            root = self.switcher.backup_dir
            ui = guided_setup.ThreadedPrompts(guided_setup.DialogPrompts(
                self._alert, self._prompt, guided_setup.CONNECT_TITLE,
                choose_folder=self._choose_folder, wait=self._wait_dialog,
            ))
            flow = connect.ConnectFlow(root, url, open_browser=connect.open_in_browser)
            self._guided_setup = ui

            def steps():
                try:
                    outcome, message = guided_setup.connect_once(flow, ui)
                    ui.say(message)
                    if outcome == "paired":
                        guided_setup.run(root, ui)
                except guided_setup.SetupClosed:
                    pass
                except Exception:
                    flow.close()
                    ui.say("Connect stopped unexpectedly. Run `openswap worker connect` in Terminal.")
                finally:
                    try:
                        ui.flush()
                    finally:
                        ui.finished = True

            threading.Thread(target=steps, name="openswap-connect", daemon=True).start()

        def _connect_other_server(self):
            """Use another server…: the only way to Connect to a backend other than OpenTag."""
            if self._worker_operation is not None or getattr(self, "_guided_setup", None) is not None:
                return
            response = self._prompt(
                title="Use another server",
                message="The address of the server to pair with, for example https://opentag.example.",
                default_text="https://", ok="Connect", cancel="Cancel",
            )
            url = str(getattr(response, "text", "")).strip()
            if getattr(response, "clicked", 0) != 1 or url in ("", "https://", "http://"):
                return
            self._run_connect(url)

        def _wait_dialog(self, *, title, message, done):
            """The "Finish in your browser…" dialog: True when the owner cancelled."""
            from openswap.menubar_dialog import run_waiting_dialog

            return run_waiting_dialog(title=title, message=message, done=done, run=self._dialog)

        def _drain_guided_setup(self):
            """On the UI thread: show the setup's pending dialog; refresh when it ends."""
            ui = getattr(self, "_guided_setup", None)
            if ui is None:
                return
            ui.serve()
            if ui.finished:
                self._guided_setup = None
                self._worker_view_active()

        def _worker_action(self, row_id, value):
            if self._worker_operation is not None:
                return
            self._worker_operation = {
                "remote_tasks_enabled": "worker_enable_or_disable",
                "remote_tasks_paused": "worker_admission_update",
                "remote_tasks_stop": "worker_stop_requested",
                "remote_tasks_account": "worker_account_update",
                "remote_tasks_web_choice": "worker_account_update",
            }.get(row_id)
            if self._worker_operation is None:
                return
            self._worker_status_cache = dict(self._worker_status_cache)
            self._worker_status_cache["operation"] = self._worker_operation
            self._worker_generation += 1
            generation = self._worker_generation
            threading.Thread(
                target=self._worker_action_worker,
                args=(generation, row_id, value),
                daemon=True,
            ).start()

        def _worker_action_worker(self, generation, row_id, value):
            diagnostic = None
            policy = None
            try:
                from openswap.settings import load_worker_settings
                from openswap.worker.cli import (
                    disable_worker,
                    enable_worker,
                    request_pause,
                    request_stop,
                    read_status,
                )

                root = self.switcher.backup_dir
                if row_id == "remote_tasks_enabled":
                    if self._worker_policy.enabled:
                        ok, _result, diagnostic = disable_worker(root)
                        if not ok:
                            diagnostic = diagnostic or "worker_disable_blocked"
                    else:
                        enable_worker(root)
                elif row_id == "remote_tasks_paused":
                    result = request_pause(root, not self._worker_policy.paused)
                    if result.get("accepted") is not True:
                        diagnostic = result.get("diagnostic_code") or "pause_refused"
                elif row_id == "remote_tasks_stop":
                    if not isinstance(value, str) or not value:
                        diagnostic = "job_identity_unknown"
                    else:
                        result = request_stop(root, value)
                        if result.get("accepted") is not True:
                            diagnostic = result.get("diagnostic_code") or "stop_refused"
                elif row_id == "remote_tasks_account":
                    diagnostic = self._pin_worker_account(root, value)
                elif row_id == "remote_tasks_web_choice":
                    diagnostic = self._toggle_web_choice(root, value)
                policy = load_worker_settings(root)
                snapshot = read_status(root)
            except Exception:
                snapshot = {
                    "process_state": "unavailable",
                    "provider": {"available": False, "diagnostic_code": "status_unavailable"},
                    "active_job": None,
                    "queue_depth": 0,
                    "lease_quarantined": True,
                }
                diagnostic = diagnostic or "worker_control_failed"
                try:
                    from openswap.settings import load_worker_settings

                    policy = load_worker_settings(self.switcher.backup_dir)
                except Exception:
                    pass
            snapshot = self._with_account_picker(snapshot)
            with self._worker_result_lock:
                if generation == self._worker_generation:
                    self._worker_result = (generation, snapshot, policy, diagnostic)

        def _pin_worker_account(self, root, value):
            """Pin (or clear) through the CLI's own function; returns a diagnostic.

            Only ``""`` (None) and an opaque ``codex:`` reference are accepted:
            the popup's Claude and ineligible entries are disabled, and a
            stray value is refused rather than guessed at.
            """
            from openswap.exceptions import ClaudeSwitchError
            from openswap.worker.accounts import AccountPinError
            from openswap.worker.cli import set_worker_account

            from openswap.worker.accounts import provider_of

            if value == "":
                selector = None
            elif provider_of(value) is not None:
                selector = str(value)
            else:
                return "account_not_eligible"
            try:
                set_worker_account(root, selector)
            except AccountPinError as exc:
                return exc.code
            except ClaudeSwitchError as exc:
                return "worker_lifecycle_busy" if str(exc) == "worker_lifecycle_busy" else "settings_unavailable"
            return None

        def _toggle_web_choice(self, root, value):
            """Allow or disallow one account for a per-job choice; returns a diagnostic.

            Only ``allow:codex:…`` and ``disallow:codex:…`` act, through the
            CLI's own functions; the pinned default is never disallowed here.
            """
            from openswap.exceptions import ClaudeSwitchError
            from openswap.worker.accounts import AccountPinError
            from openswap.worker.cli import allow_worker_account, disallow_worker_account

            from openswap.worker.accounts import provider_of

            action, _, ref = value.partition(":") if isinstance(value, str) else ("", "", "")
            if action not in {"allow", "disallow"} or provider_of(ref) is None:
                return "account_not_eligible"
            try:
                if action == "allow":
                    allow_worker_account(root, ref)
                else:
                    disallow_worker_account(root, ref)
            except AccountPinError as exc:
                return exc.code
            except ClaudeSwitchError as exc:
                return "worker_lifecycle_busy" if str(exc) == "worker_lifecycle_busy" else "settings_unavailable"
            return None

        def _drain_worker_result(self):
            with self._worker_result_lock:
                result = self._worker_result
                if result is None:
                    return
                generation, snapshot, policy, diagnostic = result
                self._worker_result = None
            if generation != self._worker_generation:
                if self._worker_operation is None:
                    self._worker_status_inflight = False
                return
            self._worker_status_inflight = False
            self._worker_operation = None
            self._worker_status_cache = dict(snapshot)
            if policy is not None:
                self._worker_policy = policy
            self._worker_status_cache.pop("operation", None)
            self._worker_status_cache["diagnostic_notice"] = diagnostic
            panel = self._panel
            if (
                panel is not None
                and panel.is_shown()
                and getattr(panel, "_page", None) == SETTINGS_PAGE
                and getattr(panel, "_settings_section", None) == SETTINGS_SECTION_REMOTE
            ):
                panel.reload()

        def _stop_codex_engine(self):
            with self._event_lock:
                codex_engine = self._codex_engine
                self._codex_engine = None
            if codex_engine is not None:
                codex_engine.stop()

        def _chatgpt_target_identity(self, number):
            try:
                return self.codex.slot_identity(str(number)) if self.codex else None
            except Exception:
                return None

        def _start_chatgpt_auto_monitor(self):
            """Start the dry-run candidate monitor; it can never switch credentials."""
            if (not chatgpt_suggestions_effective(self.settings) or self.codex is None or
                    self._chatgpt_auto_engine is not None or self._desktop_switching or
                    self._login_session is not None):
                return
            if time.monotonic() < self._chatgpt_auto_retry_at:
                return
            if self._codex_enabled():
                self._chatgpt_monitor_notice = "Suggestions paused · Codex auto-switching is on"
                self._hold_reload_pending = True
                return
            try:
                from openswap.autoswitch import STATE_FILENAME
                engine = AutoSwitchEngine(
                    self.codex, load_settings(self.switcher.backup_dir),
                    lambda _event: None, dry_run=True,
                    # Read the same cooldown recorded by a confirmed desktop
                    # switch. Dry-run never writes this policy state.
                    state_path=self.codex.state_dir / STATE_FILENAME,
                )
                self._chatgpt_auto_generation += 1
                generation = self._chatgpt_auto_generation
                engine.on_event = lambda event, e=engine, g=generation: self._on_chatgpt_auto_event(event, e, g)
                self._chatgpt_auto_engine = engine
                self._chatgpt_monitor_notice = ""
                threading.Thread(target=self._run_chatgpt_auto_engine, args=(engine, generation), daemon=True).start()
            except Exception:
                self._chatgpt_auto_engine = None
                self._chatgpt_monitor_notice = "Suggestions paused · Couldn’t check accounts"
                self._chatgpt_auto_retry_at = time.monotonic() + 60
                self._hold_reload_pending = True
                self.switcher._logger.debug("ChatGPT candidate monitor failed to start")

        def _stop_chatgpt_auto_monitor(self, *, clear_pending=True):
            with self._event_lock:
                engine = self._chatgpt_auto_engine
                self._chatgpt_auto_engine = None
                self._chatgpt_auto_generation += 1
                if clear_pending:
                    self._pending_chatgpt_switch = None
                self._chatgpt_monitor_notice = ""
                self._hold_reload_pending = True
            if engine is not None:
                engine.stop()

        def _run_chatgpt_auto_engine(self, engine, generation):
            try:
                engine.run_loop()
            except Exception:
                self.switcher._logger.debug("ChatGPT candidate monitor stopped unexpectedly")
            finally:
                with self._event_lock:
                    if (engine is self._chatgpt_auto_engine and
                            generation == self._chatgpt_auto_generation):
                        self._chatgpt_auto_engine = None
                        self._pending_chatgpt_switch = None
                        self._chatgpt_monitor_notice = "Suggestions paused · Couldn’t check accounts"
                        self._chatgpt_auto_retry_at = time.monotonic() + 60
                        self._dirty = True
                        self._hold_reload_pending = True

        def _restart_chatgpt_auto_monitor(self):
            if chatgpt_suggestions_effective(self.settings):
                self._stop_chatgpt_auto_monitor(clear_pending=True)
                self._chatgpt_auto_retry_at = 0.0
                self._start_chatgpt_auto_monitor()

        def _reconcile_chatgpt_auto_mode(self):
            if not chatgpt_suggestions_effective(self.settings):
                if self._chatgpt_auto_engine is not None or self._pending_chatgpt_switch is not None:
                    self._stop_chatgpt_auto_monitor(clear_pending=True)
                return
            if self._codex_enabled():
                if self._chatgpt_auto_engine is not None or self._pending_chatgpt_switch is not None:
                    self._stop_chatgpt_auto_monitor(clear_pending=True)
                notice = "Suggestions paused · Codex auto-switching is on"
                if self._chatgpt_monitor_notice != notice:
                    self._chatgpt_monitor_notice = notice
                    self._hold_reload_pending = True
                return
            if (self._chatgpt_auto_engine is None and not self._desktop_switching and
                    self._login_session is None):
                self._start_chatgpt_auto_monitor()

        def _chatgpt_desktop_status(self):
            if self._desktop_status.startswith(
                ("Switch failed", "Switching account", "ChatGPT reopened")
            ):
                return self._desktop_status
            if not chatgpt_tab_desktop(self.settings):
                return "Enable ChatGPT switching in Settings"
            cap = self._chatgpt_capability
            now = time.monotonic()
            if not capability_is_fresh(cap, now=now):
                return "Checking ChatGPT…"
            if cap.state not in ("running", "stopped"):
                return desktop_capability_status_copy(cap)
            if self._chatgpt_monitor_notice:
                return self._chatgpt_monitor_notice
            if self._pending_chatgpt_switch is not None:
                return "Switch ready · Confirms before restart"
            if self.settings.chatgpt_auto_enabled:
                return "Confirms before restart"
            return desktop_capability_status_copy(cap)

        def _chatgpt_switching_persisted(self):
            try:
                return chatgpt_tab_desktop(MenuBarSettings.load(settings_path))
            except Exception:
                return False

        def _on_chatgpt_view_active(self):
            if self._desktop_switching or not chatgpt_tab_desktop(self.settings):
                return
            now = time.monotonic()
            if capability_is_fresh(self._chatgpt_capability, now=now):
                return
            if self._chatgpt_probe_inflight:
                return
            self._request_chatgpt_capability()

        def _request_chatgpt_capability(self):
            if self._desktop_switching or self._chatgpt_probe_inflight or not chatgpt_tab_desktop(self.settings):
                return
            self._chatgpt_capability_generation += 1
            generation = self._chatgpt_capability_generation
            self._chatgpt_capability = DesktopCapability("checking", "checking")
            self._chatgpt_probe_inflight = True
            threading.Thread(
                target=self._chatgpt_capability_worker, args=(generation,), daemon=True,
            ).start()

        def _chatgpt_capability_worker(self, generation):
            try:
                with self._desktop_app_lock:
                    if self._desktop_switching or not chatgpt_tab_desktop(self.settings):
                        return
                    if self.codex is None:
                        cap = DesktopCapability("invalid", "codex_unavailable")
                    elif not chatgpt_desktop_config_check(self.codex.home):
                        cap = DesktopCapability("invalid", "unsupported_config")
                    else:
                        cap = self._desktop_app.observe_capability(self.codex.home)
            except Exception:
                cap = DesktopCapability(
                    "invalid", "probe_failed", observed_at=time.monotonic(),
                )
            finally:
                if generation == self._chatgpt_capability_generation:
                    self._chatgpt_probe_inflight = False
            with self._event_lock:
                if generation != self._chatgpt_capability_generation or self._desktop_switching:
                    return
                self._chatgpt_capability = cap
                if self._desktop_status.startswith("ChatGPT reopened"):
                    self._desktop_status = "Switching reopens ChatGPT"
                self._hold_reload_pending = True

        def _begin_desktop_transaction(self, *, consent_generation=None):
            self._desktop_consent_generation = consent_generation
            self._chatgpt_capability_generation += 1
            self._chatgpt_probe_inflight = False
            self._stop_codex_engine()
            self._stop_chatgpt_auto_monitor(clear_pending=True)
            self._desktop_switching = True
            self._desktop_status = "Switching account · Reopening ChatGPT…"

        def _offer_enable_chatgpt_switching(self):
            if self._alert(
                title="ChatGPT switching is off",
                message=(
                    "Turn on ChatGPT switching in Settings to change the shared "
                    "login from this menu."
                ),
                ok="Enable in Settings", cancel="Cancel",
            ) != 1:
                return
            panel = self._panel
            if panel is None:
                return
            panel._select_provider("chatgpt")
            panel._show_settings()
            panel._desktop_settings_app = "chatgpt"
            panel._select_settings_section(
                SETTINGS_SECTION_DESKTOP if self.settings.desktop_features_enabled else SETTINGS_SECTION_GENERAL
            )

        def _on_chatgpt_auto_event(self, event, engine, generation):
            with self._event_lock:
                if (engine is not self._chatgpt_auto_engine or
                        generation != self._chatgpt_auto_generation):
                    return
                if event.kind == "switch" and getattr(event, "dry_run", False):
                    number = str(((getattr(event, "to_ref", None) or {}).get("number") or ""))
                    from_number = str(((getattr(event, "from_ref", None) or {}).get("number") or ""))
                    target_identity = self._chatgpt_target_identity(number)
                    source_identity = self._chatgpt_target_identity(from_number)
                    self._pending_chatgpt_switch = (
                        (from_number, source_identity, number, target_identity)
                        if number and target_identity and from_number and source_identity else None
                    )
                    self._chatgpt_monitor_notice = ""
                elif event.kind in ("no-switch", "all-exhausted", "error"):
                    self._pending_chatgpt_switch = None
                    self._chatgpt_monitor_notice = (
                        "Suggestions paused · Couldn’t check accounts" if event.kind == "error" else ""
                    )
            self._dirty = True
            self._hold_reload_pending = True

        def _validate_pending_chatgpt_switch(self):
            with self._event_lock:
                pending = self._pending_chatgpt_switch
            if pending is None:
                return
            from_number, source_identity, target, target_identity = pending
            try:
                active = self.codex.current_account_number() if self.codex else None
                live_identity = self.codex.live_identity() if self.codex else None
                switchable = set(self.codex.switchable_account_numbers()) if self.codex else set()
            except Exception:
                active, live_identity, switchable = None, None, set()
            if (not chatgpt_suggestions_effective(self.settings) or self._codex_enabled() or
                    target not in switchable or self._chatgpt_target_identity(target) != target_identity or
                    str(active or "") != from_number or live_identity != source_identity or
                    self._desktop_switching or
                    self._login_session is not None):
                with self._event_lock:
                    if self._pending_chatgpt_switch == pending:
                        self._pending_chatgpt_switch = None
                self._dirty = True
                self._hold_reload_pending = True

        def _review_chatgpt_switch(self, _sender=None):
            self._validate_pending_chatgpt_switch()
            with self._event_lock:
                pending = self._pending_chatgpt_switch
            choices = dict(desktop_switch_choices(self.snapshot))
            if pending is None or pending[2] not in choices:
                self._show_error("That suggested account is no longer available.")
                return
            self._make_desktop_switch(pending[2], choices[pending[2]], expected_pending=pending)(None)

        def on_toggle_chatgpt_switching(self, _sender):
            if not self.settings.desktop_features_enabled:
                return
            enabling = not self.settings.chatgpt_switching_enabled
            previous = self.settings.chatgpt_switching_enabled
            self.settings.chatgpt_switching_enabled = enabling
            try:
                self.settings.save(settings_path)
            except Exception:
                self.settings.chatgpt_switching_enabled = previous
                self._show_error("Couldn’t save ChatGPT switching settings. Try again.")
                return
            self._chatgpt_auto_retry_at = 0.0
            if enabling:
                self._start_chatgpt_auto_monitor()
            else:
                self._stop_chatgpt_auto_monitor(clear_pending=True)
                self._ensure_codex_engine()
            self.rebuild_menu()
            self._reload_main_panel_if_shown()

        def on_toggle_chatgpt_auto(self, _sender):
            if not chatgpt_tab_desktop(self.settings):
                self._show_error("Turn on ChatGPT switching in Settings first.")
                return
            enabling = not self.settings.chatgpt_auto_enabled
            if enabling:
                if self._alert(
                    title="Suggest ChatGPT switches?",
                    message="Suggests accounts based on Codex usage. You approve each ChatGPT restart.\n\nThis turns off Codex auto-switching.",
                    ok="Turn On", cancel="Cancel",
                ) != 1:
                    return
            self.settings.chatgpt_auto_enabled = enabling
            try:
                self.settings.save(settings_path)
            except Exception:
                self.settings.chatgpt_auto_enabled = not enabling
                self._show_error("Couldn’t save Auto-switch settings. Try again.")
                return
            self._chatgpt_auto_retry_at = 0.0
            if enabling:
                paused_codex = True
                if self._codex_enabled():
                    paused_codex = self._guard(lambda: set_setting(
                        self.switcher.backup_dir, "autoswitch.codexEnabled", "false"
                    ))
                if not paused_codex:
                    self.settings.chatgpt_auto_enabled = False
                    try:
                        self.settings.save(settings_path)
                    except Exception:
                        self.settings.chatgpt_auto_enabled = True
                        self._show_error(
                            "Couldn’t restore Auto-switch settings. "
                            "Turn off ChatGPT suggestions and try again."
                        )
                    else:
                        self._ensure_codex_engine()
                    self.rebuild_menu()
                    self._reload_main_panel_if_shown()
                    return
                self._stop_codex_engine()
                self._start_chatgpt_auto_monitor()
            else:
                self._stop_chatgpt_auto_monitor(clear_pending=True)
                self._ensure_codex_engine()
            self.rebuild_menu()
            self._reload_main_panel_if_shown()

        def _stop_engine(self):
            with self._event_lock:
                engine = self._engine
                codex_engine = self._codex_engine
                self._engine = None
                self._codex_engine = None
                self._hold_event = None
                self._hold_slot = None
                self._tick_slot = None
                self._engine_events = []
            if engine is not None:
                engine.stop()
            if codex_engine is not None:
                codex_engine.stop()

        def _restart_engine(self):
            """Apply changed core settings by restarting the running engine."""
            if self._engine is not None:
                self._stop_engine()
                self._start_engine()
                self._apply_hold_line()

        def _on_engine_event(self, event, engine=None):
            # Runs on the engine thread; must not raise. Queue for the main
            # thread, which surfaces notifications and reacts on the sync tick.
            with self._event_lock:
                if self._engine is None and self._codex_engine is None:
                    return
                if (
                    engine is not None
                    and engine is not self._engine
                    and engine is not self._codex_engine
                ):
                    return
                self._engine_events.append(event)
                self._hold_event, self._hold_slot, self._tick_slot = (
                    hold_cache_after_event(
                        self._hold_event,
                        self._hold_slot,
                        self._tick_slot,
                        event,
                    )
                )

        def _hold_line_for(self, snap: dict) -> str | None:
            with self._event_lock:
                event = hold_event_for_snapshot(
                    self._hold_event,
                    hold_slot=self._hold_slot,
                    active_num=snap.get("active_num"),
                )
                engine_on = self._engine is not None
            cards = panel_accounts(snap, now=time.time())
            active = next((card for card in cards if card.get("active")), None)
            title = None if active is None else active.get("title")
            return extra_hold_line(
                auto_enabled=engine_on,
                event=event,
                active_title=title,
                strategy=self._strategy(),
            )

        def _apply_hold_line(self):
            # Mutate in place. Rebinding self.snapshot from the UI thread can
            # drop a newer worker snapshot that landed between the copy and
            # the write-back. Reload the open main page only when copy
            # actually changed (or a deferred reload is waiting), so the 1s
            # tick does not rebuild under a click every second. Defer while
            # the left button is down so card mouseUp still fires. Settings
            # keeps its controls.
            snap = self.snapshot
            line = self._hold_line_for(snap)
            if self.snapshot is not snap:
                return
            changed = snap.get("hold_line") != line
            if changed:
                snap["hold_line"] = line
            left_down = False
            if changed or self._hold_reload_pending:
                import AppKit
                left_down = bool(AppKit.NSEvent.pressedMouseButtons() & 1)
            reload_now, self._hold_reload_pending = hold_panel_reload_plan(
                copy_changed=changed,
                pending=self._hold_reload_pending,
                left_mouse_down=left_down,
            )
            if reload_now:
                self._reload_main_panel_if_shown()

        def _drain_engine_events(self):
            with self._event_lock:
                events, self._engine_events = self._engine_events, []
            aliases = self._alias_map()
            claude_running = bool(self.snapshot.get("claude_running", True))
            codex_running = bool(self.snapshot.get("codex_running", True))
            for ev in events:
                if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                    self._animate_swap()
                running = (
                    codex_running
                    if getattr(ev, "provider", "claude") == "codex"
                    else claude_running
                )
                copy = notification_copy_for_event(ev, aliases, running=running)
                if copy is not None:
                    self._notify(copy)
                if ev.kind == "switch" and not getattr(ev, "dry_run", False):
                    self.refresh_async()  # reflect the switch promptly

        def _threshold(self) -> int:
            """Current auto-switch threshold from core settings (for the menu)."""
            try:
                return int(load_settings(self.switcher.backup_dir).threshold)
            except Exception:
                return 0

        def _strategy(self) -> str:
            """Current auto-switch strategy from core settings (for the menu)."""
            try:
                return load_settings(self.switcher.backup_dir).strategy
            except Exception:
                return "best"

        def _codex_enabled(self) -> bool:
            try:
                return bool(load_settings(self.switcher.backup_dir).codex_enabled)
            except Exception:
                return True

        def _has_codex(self) -> bool:
            # Settings describe the installed integration, not the current
            # roster. Keep ChatGPT/Codex controls discoverable before the first
            # OpenAI account is added.
            return self.codex is not None

        # ---- menu construction -----------------------------------------------
        def _attach_panel_once(self, timer):
            timer.stop()
            try:
                from openswap.menubar_panel import (
                    MenuBarPanel,
                    fit_status_item,
                    pin_status_item,
                )
                nsitem = self._nsapp.nsstatusitem
            except Exception:
                self.switcher._logger.debug("popover attach failed", exc_info=True)
                return
            try:
                pin_status_item(nsitem)
            except Exception:
                self.switcher._logger.debug("status item autosave failed", exc_info=True)
            try:
                fit_status_item(nsitem)
            except Exception:
                self.switcher._logger.debug("status item fit failed", exc_info=True)
            self._panel = MenuBarPanel(
                on_switch=self._on_panel_account_click,
                on_rotate=lambda *_a: self._switch(None)(None),
                on_best=lambda *_a: self._switch("best")(None),
                on_add_claude=lambda: self.on_add_claude_login(None),
                on_claude_desktop=lambda sender: self._show_claude_desktop_profiles(sender),
                claude_desktop_state=self._claude_desktop_profiles.settings_state,
                on_claude_desktop_check=self._claude_desktop_profiles.refresh_status,
                on_claude_desktop_action=self._claude_desktop_profiles.perform,
                on_toggle_auto=lambda *_a: self.on_toggle_autoswitch(None),
                on_toggle_chatgpt_auto=lambda *_a: self.on_toggle_chatgpt_auto(None),
                on_review_chatgpt_switch=self._review_chatgpt_switch,
                chatgpt_switch_pending=lambda: self._pending_chatgpt_switch is not None,
                on_more=self._popup_overflow,
                on_card_menu=self._popup_card_menu,
                on_toggle_codex_auto=lambda *_a: self._toggle_codex_auto(),
                auto_enabled=lambda: self.settings.auto_switch_enabled,
                snapshot=lambda: self.snapshot,
                threshold=self._threshold,
                on_setting=self._on_setting,
                settings=lambda: self.settings,
                strategy=self._strategy,
                has_codex=self._has_codex,
                codex_enabled=self._codex_enabled,
                desktop_status=self._chatgpt_desktop_status,
                chatgpt_capability=lambda: self._chatgpt_capability,
                on_chatgpt_view_active=self._on_chatgpt_view_active,
                account_state=lambda provider: self._account_states.get(provider, "error"),
                on_empty_action=self._on_empty_action,
                login_state=lambda: dict(self._login_ui_state),
                on_login_action=self._on_login_action,
                on_add_token=self._add_token,
                worker_enabled=lambda: self._worker_policy.enabled,
                worker_paused=lambda: self._worker_policy.paused,
                worker_status=lambda: self._worker_status_cache,
                on_worker_view_active=self._worker_view_active,
                worker_paired=lambda: self._worker_policy.control_service_url is not None,
                login_item=self._login_item_state,
                status_line=self._status_line_state,
                bundled_widget=_bundled_widget,
            )
            self._panel.attach(nsitem)
            # Requests left before the observers existed: `openswap setup`
            # starting the login item, another copy that lost the lock, or one
            # that handed over to this one.
            if self._on_handoff_request():
                return
            # Settle the setup wizard before any request opens the popover: a
            # copy `openswap setup` started (its login item runs this app) is
            # done with it, so a show request left by the copy it took over
            # from opens the cards, not the wizard.
            self._welcome_on_start()
            self._on_show_request()
            if self._show_popover_pending:
                self._show_popover_pending = False
                self.show_popover()

        def _on_show_request(self):
            """The show notification, and start-up: consume the show request
            and open the popover when it is fresh (menubar_instance)."""
            from openswap.menubar_instance import consume_move_request, consume_show_request

            if consume_show_request(self.switcher.backup_dir):
                source = consume_move_request(self.switcher.backup_dir)
                if source is not None:
                    self._open_setup_wizard(source=source)
                else:
                    self.show_popover()

        def _on_handoff_request(self):
            """The hand-over notification, and start-up: ``openswap setup`` or
            ``menubar --install-service`` started the login item while this
            copy, which launchd did not start, holds the lock. When launchd's
            copy is up, leave it a show request and quit, so it can take the
            lock instead of looping under KeepAlive. True when quitting."""
            from openswap.menubar_instance import (
                consume_handoff_request,
                leave_show_request,
                login_item_waiting,
            )

            if not consume_handoff_request(self.switcher.backup_dir):
                return False
            pid = login_item_waiting(logger=self.switcher._logger)
            if pid is None:
                return False
            self.switcher._logger.info("Handing the menu bar over to the login item (pid %s)", pid)
            leave_show_request(self.switcher.backup_dir, logger=self.switcher._logger)
            self.on_quit(None)
            return True

        def show_popover(self):
            """Open the popover: the app was opened again, or another copy asked."""
            panel = self._panel
            if panel is None:
                # Not attached yet (the first moments after start): open it then.
                self._show_popover_pending = True
                return
            if not panel.is_shown():
                panel.toggle()

        def _on_reopen(self):
            """The app was opened again while running: the setup wizard while
            it is due (a temporary copy, or setup not finished), else the
            popover."""
            from openswap import bundle

            if bundle.is_frozen() and (
                bundle.runs_from_temporary_location() or not self.settings.welcome_done
            ):
                self._open_setup_wizard()
            else:
                self.show_popover()

        # ---- first run: the setup wizard (plans 020, 021) ------------------
        def _login_item_runs_this(self) -> bool:
            """Whether the login item starts this program (not merely exists:
            a plist for another copy of the app is not this app's login item)."""
            from openswap import launch_agent

            try:
                return bool(launch_agent.login_item_runs_this())
            except Exception:
                self.switcher._logger.debug("login item read failed", exc_info=True)
                return False

        def _welcome_on_start(self):
            """Open the setup wizard once, by itself, when it is due.

            An LSUIElement app shows no window, so without this a first launch
            from Finder looks like nothing happened. A copy at a temporary
            location opens it on Move, set up or not: opening a newer DMG is
            also how a Finder install updates.
            """
            from openswap import bundle

            action = welcome_on_start(
                self.settings,
                frozen=bundle.is_frozen(),
                temporary=bundle.runs_from_temporary_location(),
                login_item_runs_this=self._login_item_runs_this(),
            )
            if action == "done":
                # `openswap setup` already set this app up; nothing to ask.
                self.settings.welcome_done = True
                try:
                    self.settings.save(settings_path)
                except OSError:
                    self.switcher._logger.debug("could not save welcome_done", exc_info=True)
            elif action in ("show", "move"):
                self._open_setup_wizard()

        def _on_welcome_continue(self, open_at_login):
            """Save ``welcome_done``, then apply the switch (a hand-over last)."""
            from openswap import bundle

            if bundle.runs_from_temporary_location():
                # Nothing may be pinned to a path that vanishes; the wizard
                # only offers Move there, so this is never reached from it.
                return
            self.settings.welcome_done = True
            try:
                self.settings.save(settings_path)
            except OSError as e:
                self._show_error(f"Couldn't save settings: {e}")
            if not open_at_login and not self._login_item_runs_this():
                # Off, and no login item of this app's to turn off: a plist for
                # another copy (the cask's, while a dev build runs) is left alone.
                return
            self._login_item_from_welcome = True
            self._apply_login_item(bool(open_at_login))
            if not self._login_item_busy:
                self._login_item_from_welcome = False  # it did not start

        def _open_setup_wizard(self, step=None, source=None):
            """Show the setup wizard (``setup_wizard``) on ``step``, or on its
            first step. Each opening starts from fresh choices. ``source`` is
            another copy's app to offer Move for (a move request)."""
            from openswap.setup_wizard import initial_choices, wizard_steps

            self._wizard_move = None
            self._wizard_source = source
            facts = self._wizard_facts()
            if self._wizard is None:
                try:
                    from openswap.menubar_wizard import make_setup_wizard

                    self._wizard = make_setup_wizard(self._on_wizard_action)
                except Exception:
                    self.switcher._logger.warning("setup wizard unavailable", exc_info=True)
                    self.show_popover()
                    return
            self._wizard_choices = initial_choices(facts)
            steps = wizard_steps(facts)
            self._wizard_step = step if step in steps else steps[0]
            self._wizard_seen = self._wizard_account_signature()
            self._wizard.show(self._wizard_page(facts))

        def _wizard_page(self, facts=None):
            from openswap.setup_wizard import wizard_page

            facts = facts if facts is not None else self._wizard_facts()
            return wizard_page(self._wizard_step, facts, self._wizard_choices)

        def _wizard_render(self):
            if self._wizard is not None and self._wizard_step is not None:
                self._wizard_seen = self._wizard_account_signature()
                self._wizard.render(self._wizard_page())

        def _wizard_cards(self):
            cards = panel_accounts(self.snapshot)
            claude = tuple(
                (str(c.get("title") or ""), str(c.get("subtitle") or ""))
                for c in provider_cards(cards, "claude")
            )
            codex = tuple(
                (str(c.get("title") or ""), str(c.get("subtitle") or ""))
                for c in provider_cards(cards, "chatgpt")
            )
            return claude, codex

        def _codex_signing_in(self) -> bool:
            return self._login_ui_state.get("stage") in ("starting", "waiting", "saving")

        def _wizard_account_signature(self):
            """What the account steps show, to redraw them when it changes."""
            claude, codex = self._wizard_cards()
            return (
                claude, codex,
                self._account_states.get("claude", "error"),
                self._account_states.get("chatgpt", "error"),
                self._claude_login is not None,
                self._codex_signing_in(),
            )

        def _wizard_facts(self):
            """Everything a wizard page reads, from the same sources Settings
            and the popover use."""
            import shutil as _shutil

            from openswap import bundle
            from openswap.setup_wizard import WizardFacts

            temporary = self._wizard_source is not None or bundle.runs_from_temporary_location()
            if temporary and self._wizard_move is None:
                app = self._wizard_source or bundle.app_path()
                try:
                    self._wizard_move = bundle.move_plan(app, bundle.applications_dir())
                except Exception as e:
                    self.switcher._logger.warning("Move to Applications plan failed", exc_info=True)
                    self._wizard_move = {"action": "error", "error": str(e)}
            claude, codex = self._wizard_cards()
            exe = bundle.executable()
            on_path = _shutil.which("openswap")
            try:
                command_on_path = bool(on_path) and exe is not None and (
                    Path(on_path).resolve() == exe.resolve()
                )
            except (OSError, RuntimeError):
                command_on_path = False
            return WizardFacts(
                temporary=temporary,
                move=self._wizard_move,
                claude_accounts=claude,
                claude_state=self._account_states.get("claude", "error"),
                claude_signing_in=self._claude_login is not None,
                claude_installed=resolve_claude_bin() is not None,
                codex_available=self._has_codex(),
                codex_accounts=codex,
                codex_state=self._account_states.get("chatgpt", "error"),
                codex_signing_in=self._codex_signing_in(),
                login_item=self._login_item_runs_this(),
                status_line=self._status_line_state(),
                widget=bundle.is_frozen() and not temporary,
                homebrew=bundle.installed_by_homebrew(),
                command_on_path=command_on_path,
                command=str(exe) if exe is not None else "",
            )

        def _on_wizard_action(self, action):
            """A wizard button or switch (``setup_wizard.ACTION_*``)."""
            from openswap.setup_wizard import ACCOUNT_ACTIONS, next_step, previous_step

            if self._wizard is None or self._wizard_step is None:
                return
            if action == "close":
                self._wizard.close()
                self._wizard_step = None
                return
            if action in ("next", "back"):
                facts = self._wizard_facts()
                move = next_step if action == "next" else previous_step
                self._wizard_step = move(self._wizard_step, facts)
                self._wizard.render(self._wizard_page(facts))
                return
            if action == "toggle_open_at_login":
                self._wizard_choices.open_at_login = not self._wizard_choices.open_at_login
            elif action == "toggle_status_line":
                self._wizard_choices.status_line = not self._wizard_choices.status_line
            elif action in ACCOUNT_ACTIONS:
                # The popover's empty-state actions: a browser sign-in, or
                # saving the login Claude Code (or Codex) already has.
                self._on_empty_action(*ACCOUNT_ACTIONS[action])
            elif action in ("move", "open_existing"):
                self._wizard_move_app(action)
                return
            elif action == "finish":
                self._wizard_finish()
                return
            self._wizard_render()

        def _wizard_tick(self):
            """Redraw an account step when its roster or sign-in changed (a
            browser sign-in finishes on its own time)."""
            from openswap.setup_wizard import CLAUDE, CODEX

            if self._wizard is None or self._wizard_step not in (CLAUDE, CODEX):
                return
            if not self._wizard.is_visible():
                return
            if self._wizard_account_signature() != self._wizard_seen:
                self._wizard_render()

        def _wizard_move_app(self, action):
            """Move: copy into Applications (or open the copy already there),
            then quit; a detached shell opens it once this copy is gone and
            ejects the disk image."""
            from openswap import bundle

            plan = self._wizard_move or {}
            source = self._wizard_source or bundle.app_path()
            # The plan was made when the window opened; Applications may have
            # changed since (another install, a deleted copy). Plan again and,
            # when the answer differs, show that instead of acting on the old one.
            try:
                fresh = bundle.move_plan(source, Path(plan["destination"]))
            except (OSError, KeyError, ValueError) as e:
                self._show_error(f"Couldn’t check Applications: {e}")
                return
            if fresh.get("action") != plan.get("action") or fresh.get("target") != plan.get("target"):
                self._wizard_move = fresh
                self._wizard_render()
                return
            plan = fresh
            try:
                if action == "move":
                    target = bundle.copy_to_applications(source, plan)
                else:
                    target = Path(plan["target"])
                    if self._wizard_source is not None and bundle.app_is(target):
                        # The copy already there is this one, running: nothing
                        # to open but the popover. The DMG is done with. (A
                        # DMG copy that won the lock on a retry consumes its
                        # own request; it is not the installed copy.)
                        bundle.eject(plan.get("eject"))
                        self._wizard.close()
                        self._wizard_step = None
                        self.show_popover()
                        return
            except (ClaudeSwitchError, KeyError) as e:
                self._show_error(str(e))
                self._wizard_move = None
                self._wizard_render()
                return
            self.switcher._logger.info("Moved to %s; reopening from there", target)
            try:
                bundle.relaunch_after_exit(target, os.getpid(), plan.get("eject"))
            except OSError as e:
                self._show_error(f"OpenSwap is in {target.parent}, but couldn’t reopen from there: {e}")
                return
            self._wizard.close()
            self._wizard_step = None
            self.on_quit(None)

        def _wizard_finish(self):
            """Open OpenSwap: apply the status line, then save
            ``welcome_done`` and apply Open at login (a hand-over last)."""
            from openswap import paths
            from openswap.statusline import set_installed

            choices = self._wizard_choices
            self._wizard.close()
            self._wizard_step = None
            if choices.status_line != (self._status_line_state() == "on"):
                try:
                    set_installed(
                        paths.get_claude_config_home(), self.switcher.backup_dir, choices.status_line,
                    )
                except (ClaudeSwitchError, OSError) as e:
                    self._show_error(f"Couldn't change the Claude Code status line: {e}")
            self._on_welcome_continue(choices.open_at_login)
            if not self._login_item_busy:
                self.show_popover()

        def _on_empty_action(self, provider, action):
            if self._desktop_switching or self._refreshing:
                return
            if provider not in ("claude", "chatgpt"):
                return
            if action == "retry":
                self.refresh_async()
            elif action == "start" and provider == "claude":
                self.on_add_claude_login(None)
            elif action in ("add", "start"):
                if provider == "chatgpt":
                    self._on_login_action("start")
                else:
                    self.on_add_login(None)
            elif action == "capture" and provider == "chatgpt":
                self.on_add_codex_login(None)

        def _show_login_panel(self):
            if self._panel is not None:
                self._panel.show_login()
                if not self._panel.is_shown():
                    self._panel.toggle()

        def _on_login_action(self, action, alias=None):
            if self._desktop_switching:
                return
            if self._login_ui_state.get("stage") in ("saving", "canceling"):
                return
            session = self._login_session
            if action in ("start", "retry", "device"):
                if self.codex is None:
                    return
                if self._panel is None:
                    self._show_error("Sign-in needs the account panel. Restart OpenSwap and try again.")
                    return
                if action == "start" and session is not None and session.state().get("stage") != "canceled":
                    self._show_login_panel()
                    return
                from openswap.codex.onboarding import LoginSession
                try:
                    new_session = LoginSession(self.codex, mode="device" if action == "device" else "browser")
                except Exception:
                    self._login_ui_state = {"stage": "error", "message": "Couldn’t start sign-in. Try again."}
                    self._show_login_panel()
                    return
                self._stop_chatgpt_auto_monitor(clear_pending=True)
                self._login_session = new_session
                self._login_ui_state = {"stage": "starting"}
                self._show_login_panel()
                threading.Thread(target=self._login_run_worker, args=(session, new_session), daemon=True).start()
            elif action == "capture":
                if session is None:
                    self.on_add_codex_login(None)
            elif action == "cancel":
                self._hold_reload_pending = True
                if session is None:
                    self._login_ui_state = {"stage": "idle"}
                else:
                    self._login_ui_state = {"stage": "canceling"}
                    threading.Thread(target=self._login_cancel_worker, args=(session,), daemon=True).start()
            elif action == "save" and session is not None:
                if session.state().get("stage") != "ready":
                    return
                self._login_ui_state = {**self._login_ui_state, "stage": "saving"}
                self._hold_reload_pending = True
                threading.Thread(target=self._login_save_worker, args=(session, alias), daemon=True).start()
            elif action == "dismiss":
                if self._login_ui_state.get("stage") in ("saved", "canceled"):
                    self._login_session = None
                    self._login_ui_state = {"stage": "idle"}
                    self._hold_reload_pending = True
                    self._start_chatgpt_auto_monitor()
            elif action in ("copy_link", "open_browser", "copy_code") and session is not None:
                value = (session.state().get("device_code") if action == "copy_code" else session.login_url())
                if not value:
                    return
                if action == "open_browser":
                    import webbrowser
                    webbrowser.open(value)
                else:
                    from AppKit import NSPasteboard, NSPasteboardTypeString
                    clipboard = NSPasteboard.generalPasteboard()
                    clipboard.clearContents()
                    clipboard.setString_forType_(value, NSPasteboardTypeString)

        def _login_run_worker(self, previous, session):
            if previous is not None:
                previous.cancel()
            session.run()

        def _login_cancel_worker(self, session):
            session.cancel()
            with self._event_lock:
                self._login_canceled_session = session

        def _login_save_worker(self, session, alias):
            try:
                number = session.save(alias=alias or None)
                outcome = (session, number, None)
            except ClaudeSwitchError as exc:
                outcome = (session, None, str(exc))
            except Exception:
                outcome = (session, None, "Couldn’t save the account. Try again.")
            with self._event_lock:
                self._login_save_result = outcome

        def _poll_login(self):
            with self._event_lock:
                canceled = self._login_canceled_session
                self._login_canceled_session = None
                result = self._login_save_result
                self._login_save_result = None
            if canceled is self._login_session and canceled is not None:
                self._login_session = None
                self._login_ui_state = {"stage": "idle"}
                self._hold_reload_pending = True
                self._start_chatgpt_auto_monitor()
            if result is not None and result[0] is self._login_session:
                _session, _number, error = result
                if error:
                    self._login_ui_state = {"stage": "error", "message": error}
                else:
                    self._login_ui_state = {"stage": "saved"}
                    self.refresh_async()
                self._hold_reload_pending = True
                return
            if self._login_session is None or self._login_ui_state.get("stage") in ("saving", "canceling", "saved"):
                return
            state = self._login_session.state()
            # Keep save errors visible until the user chooses Retry or Cancel.
            if self._login_ui_state.get("stage") == "error" and state.get("stage") == "ready":
                return
            if state != self._login_ui_state:
                self._login_ui_state = state
                self._hold_reload_pending = True

        def _enable_codex_account(self, number):
            if self.codex is None:
                return False
            if self._alert(title="Enable account?", message="This account can be used by automatic rotation when it’s on.",
                           ok="Enable", cancel="Cancel") != 1:
                return False
            return self._guard(lambda: self.codex.set_account_disabled(number, False))

        def _toggle_desktop_features(self):
            if self._desktop_switching or self._claude_desktop_profiles.busy:
                self._show_error("Wait for the current app opening or switch to finish, then try again.")
                return
            previous = self.settings.desktop_features_enabled
            self.settings.desktop_features_enabled = not previous
            try:
                self.settings.save(settings_path)
            except Exception:
                self.settings.desktop_features_enabled = previous
                self._show_error("Couldn’t save Desktop settings. Try again.")
                return
            if previous:
                self._claude_desktop_profiles.suspend_checks()
                self._chatgpt_capability_generation += 1
                self._chatgpt_probe_inflight = False
                self._stop_chatgpt_auto_monitor(clear_pending=True)
                self._ensure_codex_engine()
            else:
                self._chatgpt_auto_retry_at = 0.0
                self._start_chatgpt_auto_monitor()
                if self._claude_desktop_profiles.signin_links:
                    self._claude_desktop_profiles.signin_links.prepare()
            self.rebuild_menu()
            panel = self._panel
            if panel is not None and panel.is_shown():
                if getattr(panel, "_page", None) == SETTINGS_PAGE:
                    panel.reload()
                else:
                    self._reload_main_panel_if_shown()

        def _mark_claude_desktop_guide_seen(self):
            previous = self.settings.claude_desktop_guide_seen
            self.settings.claude_desktop_guide_seen = True
            try:
                self.settings.save(settings_path)
            except OSError:
                self.settings.claude_desktop_guide_seen = previous
                self._show_error("Could not save this preference. The guide may appear again next time.")

        def _on_setting(self, row_id, value):
            if row_id == "claude_desktop_guide":
                self._claude_desktop_profiles.show_guide()
                return
            if row_id == "claude_desktop_check":
                self._claude_desktop_profiles.refresh_status()
                return
            if row_id == "desktop_features_enabled":
                self._toggle_desktop_features()
                return
            if row_id == "remote_tasks_web_choice" and not value:
                return  # the summary item: nothing to toggle
            if row_id == "remote_tasks_setup":
                self._run_guided_setup()
            elif row_id in ("remote_tasks_connect", "remote_tasks_reconnect"):
                self._run_connect()
            elif row_id == "remote_tasks_connect_other":
                self._connect_other_server()
            elif row_id == "open_at_login":
                self._toggle_login_item()
            elif row_id == "open_at_login_failed":
                subprocess.run(["open", LOGIN_ITEMS_SETTINGS_URL], check=False)
                return
            elif row_id == "setup_assistant":
                from openswap.setup_wizard import WELCOME

                panel = self._panel
                if panel is not None and panel.is_shown():
                    panel.toggle()
                self._open_setup_wizard(WELCOME)
                return
            elif row_id in {
                "remote_tasks_enabled", "remote_tasks_paused", "remote_tasks_stop",
                "remote_tasks_account", "remote_tasks_web_choice",
            }:
                self._worker_action(row_id, value)
            elif row_id == "menu_bar_provider":
                if value not in MENU_BAR_PROVIDER_CHOICES:
                    return
                self.settings.menu_bar_provider = value
                self._save_and_rebuild()
            elif row_id == "show_account_name":
                self.on_toggle_name(None)
            elif row_id == "title_pct_5h":
                self.on_toggle_title_5h(None)
            elif row_id == "title_pct_7d":
                self.on_toggle_title_7d(None)
            elif row_id == "title_scoped":
                self.on_toggle_scoped(None)
            elif row_id == "confirm_switch":
                self.on_toggle_confirm_switch(None)
            elif row_id == "refresh_interval":
                self._make_interval(int(value))(None)
            elif row_id == "auto_switch_enabled":
                self.on_toggle_autoswitch(None)
            elif row_id == "chatgpt_switching_enabled":
                self.on_toggle_chatgpt_switching(None)
            elif row_id == "chatgpt_auto_enabled":
                self.on_toggle_chatgpt_auto(None)
            elif row_id == "threshold":
                self._make_threshold(int(value))(None)
            elif row_id == "strategy":
                self._make_strategy(value)(None)
            elif row_id == "codex_enabled":
                if not self._toggle_codex_auto():
                    return
            elif row_id == "kickoff_enabled":
                self.on_toggle_kickoff(None)
            elif row_id == "claude_status_line":
                self._toggle_status_line()
            elif row_id == "kickoff_time":
                self.on_kickoff_time(value)
                # The popup already shows the pick; do not rebuild the page
                # under the open menu.
                return
            else:
                return
            panel = self._panel
            if (
                panel is not None
                and panel.is_shown()
                and getattr(panel, "_page", None) == SETTINGS_PAGE
            ):
                panel.reload()

        def _login_item_state(self) -> dict:
            """General › This Mac, read each time Settings is built."""
            from openswap import bundle, launch_agent

            try:
                enabled = launch_agent.login_item_enabled()
            except OSError:
                enabled = False
            return {
                **self._login_item_last,
                "enabled": enabled,
                "temporary": bundle.runs_from_temporary_location(),
                "busy": self._login_item_busy,
                "start_failed": launch_agent.last_start_failure(self.switcher.backup_dir) is not None,
                "as_login_item": launch_agent.runs_as_login_item(),
            }

        def _toggle_login_item(self):
            """Settings › Open at login."""
            from openswap import launch_agent

            try:
                enabled = not launch_agent.login_item_enabled()
            except OSError:
                return
            self._apply_login_item(enabled)

        def _apply_login_item(self, enabled):
            """``launch_agent.set_login_item(enabled)`` off the main thread (it
            runs launchctl and waits for launchd's pid). The 1 s tick applies
            the outcome (_drain_login_item_result): quits on a hand-over, else
            shows it under the switch. Ignored while one is running."""
            from openswap import bundle

            if self._login_item_busy or bundle.runs_from_temporary_location():
                return
            self._login_item_busy = True
            self._login_item_last = {}
            threading.Thread(
                target=self._login_item_worker, args=(enabled,), daemon=True
            ).start()

        def _login_item_worker(self, enabled):
            from openswap import launch_agent

            log = self.switcher._logger
            try:
                outcome = launch_agent.set_login_item(enabled, backup_dir=self.switcher.backup_dir)
                log.info("Open at login %s: %s", "on" if enabled else "off", outcome)
                result = {"outcome": outcome}
            except launch_agent.LoginItemNotStarted as e:
                # Recorded in login_item_status.json; the warning row says it.
                log.warning("Open at login: launchd did not start the login item: %s", e)
                result = {}
            except ClaudeSwitchError as e:
                log.warning("Open at login failed: %s", e)
                result = {"error": str(e)}
            except Exception:
                log.error("Open at login failed", exc_info=True)
                result = {"error": "Couldn’t change Open at login. See openswap.log."}
            with self._event_lock:
                self._login_item_result = result

        def _drain_login_item_result(self):
            with self._event_lock:
                result = self._login_item_result
                self._login_item_result = None
            if result is None:
                return
            self._login_item_busy = False
            from_welcome, self._login_item_from_welcome = self._login_item_from_welcome, False
            if result.get("outcome") == "handoff":
                # set_login_item returned once launchd's copy was up; it is
                # waiting for this copy's menu bar lock, so quit now. After the
                # wizard, that copy opens its popover, as this one would have.
                if from_welcome:
                    from openswap.menubar_instance import leave_show_request

                    leave_show_request(self.switcher.backup_dir, logger=self.switcher._logger)
                self.on_quit(None)
                return
            if from_welcome and result.get("outcome"):
                self.show_popover()
            self._login_item_last = result
            if from_welcome and not result.get("outcome"):
                # Continue went on to the cards, where Settings' hint (an
                # error) or warning (a failed start: no outcome, no error)
                # would go unseen.
                self._show_error(result.get("error") or OPEN_AT_LOGIN_FAILED_ALERT)
            panel = self._panel
            if (
                panel is not None
                and panel.is_shown()
                and getattr(panel, "_page", None) == SETTINGS_PAGE
            ):
                panel.reload()

        def _status_line_state(self) -> str:
            """``"on"``: Claude Code's status line runs this OpenSwap.
            ``"other"``: an OpenSwap hook for another program (a bare
            ``openswap statusline``, or another copy). ``"off"``: neither."""
            from openswap import paths
            from openswap.statusline import is_installed, runs_this_program

            config_home = paths.get_claude_config_home()
            try:
                if runs_this_program(config_home):
                    return "on"
                return "other" if is_installed(config_home) else "off"
            except Exception:
                self.switcher._logger.debug("status line read failed", exc_info=True)
                return "off"

        def _toggle_status_line(self) -> None:
            """Settings › This Mac: `openswap statusline --install/--uninstall`.

            Flips what is on disk now, not what the page showed: the CLI may
            have changed it since the page was built. A hook for another
            OpenSwap shows as off, so this turns it on, which repoints it. From
            a translocated or mounted copy, turning it on is refused (the row
            is disabled too).
            """
            from openswap import paths
            from openswap.statusline import set_installed

            enable = self._status_line_state() != "on"
            try:
                set_installed(paths.get_claude_config_home(), self.switcher.backup_dir, enable)
            except (ClaudeSwitchError, OSError) as e:
                self._show_error(f"Couldn't change the Claude Code status line: {e}")

        def _toggle_codex_auto(self) -> bool:
            """Flip live Codex CLI rotation (Settings row and the Codex tab's footer toggle)."""
            if (not self._codex_enabled()) and chatgpt_suggestions_effective(self.settings):
                self._show_error("Turn off ChatGPT suggestions before enabling Codex auto-switching.")
                return False
            try:
                current = load_settings(self.switcher.backup_dir).codex_enabled
                set_setting(
                    self.switcher.backup_dir,
                    "autoswitch.codexEnabled",
                    "false" if current else "true",
                )
            except Exception as e:
                self._show_error(f"Couldn't set Codex auto-switch: {e}")
                return False
            if current:
                self._stop_codex_engine()
            else:
                self._ensure_codex_engine()
            self._reload_main_panel_if_shown()
            return True

        def _popup_overflow(self, sender=None):
            menu = self.menu._menu
            view = sender if sender is not None else self._nsapp.nsstatusitem.button()
            if view is None:
                return
            loc = (0, 0)
            try:
                loc = (0, view.bounds().size.height)
            except Exception:
                pass
            menu.popUpMenuPositioningItem_atLocation_inView_(None, loc, view)

        def _popup_card_menu(self, card, view):
            """Rename / Disable / Remove for one account, under its card's ··· button."""
            if view is None:
                return
            num = card.get("num")
            actions = {
                CARD_MENU_RENAME: self._make_rename(num, card.get("email") or "", card.get("alias") or None),
                CARD_MENU_TOGGLE_DISABLED: self._make_toggle_disabled(num, account_disabled(card)),
                CARD_MENU_REMOVE: self._make_remove(num),
            }
            menu = rumps.MenuItem("Account")
            # provider_cards already relabels Codex rows "chatgpt" for the tab.
            ui_provider = "chatgpt" if card.get("provider") in ("codex", "chatgpt") else "claude"
            for action, label in card_menu_items(
                card, ui_provider, desktop=chatgpt_tab_desktop(self.settings),
            ):
                menu.add(rumps.MenuItem(label, callback=actions[action]))
            loc = (0, 0)
            try:
                loc = (0, view.bounds().size.height)
            except Exception:
                pass
            try:
                menu._menu.popUpMenuPositioningItem_atLocation_inView_(None, loc, view)
            finally:
                # Same leak as rebuild_menu: rumps registers every callback in a
                # process-global dict and never drops it.
                _reg = getattr(rumps.rumps.NSApp, "_ns_to_py_and_callback", None)
                if _reg is not None:
                    # The parent registers itself too, callback or not.
                    _reg.pop(getattr(menu, "_menuitem", None), None)
                    for item in menu._menu.itemArray():
                        _reg.pop(item, None)

        def _fit_status_item(self, title: str | None = None):
            nsapp = getattr(self, "_nsapp", None)
            nsitem = getattr(nsapp, "nsstatusitem", None) if nsapp is not None else None
            if nsitem is None:
                return
            try:
                from openswap.menubar_panel import fit_status_item

                fit_status_item(
                    nsitem,
                    title=title,
                )
                if self._status_icon_animator is not None:
                    self._status_icon_animator.reapply()
            except Exception:
                self.switcher._logger.debug("status item fit failed", exc_info=True)

        def _animate_swap(self):
            nsapp = getattr(self, "_nsapp", None)
            nsitem = getattr(nsapp, "nsstatusitem", None) if nsapp is not None else None
            if nsitem is None:
                return
            try:
                from openswap.menubar_panel import StatusIconAnimator

                if self._status_icon_animator is None:
                    self._status_icon_animator = StatusIconAnimator(nsitem)
                self._status_icon_animator.play()
            except Exception:
                # Visual feedback must never interfere with a completed swap.
                self.switcher._logger.debug("status icon animation failed", exc_info=True)

        def rebuild_menu(self):
            title = format_menu_bar_title(self.snapshot, self.settings)
            self.title = title
            self._fit_status_item(title)
            # Stop a rumps memory leak: rumps registers each menu item's callback
            # in the process-global NSApp._ns_to_py_and_callback, but Menu.clear()
            # never removes them, so rebuilding the whole menu on every refresh
            # leaks every item forever (~1GB after days on a busy machine). Purge
            # this menu's entries before we tear it down. We walk the *native*
            # NSMenu tree (itemArray, recursing into submenus) rather than the
            # rumps Python dict: that dict is keyed by title and silently drops
            # same-title items, which would leave leaked entries behind.
            # Guard the private rumps attribute: if a future rumps release renames
            # it, degrade to "leaks again" rather than crashing on every rebuild.
            _reg = getattr(rumps.rumps.NSApp, "_ns_to_py_and_callback", None)
            if _reg is not None:
                def _purge(nsmenu):
                    for _it in nsmenu.itemArray():
                        _reg.pop(_it, None)
                        _sub = _it.submenu()
                        if _sub is not None:
                            _purge(_sub)
                _purge(self.menu._menu)
            self.menu.clear()
            if self._desktop_switching:
                self.menu = [rumps.MenuItem("Restarting ChatGPT…", callback=None)]
                return
            # Overflow menu (popover More…): account switching and settings live
            # in the popover, so this list is management only. With the popover
            # attached, Rotate / Best sit in its footer and Rename / Disable /
            # Remove hang off each card's ··· button, so they stay out of here;
            # the full list remains for the menu-only fallback.
            fallback = self._panel is None
            self.menu = [
                *([
                    rumps.MenuItem(f"Update available: OpenSwap {self._update_shown}…", callback=self.on_update),
                    None,
                ] if self._update_shown else []),
                *([
                    rumps.MenuItem("Claude Code: rotate to next", callback=self._switch(None)),
                    rumps.MenuItem("Claude Code: switch to best", callback=self._switch("best")),
                ] if fallback else []),
                rumps.MenuItem("Claude Code: next available", callback=self._switch("next-available")),
                None,
                self._add_menu(rumps),
                *([self._claude_desktop_profiles.menu(rumps)] if self.settings.desktop_features_enabled else []),
                *([self._desktop_menu(rumps)] if fallback and self.settings.desktop_features_enabled else []),
                *([
                    self._rename_menu(rumps),
                    self._disable_menu(rumps),
                    self._remove_menu(rumps),
                ] if fallback else []),
                rumps.MenuItem("Refresh current credentials", callback=self.on_refresh_creds),
                self._history_menu(rumps),
                None,
                rumps.MenuItem("Refresh now", callback=self.on_refresh_now),
                None,
                rumps.MenuItem("Uninstall OpenSwap…", callback=self.on_uninstall),
                rumps.MenuItem("Quit", callback=self.on_quit),
            ]
            if self._panel is not None:
                try:
                    self._nsapp.nsstatusitem.setMenu_(None)
                except AttributeError:
                    pass
                # Do not reload an open popover: replacing the view tree
                # between mouseDown and mouseUp swallows the account-row click.
                # Settings-page actions reload from _on_setting, not from here.

        def _claude_desktop_changed(self):
            panel = self._panel
            if panel is not None and getattr(panel, "_menu_open", False):
                self._claude_desktop_update_pending = True
                return  # preserve the menu's anchor and callbacks until it closes
            self._claude_desktop_update_pending = False
            self.rebuild_menu()
            if (panel is not None and panel.is_shown() and (
                    (getattr(panel, "_page", None) == SETTINGS_PAGE
                     and getattr(panel, "_settings_section", None) == SETTINGS_SECTION_DESKTOP)
                    or (getattr(panel, "_page", None) == MAIN_PAGE
                        and getattr(panel, "_selected_provider", None) == "claude_desktop"))):
                panel.reload()

        def _show_claude_desktop_profiles(self, sender):
            # Rebuild from disk so CLI edits are visible immediately. Reusing
            # the regular menu also uses its rumps callback-registry cleanup.
            if self._desktop_switching or not self.settings.desktop_features_enabled:
                return
            self.rebuild_menu()
            menu = self.menu["Claude Desktop profiles"]
            menu._menu.popUpMenuPositioningItem_atLocation_inView_(None, (0, sender.bounds().size.height), sender)

        def _add_menu(self, rumps):
            menu = rumps.MenuItem("Add account")
            menu.add(rumps.MenuItem("Sign in to Claude Code…", callback=self.on_add_claude_login))
            menu.add(rumps.MenuItem("Save current Claude Code login", callback=self.on_add_login))
            if self.codex is not None:
                menu.add(rumps.MenuItem("Sign in with ChatGPT…", callback=lambda _sender: self._on_login_action("start")))
                menu.add(rumps.MenuItem(
                    "Save current Codex login", callback=self.on_add_codex_login
                ))
            if hasattr(self.switcher, "add_account_from_token"):
                menu.add(rumps.MenuItem("From API key or setup token…", callback=self.on_add_token))
            return menu

        def _rename_menu(self, rumps):
            menu = rumps.MenuItem("Rename account")
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.add(rumps.MenuItem("No managed accounts", callback=None))
            for num, email, _is_active, _display, _last_good, alias, _org, _disabled, _fetched_at in accounts:
                label = f"{num}  {alias}  ({email})" if alias else f"{num}  {email}"
                menu.add(rumps.MenuItem(label, callback=self._make_rename(num, email, alias)))
            return menu

        def _desktop_menu(self, rumps):
            menu = rumps.MenuItem("Switch ChatGPT app")
            if self.codex is not None and self._codex_enabled():
                menu.add(rumps.MenuItem(
                    "Pause Codex auto-switching for testing…",
                    callback=self._pause_codex_for_desktop,
                ))
                menu.add(None)
            choices = desktop_switch_choices(self.snapshot) if self.codex else []
            if not choices:
                menu.add(rumps.MenuItem("Add a ChatGPT login via Codex first", callback=None))
            for num, label in choices:
                menu.add(rumps.MenuItem(label, callback=self._make_desktop_switch(num, label)))
            return menu

        def _pause_codex_for_desktop(self, _sender):
            if self._desktop_switching or not self.settings.desktop_features_enabled:
                return
            if self._alert(
                title="Pause Codex auto-switching?",
                message=(
                    "This turns off automatic Codex account rotation for all OpenSwap "
                    "instances using this account store. Claude Code rotation is unchanged. "
                    "It stays off until you re-enable it in Settings or with the CLI."
                ),
                ok="Pause Codex", cancel="Cancel",
            ) != 1:
                return
            if self._guard(lambda: set_setting(
                self.switcher.backup_dir, "autoswitch.codexEnabled", "false"
            )):
                self._stop_codex_engine()
                self.rebuild_menu()

        def _make_desktop_switch(self, num, label, *, expected_pending=None):
            def cb(_sender):
                if self.codex is None or self._desktop_switching:
                    return
                if self._login_session is not None:
                    self._show_error("Finish or cancel adding the account first.")
                    return
                if self._refreshing or self._kickoff_running:
                    self._show_error("Wait for the current refresh or kickoff to finish, then try again.")
                    return
                generation = self._chatgpt_capability_generation
                persisted = self._chatgpt_switching_persisted()
                if not persisted or not chatgpt_tab_desktop(self.settings):
                    self._offer_enable_chatgpt_switching()
                    return
                now = time.monotonic()
                if not chatgpt_manual_activation_allowed(
                    self.settings, self._chatgpt_capability, now=now
                ):
                    cap = self._chatgpt_capability
                    self._show_error(
                        desktop_capability_status_copy(
                            DesktopCapability("checking", "checking")
                            if not capability_is_fresh(cap, now=now)
                            else cap
                        )
                    )
                    self._request_chatgpt_capability()
                    return
                pause_auto = self._codex_enabled()
                process_state = self._chatgpt_capability.state
                title, message = desktop_switch_confirm_copy(
                    label, pause_auto=pause_auto, process_state=process_state,
                )
                if self._alert(
                    title=title, message=message,
                    ok=desktop_switch_confirm_ok(process_state), cancel="Cancel",
                ) != 1:
                    return
                if expected_pending is not None:
                    self._validate_pending_chatgpt_switch()
                    if self._pending_chatgpt_switch != expected_pending:
                        self._show_error("The suggested account changed. Review the latest suggestion.")
                        return
                # A native modal dialog can pump the timer run loop. Recheck
                # activity that may have started while consent was visible.
                if self._refreshing or self._kickoff_running:
                    self._show_error("A refresh or kickoff started. Wait for it to finish, then try again.")
                    return
                if (
                    generation != self._chatgpt_capability_generation
                    or not self._chatgpt_switching_persisted()
                    or not chatgpt_tab_desktop(self.settings)
                    or self._chatgpt_capability.state != process_state
                    or self._chatgpt_capability.state not in ("running", "stopped")
                ):
                    self._show_error("ChatGPT status changed. Try again.")
                    return
                if pause_auto:
                    if not self._guard(lambda: set_setting(
                        self.switcher.backup_dir, "autoswitch.codexEnabled", "false"
                    )):
                        return
                elif self._codex_enabled():
                    self._show_error("Codex auto-switching was enabled while confirming. Try again to confirm pausing it.")
                    return
                self._begin_desktop_transaction(consent_generation=generation)
                self.rebuild_menu()
                if self._panel is not None:
                    self._panel.close()
                threading.Thread(
                    target=self._desktop_worker,
                    args=(num, generation, process_state),
                    daemon=True,
                ).start()
            return cb

        def _desktop_worker(self, num, generation=None, consent_state=None):
            try:
                persisted = self._chatgpt_switching_persisted()
                if not persisted:
                    outcome = (None, "ChatGPT switching is off.")
                elif (
                    generation is not None
                    and getattr(self, "_desktop_consent_generation", None) not in (None, generation)
                ):
                    outcome = (None, "ChatGPT status changed. Try again.")
                else:
                    with self._desktop_app_lock:
                        self._desktop_app.invalidate_validation_cache()
                        observed = (
                            self._desktop_app.observe_capability(self.codex.home)
                            if consent_state is not None else None
                        )
                        if observed is not None and observed.state != consent_state:
                            outcome = (None, "ChatGPT status changed. Try again.")
                        else:
                            from openswap.codex.desktop import DesktopSwitcher
                            result = DesktopSwitcher(self.codex, app=self._desktop_app).switch(
                                num, confirm_restart=True, confirm_idle=True
                            )
                            outcome = (result, None)
            except ClaudeSwitchError as exc:
                outcome = (None, str(exc))
            except Exception:
                # Never surface arbitrary credential/protocol details.
                outcome = (None, "The desktop switch failed. Check the selected account before retrying.")
            with self._event_lock:
                self._desktop_result = outcome

        def _drain_desktop_result(self):
            with self._event_lock:
                pending = self._desktop_result
                self._desktop_result = None
            if pending is None:
                return
            self._desktop_switching = False
            self._chatgpt_probe_inflight = False
            self._start_chatgpt_auto_monitor()
            result, error = pending
            self._desktop_status = (
                "Switch failed · Check ChatGPT before retrying" if error else
                "ChatGPT reopened · Check the profile"
            )
            self._request_chatgpt_capability()
            # The panel may have been reopened during the background work.
            # Reuse the existing mouse-up-safe main-panel refresh queue.
            self._hold_reload_pending = True
            self.rebuild_menu()
            if error:
                self._show_error(error)
            elif result is not None:
                self._animate_swap()
                record_manual_switch(self.codex.state_dir)
                try:
                    self._notify(notification_copy_for_desktop_switch())
                except Exception:
                    # Inline status and refresh are the durable fallback.
                    pass
            self.refresh_async()

        def _remove_menu(self, rumps):
            menu = rumps.MenuItem("Remove account")
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.add(rumps.MenuItem("No managed accounts", callback=None))
            for num, email, _is_active, _display, _last_good, alias, _org, _disabled, _fetched_at in accounts:
                label = f"{num}  {alias}  ({email})" if alias else f"{num}  {email}"
                menu.add(rumps.MenuItem(label, callback=self._make_remove(num)))
            return menu

        def _disable_menu(self, rumps):
            menu = rumps.MenuItem("Disable / enable account")
            accounts = self.snapshot["accounts"]
            if not accounts:
                menu.add(rumps.MenuItem("No managed accounts", callback=None))
            for num, email, _is_active, _display, _last_good, alias, _org, disabled, _fetched_at in accounts:
                name = f"{alias}  ({email})" if alias else email
                item = rumps.MenuItem(
                    f"{num}  {name}", callback=self._make_toggle_disabled(num, disabled)
                )
                # A check-mark reads as "held out of rotation" — same glyph the
                # active row uses, but here it means disabled, not selected.
                item.state = 1 if disabled else 0
                menu.add(item)
            return menu

        def _history_menu(self, rumps):
            menu = rumps.MenuItem("Switch history")
            try:
                text = log_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            entries = parse_switch_history(text)
            if entries:
                for line in entries:
                    menu.add(rumps.MenuItem(line, callback=None))
            else:
                menu.add(rumps.MenuItem("No switches logged yet", callback=None))
            menu.add(None)
            menu.add(rumps.MenuItem("Open full log…", callback=self.on_open_log))
            return menu

        # ---- callbacks --------------------------------------------------------
        def _save_and_rebuild(self):
            self.settings.save(settings_path)
            self.rebuild_menu()

        def _dialog(self, run):
            """Run a modal dialog in front of the popover without closing it.

            The popover floats at menu level, above a modal alert, and the
            modal runner pins the alert's own level, so the popover's window
            is lowered for the dialog's lifetime instead; Cancel then leaves
            the user where they were. A menu-bar (accessory) app is not the
            active app either, so it is brought forward or the modal can
            render blank.
            """
            import AppKit
            AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
            popover = self._panel.popover_window() if self._panel is not None else None
            if popover is None:
                return run()
            level = popover.level()
            popover.setLevel_(AppKit.NSNormalWindowLevel)
            try:
                return run()
            finally:
                popover.setLevel_(level)

        def _alert(self, **kwargs) -> int:
            from openswap.menubar_dialog import make_dialog_alert

            alert = make_dialog_alert(**kwargs)
            return self._dialog(alert.runModal)

        def _prompt(self, **kwargs):
            from openswap.menubar_dialog import make_dialog_prompt

            prompt = make_dialog_prompt(**kwargs)
            return self._dialog(prompt.run)

        def _confirm(self, **kwargs):
            """An alert whose answer carries its checkbox (``DialogResponse``)."""
            from openswap.menubar_dialog import make_dialog_alert

            alert = make_dialog_alert(**kwargs)
            return self._dialog(alert.run)

        def _choose_folder(self, *, title: str, message: str, close: str | None = None):
            """The native folder chooser: a path, None on Cancel, or ``DIALOG_CLOSE`` for ``close``."""
            from openswap.menubar_dialog import choose_folder

            return choose_folder(title=title, message=message, close=close, run=self._dialog)

        def _show_error(self, message: str):
            self._alert(title="openswap", message=message)

        def _guard(self, fn):
            """Run a switcher action, surfacing ClaudeSwitchError via an alert."""
            try:
                fn()
                return True
            except ClaudeSwitchError as e:
                self._show_error(str(e))
                return False

        def _run_switch(self, fn):
            try:
                return fn()
            except ClaudeSwitchError as e:
                self._show_error(str(e))
                return None

        def _finish_manual_switch(self, result, dest_name, *, close_panel, provider="claude"):
            if result is None:
                return
            if should_notify_manual_switch(result):
                self._animate_swap()
                self._clear_hold_event()
                stamp_dir = (
                    self.codex.state_dir
                    if provider == "codex" and self.codex is not None
                    else self.switcher.backup_dir
                )
                record_manual_switch(stamp_dir)
                self._notify_switched(dest_name, provider=provider)
                self.refresh_async()
                self._apply_hold_line()
            if (
                close_panel
                and should_dismiss_panel_after_switch(result)
                and self._panel is not None
            ):
                self._panel.close()
            elif should_notify_manual_switch(result):
                self._reload_main_panel_if_shown()

        def _notify(self, copy: NotificationCopy | None):
            if copy is None:
                return
            kwargs = {} if copy.sound is None else {"sound": copy.sound}
            rumps.notification(*copy.rumps_args(), **kwargs)

        def _alias_map(self) -> dict[str, str]:
            aliases: dict[str, str] = {}
            for num, email, _a, _d, _lg, alias, _org, _dis, _fa in self.snapshot["accounts"]:
                if alias:
                    aliases[str(num)] = alias
                    aliases[email] = alias
            return aliases

        def _name_for_num(self, num) -> str:
            for row in self.snapshot["accounts"]:
                if str(row[0]) == str(num):
                    return account_short_name(row[1], row[5] or None, num)
            return account_short_name(None, None, num)

        def _name_for_identity(self, identity: tuple[str, str] | None) -> str | None:
            if identity is None:
                return None
            email, _org = identity
            for row in self.snapshot.get("accounts") or []:
                if row[1] == email and self._slot_identity(row[0]) == identity:
                    return account_short_name(email, row[5] or None, row[0])
            return account_short_name(email, None)

        def _current_dest_name(self) -> str:
            current = self.switcher.live_identity()
            email = current[0] if current else None
            alias = None
            if email:
                for row in self.snapshot["accounts"]:
                    if row[1] == email:
                        alias = row[5] or None
                        break
            return account_short_name(email, alias)

        def _notify_switched(self, dest_name: str, *, provider: str = "claude"):
            if provider == "codex":
                running = bool(self.snapshot.get("codex_running", True))
            else:
                running = bool(self.snapshot.get("claude_running", True))
            self._notify(
                notification_copy_for_manual_switch(
                    dest_name, running=running, provider=provider
                )
            )

        def _switch_from_widget(self, num):
            self._on_account_click(num, close_panel=False)

        def _on_panel_account_click(self, num):
            """Second-tab row click: Codex CLI switch, or the guarded desktop relaunch."""
            from openswap.codex import split_provider_num
            provider, slot = split_provider_num(num)
            if provider != "codex":
                self._on_account_click(num, close_panel=True)
                return
            desktop_tab = chatgpt_tab_desktop(self.settings)
            if self._slot_needs_relogin(num):
                # Neither mode should make an expired login live: Codex's
                # switch_to only checks that the stored auth parses.
                self._show_error(
                    "This ChatGPT OAuth session expired. Sign in again with Codex, then try again."
                    if desktop_tab
                    else "This Codex OAuth session expired. Sign in again with Codex, then try again."
                )
                return
            if not desktop_tab:
                # Codex tab: select this login for Codex CLI, same confirm and
                # toast as a Claude card. Nothing touches the ChatGPT app.
                self._on_account_click(num, close_panel=True)
                return
            if not self._chatgpt_switching_persisted():
                # The tab shows ChatGPT mode but the stored preference says
                # off (another instance or the CLI changed it): do not quietly
                # rewrite the shared login under a running ChatGPT.
                self._offer_enable_chatgpt_switching()
                return
            choices = dict(desktop_switch_choices(self.snapshot))
            if slot not in choices:
                self._show_error("Choose an enabled ChatGPT OAuth account. API keys are only supported by the Codex CLI.")
                return
            self._make_desktop_switch(slot, choices[slot])(None)

        def _slot_needs_relogin(self, num) -> bool:
            if str(num) in {
                str(value)
                for value in self.snapshot.get("kickoff_action_required") or []
            }:
                return True
            for row in self.snapshot.get("accounts") or []:
                if str(row[0]) == str(num):
                    return display_needs_relogin(row[3])
            return False

        def _slot_needs_restore(self, num) -> bool:
            for row in self.snapshot.get("accounts") or []:
                if str(row[0]) == str(num):
                    return display_needs_restore(row[3]) or (
                        bool(row[2])
                        and display_missing_credentials(row[3])
                        and isinstance(row[4], dict)
                    )
            return False

        def _slot_needs_reconcile(self, num) -> bool:
            for row in self.snapshot.get("accounts") or []:
                if str(row[0]) == str(num):
                    return display_needs_reconcile(row[3])
            return False

        def _slot_missing_login(self, num) -> bool:
            for row in self.snapshot.get("accounts") or []:
                if str(row[0]) == str(num):
                    return display_missing_credentials(row[3]) and not (
                        bool(row[2]) and isinstance(row[4], dict)
                    )
            return False

        def _slot_identity(self, num) -> tuple[str, str] | None:
            ident = (self.snapshot.get("identities") or {}).get(str(num))
            if ident is not None:
                return ident
            try:
                return self.switcher.slot_identity(num)
            except Exception:
                return None

        def _on_account_click(self, num, *, close_panel):
            if self._desktop_switching:
                return
            from openswap.codex import split_provider_num
            provider, n = split_provider_num(num)
            if provider == "codex":
                if self.codex is None or not self._confirm_switch(num):
                    return
                result = self._run_switch(
                    lambda: self.codex.switch_to(n, json_output=True)
                )
                self._finish_manual_switch(
                    result, self._name_for_num(num), close_panel=close_panel,
                    provider="codex",
                )
                return
            if self._slot_needs_relogin(num):
                self._repair_relogin(
                    num, close_panel=close_panel, force_login=True
                )
                return
            if self._slot_missing_login(num):
                self._repair_relogin(
                    num, close_panel=close_panel, force_login=True,
                    reason="missing",
                )
                return
            if self._slot_needs_reconcile(num):
                self._repair_reconcile(num, close_panel=close_panel)
                return
            if self._slot_needs_restore(num):
                try:
                    live_credentials_present = self.switcher.has_live_credentials()
                except CredentialReadError as e:
                    self._show_error(str(e))
                    return
                if live_credentials_present:
                    if not self._confirm_switch(num):
                        return
                    result = self._run_switch(
                        lambda: self.switcher.switch_to(
                            str(num), json_output=True
                        )
                    )
                    self._finish_manual_switch(
                        result, self._name_for_num(num), close_panel=close_panel
                    )
                    return
                result = self._run_switch(
                    lambda: self.switcher.switch_to(
                        str(num), json_output=True, force=True,
                        force_if_live_missing=True,
                    )
                )
                self._finish_manual_switch(
                    result, self._name_for_num(num), close_panel=close_panel
                )
                return
            if not self._confirm_switch(num):
                return
            result = self._run_switch(
                lambda: self.switcher.switch_to(str(num), json_output=True)
            )
            self._finish_manual_switch(
                result, self._name_for_num(num), close_panel=close_panel
            )

        def _confirm_switch(self, num) -> bool:
            """Ask before a card or widget tap swaps the live login."""
            if not should_confirm_switch(
                self.settings.confirm_switch, is_active=self._live_is_active(num)
            ):
                return True
            from openswap.codex import CODEX_NUM_PREFIX, split_provider_num
            provider, _n = split_provider_num(num)
            if provider == "codex":
                app = "Codex CLI"
                live = self.codex.current_account_number()
                if live:
                    live_name = self._name_for_num(f"{CODEX_NUM_PREFIX}{live}")
                else:
                    ident = self.codex.live_identity()
                    live_name = account_short_name(ident[0]) if ident and ident[0] else None
            else:
                app = "Claude Code"
                live_name = self._name_for_identity(self.switcher.live_identity())
            title, message = switch_confirm_copy(
                self._name_for_num(num), live_name=live_name, app=app
            )
            return self._alert(title=title, message=message, ok="Switch", cancel="Cancel") == 1

        def _live_is_active(self, num) -> bool:
            # A consent gate must not trust the cached snapshot, which a
            # failing worker keeps stale on purpose; read the live slot and
            # treat any doubt as "not active" so the dialog shows.
            from openswap.codex import split_provider_num
            provider, n = split_provider_num(num)
            engine = self.codex if provider == "codex" else self.switcher
            try:
                live = engine.current_account_number() if engine is not None else None
            except (ClaudeSwitchError, OSError):
                return False
            return live is not None and str(live) == str(n)

        def _confirm_strategy_switch(self, strategy) -> bool:
            if not self.settings.confirm_switch:
                return True
            title, message = strategy_confirm_copy(
                strategy, live_name=self._name_for_identity(self.switcher.live_identity())
            )
            return self._alert(title=title, message=message, ok="Switch", cancel="Cancel") == 1

        def _repair_relogin(
            self, num, *, close_panel, force_login=False, reason="expired"
        ):
            slot = self._slot_identity(num)
            slot_name = self._name_for_num(num)
            live = self.switcher.live_identity()
            live_name = self._name_for_identity(live)
            plan = plan_relogin_click(
                live=live, slot=slot, slot_name=slot_name, live_name=live_name,
                reason=reason,
            )
            if plan is None:
                self._show_error(f"Couldn't find {slot_name} in the account list.")
                return
            if force_login and plan.kind == "capture":
                plan = ReloginClickPlan(
                    "open_login", plan.slot_name, plan.login_email,
                    plan.live_name, plan.reason,
                )
            if plan.kind == "capture":
                self._capture_relogin(num, close_panel=close_panel)
                return
            if plan.kind == "confirm_open_login" and self._alert(
                title=relogin_wrong_account_title(plan),
                message=relogin_wrong_account_message(plan),
                ok=f"Sign in as {plan.slot_name}",
                cancel="Cancel",
            ) != 1:
                return
            self._open_claude_login(num, plan)

        def _repair_reconcile(self, num, *, close_panel):
            name = self._name_for_num(num)
            try:
                owner = self._run_switch(
                    lambda: self.switcher.live_credential_owner(num)
                )
            except OSError as e:
                # The lookup can record a verified uuid in the roster.
                self._show_error(f"Couldn't update OpenSwap's account list: {e}")
                return
            if owner is None:
                return
            if owner["state"] in ("matches", "own"):
                # Already this account's login (fixed since the card was
                # drawn, or just a token refresh): nothing to repair, and no
                # restore may run without the confirmation below.
                self._notify(
                    NotificationCopy(
                        title=f"{name}'s login is already fixed",
                        body="Claude Code is using the right account.",
                    )
                )
                self.refresh_async()
                return
            slot = self._slot_identity(num)
            owner_email = owner.get("email")
            if owner["state"] == "other" and not owner_email:
                owner_email = "an unknown account"
            title, message, ok = reconcile_dialog_copy(
                name, slot[0] if slot else "",
                owner_email=owner_email,
                owner_name=(
                    self._name_for_num(owner["slot"])
                    if owner.get("slot") else None
                ),
            )
            if self._alert(
                title=title, message=message, ok=ok, cancel="Cancel"
            ) != 1:
                return
            if owner["state"] == "unknown":
                self._repair_relogin(
                    num, close_panel=close_panel, force_login=True,
                    reason="unverified",
                )
                return
            result = self._run_switch(
                lambda: self.switcher.switch_to(str(num), json_output=True)
            )
            if result is None:
                return
            if result.get("reason") == "repaired":
                self._notify(
                    NotificationCopy(
                        title=f"{name}'s login restored",
                        body="Claude Code is using the right account again.",
                    )
                )
                self.refresh_async()
            elif not result.get("switched"):
                # The login changed again between the check and the repair.
                self._show_error(
                    f"Claude Code's login changed while checking. Click {name} "
                    "again to see what it's using now."
                )
                return
            self._finish_manual_switch(result, name, close_panel=close_panel)

        def _capture_relogin(self, num, *, close_panel):
            slot = self._slot_identity(num)
            live = self.switcher.live_identity()
            if slot is None or live is None or live != slot:
                self._show_error(
                    f"Claude Code is not signed in as {self._name_for_num(num)}. "
                    "Not capturing."
                )
                return
            try:
                self.switcher.add_account(slot=None)
            except CredentialReadError as e:
                self._alert_credential_read_error(e)
                return
            except ClaudeSwitchError as e:
                self._show_error(str(e))
                return
            name = self._name_for_num(num)
            self._notify(notification_copy_for_relogin_captured(name))
            self.refresh_async()
            if close_panel and self._panel is not None:
                self._panel.close()
            return True

        def _alert_credential_read_error(self, error):
            # Almost always a launchd/login-agent Keychain block: the active
            # credential lives in the macOS Keychain, which a background agent
            # can't read (the security call times out). Point at the fix.
            self._alert(
                title="openswap",
                message=f"Couldn't read the active credential ({error}). If the menu bar "
                        "is running as a background/login agent, macOS blocks its Keychain "
                        "access — quit and relaunch it from a Terminal with: openswap menubar",
            )

        def _open_claude_login(self, num, plan: ReloginClickPlan):
            claude_bin = self._claude_bin()
            if claude_bin is None:
                return
            self._start_claude_login(claude_bin, plan.login_email, num=str(num))
            self._notify(
                NotificationCopy(
                    title=f"Sign in as {plan.slot_name}",
                    body=relogin_login_opened_message(plan.slot_name),
                )
            )

        def on_add_claude_login(self, _sender):
            claude_bin = self._claude_bin()
            if claude_bin is None or not self._keep_outgoing_claude_login():
                return
            self._start_claude_login(claude_bin, None, num=None)
            self._notify(
                NotificationCopy(
                    title="Sign in to Claude Code", body=claude_add_opened_message()
                )
            )

        def _keep_outgoing_claude_login(self) -> bool:
            """Save the live login before the sign-in replaces it; False to stop.

            Claude Code may have rotated its refresh token since the slot's
            backup was written, and the sign-in overwrites the only current copy.
            The read is strict: a torn config must not pass for "logged out".
            """
            from openswap.worker.leases import AccountLeaseError

            try:
                live = self.switcher._get_current_identity_triple(strict=True)
                if live is None:
                    return True
                if self.switcher.current_account_number() is None:
                    message = claude_add_unsaved_live_message(live[0])
                else:
                    self.switcher.add_account(slot=None)
                    return True
            except CredentialMissingError as e:
                # Nothing to protect, so this falls to the confirm below.
                self.switcher._logger.warning("claude add: backup failed: %s", e)
                message = claude_add_backup_failed_message(str(e))
            except CredentialReadError as e:
                self.switcher._logger.warning("claude add: backup failed: %s", e)
                self._alert_credential_read_error(e)
                return False
            except AccountLeaseError as e:
                # A worker job is using this login; signing in would pull it away.
                self._show_error(str(e))
                return False
            except ClaudeSwitchError as e:
                self.switcher._logger.warning("claude add: backup failed: %s", e)
                message = claude_add_backup_failed_message(str(e))
            return self._alert(
                title="Sign out of Claude Code's current login?", message=message,
                ok="Continue", cancel="Cancel",
            ) == 1

        def _claude_bin(self) -> str | None:
            claude_bin = resolve_claude_bin()
            if not claude_bin:
                self._show_error(
                    "'claude' was not found. Install Claude Code, then try again."
                )
                return None
            return claude_bin

        def _start_claude_login(self, claude_bin, email, *, num):
            """Start a browser sign-in; ``num`` None adds whoever signs in."""
            # A second click restarts the sign-in (the browser tab may be gone).
            if self._claude_login is not None:
                self._claude_login.cancel()
            run = ClaudeLoginRun(
                claude_bin, email,
                on_done=lambda result: self._on_claude_login_done(run, result),
            )
            with self._event_lock:
                self._claude_login = run
                self._claude_login_num = num
            run.start()

        def _on_claude_login_done(self, run, result):
            # Worker thread: hand the result to the main-thread tick.
            with self._event_lock:
                self._claude_login_results.append((run, result))

        def _drain_claude_login_result(self):
            with self._event_lock:
                pending, self._claude_login_results = self._claude_login_results, []
                # A run canceled by a later click reports too; only the
                # current run's result counts.
                results = [r for run, r in pending if run is self._claude_login]
                if not results:
                    return
                result = results[-1]
                num = self._claude_login_num
                self._claude_login = None
                self._claude_login_num = None
            if num is None:
                self._finish_claude_add(result)
                return
            name = self._name_for_num(num)
            live = self.switcher.live_identity()
            outcome = login_outcome(result, live=live, slot=self._slot_identity(num))
            self.switcher._logger.debug(
                "claude login for %s: %s (returncode=%s timed_out=%s)\n%s",
                name, outcome, result.returncode, result.timed_out, result.output_tail,
            )
            if outcome == "canceled":
                return
            if outcome == "timed_out":
                self._show_error(claude_login_timed_out_message(name))
            elif outcome == "failed":
                self._show_error(claude_login_failed_message(name, result.output_tail))
            elif outcome == "no_login":
                self._show_error(claude_login_no_login_message(name))
            elif outcome == "mismatch":
                self._show_error(
                    claude_login_mismatch_message(name, self._name_for_identity(live))
                )
            elif self._capture_relogin(num, close_panel=False):
                # Captured here, not left to the tick: auto-capture may already
                # have saved this slot's old, dead credential and won't look again.
                self._auto_captured_nums.add(num)

        def _finish_claude_add(self, result):
            live = self.switcher.live_identity()
            outcome = new_login_outcome(result, live=live)
            self.switcher._logger.debug(
                "claude login for a new account: %s (returncode=%s timed_out=%s)\n%s",
                outcome, result.returncode, result.timed_out, result.output_tail,
            )
            if outcome == "canceled":
                return
            if outcome == "timed_out":
                self._show_error(claude_add_timed_out_message())
                return
            if outcome == "failed":
                self._show_error(claude_add_failed_message(result.output_tail))
                return
            if outcome == "no_login":
                self._show_error(claude_add_no_login_message())
                return
            try:
                num = self.switcher.current_account_number()
                self.switcher.add_account(slot=None)
            except CredentialReadError as e:
                self.switcher._logger.warning("claude add: capture failed: %s", e)
                if isinstance(e, CredentialMissingError):
                    self._show_error(str(e))
                else:
                    self._alert_credential_read_error(e)
                return
            except ClaudeSwitchError as e:
                self.switcher._logger.warning("claude add: capture failed: %s", e)
                self._show_error(str(e))
                return
            if num is None:
                self._notify(notification_copy_for_claude_added(live[0], existed=False))
            else:
                # Saved here, not left to the tick: a signed-out slot is now captured.
                self._auto_captured_nums.add(num)
                self._notify(
                    notification_copy_for_claude_added(self._name_for_num(num), existed=True)
                )
            self.refresh_async()

        def _drain_relogin_notifies(self):
            with self._event_lock:
                pending, self._pending_relogin_notifies = self._pending_relogin_notifies, set()
            for num in sorted(pending):
                from openswap.codex import split_provider_num

                provider, _slot = split_provider_num(num)
                self._notify(
                    notification_copy_for_relogin(
                        self._name_for_num(num), provider=provider
                    )
                )

        def _maybe_auto_capture_relogin(self):
            if self._refreshing or self._auto_capturing:
                return
            # Claude auto-capture must never adopt a Codex/ChatGPT credential;
            # the two providers use different identity and storage formats.
            nums = relogin_slot_nums(self.snapshot, provider="claude")
            self._auto_captured_nums &= nums
            if not nums:
                self._auto_capture_failed_for = None
                return
            live = self.switcher.live_identity()
            if live is None or live == self._auto_capture_failed_for:
                return
            identities = {}
            for num in nums:
                ident = self._slot_identity(num)
                if ident is not None:
                    identities[str(num)] = ident
            match = matching_relogin_slot(live, identities, nums)
            if match is None or match in self._auto_captured_nums:
                return
            self._auto_capturing = True
            try:
                live_now = self.switcher.live_identity()
                ident = self._slot_identity(match)
                if live_now is None or ident is None or live_now != ident:
                    return
                self.switcher.add_account(slot=None)
            except Exception:
                self._auto_capture_failed_for = live
                self.switcher._logger.debug(
                    "auto-capture of signed-out account failed", exc_info=True
                )
                return
            finally:
                self._auto_capturing = False
            self._auto_captured_nums.add(match)
            self._auto_capture_failed_for = None
            self._notify(notification_copy_for_relogin_captured(self._name_for_num(match)))
            self.refresh_async()

        def _switch(self, strategy):
            def cb(_sender):
                if not self._confirm_strategy_switch(strategy):
                    return
                result = self._run_switch(
                    lambda: self.switcher.switch(strategy=strategy, json_output=True)
                )
                self._finish_manual_switch(
                    result, self._current_dest_name(), close_panel=False
                )
            return cb

        def _make_rename(self, num, email, current):
            def cb(_sender):
                resp = self._prompt(
                    title="Rename account",
                    message=(
                        f"Short name for {account_short_name(email, None, num)} "
                        "(leave blank to remove it):"
                    ),
                    default_text=current or "",
                    ok="Save", cancel="Cancel",
                )
                if resp.clicked != 1:
                    return
                action, value = alias_edit(resp.text, current or None)
                if action == "noop":
                    return
                from openswap.codex import split_provider_num
                provider, n = split_provider_num(num)
                engine = self.codex if provider == "codex" else self.switcher
                if action == "set":
                    ok = self._guard(lambda: engine.set_alias(n, value))
                else:
                    ok = self._guard(lambda: engine.unset_alias(n))
                if ok:
                    self._refresh_and_reload_cards()
            return cb

        def _make_remove(self, num):
            def cb(_sender):
                if self._alert(
                    title="Remove account",
                    message=f"Remove account {num}?",
                    ok="Remove",
                    cancel="Cancel",
                    destructive=True,
                ) == 1:  # 1 == OK
                    from openswap.codex import split_provider_num
                    provider, n = split_provider_num(num)
                    if provider == "codex" and self.codex is not None:
                        ok = self._guard(
                            lambda: self.codex.remove_account(n, assume_yes=True)
                        )
                    else:
                        ok = self._guard(
                            lambda: self.switcher.remove_account(str(num), assume_yes=True)
                        )
                    if ok:
                        self.refresh_async()
            return cb

        def _make_toggle_disabled(self, num, disabled):
            # `disabled` is this row's current state; selecting it flips it.
            target = not disabled
            def cb(_sender):
                from openswap.codex import split_provider_num
                provider, n = split_provider_num(num)
                if provider == "codex" and self.codex is not None:
                    ok = (self._enable_codex_account(n) if not target else self._guard(
                        lambda: self.codex.set_account_disabled(n, True)
                    ))
                else:
                    ok = self._guard(
                        lambda: self.switcher.set_account_disabled(str(num), target)
                    )
                if ok:
                    self._refresh_and_reload_cards()
                    if provider == "codex" and not target:
                        self._ensure_codex_engine()
            return cb

        def _refresh_and_reload_cards(self):
            """Refresh, then rebuild the open main page once the snapshot lands.

            The worker only flags a reload for slot-list or provider-state
            changes; an alias or disabled flag edited from a card's ··· menu
            changes neither, yet the card's title, suffix, opacity and
            clickability all come from it.
            """
            self._reload_after_refresh = True
            self.refresh_async()

        def on_add_login(self, _sender):
            if self._guard(self.switcher.add_account):
                self.refresh_async()

        def on_add_codex_login(self, _sender):
            if self.codex is not None and self._guard(self.codex.add_account):
                self.refresh_async()
                if self._engine is not None:
                    self._ensure_codex_engine()

        def on_add_token(self, _sender):
            if self._panel is None:
                self._add_token_prompts()
                return
            self._panel.show_token_page()
            if not self._panel.is_shown():
                self._panel.toggle()

        def _add_token_prompts(self):
            """Fallback when the popover could not attach: the old two prompts."""
            email_resp = self._prompt(
                title="Add account from token",
                message="Email label (optional; leave blank to auto-name):",
                ok="Next", cancel="Cancel",
            )
            if email_resp.clicked != 1:
                return
            email = email_resp.text.strip() or None
            token_resp = self._prompt(
                title="Add account from token",
                message="API key (sk-ant-api…) or setup token (sk-ant-oat01-…):",
                ok="Add", cancel="Cancel",
                secure=True,
            )
            if token_resp.clicked != 1 or not token_resp.text.strip():
                return
            if self._guard(lambda: self.switcher.add_account_from_token(
                token=token_resp.text.strip(), email=email, slot=None,
            )):
                self.refresh_async()

        def _add_token(self, token, label, credit_for):
            """Token page submit. Returns sanitized error copy, or None on success."""
            try:
                self.switcher.add_account_from_token(
                    token=token, email=label or None, slot=None,
                    credit_for=credit_for,
                )
            except ClaudeSwitchError as e:
                copy = sanitize_token_error(str(e)) or "Could not add this token."
                self.switcher._logger.info("add-from-token rejected: %s", copy)
                return copy
            except OSError:
                self.switcher._logger.error("add-from-token failed", exc_info=True)
                return "Could not save this token (disk or permission error). See openswap.log."
            try:
                self.refresh_async()
            except Exception:
                self.switcher._logger.warning("refresh after add-from-token failed", exc_info=True)
            return None

        def on_open_log(self, _sender):
            import subprocess
            # Reveal the log in Finder (-R); if it doesn't exist yet, open the dir.
            target = log_path if log_path.exists() else log_path.parent
            subprocess.run(["open", "-R", str(target)], check=False)

        def on_refresh_creds(self, _sender):
            if self.switcher.live_identity() is None:
                self._alert(title="openswap",
                            message="No active Claude Code login detected. Log in first.")
                return
            try:
                self.switcher.add_account(slot=None)
            except CredentialReadError as e:
                self._alert_credential_read_error(e)
                return
            except ClaudeSwitchError as e:
                self._alert(title="openswap", message=str(e))
                return
            self.refresh_async()

        def on_refresh_now(self, _sender):
            self.refresh_async(full=True)  # explicit user refresh → full pass

        def on_quit(self, _sender):
            profiles = getattr(self, "_claude_desktop_profiles", None)
            links = getattr(profiles, "signin_links", None)
            if links is not None:
                links.restore(lambda: self._finish_quit(_sender))
                return
            self._finish_quit(_sender)

        def _finish_quit(self, _sender):
            if getattr(self, "_claude_desktop_profiles", None) is not None:
                self._claude_desktop_profiles.suspend_checks()
            if self._status_icon_animator is not None:
                self._status_icon_animator.stop()
            if self._login_session is not None:
                self._login_session.cancel()
            if self._claude_login is not None:
                self._claude_login.cancel()
            self._stop_chatgpt_auto_monitor(clear_pending=True)
            self._stop_engine()
            # Open at login was turned off from this job: unload it on the way
            # out so the cask's kickstart cannot revive it. A no-op otherwise.
            try:
                from openswap import launch_agent

                launch_agent.unload_own_job()
            except Exception:
                self.switcher._logger.warning("could not unload the login item", exc_info=True)
            rumps.quit_application()

        def on_uninstall(self, _sender):
            """More… › Uninstall OpenSwap…: confirm, start ``openswap
            uninstall`` in a detached child, and quit.

            Removing the login item boots this extra out, so nothing is
            removed in this process; the child waits for it to quit first.
            """
            from openswap import bundle, uninstall

            try:
                bundle.refuse_uv_beside_cask("uninstall OpenSwap")
            except ClaudeSwitchError as e:
                self._alert(title="Could not uninstall", message=str(e))
                return
            answer = self._confirm(**uninstall.app_dialog(), cancel=True, destructive=True)
            if answer.clicked != 1:
                return
            if answer.checked:
                try:
                    uninstall.refuse_live_sessions()
                except ClaudeSwitchError as e:
                    self._alert(title="Cannot delete saved accounts yet", message=str(e))
                    return
            try:
                uninstall.start_from_app(answer.checked)
            except OSError as e:
                self._alert(title="Could not uninstall",
                            message=f"OpenSwap could not start the uninstall: {e}")
                return
            self.on_quit(None)

        def on_toggle_name(self, _sender):
            self.settings.show_account_name = not self.settings.show_account_name
            self._save_and_rebuild()

        def on_toggle_confirm_switch(self, _sender):
            self.settings.confirm_switch = not self.settings.confirm_switch
            self._save_and_rebuild()

        def on_toggle_scoped(self, _sender):
            self.settings.title_scoped = not self.settings.title_scoped
            self._save_and_rebuild()

        def on_toggle_title_5h(self, _sender):
            self.settings.title_pct = combine_title_pct(
                not title_shows_5h(self.settings.title_pct),
                title_shows_7d(self.settings.title_pct),
            )
            self._save_and_rebuild()

        def on_toggle_title_7d(self, _sender):
            self.settings.title_pct = combine_title_pct(
                title_shows_5h(self.settings.title_pct),
                not title_shows_7d(self.settings.title_pct),
            )
            self._save_and_rebuild()

        def _make_interval(self, secs):
            def cb(_sender):
                self.settings.refresh_interval = secs
                # rumps 0.4.0's Timer.interval setter is a no-op while running
                # unless a full interval has elapsed; stop/start forces the new
                # cadence to take effect immediately.
                self.refresh_timer.stop()
                self.refresh_timer.interval = secs
                self.refresh_timer.start()
                self._save_and_rebuild()
            return cb

        def on_toggle_autoswitch(self, _sender):
            self.settings.auto_switch_enabled = not self.settings.auto_switch_enabled
            self.settings.save(settings_path)
            if self.settings.auto_switch_enabled:
                self._start_engine()
            else:
                self._stop_engine()
            self._apply_hold_line()
            self.rebuild_menu()
            self._reload_main_panel_if_shown()

        def on_toggle_kickoff(self, _sender):
            self.settings.kickoff_enabled = not self.settings.kickoff_enabled
            self._save_and_rebuild()

        def on_kickoff_time(self, value):
            parsed = parse_kickoff_time("" if value is None else str(value))
            if parsed is None:
                return
            hour, minute = parsed
            if (
                hour == self.settings.kickoff_hour
                and minute == self.settings.kickoff_minute
            ):
                return
            self.settings.kickoff_hour = hour
            self.settings.kickoff_minute = minute
            self._save_and_rebuild()

        def _maybe_kickoff(self):
            if self._desktop_switching or self._kickoff_running or self._kickoff_results is not None:
                return
            now = datetime.now()
            today = now.date().isoformat()
            if self._kickoff_success_date != today:
                self._kickoff_succeeded_nums.clear()
                self._kickoff_success_date = today
            if kickoff_backoff_active(
                now=time.time(), retry_after=self._kickoff_retry_after
            ):
                return
            s = self.settings
            if not kickoff_is_due(
                s.kickoff_enabled,
                s.kickoff_hour,
                s.kickoff_minute,
                s.kickoff_last_date,
                now,
            ):
                return
            # Don't start before the first snapshot has arrived.
            if not self.snapshot.get("accounts") and self._snapshot_at == 0.0:
                return
            self._kickoff_running = True
            threading.Thread(target=self._run_kickoff, daemon=True).start()

        def _run_kickoff(self):
            results: list[KickoffResult] = []
            try:
                from openswap.codex import split_provider_num
                from openswap.kickoff import invoke_codex_kickoff
                from openswap.session import SessionManager

                mgr = SessionManager(self.switcher)
                for (
                    num, email, is_active, display, last_good, alias, _org, _dis, _fa
                ) in self.snapshot["accounts"]:
                    if str(num) in self._kickoff_succeeded_nums:
                        continue
                    provider, slot_n = split_provider_num(num)
                    try:
                        is_api = (
                            (self.snapshot.get("kinds") or {}).get(str(num))
                            == "api_key"
                        )
                    except Exception:
                        is_api = display in (
                            SENTINEL_NOTES.get(USAGE_API_KEY),
                            USAGE_API_KEY,
                        )
                    if not kickoff_account_eligible(
                        is_api_key=is_api,
                        usage=last_good if isinstance(last_good, dict) else None,
                    ):
                        continue
                    name = account_short_name(email, alias or None, num)
                    try:
                        if provider == "codex":
                            if self.codex is None:
                                continue
                            if kickoff_uses_default_login(is_active=bool(is_active)):
                                proc = invoke_codex_kickoff(self.codex.home)
                            else:
                                proc = invoke_codex_kickoff(
                                    self.codex.slots_dir / slot_n
                                )
                        elif kickoff_uses_default_login(is_active=bool(is_active)):
                            proc = invoke_kickoff()
                        else:
                            session_dir, _, _ = mgr.setup_session(
                                str(num), share=True, share_history=False
                            )
                            proc = invoke_kickoff(session_dir)
                        if proc.returncode == 0:
                            results.append(
                                KickoffResult(provider, str(num), name, True)
                            )
                            self._kickoff_succeeded_nums.add(str(num))
                        else:
                            fallback = (
                                "codex exited with an error"
                                if provider == "codex"
                                else "claude exited with an error"
                            )
                            err = (proc.stderr or proc.stdout or fallback).strip()
                            results.append(
                                KickoffResult(
                                    provider, str(num), name, False, err[:200]
                                )
                            )
                    except Exception as e:
                        results.append(
                            KickoffResult(provider, str(num), name, False, str(e))
                        )
            except Exception as e:
                results.append(
                    KickoffResult("openswap", "", "kickoff", False, str(e))
                )
            finally:
                with self._event_lock:
                    self._kickoff_results = results
                    self._kickoff_running = False

        def _drain_kickoff_results(self):
            with self._event_lock:
                results = self._kickoff_results
                self._kickoff_results = None
            if results is None:
                return
            now = time.time()
            with self._event_lock:
                self._kickoff_action_required = (
                    kickoff_action_required_after_results(
                        self._kickoff_action_required, results, now=now
                    )
                )
                action_required = sorted(self._kickoff_action_required)
            self.snapshot["kickoff_action_required"] = action_required
            self._hold_reload_pending = True
            if kickoff_pass_complete(results):
                self.settings.kickoff_last_date = datetime.now().date().isoformat()
                self.settings.save(settings_path)
                self._kickoff_retry_after = None
                self._kickoff_succeeded_nums.clear()
                self._kickoff_notified_failure = None
                self._notify(notification_copy_for_kickoff(results))
            else:
                self._kickoff_retry_after = now + kickoff_retry_backoff(results)
                failure = kickoff_failure_signature(results)
                if failure != self._kickoff_notified_failure:
                    self._notify(notification_copy_for_kickoff(results))
                    self._kickoff_notified_failure = failure
            self.refresh_async()

        def _make_threshold(self, pct):
            def cb(_sender):
                try:
                    set_setting(self.switcher.backup_dir, "autoswitch.threshold", str(pct))
                except Exception as e:
                    self._alert(title="openswap", message=f"Couldn't set threshold: {e}")
                    return
                self._restart_engine()  # apply immediately if running
                self._restart_chatgpt_auto_monitor()
                self.rebuild_menu()
            return cb

        def _make_strategy(self, strategy):
            def cb(_sender):
                try:
                    set_setting(
                        self.switcher.backup_dir, "autoswitch.strategy", strategy
                    )
                except Exception as e:
                    self._alert(title="openswap", message=f"Couldn't set strategy: {e}")
                    return
                self._restart_engine()
                self._restart_chatgpt_auto_monitor()
                self.rebuild_menu()
            return cb

    MenuBarApp().run()
    return 0
