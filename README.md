# OpenSwap

OpenSoft macOS menu bar app for rotating AI coding accounts (Claude Code and Codex CLI). Track 5-hour and 7-day usage, and auto-rotate before you hit a limit. The extra, widget, and kickoff are macOS. We do not ship Windows or Linux.

OpenSwap is a standalone MIT descendant of [claude-swap](https://github.com/realiti4/claude-swap) (copyright Onur Cetinkol). It is not the PyPI `claude-swap` package.

**Website:** [opensoft.me/swap](https://opensoft.me/swap)
**What's new:** [CHANGELOG.md](CHANGELOG.md)
**Users:** [Wiki](https://github.com/mar3co/openswap/wiki) (features, install, extra, widget, kickoff)
**Developers:** [docs/](docs/README.md) (architecture, hacking, tests)

**Desktop apps (experimental, opt-in):** turn on **Settings → General → Enable
desktop app features**, then open the main **Claude Desktop** tab. Your existing
Claude profile appears as **Default** automatically. **Add profile** creates
additional Personal, Work or other profiles. Email and organization update automatically
after sign-in or account changes. Each profile can stay open with its own sign-in.
OpenSwap automatically links or adds the same account and organization to
Claude Code. Complete browser authorization if Claude asks; no separate add
step or checkbox is needed. Linked profiles show the Code account's usage.
If the browser's **Open Claude** button returns to the wrong window, copy its
link and use the intended profile's **Finish sign-in…** action.
**Create shortcut…** makes a launcher for Finder and the Dock, with an optional
number and colored badge on Claude's icon. Claude Code switching and rotation
stay independent. The Desktop app picker also contains ChatGPT's controls.
See [setup, shortcuts, limitations and recovery](docs/claude-desktop-profiles.md).

## Install

Needs a Mac (macOS 14 or later). With [Homebrew](https://brew.sh):

```bash
brew install --cask mar3co/openswap/openswap
```

That installs `OpenSwap.app` (signed and notarized; the menu bar extra, the widget, and the `openswap` command in one app). Then finish setting up from the app or from a terminal; both do the same thing:

- **From the app:** open OpenSwap from Applications. A setup window walks you through it: sign in with Claude or save the login [Claude Code](https://docs.anthropic.com/en/docs/claude-code) already has (and Codex, if you use it), leave **Open at login** on, choose the extras, and click **Open OpenSwap**. Settings › General › **Setup assistant** opens it again later.
- **From a terminal:** run `openswap setup`. It saves the Claude account you are logged into (or warns and carries on when there is none) and starts the extra.

Either way OpenSwap is in your menu bar now and at every login, through the same login item, and the app registers its widget when it starts. Settings › General › **This Mac** keeps the login item and the Claude Code status line switchable later. Update with `brew upgrade --cask openswap` (`openswap upgrade` prints that command). OpenSwap is not on PyPI.

No Homebrew? Download the DMG for your Mac from the [latest release](https://github.com/mar3co/openswap/releases/latest) (`arm64` for Apple silicon, `x86_64` for Intel), open it, drag OpenSwap into Applications, and open it from there. It is the same signed and notarized app, and it finishes setting up the same way. Two things differ from the cask: there is no `openswap` command on your PATH (link it yourself if you want it, for example `ln -s /Applications/OpenSwap.app/Contents/MacOS/OpenSwap ~/.local/bin/openswap` when `~/.local/bin` is on your PATH), and updates come from the releases page (`openswap upgrade` says so). **More… › Uninstall OpenSwap…** moves this copy to the Trash itself.

To install from source instead, one command does it:

```bash
curl -fsSL https://raw.githubusercontent.com/mar3co/openswap/main/install.sh | bash
```

On a Mac with Homebrew the same script installs the cask above, or upgrades it when it is already installed. Without it, the script installs [uv](https://docs.astral.sh/uv/) if you do not have it (uv brings its own Python), clones OpenSwap to `~/.openswap`, and runs setup. On a brand-new Mac, macOS first asks to install its Command Line Tools; run the command again once that finishes. Re-run it any time to update; it pulls instead of cloning, then runs setup again. `openswap upgrade` does the same from the terminal.

Already have a checkout? `OPENSWAP_DIR=/path/to/openswap bash install.sh` installs from it (with or without Homebrew).

To uninstall, choose **More… › Uninstall OpenSwap…** in the menu bar, or run:

```bash
openswap uninstall          # or: brew uninstall --cask openswap
openswap uninstall --zap    # also delete saved accounts, settings and logs
```

Either way OpenSwap stops and removes its menu bar login item and the Remote tasks worker, puts back your own Claude Code status line if it wrapped it, and then removes the app. Your saved accounts stay unless you zap (`brew uninstall --zap --cask openswap` does the same, including the saved logins in the Keychain). `brew upgrade` and `brew reinstall` keep all of it. A source install's `openswap uninstall` removes the same things and ends by naming `uv tool uninstall openswap`. A zap also forgets a Remote tasks pairing, but the server keeps listing the Mac until you remove it there (OpenSwap names it). From the menu bar, **Also delete saved accounts, settings and logs** is the zap; OpenSwap quits and does the rest in the background, logging to `~/Library/Logs/com.opensoft.openswap.uninstall.log` (opened if a step fails), and a copy not installed by Homebrew moves itself to the Trash.

Dragged OpenSwap to the Trash instead? Then nothing ran: the login item, the Remote tasks worker and the Claude Code status line still point at the missing app. macOS tries the login item once per login and gives up, and the status line goes blank. Put the app back (or reinstall it) and choose **Uninstall OpenSwap…** to remove them and get your own status line back.

Moving from the source install to the cask: run `$(brew --prefix)/bin/openswap setup` (the uv `openswap` may come first on PATH; with the cask installed it refuses `setup`, `upgrade`, `widget --install` and `menubar --install-service` and names the cask's command). Setup points the login item, the Remote tasks worker, and the status line at the app and retires the old widget host. It leaves the uv tool in place and tells you to run `uv tool uninstall openswap`; `~/.openswap` is yours to delete. Widgets placed with the old host may need adding again.

## Quick start

If Claude Code was logged in, `openswap setup` (or **Save Current Login** in the setup window) saved that account. To add another, click **Add account** on the extra's Claude page (**Sign in with Claude** when it has no accounts yet) and sign in through your browser (Claude Code ends up on the new account). From a terminal, run `claude auth login` with the other account, then:

```bash
openswap add
openswap add-token sk-ant-api03-...   # API key slot (switch only, no usage bars)
openswap add-token sk-ant-api03-... --for 1   # API credit for account 1: the last resort when no subscription has room
openswap list             # 5h / 7d usage for every account
openswap switch           # rotate
openswap switch 2         # jump to a slot, email, or alias
openswap auto             # switch for you before a window hits 90%
```

Claude Code on a key linked with `--for` spends credit bought in that key's Claude Console organization. The monthly API credit included with Max and Team plans does not cover Claude Code, so a key whose organization holds only that credit fails with "Credit balance too low".

OpenSwap does not cap what a key spends: it cannot see a key's spend, and a running Claude Code session keeps spending until a request fails. To cap it, create the key in its own Console workspace and set a monthly spend limit on that workspace (Settings › Workspaces › Spend limits; the Default workspace cannot have one). When a key reaches the limit, OpenSwap marks it **API key at its spend limit** and stops using it until the limit is raised or the month resets.

Do not run `/logout` before `openswap add`: current Claude Code may revoke the refresh token you are about to save.

Walkthrough: [Getting started](https://github.com/mar3co/openswap/wiki/Getting-Started). Every feature: [Features](https://github.com/mar3co/openswap/wiki/Features).

## macOS extras

```bash
openswap setup                        # save the current login, start the extra at login (the app's setup window does the same)
openswap widget --install             # Desktop / Notification Center (the app registers it by itself)
openswap statusline --install         # account name in Claude Code's status line (Settings › This Mac too)
```

The widget ships inside the cask's app, which registers it each time it starts if macOS has not; `openswap widget --install` does the same by hand, and no Xcode is needed. A source install builds it with Xcode instead. Settings › General › This Mac switches the Claude Code status line on and off through the same code as `openswap statusline --install` / `--uninstall`; turning it on also points a hook left by another OpenSwap (such as an older uv install) at this one.

Click the extra for usage bars, then a card to switch. **Add account** on the Claude page (also **More › Add account › Sign in with Claude…**) refreshes the current login's saved copy (or asks first if OpenSwap hasn't saved it), runs the Claude browser sign-in, and saves whoever signs in (a new card, or a refresh of one already saved); Claude Code switches to that account. Auto-switch waits five minutes before it can move you again. Popover Settings separates **General** display controls from **Automation** for Claude, ChatGPT, and Codex, with one shared quota policy and an optional kickoff schedule for accounts that report a 5-hour window, and **Remote** for the local worker. Add the widget from Edit Widgets (search **OpenSwap**). The popover header and widget host use the OpenSoft mark. Right-click the widget to choose all accounts, combined remaining, or one account.

[Menu bar](https://github.com/mar3co/openswap/wiki/Menu-Bar) · [Widget](https://github.com/mar3co/openswap/wiki/Desktop-Widget) · [Kickoff](https://github.com/mar3co/openswap/wiki/Five-Hour-Kickoff)

The command-line executable is `openswap`.

## Commands

| Command | What it does |
| --- | --- |
| `openswap setup` | Save the current login and start the menu bar extra |
| `openswap claude-desktop add/list/open` | Experimental Claude Desktop profiles; sign in once per profile (`shortcut`, `rename`, `archive`, `restore`, `check`, `path` also available) |
| `openswap list` | Accounts with 5h / 7d usage |
| `openswap switch` / `openswap switch 2` | Rotate, or jump to a slot |
| `openswap add` | Save the current OAuth login |
| `openswap add-token` | Save an API key or setup token; `--for NUM\|EMAIL\|ALIAS` links a key to a subscription account as its API credit, used only on `at-limit` when no subscription has known room, or on `failover` when a subscription was measured and none has room (or none is in rotation). A revoked or out-of-credit key is skipped |
| `openswap remove` | Remove a stored account |
| `openswap auto` | Background rotation near rate limits |
| `openswap codex add` | Save the current Codex CLI login |
| `openswap codex list` | Codex accounts with 5h / 7d usage |
| `openswap codex switch` | Rotate or jump to a Codex slot |
| `openswap codex remove` | Remove a stored Codex account |
| `openswap codex export` | Export Codex auth.json envelopes |
| `openswap codex import` | Import Codex auth.json envelopes |
| `openswap codex swap` | Exchange two Codex slot numbers |
| `openswap codex move` | Assign a Codex account to a slot |
| `openswap codex desktop` | Experimental ChatGPT preflight, switch, and recovery (`switch` and `recover` need `--confirm-restart --confirm-idle`) |
| `openswap config` | Shared settings (`autoswitch.*`, including `autoswitch.codexEnabled`) |
| `openswap worker connect [url]` | Remote tasks (opt-in): pair this Mac by approving it in your browser (OpenTag by default), then the same guided setup as `pair`; on an expired key it reconnects and keeps the worker ID |
| `openswap worker pair <url> <code>` | Remote tasks (opt-in): pair with a control service, then the guided setup: start the worker, pick an account, pick the folders where remote sessions work (`~/GitHub` is suggested) |
| `openswap worker setup` | Rerun the guided setup on a paired Mac (also Settings → Remote → **Set up Remote tasks…**) |
| `openswap worker enable` | Start the Remote tasks worker (LaunchAgent) so a paired Mac heartbeats and shows online |
| `openswap worker account [slot]` | List or pin the Codex or Claude account remote jobs run on (`claude:4`, `codex:2`) |
| `openswap worker account allow <slot>` | Allow an account for a per-job choice by the control service (`disallow`, `label`) |
| `openswap worker workspace add --work <folder>` | Let remote sessions work in a git repo (or each repo in a folder of repos), each task in its own worktree on an `openswap/` branch (`mode <id> direct` works in the folder itself; `openswap worker worktrees` lists and prunes) |
| `openswap worker workspace add --read <folder>` | Let remote tasks read a folder but never change it; results go to `~/OpenSwap Research/<id>` (`add <id> <folder>` approves a results folder; `list`, `label`, `remove`). The control service sees only IDs and labels |
| `openswap worker codex install` | Install the pinned official Codex CLI 0.157.1 for Remote tasks (SHA-256 verified) |
| `openswap worker codex login [slot]` | Sign an account in to its own isolated Codex home (your default login is untouched) |
| `openswap worker codex settings [slot]` | Show or set the approval policy and sandbox mode that account's remote tasks follow (`--copy-settings` takes yours from `~/.codex/config.toml`) |
| `openswap worker claude pin` / `prepare` | Pin the installed Claude Code binary and prepare an account's OpenSwap profile (your default login is untouched); `prepare --copy-settings --mode <mode>` sets the permission settings its remote tasks follow |
| `openswap worker permissions [follow\|no-shell\|read-only]` | Remote sessions follow each account's own Claude or Codex permission settings; anything that would ask is denied. Optionally limit every remote task on this Mac (set only here, never from the service) |
| `openswap worker origins [refuse\|allow <kind>]` | List where remote tasks may come from (`slack`, `assistant`, `portal`, `test`, `unspecified`) or refuse one on this Mac, for example `refuse assistant` (set only here, never from the service) |
| `openswap worker live-check` | Run short real jobs on this Mac, record phase-1 evidence, then offer to enable live execution (`--provider claude`) |
| `openswap worker live status` | Show whether remote jobs really run (`enable --evidence FILE`, `disable`) |
| `openswap menubar` | macOS extra |
| `openswap widget --install` | macOS widget |
| `openswap upgrade` | Pull the checkout and reinstall (the cask: prints `brew upgrade --cask openswap`) |
| `openswap statusline --install` | Opt-in: wrap Claude Code status line |
| `openswap statusline --codex` | Paint the live Codex account label (no config.toml wrap) |

Remote tasks are off by default, and jobs do not run until you pass the live check and enable live execution on this Mac; the pairing, account and folder setup is described in [the worker protocol doc](docs/worker-protocol.md#choosing-the-account-and-research-folders) and the live steps in [Running jobs live](docs/worker-protocol.md#running-jobs-live-codex).
Pairing does not start the worker: the portal shows the Mac Offline until you run `openswap worker enable` (or turn on Settings → Remote → **Enable local worker**). On a terminal, `pair` asks whether to start it now; `openswap worker status` says so while the Mac is paired but the worker is off.

`openswap help` lists the main commands. Full list: [CLI reference](https://github.com/mar3co/openswap/wiki/CLI-Reference).

The main tabs are **Claude Code** and **Codex**. Enabling Desktop features adds
**Claude Desktop** between them for profiles. App checks, preferences and help
stay in **Settings → Desktop**. The Codex tab keeps its name when ChatGPT
switching is enabled and shows **Also restarts ChatGPT** above its accounts.
Usage bars are **Codex usage**, not ChatGPT
message limits. Desktop switching is experimental and **off by default**: turn on
Settings → General → **Enable desktop app features**, then Settings → Desktop →
ChatGPT Desktop → **Enable ChatGPT switching**. Wait until the extra shows
**Switch and open ChatGPT** or **Restart ChatGPT**, then click an OAuth account
and confirm. With ChatGPT switching off, account clicks switch Codex only.
Incompatible builds show **This ChatGPT build isn’t compatible with switching.**
Relaunch does not prove which profile ChatGPT loaded. Read the
[test and recovery guide](docs/chatgpt-desktop-testing.md) first.

**Suggest account switches** (**Suggest switches** on the Codex tab) only runs
when switching is on. It picks a replacement from Codex quota, then **Review
switch**; you still confirm before ChatGPT restarts. That mode cannot run with
live Codex CLI rotation. Unattended desktop rotation is not implemented.

To add another account, choose **ChatGPT → Add account** (or **Sign in with
ChatGPT** on an empty tab). Sign in through your browser, review the account,
then **Save account**. That session is separate: it does not log out or replace
your current login. New accounts are enabled when saved. They can be suggested
for a confirmed ChatGPT restart, or used by Codex CLI auto-switch — not both at
once. **Copy link** uses another browser profile; **Use a code** is device
sign-in where enabled. **Save current login** captures an existing login.

## JSON output for scripting

`list`, `status`, and `switch` take `--json` (one object on stdout; notices on
stderr). `openswap auto --json` is an event stream. Schema version 1; ignore
unknown fields. Envelope details: [CLI reference](https://github.com/mar3co/openswap/wiki/CLI-Reference#json).

---

An OpenSoft project. [opensoft.me](https://opensoft.me)
