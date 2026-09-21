# Menu bar extra

Entry: `openswap menubar` → `openswap.menubar.run`. Optional extra: `rumps`.

## Process

LaunchAgent `com.opensoft.openswap.menubar` runs `openswap menubar`. Accessory activation policy (no Dock icon). `NSApp.appearance` is left `None` so the extra inherits System Settings → Appearance.

After `rumps` attaches the status item, a short timer steals the click for the popover (`menubar_panel.MenuBarPanel`) and sets autosave name `com.opensoft.openswap.menubar`.

## Split: pure vs AppKit

`menubar_display.py` holds import-safe helpers (`format_title`, `MenuBarSettings`, notification copy, panel snapshot adapters). `menubar.py` is the rumps app and re-exports those helpers. Tests in `tests/test_menubar.py` import `openswap.menubar` and never import AppKit.

`menubar_panel.py` is AppKit-only: popover, bars, Dark Mode colors, `fit_status_item`. The header loads the packaged OpenSoft symbol from `openswap/assets`; the same OpenSoft app icon source is rendered into the widget host's AppIcon catalog.

The header pins the OpenSoft mark and **OpenSwap** name on the left and an **Auto-switch** label plus `NSSwitch` on the trailing edge (`trailing_header_frames`). Behavior is `NSPopoverBehaviorApplicationDefined` so More’s overflow menu does not dismiss the popover (Transient would). Click-outside is a global left-click monitor (skip the popover, the status extra, and while More is open). Leave-delay `POPOVER_AUTO_CLOSE_S` (12s) starts only after the pointer exits the popover, not on open; the status extra counts as still inside. Rotate / Best / Settings / More do not close the popover. Settings is an in-popover page (Back returns to the cards) with **General** and **Automation** sections; More is overflow (Claude rotate/best/next-available, Add including ChatGPT sign-in, Rename, Disable, Remove, Refresh creds, History, Refresh now, Quit; a desktop submenu only if the popover cannot attach). The 1s usage tick does not reload Settings, and it does not rebuild the main page on every snapshot (that would swallow a card click). If hold copy changes, `_apply_hold_line` reloads the open main page only, deferred while the left mouse button is down; Settings stays put.

Card title is the alias or org tag (`personal` when the org name is empty); email is the subtitle. Sentinel notes (signed-out, foreign credential, …) render even when last-good bars are present. Claude account-row clicks call `switch_to(..., json_output=True)` unless the slot is `USAGE_RELOGIN_REQUIRED`. ChatGPT rows go through `_on_panel_account_click`: parent toggle, capability probe, then the experimental quit/relaunch. A signed-out ChatGPT OAuth row errors instead of opening `claude auth login`. Signed-out Claude clicks never switch a dead backup: capture only when the live Claude login matches that slot’s email **and** org uuid; otherwise the extra opens `claude auth login --email` in Terminal (after confirming if another managed account is signed in). After a matching login, the extra auto-captures on the 1s tick. A real switch toasts, stamps `autoswitch_state.json` `lastSwitchAt` (engine cooldown, default 5 minutes), and closes. Already-active closes with no toast. A `ClaudeSwitchError` leaves the popover open and brings the extra forward before `rumps.alert`. Cards implement `acceptsFirstMouse_` so the first click on a non-key popover switches. `rebuild_menu` does not reload an open popover (that replaced the view tree mid-click). `_detect_active_change` compares slot number, not email, so two orgs that share an address still refresh.

## Menu-bar display

Settings → General → Menu bar → **Show** selects **Claude**, **ChatGPT**, **Both**, or **Logo only**, independently of the popover tab. Existing settings default to Claude. **Account name**, **5-hour usage**, and **7-day usage** apply to the selected providers; **Claude model limits** appears only when Claude is included. Logo only hides these controls without resetting their saved choices.

`format_menu_bar_title` prefixes each provider and labels quota windows (5h / 7d). Both joins the two provider summaries. ChatGPT uses the selected shared Codex credential, labels its quota **Codex**, and explicitly marks the app identity **unverified**: shared credentials do not prove which account the ChatGPT desktop session is using. Missing shared credentials (including API-key authentication) show **No shared account**, never another saved account. Claude's empty state is **No account**. ChatGPT message limits are not available here.

### Title width

`fit_status_item(..., title=...)` writes the optional title on the status-item **button** (rumps still uses the deprecated `NSStatusItem.setTitle_`) and always displays the packaged OpenSoft template image at 16pt. AppKit sizes the image and text together; **Logo only** leaves the logo visible by itself. There is no asterisk preference; legacy `show_icon` values are ignored. Call it after every title rebuild and on popover attach. A sentinel on either provider's active slot still titles from `last_good` (`title_usage`), frozen at that provider's `fetched_at` so a passed weekly reset does not paint as a fresh 0%.

## Appearance

An `NSPopover` shown from a status item inherits the **menu bar** appearance (wallpaper tint), which can stay Aqua while System Settings is Dark. `resolve_popover_theme` prefers `AppleInterfaceStyle` (`Dark` or unset), then `NSApp.effectiveAppearance`.

Palette tokens are catalog `NSColor`s with dynamic providers (`_dynamic`). Draw fills in `drawRect_`. Do not set `CALayer` `CGColor` from PyObjC (logs `ObjCPointerWarning` and does not flip with appearance).

Observe `effectiveAppearance` and `AppleInterfaceThemeChangedNotification`; reload the popover if it is shown.

## Auto-switch from the extra

`MenuBarSettings` is `menubar_settings.json` (title, refresh, Claude auto on/off, ChatGPT switching, ChatGPT suggestions, kickoff). General holds menu-bar display, confirmation, and refresh controls. Automation names each surface explicitly: **Auto-switch Claude accounts**, **Enable ChatGPT switching**, **Suggest ChatGPT account switches**, and **Auto-switch Codex CLI accounts**. ChatGPT switching defaults off. The suggestions toggle is disabled while the parent is off; suggestions still require their own stored preference and never restart ChatGPT without approval. A legacy file that already had suggestions on and lacks the parent key migrates the parent on; a present `false` always wins. ChatGPT accounts and add/disable/remove stay visible when switching is off; only credential-changing row activation is refused (controller checks are authoritative) and offers **Enable in Settings**.

When switching is on, the extra verifies the ChatGPT app's OpenAI signature and required bundle shape, then probes its bundled Codex backend against a disposable file-backed credential home. It also applies the transaction’s complete Codex configuration policy on a worker whenever the ChatGPT view is active and the observation is stale: never codesign, backend probing, or process inspection on the AppKit thread. Routine signed app updates remain available when this capability contract passes; known-incompatible builds can be blocked. Typed states are `checking`, `unsupported`, `missing`, `invalid`, `stopped`, and `running`. A fresh stopped observation uses **Switch and open ChatGPT**; a fresh running observation uses **Restart ChatGPT**. Running/stopped observations and retryable operational probe failures expire after five seconds. Installation, process state, selected credentials, and relaunch do not verify the profile ChatGPT actually loaded.

ChatGPT suggestions use Codex quota. They cannot run with live Codex CLI rotation; the Codex control is disabled with an explanation while both ChatGPT switching and suggestions are on. The threshold and next-account strategy form one **Shared rotation policy** in `settings.json` via `openswap config`. Cooldown / last switch live in `autoswitch_state.json`. Changing strategy from the extra calls `set_setting` then `_restart_engine` so the running engine reloads policy.

Settings-page strategies: **Most quota left** (`best`), **Burn weekly first** (`consume-first`, ranks 7d `resets_at`), **Burn 5-hour first** (`soonest-5h`, ranks 5h `resets_at`). A muted hint under the picker explains the selected strategy (7-day vs 5-hour when relevant). See `autoswitch._window_reset_ts`. `SwitchEvent.trigger` stays `consume-first` for both consume strategies; that is not the settings key. A muted hold-reason line under the popover header paraphrases the engine's last no-switch or exhausted event (`hold_line_from_event`); the extra does not re-rank cards. When a Claude Code session or IDE lock is live, a muted `running_line` sits above the footer. A live Codex TUI (not `codex exec` or `codex app-server`) adds a second muted `codex_running_line`. Extra Settings can turn Codex rotation off with `autoswitch.codexEnabled` without stopping Claude auto-switch. A successful extra switch (card, Rotate, Best) calls `record_manual_switch` so that policy does not undo the pick for `cooldownSeconds`.

## Kickoff

`kickoff.py` is due/eligibility + `claude -p ok`. The extra owns the clock (`kickoff_hour` / `minute`, `kickoff_last_date`). **Start Claude 5-hour window** and its time live under Settings → Automation; time is an hourly `NSPopUpButton` (`kickoff_time_options`). Persist `kickoff_last_date` only after a successful pass. Skip API-key accounts and windows whose `resets_at` is still in the future.

## Notifications

`rumps.notification` needs a bundle id. `ensure_notification_identity` writes a tiny `Info.plist` next to the uv interpreter (`com.opensoft.openswap.menubar`) if missing.

After an experimental ChatGPT desktop switch completes, the extra sends a
silent **ChatGPT reopened** notification asking the operator to check the
selected account. The inline **ChatGPT reopened · Check the profile** status
remains the fallback if notification delivery fails. Consent and error feedback
remain dialogs; completion does not claim that the running profile is verified.

A switch toast includes “Restart Claude Code to apply now, or wait about 30 seconds.” only when a Claude Code session or IDE lock is live (`claude_running`). Otherwise the restart sentence is omitted. Codex switches include “Restart Codex to apply.” only when a Codex TUI is live (`codex_running`).

A slot that newly becomes re-login-needed toasts once (`personal signed out`) when auto-switch is off (the engine already emits `account-quarantined` when it is on). Capture success toasts `{name} is signed in again`.
