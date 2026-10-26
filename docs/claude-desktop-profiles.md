# Claude Desktop profiles and shortcuts (experimental)

OpenSwap can open named Claude Desktop profiles side by side on macOS. Separate
profiles keep their own sign-in, desktop data and Code configuration. The Default
profile uses the existing Claude Desktop data in place. Desktop
profiles are optional and independent of Claude Code account switching and
rotation. A profile name is your label, not a verified account identity.

## Add a profile and make a shortcut

1. Install the official Claude app in `/Applications/Claude.app`.
2. Turn on **Settings → General → Enable desktop app features**.
3. Open the **Claude Desktop** tab on the main screen and choose **Add profile**.
   If Claude already has a profile on this Mac, choose **Use existing Claude
   profile (Default)** or **Create a separate profile**. A separate profile needs
   a name, such as Personal or Work, before **Create and open**.
4. Default reuses your existing Claude window and sign-in. For a separate
   profile, sign in inside its new window. Email and organization appear
   automatically after detection succeeds.
5. Open that profile's **•••** menu and choose **Create shortcut…**. Choose a number from
   1 to 999 and an orange, blue, purple or green badge. Leave the number blank
   for Claude's standard icon.
6. OpenSwap creates `Claude Work.app` (or your chosen profile name) under
   `~/Applications/OpenSwap Profiles/` and reveals it in Finder. Drag that
   launcher to the Dock for one-click access.

With Desktop features enabled, the main tabs are **Claude Code**, **Claude Desktop**,
and **Codex**. Turning Desktop features off hides the Claude Desktop tab.
Each profile has an **Open** button and a **•••** menu for **Create shortcut…**,
**Account details…**, **Rename…**, and **Archive…**. The same actions are available under
**More → Claude Desktop profiles**. App checks and **About profiles** help remain
under **Settings → Desktop → Claude Desktop**. The gear button on the Claude
Desktop tab opens those settings; Back returns to the profile list.

Opening a profile again brings its existing process forward when macOS permits
it. Other profile windows stay open. Neither opening a profile nor using its
shortcut changes the default Code login in your terminal or editor. Desktop
profiles have no automatic rotation, and do not require saved Code accounts.

Claude handles sign-in inside each profile. OpenSwap does not transfer credentials
between profiles. Default references the existing profile without copying it. The personal/Team/Enterprise selection
within one email still happens inside Claude. Creating a profile does not
create an Anthropic account or subscription.

## Default profile and automatic account details

Default is added only through **Add profile → Use existing Claude profile
(Default)**. OpenSwap never adds it just because it exists. The existing data
stays in `~/Library/Application Support/Claude`; its permissions and sign-in are
unchanged. Default uses the normal Claude launch without a separate data folder
or Code environment overrides. It is saved only once, even if renamed. Adding
it again after archiving restores the same profile and shortcuts.

While Desktop features are enabled, OpenSwap watches registered profiles for
new sign-ins, sign-outs and account/organization changes. Changed profile files
are checked about every ten seconds. Account requests run only when the login
changes or a periodic refresh is due; they never block opening a profile.
Detected details are saved automatically beneath the existing profile name.
The profile name and ID never change based on email, and several profiles can
use the same email or account.

Add profile explains that macOS may ask OpenSwap for access to **Claude Safe
Storage** in Keychain to show the email and organization. The shared Keychain
helper uses native Security.framework access for this Claude-owned item, so the
packaged app is the requester. Source installs may identify their Python
interpreter instead. The existing Code credential path keeps its stable Apple
helper and existing access behavior.

If access is denied or unavailable, automatic requests stay paused across
restarts, feature toggles and new profiles. Choose **••• → Account details… →
Detect** to retry. A private, credential-free `keychain-paused` marker is written
before the system request and removed after successful access or explicit
retry; quitting during a request also leaves detection paused. No repeated
background permission dialogs are shown. A successful read caches the decryption
key only in memory. Disabling Desktop features clears that key and discards
in-flight results. No tokens are refreshed or transferred between profiles.

**Account details…** also offers manual editing and an **Update account details
automatically** checkbox. Turn it off to keep your own labels. Older manual
labels stay manual when upgrading. Detect still works when automatic updates
are off; review its result and choose Save. Cancel preserves saved details.

Automatic results are discarded if a profile is archived, edited or disabled
while a check runs. A confirmed sign-out clears previously detected labels.
When a new login cannot be resolved, the previous detected account is cleared
and **Account details unavailable** is shown. **Waiting for sign-in** and
**Account detection paused** explain the other common states. Manual labels
remain user-owned. Detection failures never prevent opening profiles.

Detected labels are a last-detected snapshot; hover for the timestamp. Switching
inside Claude can take a short time to appear. Check Code and Cowork identity
inside those features. Archived profiles and profiles with automatic updates
off are not inspected in the background.

## What the shortcut and icon do

The shortcut is a small, locally generated macOS application. It calls OpenSwap
with the profile's stable ID, then exits. OpenSwap checks the installed Claude
publisher and opens that profile. Claude's app is never copied or modified.

The numbered icon identifies the **shortcut in Finder and when pinned to the
Dock**. Claude's own running-app icon and window titles remain unchanged; macOS
can show the usual Claude icon for each running instance. The badge is not
an unread count or proof of which account is signed in. Pin the generated
launcher, rather than Claude's ordinary running-app icon, to reopen that profile.

Badge numbers are assigned initially in profile order and can be changed when
creating a shortcut. Numbers need not be unique. Creating a shortcut again with
the same name and destination updates that profile's launcher and icon.
OpenSwap refuses to replace unrelated apps or a shortcut for another profile.
If macOS retains an old pinned icon, remove that Dock item and drag the updated
launcher in again.

Renaming a profile preserves its stable ID, data and existing working shortcuts.
It does not rename shortcuts already created. Creating a shortcut after renaming
uses the new name; remove an old launcher yourself if no longer needed. Deleting
a launcher does not delete its profile or sign out of Claude.

Shortcuts need OpenSwap to remain installed. If OpenSwap or a source/uv
installation moves, recreate the shortcut from the new location. Claude updates
continue to use `/Applications/Claude.app`; its publisher is checked on each
open. App updates may still change profile compatibility.

## Sign-in, status, and archiving

Use **Settings → Desktop → Claude Desktop → Check** to verify the installed publisher/version and refresh running
status without opening Claude or reading credentials. Profile rows show
**Running**, **Not running**, or **Could not confirm**; hover over the status
for the check's time.
Running means an app process exists, not that its sign-in is verified.

**Archive…** explains what is kept before hiding a profile from the available list and preventing
its OpenSwap shortcuts from opening it. Default's ordinary Claude app/Dock entry keeps working. Sign-ins, settings, local conversations, icons
and shortcut files are kept; running windows stay open. **Archived profiles →
Restore** makes the same profile and shortcuts usable again without
launching it. Use Claude's own Quit action to close a running profile.

The ordinary Claude app/Dock icon does not select an OpenSwap profile and can
focus an already-running Claude instance. Open managed profiles through OpenSwap
or their generated shortcuts, including after a restart.

Desktop features are off by default. Turning them off hides the Desktop tab,
profile shortcut and Desktop menus, and stops new Desktop checks/suggestions.
It keeps profiles, credentials, windows, shortcuts and per-app preferences.
Explicit terminal commands and already-created shortcuts remain available.
Finish an in-progress profile action before changing the option.

An existing ChatGPT Desktop preference or saved Claude profile keeps Desktop
features visible on upgrade. An explicitly saved off preference always wins.
The Desktop app picker separates Claude profiles from ChatGPT restart-and-switch
controls. Claude Code's account cards and Automation settings remain Code-only
whether Desktop features are on or off.

## Terminal commands

```sh
openswap claude-desktop check
openswap claude-desktop add "Personal"
openswap claude-desktop add "Work"
openswap claude-desktop list
openswap claude-desktop open "Work" --focus
openswap claude-desktop shortcut "Work" --badge 2 --color blue
openswap claude-desktop shortcut "Personal" --badge ""
openswap claude-desktop rename "Work" "Client work"
openswap claude-desktop archive "Client work"
openswap claude-desktop list --all
openswap claude-desktop restore "Client work"
openswap claude-desktop path
```

Every action accepts `--json`. Names or full stable IDs identify profiles.
`shortcut --directory PATH` chooses another output folder. `add` saves a profile;
`open` launches it. This dispatch does not construct the Code account engine,
migrate credentials, or change automatic rotation. Shortcuts use the same open
path and respect archived profiles. Launch and shortcut creation require macOS.

## Storage, recovery, and removal

Separate profile data lives in `~/Library/Application Support/OpenSwap Claude Desktop/`:

- `profiles.json`: versioned labels, stable IDs, archive flags and badge settings;
  optional email/organization labels, a last-detected timestamp, profile kind and automatic-update preference; no tokens or Code account associations.
- `<id>/desktop/`: Claude-owned desktop data, including local sessions.
- `<id>/code/`: a separate `CLAUDE_CONFIG_DIR` and Keychain namespace.

Default has only a registry entry here. Its original Claude-owned folder is
never moved, recreated, chmodded or deleted by OpenSwap.

These private folders can contain conversations and credentials created by
Claude. Do not share them as a diagnostic attachment. Profile versions 1–4, including the
unreleased linked-selection version 2, are readable. Their next save upgrades
metadata to version 5, preserving IDs, data and archives while discarding old
account-link metadata. No account credentials are changed during migration.

A failed launch keeps the profile. Corrupt/newer metadata and missing data
folders are refused without replacing the login. Restore backups to their
original paths; Code Keychain names depend on the absolute path. Never restore
or copy profile data over a running Claude process.

Shortcut creation builds and signs the new launcher in a temporary folder,
then replaces only a matching OpenSwap launcher. Failure restores the old
launcher. Badge settings are saved only after the launcher is installed.

Normal uninstall and `--zap` preserve profile data and shortcut applications.
Remove unused launchers yourself. To permanently remove a profile, first sign
out and quit that profile in Claude, then remove its data folder yourself.
Claude owns its Keychain entries; OpenSwap does not delete them.

## Implementation and verification

The inspected Claude Desktop version is **3.6556.2**, bundle
`com.anthropic.claudefordesktop`, publisher team `Q6L2SF6YDW`.

- The original signed executable accepts `--user-data-dir`, which selects
  Electron's desktop data directory.
- For separate profiles, both `CLAUDE_CONFIG_DIR` and `CLAUDE_SECURESTORAGE_CONFIG_DIR` point at the
  profile's Code folder. The inspected app uses a path-derived Keychain suffix.
- API credentials and runtime-injection variables are removed from the launch
  environment. OS proxy/trust settings and managed policy paths are preserved.
- Profile locks serialize management and launch checks. Matching processes are
  reused. Conflicting or uncertain process state is refused; user processes
  are never killed. Launch always reports `identity_verified: false`.
- Automatic and on-demand detection support the inspected Chromium v24 cookie schema and
  macOS v10 encryption. They check the signed Claude app, read only the selected
  registered profile (including Default), and use a private temporary copy of the original DB/WAL so
  SQLite cannot create sidecars in Claude's folder. The copy is removed after
  reading. Decrypted cookies and the Keychain key stay in memory. Only the
  selected Claude session cookies go to the fixed HTTPS `/api/bootstrap`
  endpoint, with redirects refused and bounded response size/timeouts. The
  selected organization must match an account membership; account or cookie
  changes during the request reject the result. Default is read only when
  explicitly registered; a separate profile never falls back to Default or CLI
  identity. Background results use conditional writes and a final file-change
  check so manual edits and newer logins win. Unknown formats, blocked requests and denied Keychain
  access leave manual labels usable.
- Shortcuts use Apple's `osacompile` applets. Shell arguments and AppleScript
  strings are escaped independently. Icons are rendered locally from the
  installed app's icon at all ICNS sizes. Only the generated launcher receives
  an ad-hoc signature; Claude's signature is untouched.

References: [Apple's script applications](https://developer.apple.com/library/archive/documentation/LanguagesUtilities/Conceptual/MacAutomationScriptingGuide/SaveaScript.html),
[app icon keys](https://developer.apple.com/library/archive/documentation/General/Reference/InfoPlistKeyReference/Articles/CoreFoundationKeys.html),
[Electron data paths](https://www.electronjs.org/docs/latest/api/app), and
read-only inspection of the installed Claude app. These mechanisms do not
constitute Anthropic endorsement or authenticated compatibility proof.
Detection also follows Chromium's [macOS cookie encryption](https://chromium.googlesource.com/chromium/src/+/93d0dcd55046ac06154f4d642c26d7697c4b0304/components/os_crypt/sync/os_crypt_mac.mm)
and [v24 domain binding](https://chromium.googlesource.com/chromium/src/+/5ea6d65c622a3d5ff75db9dc0257ea3869f31289%5E%21/).
Claude's bootstrap endpoint is private and may change.

Local tests cover private profile storage, concurrency, corrupt metadata,
symlinks/hardlinks, publisher refusal, launch reuse and failure, independent
Code behavior, metadata migration, badge validation, canceled dialogs, shortcut
collisions, failed-update recovery, shell escaping, and CLI/UI dispatch.
The relevant local regression selection passed **1,937 tests** on October 26,
2026, covering profile/shortcut commands, settings, menu bar behavior, Code
switching/rotation, and uninstall preservation.

The account-details follow-up passed **744 focused tests** and **15 native dialog
tests**. It covers selected-organization matching, same-email profiles, cookie
isolation and WAL reads, account changes during detection, denied Keychain
access, malformed responses, redirects, canceled dialogs, manual edits and
metadata migration. Light/dark native renders cover the new fields and profile
rows, long labels, archived profiles and scrolling. Only the Keychain item's
name/account label was checked live; no real sign-in was read or sent during
these tests.

The Default-profile and automatic-update follow-up passed **778 focused tests**
and **15 native dialog tests**. Additional coverage includes explicit adoption,
no credential copying, ordinary-process reuse, default/isolated launch arguments,
version 4 migration, login/org changes, sign-out, backoff, Keychain denial,
feature disabling, and late-result rejection. Native light/dark checks cover
both create-flow choices, automatic-update controls and paused/waiting states.
A live read-only check against the existing Default profile reached macOS
Keychain access, which timed out. No live account response was retrieved.

The Keychain UX follow-up passed **834 focused tests** (three real-Keychain CI
tests skipped locally) and **15 native dialog tests**. It covers the shared
helper's native query, denial/cancellation, resource cleanup, persisted pauses,
explicit retry, feature toggles, restart and interrupted requests. Light/dark
renders verify the explanation in both Add profile choices. A temporary
PyInstaller DEV app named OpenSwap passed strict signature verification and a
native lookup for a unique nonexistent item without reading a credential.
It is running locally with settings and the Homebrew login item preserved.
The password dialog's displayed requester and real signed-in detection still
need user verification; this local build is ad hoc signed, not a notarized
release or proof that Keychain trust persists across release updates.

Observed on October 26, 2026: a disposable native launcher was compiled and
signed, rendered its numbered Claude icon, launched through Finder, preserved
quoted/Unicode arguments, and launched again after an icon update. Creation
also worked on a background thread. Its target was a harmless argument recorder,
not an authenticated Claude account. Native settings and account panels were
checked in light/dark appearance, including scrolling lists.

Earlier signed-out smoke testing on Claude 3.6556.2 opened two temporary profiles
with separate data/cookie stores and reused their original PIDs. It did not
exercise sign-in or create Code identity files. The optional disposable probe is:

```sh
PYTHONPATH=src python tools/probe_claude_desktop_profiles.py --launch
```

A clean full local suite was unavailable on this Mac: seven bundle-move failures
and a worker-folder stall also reproduced on untouched baseline `d221f50`.
The worker stall occurred inside `pathid._on_disk_darwin` while opening a system
path. These unrelated failures are not counted as passing checks.

## Remaining live acceptance checks

The feature remains experimental. Chrome-extension local pairing is disabled
for relocated data in the inspected Claude build. OpenSwap does not verify
Desktop identity or quota, move conversations, or promise embedded Code/Cowork
compatibility from a successful launch.

Before claiming authenticated account support:

- Detect details in two signed-in profiles, including the same email with
  different selected organizations. Check Keychain allow/deny, expired sign-in,
  organization switching, same-profile sign-out/sign-in, and application updates.
  Automated fixtures and a real CommonCrypto/OpenSSL vector verify the reader;
  authenticated detection still needs a live profile test. A read-only check
  of the existing Default profile reached Keychain access, which timed out;
  account details were not retrieved or saved.
- Sign into two different accounts and verify email, organization and restart
  persistence in each profile. Exercise Google, magic-link and organization SSO
  callbacks with another profile/default Claude already running.
- Start a chat, a Code session and a Cowork task in each profile. Confirm billing
  identity, permissions and history. Opening another profile must preserve
  ongoing work and the default terminal/editor Code login.
- Check app updates, expired sign-ins, managed policy, missing-app recovery,
  archive/restore with existing shortcuts, and pinned Dock icons across restart.

Record authenticated results separately from synthetic tests and signed-out
smoke tests. Do not include tokens or conversation contents in evidence.
