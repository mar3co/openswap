"""macOS menu bar extra for OpenSwap (``openswap menubar``).

A thin rumps shell over ``openswap.engine.Engine`` and ``openswap.autoswitch``.
It never re-implements account, usage, or auto-switch math. Pure helpers live
in ``openswap.menubar_display`` and are re-exported here so tests and the
popover keep importing from this module. ``rumps`` is imported only in
``run()``.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from datetime import datetime

from openswap.exceptions import ClaudeSwitchError, CredentialReadError
from openswap.menubar_display import *  # noqa: F403
from openswap.menubar_display import (  # noqa: F401  tests poke these
    EMPTY_SNAPSHOT,
    MenuBarSettings,
    _account_display_usage,
    _adapt_snapshot,
    _live_countdown,
    _resets_at_ts,
    _rolled_weekly_window,
    _usage_log_key,
    ensure_notification_identity,
    codex_live_slot_changed,
    codex_restart_hint,
)

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

    settings_path = menubar_settings_path(switcher.backup_dir)
    log_path = switcher.backup_dir / "openswap.log"

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__("openswap", quit_button=None)
            self.switcher = switcher
            self.codex = codex
            self.settings = MenuBarSettings.load(settings_path)
            # The supported paced read path: per refresh it fetches only the
            # active account plus (at most once per freshness window) one stale
            # alternate, so an open menu costs O(1) requests per window instead
            # of a full pass per tick — which kept every token at its per-account
            # rate-limit edge. Reused across refreshes to hold its pacing state.
            self._snapshot_source = SnapshotSource(switcher)
            self._codex_source = SnapshotSource(codex) if codex is not None else None
            self.snapshot = dict(EMPTY_SNAPSHOT)
            self._dirty = False
            self._snapshot_at = 0.0
            self._refreshing = False
            self._config_path = switcher._get_claude_config_path()
            self._config_mtime = 0.0
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
            self._engine_events: list = []
            self._hold_event = None
            self._hold_slot = None
            self._tick_slot = None
            self._hold_reload_pending = False
            self._event_lock = threading.Lock()
            self._panel = None
            self._kickoff_running = False
            self._kickoff_results = None
            self._kickoff_retry_after: float | None = None
            self._kickoff_succeeded_nums: set[str] = set()
            self._kickoff_success_date = ""
            self._relogin_notified: set[str] = set()
            self._pending_relogin_notifies: set[str] = set()
            self._auto_capturing = False
            self._auto_captured_nums: set[str] = set()
            self._auto_capture_failed_for: tuple[str, str] | None = None
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
            self.refresh_async()  # first display fetch
            from openswap.widget_snapshot import wake_widget_host
            wake_widget_host()
            if self.settings.auto_switch_enabled:
                self._start_engine()

        # ---- display refresh plumbing ----------------------------------------
        def refresh_async(self, full=False):
            if self._refreshing:
                return  # in-flight guard: one worker at a time (SnapshotSource
                        # pacing state is only touched by this single worker)
            self._refreshing = True
            threading.Thread(target=self._worker, args=(full,), daemon=True).start()

        def _worker(self, full):
            # Lock-free handoff: worker only rebinds plain attributes (atomic in
            # CPython); the main-thread sync tick reads them. While the engine
            # runs it already paces all fetching, so the display reads store-only.
            try:
                try:
                    raw = self._snapshot_source.take(
                        full=full, store_only=self._engine is not None
                    )
                except Exception:
                    # Keep the last good snapshot rather than blanking the menu.
                    self.switcher._logger.debug("menubar snapshot failed", exc_info=True)
                    return
                codex_raw = None
                if self._codex_source is not None:
                    try:
                        codex_raw = self._codex_source.take(
                            full=full, store_only=self._codex_engine is not None
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
                self.snapshot = snap
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
            finally:
                self._refreshing = False

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

        def on_sync_tick(self, _timer):
            self._consume_widget_command()
            if self._dirty:
                self._dirty = False
                self.rebuild_menu()
                # Dirty path must not reload the popover: Settings would lose
                # controls, and a main-page reload mid-click swallows mouseUp.
                # Hold copy is applied below and reloads the open main page
                # only when it changes, deferred while the left button is down.
            self._detect_active_change()
            self._detect_store_change()
            self._drain_engine_events()
            self._apply_hold_line()
            self._drain_relogin_notifies()
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
                if live_slot_changed(
                    self.snapshot, self.switcher.current_account_number()
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
            if changed:
                # Do not clear the hold cache here: the engine may already have
                # recorded a reason for the new slot. Display is gated by
                # hold_event_for_snapshot so an old hold cannot caption a new card.
                self.refresh_async()

        # ---- auto-switch engine ----------------------------------------------
        def _ensure_codex_engine(self):
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
                panel.reload()

        def _stop_codex_engine(self):
            with self._event_lock:
                codex_engine = self._codex_engine
                self._codex_engine = None
            if codex_engine is not None:
                codex_engine.stop()

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
            if self.codex is None:
                return False
            try:
                return bool(self.codex.switchable_account_numbers())
            except Exception:
                return False

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
                fit_status_item(nsitem, compact=not self.settings.show_icon)
            except Exception:
                self.switcher._logger.debug("status item fit failed", exc_info=True)
            self._panel = MenuBarPanel(
                on_switch=lambda num: self._on_account_click(num, close_panel=True),
                on_rotate=lambda *_a: self._switch(None)(None),
                on_best=lambda *_a: self._switch("best")(None),
                on_toggle_auto=lambda *_a: self.on_toggle_autoswitch(None),
                on_more=self._popup_overflow,
                auto_enabled=lambda: self.settings.auto_switch_enabled,
                snapshot=lambda: self.snapshot,
                threshold=self._threshold,
                on_setting=self._on_setting,
                settings=lambda: self.settings,
                strategy=self._strategy,
                has_codex=self._has_codex,
                codex_enabled=self._codex_enabled,
            )
            self._panel.attach(nsitem)

        def _on_setting(self, row_id, value):
            if row_id == "show_account_name":
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
            elif row_id == "threshold":
                self._make_threshold(int(value))(None)
            elif row_id == "strategy":
                self._make_strategy(value)(None)
            elif row_id == "codex_enabled":
                try:
                    current = load_settings(self.switcher.backup_dir).codex_enabled
                    set_setting(
                        self.switcher.backup_dir,
                        "autoswitch.codexEnabled",
                        "false" if current else "true",
                    )
                except Exception as e:
                    self._show_error(f"Couldn't set Codex auto-switch: {e}")
                    return
                if current:
                    self._stop_codex_engine()
                else:
                    self._ensure_codex_engine()
            elif row_id == "kickoff_enabled":
                self.on_toggle_kickoff(None)
            elif row_id == "kickoff_time":
                self.on_kickoff_time(value)
                # The popup already shows the pick; do not rebuild the page
                # under the open menu.
                return
            elif row_id == "show_icon":
                self.on_toggle_icon(None)
            else:
                return
            panel = self._panel
            if (
                panel is not None
                and panel.is_shown()
                and getattr(panel, "_page", None) == SETTINGS_PAGE
            ):
                panel.reload()

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

        def _fit_status_item(self, title: str | None = None):
            nsapp = getattr(self, "_nsapp", None)
            nsitem = getattr(nsapp, "nsstatusitem", None) if nsapp is not None else None
            if nsitem is None:
                return
            try:
                from openswap.menubar_panel import fit_status_item

                fit_status_item(
                    nsitem,
                    compact=not self.settings.show_icon,
                    title=title,
                )
            except Exception:
                self.switcher._logger.debug("status item fit failed", exc_info=True)

        def rebuild_menu(self):
            title = format_title(
                self.snapshot["active_email"],
                title_usage(self.snapshot),
                self.settings,
                now=title_clock(self.snapshot),
                alias=self.snapshot.get("active_alias"),
                org_name=self.snapshot.get("active_org"),
            )
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
            # Overflow menu (popover More…): account switching and settings live
            # in the popover, so this list is management only.
            self.menu = [
                rumps.MenuItem("Rotate to next", callback=self._switch(None)),
                rumps.MenuItem("Switch to best", callback=self._switch("best")),
                rumps.MenuItem("Next available", callback=self._switch("next-available")),
                None,
                self._add_menu(rumps),
                self._rename_menu(rumps),
                self._disable_menu(rumps),
                self._remove_menu(rumps),
                rumps.MenuItem("Refresh current credentials", callback=self.on_refresh_creds),
                self._history_menu(rumps),
                None,
                rumps.MenuItem("Refresh now", callback=self.on_refresh_now),
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

        def _add_menu(self, rumps):
            menu = rumps.MenuItem("Add account")
            menu.add(rumps.MenuItem("From current login", callback=self.on_add_login))
            if self.codex is not None:
                menu.add(rumps.MenuItem(
                    "From current Codex login", callback=self.on_add_codex_login
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
            return self._dialog(lambda: rumps.alert(**kwargs))

        def _prompt(self, **kwargs):
            return self._dialog(lambda: rumps.Window(**kwargs).run())

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
            rumps.notification(*copy.rumps_args())

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

        def _slot_needs_relogin(self, num) -> bool:
            for row in self.snapshot.get("accounts") or []:
                if str(row[0]) == str(num):
                    return display_needs_relogin(row[3])
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
                self._repair_relogin(num, close_panel=close_panel)
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

        def _repair_relogin(self, num, *, close_panel):
            slot = self._slot_identity(num)
            slot_name = self._name_for_num(num)
            live = self.switcher.live_identity()
            live_name = self._name_for_identity(live)
            plan = plan_relogin_click(
                live=live, slot=slot, slot_name=slot_name, live_name=live_name
            )
            if plan is None:
                self._show_error(f"Couldn't find {slot_name} in the account list.")
                return
            if plan.kind == "capture":
                self._capture_relogin(num, close_panel=close_panel)
                return
            if plan.kind == "confirm_open_login" and self._alert(
                title=relogin_wrong_account_title(plan),
                message=relogin_wrong_account_message(plan),
                ok="Open login",
                cancel="Cancel",
            ) != 1:
                return
            self._open_claude_login(plan)

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
            except CredentialReadError:
                self._alert(
                    title="openswap",
                    message="Couldn't read the active credential. If the menu bar is running "
                            "as a background/login agent, macOS blocks its Keychain access — "
                            "quit and relaunch it from a Terminal with: openswap menubar",
                )
                return
            except ClaudeSwitchError as e:
                self._show_error(str(e))
                return
            name = self._name_for_num(num)
            self._notify(notification_copy_for_relogin_captured(name))
            self.refresh_async()
            if close_panel and self._panel is not None:
                self._panel.close()

        def _open_claude_login(self, plan: ReloginClickPlan):
            try:
                launch_claude_login(plan.login_email)
            except ClaudeSwitchError as e:
                self._show_error(str(e))
                return
            self._notify(
                NotificationCopy(
                    title=f"Sign in as {plan.slot_name}",
                    body=relogin_login_opened_message(plan.slot_name),
                )
            )

        def _drain_relogin_notifies(self):
            with self._event_lock:
                pending, self._pending_relogin_notifies = self._pending_relogin_notifies, set()
            for num in sorted(pending):
                self._notify(notification_copy_for_relogin(self._name_for_num(num)))

        def _maybe_auto_capture_relogin(self):
            if self._refreshing or self._auto_capturing:
                return
            nums = relogin_slot_nums(self.snapshot)
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
                    ok="Save", cancel="Cancel", dimensions=(320, 24),
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
                    self.refresh_async()
            return cb

        def _make_remove(self, num):
            def cb(_sender):
                if self._alert(
                    title="Remove account",
                    message=f"Remove account {num}?",
                    ok="Remove",
                    cancel="Cancel",
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
                    ok = self._guard(
                        lambda: self.codex.set_account_disabled(n, target)
                    )
                else:
                    ok = self._guard(
                        lambda: self.switcher.set_account_disabled(str(num), target)
                    )
                if ok:
                    self.refresh_async()
                    if provider == "codex" and not target:
                        self._ensure_codex_engine()
            return cb

        def on_add_login(self, _sender):
            if self._guard(self.switcher.add_account):
                self.refresh_async()

        def on_add_codex_login(self, _sender):
            if self.codex is not None and self._guard(self.codex.add_account):
                self.refresh_async()
                if self._engine is not None:
                    self._ensure_codex_engine()

        def on_add_token(self, _sender):
            email_resp = self._prompt(
                title="Add account from token",
                message="Email label (optional; leave blank to auto-name):",
                ok="Next", cancel="Cancel", dimensions=(320, 24),
            )
            if email_resp.clicked != 1:
                return
            email = email_resp.text.strip() or None
            token_resp = self._prompt(
                title="Add account from token",
                message="API key (sk-ant-api…) or setup token (sk-ant-oat01-…):",
                ok="Add", cancel="Cancel", dimensions=(320, 24),
            )
            if token_resp.clicked != 1 or not token_resp.text.strip():
                return
            if self._guard(lambda: self.switcher.add_account_from_token(
                token=token_resp.text.strip(), email=email, slot=None,
            )):
                self.refresh_async()

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
            except CredentialReadError:
                # Almost always a launchd/login-agent Keychain block: the active
                # credential lives in the macOS Keychain, which a background agent
                # can't read (the security call times out). Point at the fix.
                self._alert(
                    title="openswap",
                    message="Couldn't read the active credential. If the menu bar is running "
                            "as a background/login agent, macOS blocks its Keychain access — "
                            "quit and relaunch it from a Terminal with: openswap menubar",
                )
                return
            except ClaudeSwitchError as e:
                self._alert(title="openswap", message=str(e))
                return
            self.refresh_async()

        def on_refresh_now(self, _sender):
            self.refresh_async(full=True)  # explicit user refresh → full pass

        def on_quit(self, _sender):
            self._stop_engine()
            rumps.quit_application()

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

        def on_toggle_icon(self, _sender):
            self.settings.show_icon = not self.settings.show_icon
            self._save_and_rebuild()

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
            if self._kickoff_running or self._kickoff_results is not None:
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
            results: list[tuple[str, bool, str]] = []
            try:
                from openswap.session import SessionManager

                mgr = SessionManager(self.switcher)
                for (
                    num, email, is_active, display, last_good, alias, _org, _dis, _fa
                ) in self.snapshot["accounts"]:
                    if str(num) in self._kickoff_succeeded_nums:
                        continue
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
                        from openswap.codex import split_provider_num
                        from openswap.kickoff import invoke_codex_kickoff
                        provider, slot_n = split_provider_num(num)
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
                            results.append((name, True, ""))
                            self._kickoff_succeeded_nums.add(str(num))
                        else:
                            fallback = (
                                "codex exited with an error"
                                if provider == "codex"
                                else "claude exited with an error"
                            )
                            err = (proc.stderr or proc.stdout or fallback).strip()
                            results.append((name, False, err[:200]))
                    except Exception as e:
                        results.append((name, False, str(e)))
            except Exception as e:
                results.append(("kickoff", False, str(e)))
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
            if kickoff_pass_complete(results):
                self.settings.kickoff_last_date = datetime.now().date().isoformat()
                self.settings.save(settings_path)
                self._kickoff_retry_after = None
                self._kickoff_succeeded_nums.clear()
            else:
                self._kickoff_retry_after = time.time() + KICKOFF_RETRY_BACKOFF_S
            self._notify(notification_copy_for_kickoff(results))
            self.refresh_async()

        def _make_threshold(self, pct):
            def cb(_sender):
                try:
                    set_setting(self.switcher.backup_dir, "autoswitch.threshold", str(pct))
                except Exception as e:
                    self._alert(title="openswap", message=f"Couldn't set threshold: {e}")
                    return
                self._restart_engine()  # apply immediately if running
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
                self.rebuild_menu()
            return cb

    MenuBarApp().run()
    return 0
