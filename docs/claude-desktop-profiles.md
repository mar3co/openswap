# Claude Desktop profiles (experimental)

OpenSwap can open named Claude Desktop profiles on macOS. Turn on **Settings →
General → Enable desktop app features**. The **Claude** tab then selects Code
and a linked Desktop profile together. **Profiles…** opens the **Settings →
Desktop** page; its app picker separates Claude Desktop from ChatGPT controls.
Each profile has its own desktop data folder and Claude Code config
folder. A profile name is your label; it is not a verified account identity.

This supports separate email logins, including separate personal accounts. The
personal/Team/Enterprise choice within one email still happens inside Claude.
Creating a profile does not create another Anthropic account or subscription.

## Use it

1. Install the official Claude app in `/Applications/Claude.app`.
2. Enable desktop app features in **Settings → General**. Add your Claude Code
   logins if they are not already saved.
3. Select an account in the **Claude** tab. On first use, choose an existing
   Desktop profile or create one. Confirm the email, workspace and profile link.
4. Select both. OpenSwap switches the default Code login, then opens the linked
   Desktop profile. Sign into the intended account in that window on first use.
   Check its email and selected organization, including the Code tab, before work.
5. Repeat for another account. Later selections reuse its link. An already
   running profile is brought forward when macOS permits it.

The link is your choice, not a verified Desktop identity. OpenSwap never copies
Code credentials into Desktop. The account card shows the profile name; its
**Link Desktop profile…** menu changes the link. **Manage…** also offers **Link
Code account…** and **Unlink Code account**. An unlinked profile can **Open to
sign in…** during setup without changing Code. A linked profile offers **Select
Desktop + Code**. Unlinking keeps the profile and its sign-in.

Opening a profile leaves other windows and tasks running. Use Claude's own Quit
action to close a profile. Launching the original Claude app from Applications
or its ordinary Dock icon uses its default profile, not an OpenSwap profile.
Open managed profiles through OpenSwap, including after a Claude update or Mac
restart. macOS may display the same Claude icon/name for different instances.

With Desktop enabled, **Automation → Claude · Desktop + Code → Auto-switch
linked Claude accounts** uses the existing Code quota policy and selects both.
Only available, linked OAuth accounts participate. Unlinked, archived and
API-key accounts are excluded. Desktop's own chat limits are not measured.
Code's selected badge does not prove Desktop sign-in. Other Desktop windows and
conversations remain in place; running terminal/editor Code clients may need to
reload their login. Separate Code session profiles retain their own namespace.

Desktop features are off by default. With them off, the main tabs are **Claude
Code** and **Codex**. The Desktop tab, profile shortcut, Desktop menus and ChatGPT
switching/suggestion controls are hidden. No new Desktop probes or suggestions
start. Turning the preference off clears pending suggestions and discards stale
check results; it keeps profiles, credentials, windows, and per-app preferences.
An app opening or switch already underway must finish before changing the option.
The terminal's explicit `claude-desktop` commands remain available.

On upgrade, an existing ChatGPT Desktop preference or a saved Claude Desktop
profile keeps the feature visible. An explicitly saved off preference always
wins, including after restarting OpenSwap.

The **Desktop** app picker separates Claude profiles from ChatGPT switching.
ChatGPT uses the shared Codex login and still asks before restarting the app.
Its suggestion policy lives in **Automation**. Enabling Desktop features does
not immediately open an app. If Claude auto-switch is already on, future rotation
uses the linked profiles. With Desktop features off, Claude selection and
automatic rotation return to their existing Code-only behavior.

Opening the Claude Desktop settings page checks the installed app in the background.
**Check app** repeats the publisher/version check and refreshes running-profile
status; it does not open Claude or read credentials. The profile menu also has
**Check app and running profiles**. Each profile's submenu shows **Running**,
**Not running**, or **Could not confirm**, with the time of the check. A profile
that has not been checked says so. **Running** is process status, not proof of
sign-in. Check the email and workspace in Claude; sign in again if Claude asks.

The page shows available and archived profile counts. **Archive (keep data)**
hides a profile from the available list and automatic rotation, and preserves its sign-in, settings and
local conversations; it leaves running windows open. **Archived profiles →
Profile: name → Restore profile** makes it available again without launching it.
The link is kept while archived. Selecting that account asks you to restore the
profile or choose another link. Rename only changes the label. These actions do
not delete an Anthropic account.

**Experimental limitations:** check the account in each window. OpenSwap does
not read or verify the Desktop identity or quota, or move conversations between
accounts. Chrome-extension local pairing is
disabled for relocated desktop data in the inspected Claude build. Real OAuth
callback routing, Code sessions, Cowork tasks, and app-update relaunches still
need acceptance testing with two accounts. App launch success is not that proof.
Use your default Claude window for features that are unavailable in a profile.

## Terminal commands

```sh
openswap claude-desktop check
openswap claude-desktop add "Personal"
openswap claude-desktop add "Work"
openswap claude-desktop list
openswap claude-desktop open "Personal"
openswap claude-desktop rename "Work" "Client work"
openswap claude-desktop archive "Client work"
openswap claude-desktop list --all
openswap claude-desktop restore "Client work"
openswap claude-desktop path
```

Every action accepts `--json`. Names or full stable IDs identify profiles. The
terminal's `add` saves a profile; `open` launches it. CLI dispatch does not create
the Claude Code account engine, migrate logins, or change auto-switch settings.
These explicit CLI commands retain their individual scope: `openswap switch`
changes Code only, and `claude-desktop open` opens only that profile. The combined
selection applies to the menu bar's Claude account picker and auto-switch engine.
`check` verifies the installed publisher and reports its version. It does not
certify compatibility or authenticate a profile. Non-macOS launch is refused.

## Storage, recovery, and removal

Data is kept in `~/Library/Application Support/OpenSwap Claude Desktop/`:

- `profiles.json`: versioned labels, stable IDs, archive flags and optional Code
  links (email, organization UUID and a hash of the Code store path); no tokens.
- `<id>/desktop/`: Claude-owned desktop data, including local sessions.
- `<id>/code/`: a separate `CLAUDE_CONFIG_DIR` and Keychain namespace.

These folders can contain private conversations and credentials created by
Claude. OpenSwap creates private folders/files and never exports their contents.
Do not share this directory as a diagnostic attachment.

Renaming only changes a label. Archiving hides a profile while keeping all data
and any running process. Restore it from **Archived profiles**. A failed launch
keeps the profile for retry. Corrupt or newer metadata is rejected without
replacement. Missing data folders are not silently recreated during open.
Restore a backup to its original location; Code Keychain names depend on the
absolute path. Never restore or copy a profile over a running Claude process.

Normal uninstall and `--zap` preserve this separate data directory. To permanently
remove a profile, first sign out and quit that profile in Claude, then remove its
folder yourself. Claude owns its Keychain entries; OpenSwap does not delete them.
No profile deletion or account logout is performed by archive.

## Implementation and evidence

Research date: October 25, 2026. Inspected installed Claude Desktop **3.6556.2**,
bundle `com.anthropic.claudefordesktop`, publisher team `Q6L2SF6YDW`.

- The packaged app recognizes `--user-data-dir`. Desktop persistence resolves
  Electron's `userData` directory.
- The main bundle resolves `CLAUDE_CONFIG_DIR`; `.claude.json` and Code settings
  use it. Code's secure storage uses a path-derived Keychain suffix. Both
  `CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` are set to the same
  dedicated profile path so an inherited override cannot select a shared login.
- Packaged production Claude strips `CLAUDE_USER_DATA_DIR`. OpenSwap does not
  use that variable or developer/debugging authorization flags.
- Relocated `userData` sets `localPairingDisabledReason` to `userData relocated`;
  the Chrome-extension pairing paths honor it. OpenSwap does not bypass it.

Launch calls the original, publisher-verified executable directly with a
profile directory argument. There is no shell interpolation, app copying,
re-signing, browser-cookie import, credential injection, debugging endpoint,
or change to the default Claude data. API credentials and runtime-injection
variables are not inherited; OS proxy/trust settings and explicit managed
policy paths are preserved. Claude still applies its own system policies.
Settings, connectors, permissions, skills, and history from the default profile
are not seeded into a new profile.

A cross-process lock serializes metadata changes and launch checks. A matching
running main process is reused; uncertain process state or conflicting launches
are refused. OpenSwap never kills a user process. An immediate app exit is an
error, even if its exit code is zero. A surviving process is reported as
`launched`, always with `identity_verified: false`.

Links use the exact email and organization, not the slot number. Two teams under
one email stay distinct, and moving slots preserves the link. Version-1 profile
metadata is read without changes and upgraded to version 2 on the next save.
Only one profile can be linked to a Code account within its store. Moving a
link updates both profiles atomically. Moving the Code store requires relinking.

Combined selection verifies the installed app and profile directories before
changing Code. It rechecks the link under the profile lock and the account under
the credential transaction locks. Archived, stale or changed links fail closed.
The normal Code engine owns credential writes and quota decisions; a small
adapter limits automatic destinations to linked profiles.

If Code succeeds but Desktop fails, OpenSwap reports the partial result and
refreshes Code's displayed state. It does not roll back and risk overwriting a
newer login. Select the same account again to retry opening Desktop. A concurrent
outside Code change is also reported. A Desktop window may already have opened
when that check fails; verify both apps. These operations are not an atomic
login across applications.

The app signature is checked on every open, including after updates. Passing
that check means the app is signed by the expected publisher; it is not a
compatibility test for a future build. Changed app behavior may break profiles.

Background launches and publisher checks keep the menu responsive. Results are
applied on the main thread. Opening a running profile targets its process ID,
not the shared bundle ID. Profile names never enter shell commands or paths.

Sources: [Anthropic account switching](https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account),
[organization membership](https://support.claude.com/en/articles/13566435-find-and-join-a-team-or-enterprise-organization),
[Electron app data paths](https://www.electronjs.org/docs/latest/api/app),
and read-only inspection of the installed app's packaged JavaScript. Community
[profile launchers](https://github.com/istisiki/multi-profile-claude) provide prior
art, not Anthropic endorsement or proof of this implementation.

## Verification and release checks

Automated tests cover profile lifecycle, concurrency, failed atomic writes,
invalid metadata, Unicode name collisions, symlinks/hardlinks, private storage,
publisher/platform refusal, process reuse, launch failures, environment
separation, CLI dispatch, menu actions, and worker-to-main-thread handoff.
Linked-selection coverage includes same-email/different-workspace accounts,
slot moves, stale menus, v1 metadata upgrades, exact identity rechecks under the
credential locks, concurrent selections, mode changes, cancellation, archived
links, partial failures and retrying the already-selected Code account. Tests
also run the real rotation and credential engine against temporary accounts,
with a fake Desktop app, to verify filtering and partial-failure reporting.

Observed locally on October 25, 2026: publisher verification passed for
Claude 3.6556.2. Two signed-out temporary instances survived the observation,
created separate desktop data (including separate cookie stores), and repeated
opens reused their original PIDs. Neither instance created its Code identity
file during the signed-out observation, so this did not exercise Code login.
Native empty/populated panels and the Desktop settings page (including checking
and missing-app states) rendered in light/dark appearance. The Profiles/Manage
button callbacks and submenu construction were exercised. These were
constructed native views, not a test of the installed OpenSwap build.
The settings controller also completed a read-only check against the installed
Claude 3.6556.2: publisher verified, one available and one archived disposable
profile, both correctly reported as not running. That check launched no profiles
and accessed no sign-ins.

The October 26 expanded run, after rebasing onto `025bb68`, passed **1,942 tests**
across profiles, linked selection, CLI, core switching/rotation, menu/panel,
Desktop opt-in and upgrade behavior, monitoring, onboarding and uninstall.
This includes the **39-test** linked-selection suite and main's scrolling tests.
Native views covered Desktop on/off, linked/unlinked/archived account cards,
hidden-section recovery, Automation, and the Claude Desktop/ChatGPT settings
picker in light/dark appearance, without clipped text. A 12-account native
panel stayed within a 620-point height limit with Desktop controls below the
scrolling list; scroll position retention was also checked.
A clean full-suite result was unavailable on this
Mac: seven `test_bundle_move.py` failures also reproduced in an untouched
archive of baseline `d221f50` (48 passed / 7 failed for that file), and worker
folder tests stalled inside `pathid._on_disk_darwin` while opening a system
path. A representative stall reproduced in that same untouched archive with
a 12-second bound. Full-suite attempts were interrupted after recording the
diagnostics; these failures are not reported as passing or attributed to the
new feature. No changes were made to those unrelated modules/tests.

The opt-in local smoke probe opens two empty disposable profiles, checks that
both survive and repeated opens reuse the correct process, then stops only its
own children. Do not sign in or start work in those temporary windows:

```sh
PYTHONPATH=src python tools/probe_claude_desktop_profiles.py --launch
```

Before calling this verified desktop account support, complete these checks:

- Sign into two different emails (including two personal accounts); confirm
  each email and organization. Verify A → B → A and restart persistence.
- Exercise Google, magic-link, and organization SSO callbacks with another
  profile/default Claude already running. Verify the intended window received
  the login. Cancel/retry login and sign out of one; the other must remain usable.
- Start a chat, a Code session and a Cowork task in each account; confirm
  billing identity, history and permissions. Opening the other profile must
  leave ongoing Desktop work intact. Combined selection must change the default
  Code login to the linked account; explicit profile-only CLI open must not.
- Test organization changes inside a profile, expired credentials, managed
  policy, app update/relaunch, missing app and damaged profile recovery.
- Link two Code accounts to their Desktop profiles, switch A → B → A manually
  and through automatic rotation, and check both Code and Desktop identities.
  Verify same-email/different-team links, disabling Desktop mode, archived links,
  and retry after Code succeeds but Desktop cannot open.
- Exercise the menu and popover with empty/populated rosters in light/dark
  appearance, including archive/restore and a failed launch.

Record authenticated results separately from synthetic tests and unsigned or
signed-out smoke tests. No tokens or conversation contents belong in evidence.
