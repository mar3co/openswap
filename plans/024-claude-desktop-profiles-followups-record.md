# Claude Desktop profiles follow-ups: record

Follow-up to #144 (merged as 0b4fd90). This covers issues #145, #146, #148 and #149. #147 is a manual check on the first Developer-ID signed build, so it isn't covered here.

## Rulings

- 2026-10-28, user: implementers run on Opus. The fixes land in one follow-up PR based on main, not in #144.
- 2026-10-28, user: when the legacy slot is the active Code account, #146 verifies it with Claude Code's live credential, read-only. It is never refreshed, written or copied. If it verifies, the account is linked silently; otherwise behavior is as before. This supersedes the earlier "active account: behavior as today" outcome.
- 2026-10-28, controller: for #148, the registry lock is held for neither slot kind. A legacy slot is protected by restoring its credential and config before the engine mutation guard is released. It is not protected by holding the lock.
- 2026-10-28, controller: for #146, the refresh goes only through `engine.consume_backup_grant`, never `freshen_backup` or a direct refresh. A transient failure ends in the existing needs-attention state, with no automatic retry. A token that proves to be another account ends in "mismatch", and the slot is never overwritten.
- 2026-10-28, controller: for #145, detection and notice only. After a loss, no automatic claim happens until the user chooses Reconnect sign-in.

- 2026-10-28, user: #145 early takeover. If Claude takes the handler back before the 2 s check, after OpenSwap held it in this run, OpenSwap behaves as for a late takeover: one notice, then a pause, and no automatic claim or consent prompt until Reconnect sign-in. A first claim in a run still happens. This supersedes the behavior pinned by `test_new_claude_instance_registering_again_reconnects_after_startup`.

## Pre-dispatch ruling check

Two Opus passes (the second because credential rulings changed) amended the briefs. In all, they made 15 + 9 corrections, including:
- the refresh gate
- the never-discard-a-refreshed-token rule
- 401 versus transient handling
- the restore ordering under the engine mutation guard
- arming detection when OpenSwap already owns the handler
- no notice while a consent sheet is open
- decoding the percent-encoded host
- keeping uninstall's failed-step reporting

The amended briefs are the lane notes below.

## Lanes

Dependency graph: the lanes share no source files. Lane 1 owns claude_desktop_code.py, oauth.py and engine/consume|freshen. Lane 2 owns claude_desktop_links.py. Lane 3 owns menu, uninstall, dock, presence, shortcut, guide and menubar_dialog. There is no migration, and nothing every lane appends to.

- Lane 1: #148, #146, #149 F-5 and F-11.
- Lane 2: #145, #149 F-6 and F-8.
- Lane 3: #149 F-2, F-3, F-4, F-7, F-9, F-10 and F-12.

## Lane notes


### Lane 1: Code setup credential path (issues #148, #146, #149 items F-5 and F-11)

Repo: mar3co/openswap. Read each issue in full first (`gh issue view <n> --repo mar3co/openswap --comments`).
Files you own: `src/openswap/claude_desktop_code.py`, `src/openswap/oauth.py` (only for the #146 response classification), the engine's backup refresh gate (`src/openswap/engine/consume.py`, `engine/freshen.py`) if a change there is truly needed, and their tests. Another lane owns `claude_desktop_links.py`, `claude_desktop_menu.py`, `uninstall.py`, `claude_desktop_dock.py`, `claude_desktop_presence.py`, `claude_desktop_shortcut.py`, `claude_desktop_guide.py` and `menubar_dialog.py`. If you need one of those, stop and say why.

The outcomes below are required. Design the mechanism yourself with the code in front of you.

#### #148: no Keychain write while holding the profile registry lock

Ruling: the lock is held for neither slot kind. A legacy slot is protected by restoring it, not by holding the lock.

| State | Required outcome |
|---|---|
| Code setup commits while the credential write is slow (up to the 5 s `security` timeout) | A concurrent profile open, identity-monitor write or `ensure_default` succeeds; it doesn't fail with "Failed to acquire lock". No Keychain or `security` subprocess runs while the registry lock is held, for either slot kind. |
| Profile changed or archived, detected before the slot write | The legacy slot is byte-for-byte unchanged. A new-number slot is never created. `test_profile_change_before_commit_leaves_a_legacy_slot_byte_for_byte_unchanged` keeps passing unmodified. |
| Profile changed or archived during or after the slot write, detected at the final locked check, legacy slot | The slot's original contents are restored, and nothing is committed to the registry. If the restore fails, a warning is logged naming only the exception type; the slot then keeps the new verified credential, which is for the slot's own email and organization. Add a test where the change lands during the credential write. |
| Same, new-number slot | The slot's written credentials and config are removed (today's abort path). |
| The credential write itself fails | No registry metadata is committed. A legacy slot is restored to its original contents; a new-number slot is removed. The failure surfaces as it does today. |
| Normal commit, no concurrent change | The same end state as today: slot written, registry row updated. |

Also required:
- The restore of a legacy slot, and the removal of a new-number slot, finish before the engine `AccountLeaseStore.mutation_guard()` that `save_verified_account` holds is released, so no credential consumer (usage collector, freshen) can run between the write and the restore.
- "Original contents" means the slot's credential and its config file, including absent ones: a config the save created is removed on restore.
- If the legacy slot's current credential or config can't be read at commit time (Keychain locked or denied), nothing is written and setup ends in "failed".

The fresh-row check and the final metadata commit stay under the registry lock. Add a test that shows the lock is free during the credential write (for example, a fake write that takes the registry lock with the normal 0.5 s timeout and succeeds).

#### #146: refresh an expired legacy-slot token before verifying

Ruling: the refresh goes only through `engine.consume_backup_grant`, never `freshen_backup` (it skips tokens not near expiry, and it fills the account UUID unguarded) and never a direct `oauth.try_refresh_oauth_credentials` call. Refresh tokens are single-use; the gate serializes their use with the usage collector and saves the new token only if the slot still holds the token it read. The account UUID is filled only by the existing guarded, credential-pinned `backfill_account_uuid` call in `_connect`. The connector itself never writes refreshed credentials to the slot; the slot holds whatever the gate left. Active accounts are never refreshed, because Claude Code owns those credentials.

Ruling (user, 2026-10-28): when the legacy slot is the active Code account, verify it with Claude Code's live credential, read-only: never refreshed, never written, never copied into the slot. If that verifies, complete and matches, the account is linked silently. If it is rejected or incomplete, use browser login as today. If it is transient, the state is "failed". A contradicting identity means "mismatch". Reading the live credential must not change Claude Code's sign-in in any way.

Classify every profile-endpoint answer like this: 200 with complete identity = verified. 200 with organization, email or `account.uuid` missing = incomplete. 401, 403, 404 and other 4xx except 429 = rejected. Network error, timeout, 429 or 5xx = transient.

| State | Required outcome |
|---|---|
| Legacy slot (not active), backup token verified | Behavior as today. No refresh. |
| Backup token transient | "Failed" (needs attention), no browser login. |
| Backup token rejected or expired | Refresh through the gate. Then: |
| → the gate returns credentials with no error, and the new token verifies and matches | The account is linked silently. No browser login. |
| → new token verified, but a non-empty server field contradicts the Desktop profile (different account UUID or organization) | "Mismatch" (needs attention). No browser login, and the slot is never overwritten by a later browser credential for another account. |
| → new token incomplete (fields missing, nothing contradicting) | Browser login as today. |
| → new token rejected | Browser login. |
| → new token transient | "Failed", no browser login. |
| → gate `invalid_grant`, `invalid_client`, `no_refresh_token` or `store-unmirrored` | Browser login. |
| → any gate outcome whose error is set (even when credentials come back, for example `transient` with `stashed=True`), plus `consume-busy`, `skip-live-session` and `stash-unreadable` | "Failed", no browser login. |
| Backup token incomplete | Browser login as today. |

"Failed" and "mismatch" are the existing needs-attention states, which the user retries with a click. Don't add any automatic retry.

To tell a rejected answer apart from a transient one, add a classifying call or result in `oauth.py`, leaving `fetch_oauth_profile`'s existing callers unchanged. Extract the outcome decision so it is unit-testable, and test it state by state. This is a credential path: the connector writes nothing to the slot itself, and every branch that leads to a browser login must not overwrite a slot proven to hold another account.

#### #149 F-5 and F-11

- F-5: `_discard_login` with `except Exception: pass`. Log a warning naming only `type(exc).__name__`, never the exception text.
- F-11: if `self._sweeper.start()` raises, reset `_sweeper` so a later poll can start a sweeper again. Test it.

#### Rules

- Commit on your worktree branch with plain imperative subjects. No AI attribution trailers. Use American English.
- Run `uv run pytest` (the full suite) on your final tree. Report the pass/fail counts and any failures with their first error line.
- Don't add comments that narrate code. Match the file's style.
- Suggestions: if you see a better or simpler solution, put it in a "Suggestions" section of your report, and still implement this brief as written. If a suggestion would change a required outcome above, ask before starting. "None" is the expected answer.
- Report: what changed per item, the tests added, the gate results, and suggestions.

### Lane 2: Claude link handling (issue #145, issue #149 items F-6 and F-8)

Repo: mar3co/openswap. Read each issue in full first (`gh issue view <n> --repo mar3co/openswap --comments`).
Files you own: `src/openswap/claude_desktop_links.py` and its tests. If a fix needs another source file (for example, to show a notice from the menu), stop and say which file and why before editing it. Another lane owns `claude_desktop_code.py`, `claude_desktop_menu.py`, `uninstall.py`, `claude_desktop_dock.py`, `claude_desktop_presence.py`, `claude_desktop_shortcut.py` and `menubar_dialog.py`.

The outcomes below are required. Design the mechanism yourself with the code in front of you.

#### #145: notice lost `claude://` ownership, never re-claim

Advisor constraint, binding: never re-prompt. Every `setDefaultApplicationAtURL` claim can show the macOS consent sheet, so loss detection must never call it.

| State | Required outcome |
|---|---|
| Detection is armed whenever OpenSwap is seen as the handler, whether from the 2 s check (OpenSwap already owned it, the usual case after login) or after a successful claim | Armed. |
| Armed, and a later check (well after the 2 s check, long enough for a slow Claude cold start) finds `current()` is not OpenSwap while profiles still need the handler | Show one notice, through the existing error/notice callback, telling the user to choose Reconnect sign-in in the profile menu. No claim call. From then on, no automatic claim happens on later Claude launches until the user chooses Reconnect sign-in. |
| Armed, and `current()` is still OpenSwap | Nothing. |
| `current()` raises or returns nothing | Nothing is shown and nothing is claimed. |
| Ownership is lost and stays lost across later Claude launches | No second notice and no claim. Only a Reconnect sign-in that succeeds re-arms detection. |
| A claim or hand-back is in progress (a consent sheet may be open), or OpenSwap itself handed the handler back | No check and no notice. |
| Features off, no profile needs the handler, or a quit/restore is in progress | No check and no notice. |
| OpenSwap never became the handler (the claim failed or was declined) | The existing claim-failure handling applies, and this check adds no notice. |

Extract the decision (state → notice or not) so it is unit-testable, and test it state by state.

#### #149 F-6 and F-8

- F-6: `_hand_back` calls `previous()` before checking `current() != application`. Look `previous()` up only in the branch that uses it, with no behavior change otherwise.
- F-8: `ordinary_link` matches the auth routes on the raw netloc and path, so a percent-encoded route (`claude://claude.ai/m%61gic-link#…`, or an encoded host such as `claude://%6Cogin/…`) counts as ordinary and gets forwarded. Required outcome: a link whose percent-decoded netloc and path match an auth route is classified as auth, exactly like the unencoded form. Malformed percent escapes must not raise. Ordinary links are unchanged. Test the encoded, malformed and plain forms.

#### Rules

- Commit on your worktree branch with plain imperative subjects. No AI attribution trailers. Use American English.
- Run `uv run pytest` (the full suite) on your final tree. Report the pass/fail counts and any failures with their first error line.
- Don't add comments that narrate code. Match the file's style.
- Suggestions: if you see a better or simpler solution, put it in a "Suggestions" section of your report, and still implement this brief as written. If a suggestion would change a required outcome above, ask before starting. "None" is the expected answer.
- Report: what changed per item, the tests added, the gate results, and suggestions.

### Lane 3: Menu, uninstall, Dock and dialog cleanups (issue #149 items F-2, F-3, F-4, F-7, F-9, F-10, F-12)

Repo: mar3co/openswap. Read issue #149 in full first (`gh issue view 149 --repo mar3co/openswap --comments`). It describes each item.
Files you own: `src/openswap/claude_desktop_menu.py`, `uninstall.py`, `claude_desktop_dock.py`, `claude_desktop_presence.py`, `claude_desktop_shortcut.py`, `menubar_dialog.py`, `claude_desktop_guide.py`, any caller of `BrandDialog`, and their tests. Another lane owns `claude_desktop_code.py` and `claude_desktop_links.py`. If you need either, stop and say why.

Required outcomes:

- **F-2:** the chooser's notes depend on which kinds of profile failed their check, not on a total count. A failure of the existing (Default) profile, whether from `ensure_default` or its own per-profile check, counts as Default once. Both notes show only when the Default and at least one managed profile failed. Test Default-only (including when both Default checks fail), managed-only, and both.
- **F-3:** when `_choose_signin_profile(held=None)` runs while not blocked, with features on and no quit or restore in progress (`signin_links.stopping`), it shows the existing "Start again from the intended Claude profile" message. In the other cases (on Quit, for example) it stays silent, like the existing check right below it. Test both.
- **F-4:** in `receive_signin`'s background `work()`, any exception queues a result, so `busy` clears and the callback isn't kept past `MAX_AGE`. Existing outcomes for `ClaudeSwitchError`/`OSError` are unchanged. Test it with an unexpected exception type.
- **F-7:** the uninstall dialog treats an unreadable shortcuts folder (`PermissionError` or another `OSError` from `iterdir()`) as having no owned launchers and doesn't raise. The uninstall run itself keeps reporting an unreadable folder as a failed step (today's behavior); don't turn it into a clean result or a warning. Test the dialog case.
- **F-9:** the "running" dot is an explicit per-item option of `BrandDialog`, set only by the Claude guide items in `claude_desktop_guide.py` that show it today. The dot is drawn only when the item opts in and has an image, so there is still no dot when Claude isn't installed (image is None). The rendered output for every current caller is unchanged.
- **F-10:** write `_prune_backups`'s slice as `backups[1:-(BACKUPS_KEPT - 1)]` (or equivalent) with identical behavior, and confirm with the existing tests.
- **F-12:** simplify `owned_bundle(info=)` only if it is clearly simpler with identical behavior. Otherwise leave it and say why in the report.

#### Rules

- Commit on your worktree branch with plain imperative subjects. No AI attribution trailers. Use American English.
- Run `uv run pytest` (the full suite) on your final tree. Report the pass/fail counts and any failures with their first error line.
- Don't add comments that narrate code. Match the file's style.
- Suggestions: if you see a better or simpler solution, put it in a "Suggestions" section of your report, and still implement this brief as written. If a suggestion would change a required outcome above, ask before starting. "None" is the expected answer.
- Report: what changed per item, the tests added, the gate results, and suggestions.

## Review dispositions

### Lane 3 (#149 menu, uninstall, Dock, dialog)
- Cold seat: one Important. An unexpected discovery exception showed the "check folder permissions" message. Fixed in 793fdd5, and the test now pins the exact text. `OSError` keeps the folder-permissions message, as F-4 requires.
- Cold seat on 793fdd5: clean.
- Note: the `suspend` route of the release test covers a state barely reachable in production. Coverage note only, no change.
- Pre-existing, noted by the fix seat: `poll` shows a discovery error before checking stopped, blocked or suspended. Ledgered as a Minor for the final review.
- F-12 left unchanged: dropping `info=` adds a second plist read and can read the version from a different file.

### Lane 2 (#145, F-6, F-8)
- Cold seat and silent seat: the same Important. In `_claim`'s `ClaudeDesktopError` branch, a stale `held=True` gives two alerts. Goes to fix round 1.
- Silent seat: the one-shot 30 s check is used up while busy or uncertain. Rated Important, because it is a behavior change on a reachable path. Goes to fix round 1.
- Silent seat: the paused state is silent after a restart. It predates this change and touches the menu, so it is filed as #150.
- Cold seat policy note: an early takeover re-claims on every launch. User ruling above; goes to fix round 1.
- Pre-dispatch reader on fix round 1: a Reconnect must always claim (never answer with "Choose Reconnect"), and the `claim_pending` test is reworked. Notes amended.
- Fix round 1 (e2a6b9f), cold seat: two Important findings with low reach, both in code the round added:
  - "wait" when `current()` is None never retries.
  - The `reconnecting` flag is consumed before `_needed()`'s early return.
  Under the convergence rule, there is no further per-task round. Both go to the final review's single fix dispatch. One Minor: unbounded polling of `current()` while it raises. Ledgered.
- Trade-off accepted by the ruling: if Claude re-registers its scheme on every launch, launching a second profile in the same OpenSwap run asks for Reconnect again.

### Lane 1 (#146, #148, F-5, F-11)
- Cold seat and silent seat: no Critical. Important findings, all in fix round 1:
  - A: the gate could spend the live token after a switch (both seats).
  - C: 408 counted as rejected.
  - D: the active branch with an empty backup.
  - E: `skip-live-session` stuck with the wrong remedy.
  - F: an active account with no live credential.
  Minor G: the `_commit_guard` docstring.
- Pre-dispatch reader on fix round 1:
  - F's premise was backwards (`""` means absent, `None` a read error), so it became test-only.
  - A: "active" now means the live config's identity, and an unreadable read means "failed".
  - B (stale restore): refuted. Every slot writer holds the provider mutation guard. Dropped.
  - C: extended to a non-JSON 403.
  - D: extended to a backup with no access token.

