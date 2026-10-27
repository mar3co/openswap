# Claude Desktop profiles and shortcuts (experimental)

OpenSwap can open named Claude Desktop profiles side by side on macOS. Separate
profiles keep their own sign-in, desktop data and Code configuration. The Default
profile uses the existing Claude Desktop data in place. Desktop
profiles automatically link or add their detected account to Claude Code.
Opening a profile leaves the currently selected CLI account unchanged.
A profile name is your label, not an account identity.

## Add a profile and make a shortcut

1. Install the official Claude app in `/Applications/Claude.app`.
2. Turn on **Settings → General → Enable desktop app features**.
3. Open the **Claude Desktop** tab on the main screen. Your existing Claude
   profile appears first as **Default**, ready to open. Use **Add profile** only
   for an additional session: enter a name such as Personal or Work, then choose
   **Create and open**.
4. Default reuses your existing Claude window and sign-in. For an additional
   profile, sign in inside its new window. Email and organization appear
   automatically after detection succeeds. OpenSwap adds that account and
   organization to Claude Code automatically. Complete Claude's browser
   authorization if asked; there is no separate add step or checkbox.
5. **Create and open** automatically creates a numbered shortcut for the new
   profile and a **Default** shortcut for the existing session, when available.
   Both are saved under `~/Applications/OpenSwap Profiles/`.
6. **Add to Dock** is checked by default. It adds the new numbered shortcut beside
   the original Claude pin. Claude’s original launcher and running indicator stay
   intact. Other pins keep their relative order. Uncheck it to create the shortcut
   files without changing the Dock. Default’s separate shortcut stays in Finder
   unless you explicitly choose **Set up Dock shortcuts** for Default.

The shortcuts select the exact profile, even when other Claude windows are open.
Their icons use Claude's original artwork without macOS's surrounding plate,
with a small OpenSwap mark and the profile number kept inside the rounded outline
so macOS does not add a gray frame. Default has the OpenSwap mark
without a number. Each profile app stays in the Dock with a running dot while
its session is open. Clicking it returns to that session; Quit requests a normal
quit of that session only. Claude’s original app icon remains available.

If setup needs attention, the new profile is kept and still opens for sign-in.
Choose **••• → Set up Dock shortcuts** to retry without creating another profile.
This also sets up shortcuts for existing profiles and refreshes their icons.
Retries do not duplicate pins. Ordinary opens and startup never re-add pins the
user removed. If an organization manages the Dock, the shortcut files are still
available in Applications → OpenSwap Profiles.

Choose **Create shortcut…** to customize the number (1–999 or blank) and badge
color (orange, blue, purple or green). Then use **Set up Dock shortcuts** to
refresh a pinned icon.

With Desktop features enabled, the main tabs are **Claude Code**, **Claude Desktop**,
and **Codex**. Turning Desktop features off hides the Claude Desktop tab.
Each profile has an **Open** button and a **•••** menu for **Set up Dock shortcuts**, **Create shortcut…**,
**Reconnect sign-in…**, **Account details…**, **Rename…**, and **Archive…**. The same actions are available under
**More → Claude Desktop profiles**. App checks and **About profiles** help remain
under **Settings → Desktop → Claude Desktop**. The gear button on the Claude
Desktop tab opens those settings; Back returns to the profile list.

Opening a profile again brings its existing process forward when macOS permits
it. Other profile windows stay open. Neither opening a profile nor using its
shortcut changes the default Code login in your terminal or editor. Desktop
profiles have no automatic rotation. Code accounts are linked or added after
authenticated account detection succeeds.

Claude handles sign-in inside each profile. OpenSwap does not transfer credentials
between profiles. Default references the existing profile without copying it. The personal/Team/Enterprise selection
within one email still happens inside Claude. Creating a profile does not
create an Anthropic account or subscription.

## Browser sign-in returns to the right profile

Install OpenSwap in Applications and keep it running while signing in. Temporary
build folders cannot receive Claude links on macOS. OpenSwap registers its bundle
and checks that macOS offers it as a handler before requesting the change; an
ineligible copy asks you to move it to Applications instead of presenting a
misleading permission error.

When an additional profile opens,
OpenSwap prepares Claude link handling. If macOS asks, allow OpenSwap to open
Claude links. Google sign-in in the inspected Claude version then uses Apple's
browser authentication session, which returns to the exact Claude instance
that started it. Each instance keeps its own sign-in state; concurrent native
sessions are not routed by whichever window opened last.

Email magic links and fallback redirects do not reliably identify a profile.
For these, OpenSwap asks **Which profile did you start signing in to?** Choose
that open profile and **Continue**. There is no link to copy or paste. The choice
is still required when only one known profile is open, because another instance
could have started the sign-in. OpenSwap never broadcasts a login callback or
guesses from the last opened window, email or organization.

Denying macOS's link permission pauses further requests, including after restart.
**••• → Reconnect sign-in…** retries permission and opens the profile. Start a
fresh sign-in there; a callback already consumed by another window cannot be
recovered. Keychain detection permission is separate and still retries through
**Account details → Detect**.

OpenSwap rechecks the signed Claude app, profile folder and chosen running PID
before delivering an external login link. Closed, restarted, archived or
ambiguous profiles are refused. Only inspected Google, email and SSO login
routes are accepted; unsupported authentication routes are refused rather than
forwarded to Default. External links expire after two minutes and stay only in memory until handled
or discarded. They are never written to disk, logged, copied to the clipboard
or put in process arguments. Duplicate deliveries are suppressed.
A delivery acknowledgement does not establish successful authentication.

Ordinary Claude links are passed to the previous handler explicitly. Quitting
OpenSwap, disabling Desktop features or uninstalling restores that handler if
OpenSwap still owns it; a later choice by the user or another app is preserved.
Claude registers its own handler when it starts, so OpenSwap prepares routing
after startup, including launches from Dock shortcuts. CLI-only users and users
with just Default do not get a permission request. Installing the app alone does
not request ownership. DEV and release use the same registration policy.

This behavior relies on Claude's native browser-auth path, inspected in version
3.6556.2, and can change with Claude updates. It does not modify Claude's signed
bundle or bypass its nonce checks. The full real Google/email/SSO login flow,
concurrent sessions, denied permission and cold-start delivery remain manual
acceptance checks; fixture callbacks do not prove a successful provider login.

## One setup flow for Desktop and Code

With the menu bar app running and Desktop features enabled, authenticated
account detection starts Code setup for both Default and additional profiles.
An existing Code account is linked by account ID and organization ID. Multiple
Desktop profiles can share one Code account; the same email in different
organizations produces distinct Code accounts. Manual email/organization labels
cannot create or link an account.

When authorization is needed, the shared Code login helper opens the browser
with the detected email prefilled. Choose the same organization as in Desktop.
It signs in using a disposable configuration folder, verifies the resulting
account, and saves it through OpenSwap's existing credential store. The active
terminal/editor login and account selection are preserved. Desktop cookies are
never used as Code credentials. A response that omits personal-account org
metadata must agree with the completed CLI authorization's identity; a server
identity disagreement always stops the add.

Profiles show **In Claude Code** when linked, plus usage from the same Code
snapshot shown in the first tab. Stale usage is labeled **Last known**. Running
still means a Desktop process exists; it does not verify an embedded Code or
Cowork session's identity.

Canceling, denying, timing out, or interrupting authorization pauses setup
across restarts. **Continue setup** resumes it; no browser repeatedly opens in
the background. Install Claude Code first if the profile asks for it. Sign-out,
an organization change, archiving, and disabling Desktop features invalidate
an in-flight authorization. New detected identities start their own setup.
Removing a saved Code account does not silently recreate it. Archiving a
Desktop profile preserves its saved Code account and the other open profiles.

## Default profile and automatic account details

With Desktop features enabled, the profile list discovers the existing Claude
data folder and shows it as **Default** automatically. Discovery saves a
reference, without opening Claude or reading credentials. The existing data
stays in `~/Library/Application Support/Claude`; its permissions and sign-in are
unchanged. Default uses the normal Claude launch without a separate data folder
or Code environment overrides. It is saved only once, even if renamed. Archived
Default profiles stay archived until explicitly restored from Archived profiles.
If Claude has not created its default folder yet, no empty replacement is made;
Default appears when the list is next shown after Claude has been used.
**Add profile** always creates an additional isolated session.

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

The shortcut is a small, locally generated, stay-open macOS application. It calls
OpenSwap with the profile's stable ID, then retains the exact Claude process
object. OpenSwap checks the installed Claude publisher and opens that profile.
Claude's app is never copied or modified.

The profile app has its own Dock icon and running dot. Clicking it focuses that
profile. Quitting it requests a normal quit of that Claude process, allowing
Claude to handle unsaved work. If Claude cancels quitting, the icon stays running.
When the profile quits or crashes, its helper exits and the dot disappears. A
pinned icon remains ready to reopen the profile. Force-quitting the helper itself
does not force-quit Claude; use Open or Refresh status to reattach the indicator.

Opening a managed profile from OpenSwap or the CLI attaches its existing profile
app in the background. Status refresh also reattaches missing indicators and
upgrades older generated launchers in place. Passive attachment never reopens a
closed profile or steals focus. Default continues to use the original Claude
icon unless its separate launcher is explicitly opened. Unticking Add to Dock
leaves the profile app unpinned, so its icon disappears after the session quits.

The numbered icon identifies the **profile app in Finder and the Dock**. Its dot
means the process is running, not that sign-in or account identity is verified.
Claude's own icon and window titles remain unchanged; macOS may also display its
ordinary icon. The badge is not an unread count or proof of which account is
signed in. Each numbered icon selects its own session.

Badge numbers are assigned initially in profile order and can be changed when
creating a shortcut. Numbers need not be unique. Creating a shortcut again with
the same name and destination updates that profile's launcher and icon.
OpenSwap refuses to replace unrelated apps or a shortcut for another profile.
**Set up Dock shortcuts** rebuilds the launchers and refreshes the Dock. A private
backup of the previous pinned-app list is saved under the profile store before
changing pins. OpenSwap changes only that list; it does not stop Claude sessions.
Dock pinning uses macOS preferences rather than a supported profile-specific
Claude API, and may need updates for future macOS versions.

Renaming a profile preserves its stable ID, data and existing working shortcuts.
It does not rename shortcuts already created. Creating a shortcut after renaming
uses the new name; Dock setup updates a recognized old pin in place. Remove an
old launcher yourself if no longer needed. Deleting
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
its OpenSwap shortcuts from opening it. The ordinary Claude app still opens from Applications; an OpenSwap Default shortcut is disabled while Default is archived. Sign-ins, settings, local conversations, icons
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
controls. Desktop sign-in adds to the Claude Code account list, while account
switching and Automation settings continue to control the CLI.

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
  optional email/organization labels, authenticated account/organization IDs,
  a hash of the detected login, a last-detected timestamp, profile kind,
  automatic-update preference and durable Code setup status. Version 6 upgrades
  older profiles without treating their labels as authenticated identities.
  No cookies, passwords or tokens are stored in this registry.
- `<id>/desktop/`: Claude-owned desktop data, including local sessions.
- `<id>/code/`: a separate `CLAUDE_CONFIG_DIR` and Keychain namespace.
- `.code-login-*/`: private scratch directories used only during Code
  authorization, removed with their temporary Keychain entries when the login
  worker finishes. Saved Code credentials use OpenSwap's existing account store.

Default has only a registry entry here. Its original Claude-owned folder is
never moved, recreated, chmodded or deleted by OpenSwap.

These private folders can contain conversations and credentials created by
Claude. Do not share them as a diagnostic attachment. Profile versions 1–5, including the
unreleased linked-selection version 2, are readable. Their next save upgrades
metadata to version 6, preserving IDs, data and archives while discarding old
selection-link metadata. Previous labels need fresh authenticated detection
before automatic Code setup. No account credentials change during migration.

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

Desktop profiles, Code setup and callback recovery passed **1,737 focused tests**, with three real-Keychain
CI tests skipped locally. These include isolated login, exact account and
organization matching, deduplication, paused retries, account changes during
authorization, credential-write failure, worker leases, usage sharing, and the
existing account-switching and credential tests. **15 native dialog tests**
also passed. Native light/dark previews cover linked usage, browser sign-in,
cancel/retry actions and long labels. These previews use fixture accounts;
authenticated provider authorization remains a live acceptance check below.

Callback recovery was checked with two real, disposable signed-out Claude
instances: macOS acknowledged an event addressed to the selected profile's PID.
The callback used invalid fixture credentials; no authenticated login was
attempted. Automated cases cover route validation, profile selection, missing
or ambiguous processes, app updates, native delivery errors and secret-free
errors/results. The browser still uses its shared handler until the user copies
the link into the profile-specific recovery action.

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
  changes during the request reject the result. Default is discovered only with
  Desktop features enabled; a separate profile never falls back to Default or CLI
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

The automatic-Default follow-up passed **836 focused tests** (the same three
CI-only tests skipped). Coverage includes discovery only when enabled, late
creation of Claude's default folder, idempotence, existing names and settings,
archived defaults, unreadable folders, and the single-step additional-profile
flow. Native light/dark renders cover the main list, settings/help, errors and
the simplified create dialog. The refreshed packaged DEV app passed signature
verification and the native absent-item lookup, then restarted with the saved
settings and Homebrew login item preserved.

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

Latest automatic Dock setup follow-up (October 26, 2026): **1,038 focused tests
passed**. Coverage includes preserving the original Claude pin, additive pinning,
idempotent retries, renamed shortcuts, archived defaults, managed Dock settings,
concurrent Dock edits, private backups, and opening a profile after shortcut
setup fails. The native preferences writer round-tripped a disposable test domain
and preserved an unrelated setting. Live setup refreshed the existing shortcuts
without changing their positions or Claude session PIDs. The user's original
Claude Dock pin was restored from its backup; future adds leave it intact.

Native `NSWorkspace.iconForFile` rendering verified Default, 1 and 999 icons
without the gray frame. Badge overhang caused Tahoe to add the frame even when
the source image had none; keeping both marks inside the rounded outline fixed
it. Generated shortcuts pass strict signature verification. The updated DEV
app uses the shared SDK policy, with the Homebrew app, settings and login item
preserved. These checks cover creation, rendering and setup, not new provider
sign-ins or Dock persistence across a reboot.

Resident Dock follow-up: **1,054 focused tests passed**, followed by targeted
coverage for renamed running helpers and conflicting stores. Native disposable-app
tests cover each helper's regular
Dock presence, repeated clicks, exact-process focus, normal Quit, canceled Quit,
crash cleanup, background attachment, closed-session attachment, preservation of
another running app, and signatures after exit. Live verification attached the
numbered icon to the user's existing secondary Claude process without stealing
focus. Two successive opens focused that exact process, with one resident helper,
both Claude PIDs unchanged, and the original Dock pin intact. The Homebrew app,
settings, login item and shared DEV/release appearance policy were preserved.
Real Claude's Quit/unsaved-work dialogs and reboot restoration still need user
acceptance; destructive lifecycle tests used disposable apps. Run the probe with:

```sh
PYTHONPATH=src python tools/probe_claude_desktop_presence.py --launch
```

## Remaining live acceptance checks

Chrome-extension local pairing is disabled for relocated data in the inspected
Claude build. Account detection verifies the selected Desktop identity; usage
comes from the matching saved Code account. Launch success alone does not
verify identity or quota, move conversations, or establish embedded Code/Cowork
compatibility.

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
- Complete automatic Code authorization for Default and an additional profile.
  Verify the same account and organization appear in both tabs, usage agrees,
  and the previously selected CLI account stays active. Cancel and restart to
  check that setup resumes only after **Continue setup**. Automated connector
  tests use provider and CLI fixtures; live authorization is still unverified.
- Start a chat, a Code session and a Cowork task in each profile. Confirm billing
  identity, permissions and history. Opening another profile must preserve
  ongoing work and the default terminal/editor Code login.
- Check app updates, expired sign-ins, managed policy, missing-app recovery,
  archive/restore with existing shortcuts, and pinned Dock icons across restart.

Record authenticated results separately from synthetic tests and signed-out
smoke tests. Do not include tokens or conversation contents in evidence.
