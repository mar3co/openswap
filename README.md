# OpenSwap

OpenSoft macOS menu bar app for rotating AI coding accounts (Claude Code and Codex CLI). Track 5-hour and 7-day usage, and auto-rotate before you hit a limit. The extra, widget, and kickoff are macOS. We do not ship Windows or Linux.

OpenSwap is a standalone MIT descendant of [claude-swap](https://github.com/realiti4/claude-swap) (copyright Onur Cetinkol). It is not the PyPI `claude-swap` package.

**Users:** [Wiki](https://github.com/mar3co/openswap/wiki) (features, install, extra, widget, kickoff)
**Developers:** [docs/](docs/README.md) (architecture, hacking, tests)

## Install

One command. Needs a Mac with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) logged in.

```bash
curl -fsSL https://raw.githubusercontent.com/mar3co/openswap/main/install.sh | bash
```

It installs [uv](https://docs.astral.sh/uv/) if you do not have it (uv brings its own Python), clones OpenSwap to `~/.openswap`, saves the Claude account you are logged into, and puts the extra in your menu bar. On a brand-new Mac, macOS first asks to install its Command Line Tools; run the command again once that finishes. Re-run it any time to update; it pulls instead of cloning, then saves the current login and starts the extra again. `openswap upgrade` updates from the terminal. Homebrew cask comes later. OpenSwap is not on PyPI.

Already have a checkout? `OPENSWAP_DIR=/path/to/openswap bash install.sh` installs from it.

## Quick start

The installer saved your first account. Log into another Claude account, then:

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

Click the extra for usage bars, then a card to switch. Auto-switch waits five minutes before it can move you again. Settings (in the popover): auto-switch, burn weekly first, burn 5-hour first, 5-hour kickoff. Add the widget from Edit Widgets (search **OpenSwap**). Right-click it to choose all accounts, combined remaining, or one account.

[Menu bar](https://github.com/mar3co/openswap/wiki/Menu-Bar) · [Widget](https://github.com/mar3co/openswap/wiki/Desktop-Widget) · [Kickoff](https://github.com/mar3co/openswap/wiki/Five-Hour-Kickoff)

The `cswap` command still works as an alias during the rename.

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
| `openswap codex desktop` | Experimental, confirmed ChatGPT quit/switch/relaunch and recovery |
| `openswap config` | Shared settings (`autoswitch.*`, including `autoswitch.codexEnabled`) |
| `openswap menubar` | macOS extra |
| `openswap widget --install` | macOS widget |
| `openswap upgrade` | Pull the checkout and reinstall |
| `openswap statusline --install` | Opt-in: wrap Claude Code status line |
| `openswap statusline --codex` | Paint the live Codex account label (no config.toml wrap) |

`openswap help` lists everything. [CLI reference](https://github.com/mar3co/openswap/wiki/CLI-Reference).

The experimental ChatGPT desktop action is also available under the menu bar's
**More… → Switch ChatGPT app (experimental)**. Read the
[test and recovery guide](docs/chatgpt-desktop-testing.md) first; automatic
desktop rotation and authenticated UI verification are not implemented.
