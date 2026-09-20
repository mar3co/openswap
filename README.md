# OpenSwap

OpenSoft macOS menu bar app for rotating AI coding accounts. Track 5-hour and 7-day usage, auto-rotate before you hit a limit, and run two accounts at once. The extra, widget, and kickoff are macOS. We do not ship Windows or Linux.

OpenSwap is a standalone MIT descendant of [claude-swap](https://github.com/realiti4/claude-swap) (copyright Onur Cetinkol). It is not the PyPI `claude-swap` package.

**Users:** [Wiki](https://github.com/mar3co/openswap/wiki) (features, install, extra, widget, kickoff)
**Developers:** [docs/](docs/README.md) (architecture, hacking, tests)

## Install

Needs macOS, Python 3.12+, [uv](https://docs.astral.sh/uv/), and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) already logged in. Homebrew cask comes later.

```bash
git clone https://github.com/mar3co/openswap.git
cd openswap
uv tool install --editable '.[menubar]'
```

Update with `git pull`, then `uv tool install --editable '.[menubar]'`. `openswap upgrade` refuses PyPI.

## Quick start

```bash
openswap add              # save the OAuth account you are logged into
# log into another Claude account, then:
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
openswap menubar --install-service    # extra at login
openswap widget --install             # Desktop / Notification Center (this checkout + Xcode)
```

Click the extra for usage bars, then a card to switch. Auto-switch waits five minutes before it can move you again. Settings: auto-switch, burn weekly first, burn 5-hour first, 5-hour kickoff. Add the widget from Edit Widgets (search **OpenSwap**). Right-click it to choose all accounts, combined remaining, or one account.

[Menu bar](https://github.com/mar3co/openswap/wiki/Menu-Bar) · [Widget](https://github.com/mar3co/openswap/wiki/Desktop-Widget) · [Kickoff](https://github.com/mar3co/openswap/wiki/Five-Hour-Kickoff)

The `cswap` command still works as an alias during the rename.

## Commands

| Command | What it does |
| --- | --- |
| `openswap list` | Accounts with 5h / 7d usage |
| `openswap switch` / `openswap switch 2` | Rotate, or jump to a slot |
| `openswap add-token` | Save an API key or setup token |
| `openswap auto` | Background rotation near rate limits |
| `openswap run 2` | Claude Code as that account, this terminal only |
| `openswap config` | Shared settings (`autoswitch.*`) |
| `openswap menubar` | macOS extra |
| `openswap widget --install` | macOS widget |

`openswap help` lists everything. [CLI reference](https://github.com/mar3co/openswap/wiki/CLI-Reference).
