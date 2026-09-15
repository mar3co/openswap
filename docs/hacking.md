# Hacking

## Clone and install

Users run `install.sh` (see the README): it installs uv, clones to `~/.openswap`, runs `uv tool install --force --editable '.[menubar]'`, then `openswap setup` (save the live login, install the menu bar LaunchAgent). Its branches are covered by `tests/test_install_script.py` with stub tools on `PATH`.

For hacking, clone wherever you like and install from there. The extra and widget expect an **editable** install so Python loads `src/openswap` from this tree (the widget builder also walks parents looking for `macos/OpenSwapWidget`).

```bash
git clone https://github.com/mar3co/openswap.git
cd openswap
uv tool install --editable '.[menubar]'
uv sync   # dev extras: pytest, etc.
```

`OPENSWAP_DIR=$PWD bash install.sh` installs the tool from an existing checkout without pulling it, then runs `openswap setup`. It does not run `uv sync`.

OpenSwap is not on PyPI. `openswap upgrade` runs `git pull` in this checkout, then `uv tool install --force --editable '.[menubar]'`, then refreshes installed LaunchAgents. If you moved the clone, reinstall from the new path.

## Run tests

```bash
uv run pytest
```

CI is `.github/workflows/ci.yml` (macOS, plus Ubuntu/Windows as a test farm). We ship macOS. See [Testing](testing.md).

## Restart the extra after a Python change

The LaunchAgent pins the `openswap` script; an editable install means that script already imports this tree. Restart the process:

```bash
launchctl kickstart -k "gui/$(id -u)/com.opensoft.openswap.menubar"
```

Logs: `~/Library/Logs/com.opensoft.openswap.menubar.{log,err}`.

The widget host is a compiled Swift app. After Swift or `project.yml` changes:

```bash
openswap widget --install
```

Derived data: `~/Library/Caches/openswap-widget`.

## Remotes and branches

| Remote | Repo |
| --- | --- |
| `origin` | `mar3co/openswap` |

Work on `main`. This repo is a standalone MIT descendant of [realiti4/claude-swap](https://github.com/realiti4/claude-swap); do not add that remote or merge their main.

## Workspace

The parent `GitHub/` directory is **not** a git repo. Run git from `openswap/`.
