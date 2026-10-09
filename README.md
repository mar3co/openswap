# OpenSwap

OpenSoft macOS menu bar app for rotating AI coding accounts (Claude Code and Codex CLI). Track 5-hour and 7-day usage, and auto-rotate before you hit a limit. The extra, widget, and kickoff are macOS. We do not ship Windows or Linux.

OpenSwap is a standalone MIT descendant of [claude-swap](https://github.com/realiti4/claude-swap) (copyright Onur Cetinkol). It is not the PyPI `claude-swap` package.

**Users:** [Wiki](https://github.com/mar3co/openswap/wiki) (features, install, extra, widget, kickoff)
**Developers:** [docs/](docs/README.md) (architecture, hacking, tests)

## Install

One command. Needs a Mac.

```bash
curl -fsSL https://raw.githubusercontent.com/mar3co/openswap/main/install.sh | bash
```

It installs [uv](https://docs.astral.sh/uv/) if you do not have it (uv brings its own Python), clones OpenSwap to `~/.openswap`, and starts the extra in your menu bar. If [Claude Code](https://docs.anthropic.com/en/docs/claude-code) is logged in, setup saves that account; otherwise the extra still starts and you add accounts later. On a brand-new Mac, macOS first asks to install its Command Line Tools; run the command again once that finishes. Re-run it any time to update; it pulls instead of cloning, then runs setup again. `openswap upgrade` updates from the terminal. Homebrew cask comes later. OpenSwap is not on PyPI.

Already have a checkout? `OPENSWAP_DIR=/path/to/openswap bash install.sh` installs from it.

## Quick start

If Claude Code was logged in, the installer saved that account. Log into another Claude account, then:

```bash
openswap add
openswap add-token sk-ant-api03-...   # API key slot (switch only, no usage bars)
openswap list             # 5h / 7d usage for every account
openswap switch           # rotate
openswap switch 2         # jump to a slot, email, or alias
openswap auto             # switch for you before a window hits 90%
```

Do not run `/logout` before `openswap add`: current Claude Code may revoke the refresh token you are about to save.

Walkthrough: [Getting started](https://github.com/mar3co/openswap/wiki/Getting-Started). Every feature: [Features](https://github.com/mar3co/openswap/wiki/Features).

## macOS extras

```bash
openswap setup                        # save the current login, start the extra at login (the installer ran this)
openswap widget --install             # Desktop / Notification Center (this checkout + Xcode)
```

Click the extra for usage bars, then a card to switch. Auto-switch waits five minutes before it can move you again. Popover Settings separates **General** display controls from **Automation** for Claude, ChatGPT, and Codex, with one shared quota policy and an optional kickoff schedule for accounts that report a 5-hour window. Add the widget from Edit Widgets (search **OpenSwap**). The popover header and widget host use the OpenSoft mark. Right-click the widget to choose all accounts, combined remaining, or one account.

[Menu bar](https://github.com/mar3co/openswap/wiki/Menu-Bar) · [Widget](https://github.com/mar3co/openswap/wiki/Desktop-Widget) · [Kickoff](https://github.com/mar3co/openswap/wiki/Five-Hour-Kickoff)

The command-line executable is `openswap`.

## Commands

| Command | What it does |
| --- | --- |
| `openswap setup` | Save the current login and start the menu bar extra |
| `openswap list` | Accounts with 5h / 7d usage |
| `openswap switch` / `openswap switch 2` | Rotate, or jump to a slot |
| `openswap add` | Save the current OAuth login |
| `openswap add-token` | Save an API key or setup token |
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
| `openswap worker pair <url> <code>` | Remote tasks (opt-in): pair with a control service, then the guided setup: start the worker, pick an account, pick the folders where remote sessions work (`~/GitHub` is suggested) |
| `openswap worker setup` | Rerun the guided setup on a paired Mac (also Settings → General → **Set up Remote tasks…**) |
| `openswap worker enable` | Start the Remote tasks worker (LaunchAgent) so a paired Mac heartbeats and shows online |
| `openswap worker account [slot]` | List or pin the Codex or Claude account remote jobs run on (`claude:4`, `codex:2`) |
| `openswap worker account allow <slot>` | Allow an account for a per-job choice by the control service (`disallow`, `label`) |
| `openswap worker workspace add --work <folder>` | Let remote sessions work in a git repo (or each repo in a folder of repos), each task in its own worktree on an `openswap/` branch (`mode <id> direct` works in the folder itself; `openswap worker worktrees` lists and prunes) |
| `openswap worker workspace add --read <folder>` | Let remote tasks read a folder but never change it; results go to `~/OpenSwap Research/<id>` (`add <id> <folder>` approves a results folder; `list`, `label`, `remove`). The control service sees only IDs and labels |
| `openswap worker codex install` | Install the pinned official Codex CLI 0.157.1 for Remote tasks (SHA-256 verified) |
| `openswap worker codex login [slot]` | Sign an account in to its own isolated Codex home (your default login is untouched) |
| `openswap worker claude pin` / `prepare` | Pin the installed Claude Code binary and prepare an account's OpenSwap profile (your default login is untouched) |
| `openswap worker live-check` | Run short real jobs on this Mac, record phase-1 evidence, then offer to enable live execution (`--provider claude`) |
| `openswap worker live status` | Show whether remote jobs really run (`enable --evidence FILE`, `disable`) |
| `openswap menubar` | macOS extra |
| `openswap widget --install` | macOS widget |
| `openswap upgrade` | Pull the checkout and reinstall |
| `openswap statusline --install` | Opt-in: wrap Claude Code status line |
| `openswap statusline --codex` | Paint the live Codex account label (no config.toml wrap) |

Remote tasks are off by default, and jobs do not run until you pass the live check and enable live execution on this Mac; the pairing, account and folder setup is described in [the worker protocol doc](docs/worker-protocol.md#choosing-the-account-and-research-folders) and the live steps in [Running jobs live](docs/worker-protocol.md#running-jobs-live-codex).
Pairing does not start the worker: the portal shows the Mac Offline until you run `openswap worker enable` (or turn on Settings → General → **Enable local worker**). On a terminal, `pair` asks whether to start it now; `openswap worker status` says so while the Mac is paired but the worker is off.

`openswap help` lists the main commands. Full list: [CLI reference](https://github.com/mar3co/openswap/wiki/CLI-Reference).

The menu-bar popover has **Claude | ChatGPT** tabs. The ChatGPT tab lists the
shared ChatGPT/Codex accounts. Usage bars are **Codex usage**, not ChatGPT
message limits. Desktop switching is experimental and **off by default**: turn on
Settings → Automation → **Enable ChatGPT switching**, wait until the extra shows
**Switch and open ChatGPT** or **Restart ChatGPT**, then click an OAuth account
and confirm. While switching is off, a click offers **Enable in Settings**.
Incompatible builds show **This ChatGPT build isn’t compatible with switching.**
Relaunch does not prove which profile ChatGPT loaded. Read the
[test and recovery guide](docs/chatgpt-desktop-testing.md) first.

**Suggest ChatGPT account switches** (the ChatGPT tab Auto-switch) only runs
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
