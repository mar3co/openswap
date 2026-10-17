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

### Fixed

- Account nicknames keep the capital letters you type, like "WorkMac", instead of being lowercased. You can still switch with any casing.

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
