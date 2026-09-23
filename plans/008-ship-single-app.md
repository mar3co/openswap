# Plan 008: Ship the single app: cask, install/upgrade rewiring, release CI

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If
> anything in "STOP conditions" occurs, stop and report. When done, update the
> status row for this plan in `plans/README.md`.
>
> **Gate (check first)**: plan 007's verdict in `packaging/macos/README.md`
> must record `source=Notarized Developer ID` for a GitHub Actions build, with
> checks 1–5 PASS. If it does not, STOP: only the operator can supply the
> Apple credentials (see "Operator prerequisites").
>
> **Drift check**: `git diff --stat 6ec5c71..HEAD -- src/openswap/launch_agent.py src/openswap/widget_install.py src/openswap/widget_snapshot.py src/openswap/update_check.py src/openswap/cli.py install.sh`
> On any change, compare the "Current state" notes with the live code first.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: MED (changes how every macOS user's login item and widget run)
- **Depends on**: 007 notarized verdict
- **Planned at**: commit `6ec5c71`, 2026-09-22
- **Already landed with this plan (2026-09-22, branch `chore/release-pipeline`)**:
  release plumbing that needs no Apple credentials. `build.sh` reads the
  version from `pyproject.toml`, signs every Mach-O by file type, checks the
  notarization status and prints Apple's log on failure, and always ends with
  `dist/OpenSwap-<version>.zip` + `.sha256` (ditto, after stapling). The
  workflow uploads that zip instead of the `.app` folder (upload-artifact
  drops execute bits and symlinks), checks the tag against the pyproject
  version, assesses the unpacked zip, lints the cask, and drafts a GitHub
  release with the zip, checksum, and rendered cask.
  `.github/workflows/homebrew-tap.yml` copies the cask into the tap when a
  person publishes the release. Template: `packaging/homebrew/openswap.rb.in`.

## Why this matters

Plan 007 proves one notarized `OpenSwap.app` can hold the Python extra, the
WidgetKit appex, and the CLI. Users still install from a git checkout with
uv, and the widget still needs Xcode and the user's own signing identity.
This plan makes `brew install --cask mar3co/openswap/openswap` the install
path and rewires the code that assumes a uv console script and a separately
built widget host.

## Operator prerequisites (not executor work)

1. Apple Developer Program membership; a **Developer ID Application**
   certificate exported as `.p12`; an App Store Connect API key (`.p8`).
2. Repository secrets from `packaging/macos/README.md` (six signing and
   notary secrets).
3. A public repo `mar3co/homebrew-openswap` with an empty `Casks/`
   directory, and repository secret `HOMEBREW_TAP_TOKEN` on `mar3co/openswap`
   (fine-grained token, Contents read/write on the tap repo only).
4. Decide the first public version (bump `pyproject.toml`) and tag it.

## Current state

- `launch_agent.resolve_program()` accepts `sys.argv[0]` only when its name
  is `openswap`, then `shutil.which("openswap")`, then
  `sys.executable -m openswap`. In the bundle, `sys.argv[0]` is
  `.../Contents/MacOS/OpenSwap` when launchd or Finder start it, and
  `/opt/homebrew/bin/openswap` (the cask's `binary` symlink) from a terminal.
  `sys.executable -m openswap` is wrong for a frozen app.
- `widget_snapshot.widget_app_path()` is always `~/Applications/OpenSwap.app`.
  `notify_widget_reload()` posts a distributed notification that only the
  separate Swift host (`macos/OpenSwapWidget/Host/main.swift`) observes.
  `wake_widget_host()` opens that host (called from `menubar.py:216`).
- `widget_install.install_widget()` runs `xcodebuild` with the user's team,
  copies to `~/Applications`, and loads LaunchAgent
  `com.opensoft.openswap.widget` for the host.
- `update_check.run_self_upgrade()` runs `git pull` and
  `uv tool install --force --editable`; `restart_widget_agent()` kickstarts
  the widget host. `cli.py:_setup_command` calls both `launch_agent.install()`
  and `restart_widget_agent()`.
- `cli._frozen_without_terminal()` already turns a bare frozen launch with no
  TTY into `menubar` (plan 007 Step 2). `menubar_display` already guards the
  notification bundle id when frozen (007 Step 1).
- The bundle ships `Contents/MacOS/openswap-widget-reload`, which calls
  `WidgetCenter.shared.reloadAllTimelines()` for the enclosing app.
- The cask (`packaging/homebrew/openswap.rb.in`) has no `uninstall
  launchctl:` on purpose: Homebrew runs uninstall directives on every
  `brew upgrade`, which would delete the login item. A `postflight_steps`
  step kickstarts `com.opensoft.openswap.menubar` instead; `zap` removes both
  LaunchAgents, `Application Support/OpenSwap`, the appex container, and logs.

## Scope

In: `src/openswap/launch_agent.py`, `widget_snapshot.py`, `widget_install.py`,
`update_check.py`, `cli.py` (setup/upgrade/widget dispatch only), a new
`src/openswap/bundle.py`, their tests, `install.sh`, `README.md`,
`docs/menubar.md`, `docs/widget.md`, `packaging/macos/README.md`,
`packaging/homebrew/openswap.rb.in`.

Out: the popover UI, account/credential code, Codex and ChatGPT desktop code,
`SMAppService` (still deferred), the official `homebrew/cask` repo.

## Steps

### Step 1: One place that knows about the bundle

Add `src/openswap/bundle.py`:

- `app_path() -> Path | None`: when `getattr(sys, "frozen", False)`, the
  `.app` that contains `Path(sys.executable).resolve()` (walk up to the first
  parent ending in `.app`); otherwise `None`. Resolve `sys.executable`, not
  `sys.argv[0]`, so the brew symlink maps to the real bundle.
- `executable() -> Path | None`: `app_path() / "Contents/MacOS/OpenSwap"`.
- `reload_helper() -> Path | None`: `.../Contents/MacOS/openswap-widget-reload`.
- `installed_by_homebrew() -> bool`: the app path's parent is the cask
  `appdir` (`/Applications` or `~/Applications`) and
  `brew --prefix`/`Caskroom/openswap` exists. Keep it pure-filesystem: check
  `/opt/homebrew/Caskroom/openswap` and `/usr/local/Caskroom/openswap`.

**Verify**: new `tests/test_bundle.py` covers frozen/unfrozen with
`monkeypatch.setattr(sys, "frozen", True, raising=False)` and a fake
`sys.executable` under `tmp_path/OpenSwap.app/Contents/MacOS/OpenSwap`.

### Step 2: LaunchAgent points at the bundle

`launch_agent.resolve_program()`: if `bundle.executable()` is a file, return
`[str(that path)]` first. Pin the in-bundle path, not the brew symlink: the
app path is stable across `brew upgrade`, and launchd starting the bundle
binary keeps the LaunchServices identity the notification guard expects.
Update the module docstring's first decision to say so.

**Verify**: `tests/test_launch_agent.py` new cases: frozen returns the bundle
path; unfrozen behaviour unchanged (existing tests still pass).

### Step 3: Widget from inside the bundle

- `widget_app_path()`: `bundle.app_path()` when frozen, else today's path.
- `notify_widget_reload()`: when `bundle.reload_helper()` is a file, run it
  with `subprocess.Popen` (DEVNULL, no wait) and return; otherwise today's
  distributed notification.
- `wake_widget_host()`: no-op when frozen (there is no separate host).
- `widget_install.install_widget()` when frozen: do not build. Run
  `pluginkit -a <app>/Contents/PlugIns/OpenSwapWidgetExtension.appex`, then
  the legacy-host migration from Step 5, then the helper once. Return a dict
  with the same keys the CLI prints (`app`, `label`, `plist` may be empty).
  `uninstall_widget()` when frozen: `pluginkit -r` only; never delete the
  bundle. `widget_status()` when frozen: report the appex registration
  (`pluginkit -m -i com.opensoft.openswap.widget.extension`).

**Verify**: tests in `tests/test_widget_snapshot.py` and
`tests/test_widget_install.py` with subprocess fakes: frozen reload runs the
helper; frozen install never calls xcodebuild; unfrozen paths unchanged.

### Step 4: Upgrade goes through Homebrew

`run_self_upgrade()`: when frozen, print
`OpenSwap was installed with Homebrew. Upgrade with: brew upgrade --cask openswap`
(or, when not `installed_by_homebrew()`, point at the release page) and
return 0. Do not shell out to brew. `restart_widget_agent()` returns None
when frozen and no legacy host exists.

**Verify**: `tests/test_update_check.py` frozen case asserts no subprocess
call and the message.

### Step 5: One-time migration from the uv install

Run from `openswap setup` and from frozen `menubar` start-up (idempotent,
never raises into the UI; log and continue):

1. If `~/Library/LaunchAgents/com.opensoft.openswap.menubar.plist` exists and
   its `ProgramArguments[0]` is not `bundle.executable()`, call
   `launch_agent.install()` (rewrites and re-bootstraps). Skip when the
   current process *is* that agent to avoid killing itself mid-start; in that
   case write the plist only and let the next login pick it up.
2. If `~/Applications/OpenSwap.app` exists and its `CFBundleIdentifier` is
   `com.opensoft.openswap.widget` (the old Swift host): boot out LaunchAgent
   `com.opensoft.openswap.widget`, remove its plist, `pluginkit -r` its appex,
   and move the app to the Trash (`NSFileManager trashItemAtURL`), not
   `rmtree`. Then `pluginkit -a` the bundled appex.
3. If `~/.local/bin/openswap` exists and is not the brew symlink, print once:
   `An older uv install is still on PATH: run uv tool uninstall openswap`.
   Do not uninstall it and do not touch `~/.openswap`.

**Verify**: `tests/test_migration_bundle.py` (new) with a temp home: each
branch, idempotence (second run is a no-op), and that a plist already
pointing at the bundle is left alone.

### Step 6: Installer and docs

- `install.sh`: when `brew` is on PATH and `OPENSWAP_DIR` is unset, say the
  cask is the supported path and run
  `brew install --cask mar3co/openswap/openswap && openswap setup` instead of
  cloning. Keep the clone path for `OPENSWAP_DIR` (developers) and Macs
  without Homebrew.
- `README.md` Install: cask first; drop "Homebrew cask comes later"; keep the
  curl line as the no-Homebrew fallback. Widget section: no Xcode needed for
  cask installs.
- `docs/widget.md`, `docs/menubar.md`: bundle layout, reload helper, the
  migration, and that desktop widgets placed with the old host may need
  re-adding (bundle id changes from `com.opensoft.openswap.widget` to
  `com.opensoft.openswap`; 007 check 4 records what actually happens).

### Step 7: First release (operator runs, executor prepares)

1. Bump `pyproject.toml` version; commit; tag `v<version>`; push the tag.
2. Workflow `macOS app` builds, notarizes, assesses the unpacked zip, and
   drafts the release with `OpenSwap-<version>.zip`, `.sha256`, `openswap.rb`.
3. Operator publishes the draft; workflow `Homebrew tap` pushes the cask.
4. On a clean user account: `brew install --cask mar3co/openswap/openswap`,
   `openswap setup`, open the popover, add the widget, `openswap list`.
5. On the operator's Mac (uv install present): same, and confirm Step 5's
   migration left one extra running and one widget provider
   (`pluginkit -m -i com.opensoft.openswap.widget.extension` shows one path).
6. Bump again, release, `brew upgrade --cask openswap`: the extra restarts on
   the new build (`launchctl print gui/$(id -u)/com.opensoft.openswap.menubar`
   shows a new pid and the bundle path). If it does not, the cask's
   `postflight_steps` kickstart is blocked by Homebrew's sandbox; see STOP.

## Test plan

- New: `tests/test_bundle.py`, `tests/test_migration_bundle.py`.
- Extended: `test_launch_agent.py`, `test_widget_snapshot.py`,
  `test_widget_install.py`, `test_update_check.py`, `test_cli.py` (setup
  calls the migration; upgrade prints the brew command when frozen).
- `uv run pytest` green; count increases only by the new tests.
- Manual: Step 7 items 4–6.

## Done criteria

- [ ] Gate satisfied (007 notarized verdict recorded)
- [ ] `uv run pytest` passes
- [ ] No code path runs `xcodebuild`, `git`, or `uv` when frozen
- [ ] Clean-account install, operator-Mac migration, and upgrade restart all
      pass (Step 7)
- [ ] `brew audit --cask --strict mar3co/openswap/openswap` passes
- [ ] README, docs, and wiki Install page describe the cask
- [ ] `plans/README.md` rows for 007 and 008 updated

## STOP conditions

- The 007 gate is not met.
- The cask's `postflight_steps` kickstart does not restart the extra after
  `brew upgrade` (Homebrew sandbox). Report it; the likely fix is the frozen
  extra noticing its bundle was replaced and exiting non-zero so launchd's
  `KeepAlive` restarts it, which the operator should approve first.
- Two widget providers remain registered after the migration.
- Moving the old host to the Trash fails for a reason other than "not found".
- Anything would switch the live Claude login or delete Keychain items.

## Maintenance notes

- The `binary` stanza symlinks the bundle executable; PyInstaller's
  bootloader resolves its own path, but Step 7 item 4 is where that is proved.
- A frozen `openswap` with no arguments and no TTY on stdin starts the extra
  (plan 007). `echo | openswap` therefore starts the extra; acceptable, but do
  not widen that rule.
- Official `homebrew/cask` submission needs notability (stars/forks) and a
  stable release history; revisit after a few tap releases.
- Deferred still: `SMAppService` login item, Sparkle-style in-app updates.
