# Changelog

What changed in each OpenSwap release, newest first. Update with `brew upgrade --cask openswap`, or download the DMG from the [releases page](https://github.com/mar3co/openswap/releases/latest).

<!--
Write for the person using the app, not for us. One line per change, in plain
words, under New, Improved, Fixed or Removed. Leave out docs, tests, CI and
refactors. Add to Unreleased in the PR that makes the change. To cut a release,
rename Unreleased to the version and date, as in "## 0.3.0 (2026-11-02)". The
release workflow copies that section into the GitHub release, and
opensoft.me/swap/changelog shows this file.
-->

## Unreleased

### Improved

- Settings › Remote › Sessions explains Codex trust and phone replies in fewer words, and names `openswap claude` instead of the app's full path when that command opens this copy.

## 0.4.0 (2026-11-13)

### New

- Reset events: OpenSwap can tell you, or your own automations, when an account's 5-hour or weekly window starts, is about to end with quota left, or resets. Turn on Settings › Automation › Reset events › Notify on this Mac, or send signed webhooks with `openswap events add-webhook`. Both are off until you set them up.
- Settings › Remote lists the folders remote tasks may work in, with Add Folder… and Remove for each.
- Settings › Remote › Sessions can tell your control service when a Claude Code or Codex session waits for your approval, when a Claude Code session asks a question or sits idle, and when a Codex turn ends, with or without details. Off until you turn it on. Codex asks you once to trust OpenSwap's hooks.
- Sessions you start with `openswap claude` can be answered from your phone and watch: allow or deny a tool, answer a question, or send a quick reply to an idle session. Turn on Settings › Remote › Sessions › Allow replies from my phone and watch on the Mac first; it is off until you do.

### Improved

- The Claude Desktop update row now shows when Claude has downloaded an update that is ready to install, or that no update is downloaded yet.
- When Claude closes a profile on its own to install an update, OpenSwap now reopens it once the update is installed. Before, only the Default profile came back. If the update is still waiting on another open profile, OpenSwap tells you once, and Update Claude installs it now.
- Settings › Remote is tidier: Remote tasks, Account, Folders and Status sections with short labels, each folder shown by name with its path, the status in one plain line with the full fields under Details, and a refused control saying what failed and why.
- Adding a folder like ~/GitHub whose repos sit in organization folders now adds each of those folders, in Settings and in the setup window.
- Set Up… and Connect to OpenTag… in Settings › Remote now finish in OpenSwap's setup window, one step per page with real controls: start the worker, pick the account, check the folders tasks may use (or add one), then a checklist of what's ready and which Settings control fixes the rest.

## 0.3.1 (2026-11-05)

### New

- Update Claude… in the Claude Desktop tab updates the Claude desktop app when several profiles are open. OpenSwap asks each profile to quit normally, waits for Claude to install the update it has already downloaded, then reopens your profiles and their Dock icons. If the update is interrupted, profiles stay closed until you choose Resume update… to finish it, or Cancel update… to stop and reopen the profiles OpenSwap closed.
- A Claude Desktop profile that needs Keychain access now says Keychain access needed and offers Retry detection right on the profile, noting that you stay signed in to Claude.

### Improved

- Add from token reflows around what you enter, keeps Save in view on short screens, and adds a Paste button and instructions for finding a key or setup token. Save names what it will save, keeps what you typed if saving fails, and confirms when the token is saved.
- Adding another Claude Code account no longer warns that your current login is unsaved when OpenSwap already has a copy of it. When it does warn, it says your saved accounts stay in OpenSwap and whether the email is saved under another organization.

### Fixed

- A Claude Desktop profile whose saved sign-in key is missing no longer pauses account detection. It shows Account details unavailable and tries again on its own.

## 0.3.0 (2026-10-28)

### New

- Claude Desktop profiles: open several Claude desktop app sessions side by side, each with its own sign-in. Turn on Enable desktop app features in Settings → General, then add profiles in the Claude Desktop tab. Each profile you add gets a numbered Dock icon, and its account is added to Claude Code for you: an account Claude Code already has is linked without signing in again, and a new one needs one browser sign-in.
- Sign-in links from email, SSO and connected apps reach the right Claude profile: Google sign-in returns on its own, and for other links OpenSwap asks which profile you started from. If Claude takes those links back, OpenSwap tells you once and waits for you to choose Reconnect sign-in…, rather than showing the macOS permission prompt again.
- OpenSwap tells you when a newer release is out: an Update available item in the menu bar's More menu, one notification per release, and a one-line notice in the terminal, each saying how to upgrade.

### Fixed

- With more accounts than fit on the screen, the menu bar panel's account list scrolls instead of running off the bottom of the screen.

## 0.2.2 (2026-10-18)

### Fixed

- Uninstalling with "Also delete saved accounts, settings and logs" (or `--zap`) also deletes OpenSwap's preferences and, once no Remote task is running, the worker's copy of Claude Code and its job files, which it used to leave behind.
- Account nicknames keep the capital letters you type, like "WorkMac", instead of being lowercased. You can still switch with any casing.

### Improved

- The Uninstall OpenSwap… dialog says which account Claude Code and Codex stay signed in to, and that Remote tasks' results are kept.
- Deleting saved accounts while uninstalling tells you that OpenTag still lists this Mac for Remote tasks, so you can remove it there.
- The menu bar panel's header icons are larger and line up with the titles beside them.

## 0.2.1 (2026-10-17)

### New

- OpenSwap notices when a linked API key is revoked, out of credit, or at its Console spend limit, and stops using it until it works again.
- Connecting a Mac to OpenTag ends on a page that says whether it worked and what to do next.

### Improved

- Setup dialogs match the setup window, with a Close Setup button and an account picker.

## 0.2.0 (2026-10-17)

The first release you can install with Homebrew or a DMG. Before this, OpenSwap ran from a source checkout.

### New

- One signed and notarized OpenSwap app holds the menu bar app, the widget and the `openswap` command. Install it with `brew install --cask mar3co/openswap/openswap`, or download the DMG for Apple silicon or Intel.
- A setup window on first launch: sign in with Claude or save the login Claude Code already has, turn on Open at login, and pick the extras. It offers to move the app to Applications.
- Codex accounts sit next to your Claude accounts. Save, switch and auto-rotate them the same way.
- Add a Claude account from the menu bar with Sign in with Claude, no terminal needed.
- Link a Claude Console API key to an account with `openswap add-token --for`. OpenSwap switches to it only when none of your subscriptions has room left.
- Remote tasks (opt-in): connect your Mac to OpenTag and run Claude Code or Codex tasks on it from anywhere. Each task works in its own git worktree, in folders you approve.
- An opt-in Claude Code status line that shows which account you are on.
- Experimental: switch the ChatGPT desktop app between accounts with `openswap codex desktop`, on app versions that allow it.
- Uninstall from the menu bar or with `openswap uninstall`. Your saved accounts stay unless you ask to delete them.
- The menu bar icon plays a short swap animation when the account changes.

### Improved

- Settings has separate General, Automation and Remote tabs.
- The menu bar title is never blank, and shows a switch made in the terminal right away.
- Rename accounts, and confirm a switch before it happens.
- Clicking a signed-out account says why it opens the Claude login, and fixes a login that does not match the account you clicked.
- Expired Claude logins repair themselves where they can.
- A failing 5-hour kickoff alerts you once, not over and over.
- `openswap upgrade` prints the update command that fits how you installed.

### Fixed

- 5-hour kickoff started the wrong accounts and missed its notifications.
- The menu bar now says why auto-switch is waiting, and updates that note while the menu is open.
- `openswap list` lines up the folders under Running instances.

### Removed

- Session mode and per-folder accounts. A switch applies everywhere.
- The `cswap` command. Use `openswap`.

## 0.1.0 (2026-09-07)

The first version under the OpenSwap name, installed from source. OpenSwap began as a fork of claude-swap and is now its own MIT-licensed OpenSoft app.

### New

- Save several Claude Code logins and switch between them with `openswap switch`, or jump to one with `openswap switch 2` (a slot number, an email or an alias).
- `openswap list` shows 5-hour and 7-day usage for every account.
- Auto-switch moves you to another account before a window passes 90%, then waits five minutes before it can move you again.
- A menu bar app with usage bars. Click an account to switch to it.
- A widget for the desktop and Notification Center.
- 5-hour kickoff starts an idle account's 5-hour window on a schedule, so it resets sooner.
- Logins stay in the macOS Keychain. Accounts saved by claude-swap carry over.
