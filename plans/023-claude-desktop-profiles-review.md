# Claude Desktop profiles — deep review record (PR #144)

Review of `codex/claude-desktop-profiles` against `main` at `a7f8de2`
(~4,500 changed source lines, ~5,100 test lines).

## Seats (round 1, all Opus, diff-only, no PR description)

The diff was split into entry-point families so no seat diluted its care:

| Family | Files | Seats |
| --- | --- | --- |
| A — link routing, sign-in, core, uninstall, packaging | `claude_desktop_links.py`, `claude_desktop_signin.py`, `claude_desktop.py`, `uninstall.py`, `packaging/` | cold, silent-failure |
| B — identity, Keychain, Code setup | `claude_desktop_identity.py`, `claude_desktop_code.py`, `claude_desktop_monitor.py`, `macos_keychain.py`, `claude_login.py`, `slots.py`, `cli.py` | cold, silent-failure |
| C — Dock, shortcuts, launcher | `claude_desktop_dock.py`, `claude_desktop_shortcut.py`, `claude_desktop_presence.py`, `profile-launcher.applescript` | cold |
| D1 — Desktop menu | `claude_desktop_menu.py`, `claude_desktop_guide.py` | cold |
| D2 — menu bar | `menubar.py`, `menubar_dialog.py`, `menubar_display.py`, `menubar_panel.py` | cold |
| A+B+C | | API-surface lens |

Prior external findings (Codex bot, on earlier commits) were re-checked at `a7f8de2`.

## Baseline

- CI on `a7f8de2`: `test-windows` fails, 11 tests in `tests/test_claude_desktop_dock.py` (POSIX file URLs on Windows). Others green.
- Local full suite: 5,987 passed, 7 failed in `tests/test_bundle_move.py`. The PR doesn't touch that file, and CI's macOS and Linux runs pass, so this machine's environment causes those failures.

## Findings and dispositions

Severity scale: Critical / Important / Minor. No Critical findings survived adjudication.

### Family A — link routing, sign-in, core, uninstall, packaging

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| A1 | Important | `DesktopSignInLinks.restore` sets `changing=True` before `set_handler`; when that raises, `changing` never resets. Links then stop being claimed or dispatched, and a later Quit queues its callback forever (Quit does nothing). Reached when Claude.app was moved or deleted after OpenSwap claimed the handler. Two seats confirmed it. | Fix + test |
| A2 | Important | Quit waits on the macOS completion handler with no deadline. An unanswered consent prompt or a lost completion makes Quit a no-op. Clicking Quit twice appends two callbacks. | Fix + test |
| A3 | Important | Uninstall's link-restore step raises when the previous handler (Claude.app) is gone or the prompt times out. `run()` then returns 1, "still installed", and the suggested remedy ("Open Claude once") is impossible. | Fix + test |
| A4 | Important (needs live verification) | `send_to_process` sends a GURL Apple event to another app. The hardened-runtime release build has no `com.apple.security.automation.apple-events` entitlement and no `NSAppleEventsUsageDescription`, so delivery may fail with -1743 in signed releases only. | Fix (Q4); verification: #147 |
| A5 | Important | `FileLock(timeout=0.5)` on profiles is held for seconds: `open` (ps, Popen, 0.75 s wait), `finish_signin` (Apple event, up to 5 s), `create_shortcut` (osacompile, iconutil, codesign). Concurrent opens, sign-ins and detection writes fail with "another instance may be running". (Same as C4 / P2.) | Fix |
| A6 | Important | `validate` compares `codesign -d` text output for Identifier and TeamIdentifier and does not require an Apple anchor. A self-signed lookalike that copies the metadata passes as publisher-verified. (Codex P1.) | Fix: `codesign --verify -R` designated requirement |
| A7 | Important | Duplicate suppression hashes every URL before route classification, so the same ordinary `claude://` link opened twice within 120 s is silently dropped. (Codex P2.) | Fix + test: deduplicate only sign-in/connector callbacks |
| A8 | Important | Archiving the last managed profile keeps OpenSwap as the system `claude://` handler until it quits; `needed()` is never rechecked. (Codex P2.) | Fix + test |
| A9 | Important | Quit continues after a restore failure, so OpenSwap exits while still the handler. (Codex P2.) | Fix (Q2) |
| A10 | Important (unverifiable) | Ownership is checked once, right after the claim. If Claude re-registers its scheme after the 2 s delay, callbacks go to whichever instance Launch Services picks, with no notice. | Deferred: #145 (Q5) |
| A11 | Important | The sign-in chooser preselects Default (`selected=0`), so a reflexive Return sends a magic link to Default when several profiles are candidates. | Fix (Q7) |
| A-m1 | Minor | `appkit_compat.py` duplicates the single-arch guard. | Fix |
| A-m2 | Minor | `set_handler` raises "Move OpenSwap to Applications…" for any target that isn't a candidate, including the restore target (Claude). | Fix (message per target) |
| A-m3 | Minor | `except Exception: pass` around `--focus`. | Fix (narrow to the import) |
| A-m4 | Minor | `_save` does not fsync the directory after `os.replace`. | Fix: match `codex/desktop.py`, which fsyncs the directory |
| A-m5 | Minor | Unknown non-auth routes are forwarded to the previous handler. | No change: same as macOS behavior without OpenSwap |
| A-m6 | Minor | Dropped expired or duplicate links give no message. | No change: fails safe, nothing misdelivered |
| A-m7 | Minor | A second `MacOSLinks` never removes the first receiver's observer. | No change: constructed once per process |
| — | — | Codex P2 "obsolete copy-and-paste help text" | Already fixed at `a7f8de2`; no change |

### Family B — identity, Keychain, Code setup

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| B1 | Important | Restart wipe: the monitor's "new login" clear compares against in-memory `previous["fingerprint"]` (empty after restart or refresh). Any detection failure (offline, Keychain paused) then clears email, organization, UUIDs and `detected_login` for an unchanged login, and Code setup stops. Reproduced by two seats; also the Codex P2 finding. | Fix + test: compare against the persisted `profile.detected_login` |
| B2 | Important | A quit or crash during "authorizing" can leave a live refresh token in a hashed Keychain item for `.code-login-*`, with nothing to sweep it. | Fix + test: startup sweep |
| B3 | Important | The Code worker holds the profiles lock (0.5 s) around the engine guard (no timeout) and a 64 MB cookie re-observe. A finished OAuth is lost to a lock timeout, and profile opens fail while an engine operation runs. | Fix |
| B4 | Important | A duplicate roster match raises inside the whole-scan `try`, which stops Code setup for every later profile. The actionable message is replaced by "Code accounts unavailable". | Fix + test |
| B5 | Important | A legacy slot with an expired access token always gets a fresh browser sign-in instead of a silent link. The result is correct, but the user does an extra browser step. | Deferred: #146 (Q5) |
| B6 | Important | `clear_dead_token` raising after the roster commit reports "failed" and skips the "account added" notice. | Fix |
| B7 | Important (narrow) | Toggling Desktop features while the Keychain prompt is open persists a pause even when access is granted. | Fix |
| B8 | Important | Re-linking a legacy slot overwrites its full config backup with a 4-field `oauthAccount` stub. | Fix: keep the existing backup or use the scratch login's `oauthAccount` |
| B9 | Important | `ProfileChanged` after a finished browser OAuth writes state `""`, which clears the setup key, so the next scan reopens the browser with no click. | Fix + test: post-launch change becomes `sign_in` (explicit resume) |
| B10 | Important | "Continue Code setup…" is offered when automatic detection is off. `retry` then fails with a misleading "Profile changed during account detection." | Fix |
| B-m1 | Minor | Overlapping clauses in the `verify_credentials` mismatch condition. | Fix (simplify) |
| B-m2 | Minor | The legacy link path backfills without the profiles lock. | Fix (folded into B3) |
| B-m3 | Minor | Bare `store.get(...)`/`_directories(...)` raise-if-gone guards in identity. | Fix (make the intent explicit) |
| B-m4 | Minor | `completed_login_identity` broad except gives the wrong message when the config is unreadable. | Fix |
| B-m5 | Minor | Cleanup in the `save_verified_account` except can mask the original error. | Fix |
| B-m6 | Minor | `save_verified_account` docstring claims checks it doesn't do; it uses the private `_write_account_credentials` when a public one exists. | Fix |
| B-m7 | Minor | `_native_get_password` rebinds ctypes signatures on every call. | No change: cold path, clarity over caching |

### Family C — Dock, shortcuts, launcher

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| C1 | Important | The launcher's `on quit` terminates Claude but never `continue quit`, so a profile app cancels logout, restart and shutdown. | Fix |
| C2 | Important | Launcher failures show "The command exited with a non-zero status." The CLI's JSON error goes to stdout, which `do shell script` discards on failure. | Fix |
| C3 | Important | Every Dock click on a running profile shells out to the CLI (validate, lock). Because the call is attach-only, failures are silent and the click does nothing. | Fix: activate the retained process directly |
| C4 | Important | Same as A5: the store lock is held across the shortcut build. | Fix |
| C5 | Important | `plan_pins` duplicates a pin when both an alias tile and the current-path tile exist (verified). | Fix + test |
| C6 | Important | New profile pins land in reverse order: each is inserted right after Claude (verified). | Fix + test |
| C7 | Important | A cold Claude start longer than 10 s makes the launcher error and quit while Claude keeps starting. | Fix |
| C8 | Important | Windows CI: Dock tests assume POSIX file URLs, 0600 modes and symlink privilege. | Fix: skip the module off macOS (macOS-only feature) |
| C-m1 | Minor | `with timeout of 120 seconds` has no effect on `do shell script`. | Fix |
| C-m2 | Minor | Default launcher rebuilt and re-signed on every Add. | Fix |
| C-m3 | Minor | Bundle-id and marker literals duplicate `DockShortcut`; `_check_destination` duplicates `_owned_bundle`; unnecessary local import. | Fix |
| C-m4 | Minor | `refresh_icons=True` always kills the Dock; `dock-backups/` grows without bound. | Fix |
| C-m5 | Minor | `activateWithOptions:2` is deprecated on macOS 14+. | No change: still honored for an app activating another app |
| C-m6 | Minor | Race between the running check and `open -g`; stale v1 applet keeps running after an in-place upgrade. | No change: narrow window, self-heals on next launch |

### Family D1 — Desktop menu

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| D1-1 | Important | `receive_signin` runs `codesign --verify --deep` and one `ps` per profile on the main thread, freezing the menu bar for seconds. | Fix |
| D1-2 | Important | One profile whose process check raises blocks the sign-in chooser for every profile. | Fix + test |
| D1-3 | Important | While an app switch is pending, the menu shows the last finished operation label ("Opening Claude…"). | Fix |
| D1-m1 | Minor | `app` unbound when the factory raises; correct only by accident. | Fix |
| D1-m2 | Minor | `_guide_shown` not initialized in `__init__`. | Fix |
| D1-m3 | Minor | `_editing_details` set inconsistently. | Fix (consistent) |
| D1-m4 | Minor | Private `_icon_bitmap` shared across modules on purpose. | Fix (public name) |
| D1-m5 | Minor | Guide ties the running dot to icon load; original icon skips `isValid()`. | Fix |

### Family D2 — menu bar

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| D2-1 | Important | `_claude_desktop_changed` reloads the open popover from timer results, which can swallow a click in progress. | Fix: defer while a mouse button is down |
| D2-2 | Important | Turning Desktop features on while Codex auto-switching is on leaves Codex running with its toggle disabled and wrong hint copy. | Fix: keep the row enabled so it can be turned off |
| D2-3 | Important | The `needed()` lambda reads the store without a guard; a broken registry raises out of the toggle and the native launch notification. | Fix + test |
| D2-4 | Important | Card-list and profiles scroll offsets survive tab changes through the Desktop tab. | Fix |
| D2-m1 | Minor | Dead `on_claude_desktop` / `_claude_desktop_profiles` / `_show_claude_desktop_profiles` chain and its tests. | Fix (remove) |
| D2-m2 | Minor | Stale copy: "titled ChatGPT", "Auto-switch settings", "experimental submenu", "live Codex rotation", "for testing…". | Fix |
| D2-m3 | Minor | Scroll-view setup copied three times; disclosure button copied twice. | Fix (extract helpers) |
| D2-m4 | Minor | `settings_state()` writes via `ensure_default()` on every build; double reload. | Fix |
| D2-m5 | Minor | Guide illustrations not counted in the dialog height limit. | Fix |
| D2-m6 | Minor | `_claude_desktop_update_pending` read via `getattr`; org UUID lowercased on one side only. | Fix |

### API-surface lens (A+B+C)

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| P1 | Important | = A3 | — |
| P2 | Important | = A5 | — |
| P3 | Important | Uninstall leaves the generated profile launchers (and pins) pointing at the deleted OpenSwap executable. | Fix (Q3) |
| P-m1 | Minor | Underscore-private names used as cross-module API (`store._lock`, `_directories`, `_save`, `app._running`, `_stamp`, `_plain`, `_fourcc`, `_read`, `_owned_bundle`). `_save` skips row validation. | Cleanup wave (Q8); no `_save` validation |
| P-m2 | Minor | Stale docstrings: `remove_integrations`, `save_verified_account`. | Fix |
| P-m3 | Minor | `install_shortcuts` trusts the caller-supplied root. | Fix (derive from store) |
| P-m4 | Minor | Registry versions 1–5 look dead against `main`. | Fix: drop versions 1–5 (Q6) |
| P-m5 | Minor | Duplicated helpers: running argv (x3), link character guard (x3), UUID regex (x2, different case rules), Info.plist read (x2), 0600 lock-file create (x2), `_labels` (x2). | Fix |

## Advisor rulings (Fable)

The user was away and delegated these decisions to a Fable advisor, whose recommendations are followed.

- **Q1 — Implementer model:** Fable, for all four family lanes and the cleanup wave. This keeps implementers and the Opus review seats on different models.
- **Q2 — Quit (A2/A9):** Quit always completes within a bounded wait and is never refused.
  - When the restore raises or completes with ok=False: show the existing "Could not restore Claude links…" alert, then quit.
  - When nothing completes within 10 s: quit silently, leaving the OS consent sheet to the user.
  - A second Quit click while waiting quits immediately (one pending quit).
  - Why: OpenSwap being the handler while not running is already a survivable state (a link cold-starts it, and it forwards ordinary links).
- **Q3 — Uninstall (A3/P3):**
  - (a) The link restore is best-effort. It never blocks uninstall: failure prints a warning with the remedy, and it is skipped when `/Applications/Claude.app` is absent.
  - (b) Uninstall removes OpenSwap-owned launchers in `~/Applications/OpenSwap Profiles` (via the `_owned_bundle` check), removes the folder when it is then empty, and removes their Dock tiles using the same backup and snapshot-compare discipline as `install_shortcuts`.
  - A launcher-deletion failure is reported like the other integration steps; a Dock refresh failure is a warning. Profile data stays.
- **Q4 — Apple-events entitlement (A4):** add `com.apple.security.automation.apple-events` and `NSAppleEventsUsageDescription` now. Also file an issue to verify `send_to_process` on the first Developer-ID build.
- **Q5 — Deferred to issues:**
  - A10: re-checking handler ownership must not re-prompt; any fix is detect-and-notify only.
  - B5: refresh a legacy slot's expired token before verifying, using `oauth.try_refresh_oauth_credentials`.
- **Q6 — Registry versions (P-m4):** accept only version 6 and drop versions 1–5. No released build wrote them, and the local `profiles.json` is already v6.
- **Q7 — Chooser preselection (A11):** preselect only when there is exactly one candidate. With two or more, the first row is "Choose a profile…", and Continue on it re-prompts; Cancel still cancels.
- **Q8 — Private-name promotion (P-m1):**
  - Done as the single post-merge cleanup dispatch, not in the parallel lanes, to avoid conflicts.
  - No row validation is added to `_save`, because every caller passes validated `Profile` rows. This supersedes the ledger's "`_save` skips validation" concern.
- **Q9 — Locks (A5/B3/C4):** the registry lock guards metadata only (list/get, directories, save), and its timeout stays 0.5 s.
  - `open`: the launch runs under a separate `launch.lock` (3 s), so a concurrent open reports `already_running`.
  - `finish_signin`: the Apple event is sent outside the registry lock.
  - `create_shortcut`: builds outside the registry lock and persists the badge through the validating `store.update`.
  - Code worker: lock ordering is engine guard → registry lock, never the reverse. Cookies are re-observed with no lock held.

## Fix round 1

Four Fable implementers, one per lane, each in its own worktree with disjoint file ownership. Their commits were cherry-picked one at a time onto the PR branch. Every pick was clean, with no hand-resolved hunks.

| Commit | Lane | Summary |
| --- | --- | --- |
| `8a36457` | C | Launcher `continue quit`, reopen focus without the CLI, cold-start wait, stderr alert; `plan_pins` exact-match and creation order; shortcut build outside the registry lock with a locked re-read; Default bundle reuse; Dock refresh only on change; backups pruned to 5; Dock tests skipped off macOS |
| `8b32d63` | B | Identity cleared only against the persisted fingerprint; orphaned `.code-login-*` sweep; lock order engine → registry with a fresh row check inside the engine guard; duplicate-slot isolation; non-fatal `clear_dead_token`; Keychain pause fix; legacy backup kept; post-launch `ProfileChanged` → `sign_in` |
| `7b83639` | controller | `LAUNCHER_VERSION` 2 → 3, so existing branch-built launchers pick up the fixed script (lane C suggestion, adopted: the user has v2 launchers with the logout-canceling quit handler) |
| `3ae4893` | D | Off-main-thread sign-in discovery with the link gate held; per-profile skip; chooser placeholder; waiting label; deferred popover reload while the mouse is down; Codex row stays switchable; `needed` raise handling; scroll reset; dead chain removed; copy fixes; scroll/disclosure helpers; single reload |
| `8599b71` | A | Restore unsticks `changing`; Quit bounded at 10 s, single pending; startup/A8 recheck; dedupe only callbacks; codesign `-R` requirement (passes on the real Claude.app); launch lock + `already_opening`; Apple-event send outside the registry lock with Automation-consent messages; entitlement + usage string; best-effort uninstall link step with a warnings channel; uninstall removes owned launchers and Dock tiles; registry v6 only; `--from-launcher` stderr; focus warning; directory fsync |

Gates on the merged tree: full suite 6,088 passed, 9 skipped, and the 7 known `test_bundle_move` environment failures. `git diff --check` is clean.

Implementer deviations and suggestions, ruled:

- **C4 deviation (accepted):** a locked re-read plus `_save` replaces `store.update`. `FileLock` isn't reentrant, so `update` can't run inside the final check; the outcome (an archive or rename mid-build leaves no launcher and no badge) holds.
- **C suggestion (adopted):** bump the launcher version (above).
- **B suggestion (rejected):** merge the scratch `oauthAccount` into kept backups. It would change what a backup holds, and keeping the existing backup already fixes the data loss.
- **B note:** a microsecond UI-thread race in B7 can leave an in-memory pause (never persisted) until retry. Ledgered as no change, since it fails safe.
- **D:** kept the Settings "Show → ChatGPT" label, because the desktop-mode menu bar title reads "ChatGPT · …". Accepted.
- **A suggestion (cleanup wave):** `_owned_bundle` should accept the marker it reads, so the uninstall `_launcher` shim goes away. `_quit_pending`/`_quit_finished` should be initialized in `__init__`.
- **A note:** with features on and the handler held, `needed()` (a `profiles.json` read) runs every 5 s. Accepted as a cheap read.

## Pre-dispatch ruling check (Opus adversarial reader, one pass)

The pass amended the required outcomes before any implementer saw them:

- **A1:** claims resume only after `prepare()` or an A8 re-arm.
- **A2:** the next startup restores the handler when OpenSwap holds it but features are off or no profile needs it. This covers a consent sheet answered after Quit.
- **A8:** only a definite `needed() == False` restores the handler; an error changes nothing. An A8 restore doesn't block re-claiming.
- **D2-3:** reversed. A store read error is treated as unknown rather than False, so a transient read error can't hand the handler back.
- **P3:** moved to lane A, so one lane owns `uninstall.py` and adds the shared warnings channel. Uninstall never creates the profile store.
- **A4:** Automation consent errors (-1743/-1744) get a message that names the right setting.
- **A5:** a launch-lock timeout is reported as already opening.
- **A6:** must pass against the real Claude.app.
- **C2:** stderr carries only a user-facing message.
- **A-m3:** a focus failure never fails a successful open.
- **A-m4:** a directory-fsync failure after the replace doesn't fail the save.
- **B2:** the sweep runs on the first poll with features on and skips live logins.
- **C1:** dropped its self-contradicting parenthetical.
- **C4:** `update` succeeds on archived rows, so an archive or rename mid-build is caught with a locked re-read instead.
- **C7:** keeps waiting while the launched PID is alive but not yet registered.
- **C-m2:** a Default bundle is reused only when its command and target path match.
- **C-m4:** also refreshes the Dock when a pinned launcher's icon was rebuilt.
- **D1-1:** no second dispatch while discovery or the chooser is pending.
- **D1-2:** shows a "could not check" message when every check raised.
- **D2-m4:** keeps `ensure_default` for sign-in discovery.

## Round-1 fix review (whole-branch final review)

Six Opus seats reviewed the fix commits: a cold seat on each of A, B, C and D, and a silent-failure seat on each of A and B. A usage limit interrupted five of them mid-run; each was resumed under the 40% context line (110–135k tokens measured). No Critical findings.

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| R2-1 | Important | A background release empties the event queue, dropping the ordinary link that cold-started OpenSwap. | Fix |
| R2-2 | Important | After the Quit deadline, `_finish_quit` → `suspend_checks` → `restore()` sends a second handler request. | Fix |
| R2-3 | Important | A failed background release alerts unprompted, on every launch when Claude.app is gone. | Fix: quiet, and skipped when the target is absent |
| R2-4 | Important | `register_application` failing for the restore target says "Reopen OpenSwap". | Fix |
| R2-5 | Important | One launch lock for all profiles: opening B while A launches answers `already_opening` and launches nothing. | Fix: per-profile lock |
| R2-6 | Important | `already_opening` is shown as "opened" in the menu, and the applet hits a null-pid coercion error. | Fix |
| R2-7 | Important | `_unpin` catches only two exception types; an escape fails the step after launchers were deleted, and a retry leaves dead tiles silently. | Fix |
| R2-8 | Important | The orphan sweep relies on a cleanup that swallows Keychain errors, so it deletes the folder and orphans a live token. Same blind spot in `_connect`'s `finally` (pre-existing). The test mocked a raise production never does. | Fix |
| R2-9 | Important | Legacy-slot credential and config writes happen before the commit guard and aren't rolled back. | Fix |
| R2-10 | Minor | The sweep runs a blocking `security` call on the main thread. | Fix |
| R2-11 | Important | `ensure_default` failing in sign-in discovery cancels it for every profile (diverged from `_profiles()`). | Fix |
| R2-12 | Important | Retrying "Set up Dock shortcuts" no longer refreshes the Dock. | Fix |
| R2-13 | Important | Backup pruning deletes the original, pre-OpenSwap Dock layout. | Fix: keep the oldest plus the newest 4 |
| — | Important (refuted) | "Launcher alerts never show the CLI message." Refuted on the merged tree: lane A writes stderr under `--from-launcher`. The seat reviewed lane C's commit alone. | No change |
| — | Minor | Late registry-lock timeout rolls back a finished shortcut build; Code worker lock timeout behind `install_shortcuts`. | No change: visible failure with a retry |
| — | Minor | 2-minute launcher wait blocks quit events during a cold start; `continue quit` before Claude confirms its quit; pinned Default refresh. | No change: accepted trade-offs of rulings C1/C7 |
| — | Minor | Whole Dock test module skipped off macOS. | No change: the `plan_pins` tests use POSIX file URLs too, which failed on Windows CI |
| — | Minor | Microsecond race between archive and launch; half-deleted launcher; focus ImportError silent; version `6.0` accepted. | No change: nothing lost, fails safe |
| R2-m1…m8 | Minor | Uninstall dialog wording; logging the `clear_dead_token` failure; `MISMATCH` order; `_require_present` inlined; padding test removed; callback kept out of `_results`; guide dot; `_default_registered`; reuse record name, single plist read, quoted pid; quit flags initialized. | Fix in the final dispatch |

The final dispatch combines R2 with the cleanup wave (Q8 renames, P-m5 dedup, docstrings) in two commits. Notes are in the session scratchpad, mirrored here by ID.

### Pre-dispatch ruling check on the final dispatch (Opus, one pass)

Amendments made before the implementer saw the notes:

- **R2-1:** ordinary links are forwarded without waiting on a pending handler change; at Quit or disable, only callbacks are discarded.
- **R2-2:** covers every Quit route (decline, raise, deadline): no second request or alert.
- **R2-3:** quiet only when no explicit restore is waiting.
- **R2-4:** an explicit restore to a missing Claude is quiet.
- **R2-6:** no focus and no "running" for `already_opening`.
- **R2-8 (security):** a kept folder holds no credential or identity files.
- **R2-10:** scoped to the Code connector.
- **R2-11 (wrong-profile risk):** an unchecked Default never allows preselection, even with one candidate.
- **R2-m2:** the log line carries only the exception type.
- **R2-m4:** the held callback is cleared on every exit.
- **CW-1:** uninstall ownership is not tied to `default_root()`.
- **CW-2:** whitespace handling is preserved per caller.

## Final dispatch

Commits:
- `ffd5914`: R2 behavior fixes, Fable.
- `a4a33ec`: cleanup wave (Q8 renames, P-m5 dedupe, docstrings).
- `27fcca1`: controller change. With any open profile unchecked, a lone candidate is never preselected. This extends R2-11 to managed profiles under the same reasoning: the unchecked profile may be the origin.

Docs (`docs/claude-desktop-profiles.md`) were updated for behavior the fixes changed: bounded Quit and quiet startup handback, launcher quit, backup retention, v6-only registry, kept scratch folders, and uninstall removing launchers.

Gates: full suite 6,116 passed, 9 skipped, and the 7 known `test_bundle_move` environment failures. `git diff --check` is clean. The launcher script compiles.

### Final review (one cold seat, one silent-failure seat, Opus)

No Critical findings. Under the hard budget, every non-Critical finding became a tracked issue:

| ID | Sev | Finding | Disposition |
| --- | --- | --- | --- |
| F-1 | Important | `save_verified_account` holds the registry lock across the Keychain credential write (R2-9 widened the guard). A concurrent open, monitor write or `ensure_default` can time out. Both seats raised it. | #148 |
| F-2…F-12 | Minor | Chooser note when both kinds go unchecked; silent drop on suspend; discovery `try/finally`; cleanup log; `previous()` order; uninstall dialog `iterdir`; percent-encoded auth route (pre-existing); dialog dot coupling; prune readability; sweeper start failure; `owned_bundle(info=)`. | #149 |

The seats verified the rename/dedupe commit as behavior-preserving. The only differences are stricter symlink and bundle-id checks in `open_presence` and the uninstall scan.

## Tracked issues

- #145: detect lost `claude://` ownership (A10).
- #146: refresh an expired legacy-slot token (B5).
- #147: verify Apple-event delivery on a Developer-ID build (A4).
- #148: registry lock during the Keychain write (F-1).
- #149: final-review Minors (F-2…F-12).

## Left for the user

- Live acceptance from the PR body still applies: real Google, email and SSO sign-in with multiple profiles; Keychain detection; Code authorization; Dock pins across reboot.
- New since the review: on a Developer-ID build, the first Apple-event sign-in delivery may show a macOS Automation prompt (#147).
- Launchers built by this branch (v2) rebuild as v3 on the next status refresh or open.
- Merging is the user's call.
