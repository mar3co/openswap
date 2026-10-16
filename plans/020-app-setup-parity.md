# Plan 020: Set up OpenSwap fully from the app or from the terminal

> **Executor instructions**: Follow this plan phase by phase. Each phase ends
> green (`uv run pytest`) and merges on its own. Run every verification and
> confirm the expected result before moving on. If anything in "STOP
> conditions" occurs, stop and report. When done, update the status row for
> this plan in `plans/README.md`.
>
> **Drift check**: `git diff --stat 43d90ea..HEAD -- src/openswap/launch_agent.py src/openswap/bundle.py src/openswap/menubar.py src/openswap/menubar_panel.py src/openswap/menubar_display.py src/openswap/widget_install.py src/openswap/statusline.py src/openswap/uninstall.py src/openswap/cli.py packaging/homebrew/openswap.rb.in`
> On any change, compare the "Current state" notes with the live code first.

## Status

- **Priority**: P1 (the cask makes the app the main install path)
- **Effort**: L, in three phases that each ship alone
- **Risk**: MED (changes how the login item is written and who owns it)
- **Depends on**: 008 (single app, cask), PR #107 (`openswap uninstall`)
- **Planned at**: commit `43d90ea`, 2026-10-15

## Why this matters

There are two ways to start OpenSwap: run `openswap setup` in a terminal, or
open `OpenSwap.app` from Applications. Only the terminal path finishes the
job. An app opened from Finder runs the menu bar, saves and signs in
accounts, sets up Remote tasks, and moves a uv install over, but it never
adds itself to login. After the next restart it is gone. The cask's upgrade
step (`launchctl kickstart` of the login item) also does nothing for it, so a
`brew upgrade` leaves the old build running. The widget and the Claude Code
status line can only be set up from the terminal, and there is no way to
uninstall from the app.

The goal is that both paths are complete and do the same thing, because they
call the same code. The terminal stays first-class: `openswap setup`,
`openswap uninstall` and every flag keep working as they do today.

## Where the two paths stand today

| Piece | Terminal | App opened from Finder |
|---|---|---|
| Save the logged-in Claude account | `openswap setup`, `openswap add` | Empty state: **Save current login** |
| Sign in another account | `claude auth login`, then `openswap add` | **Sign in with Claude** |
| Start at login (LaunchAgent) | `setup`, `menubar --install-service` | Missing |
| Move a uv install over | `setup` | On every start (`bundle.migrate_from_uv_install`) |
| Widget | `widget --install` | Missing (macOS may register the appex by itself) |
| Claude Code status line | `statusline --install` / `--uninstall` | Missing |
| Remote tasks | `worker setup`, `worker enable` | Settings › Remote |
| Automation policy | `auto`, `config set` | Settings › Automation |
| Uninstall | `openswap uninstall [--zap]`, `brew uninstall` | Missing |

## Current state

- `launch_agent.install()` writes `~/Library/LaunchAgents/com.opensoft.openswap.menubar.plist`
  (`RunAtLoad`, `KeepAlive: {SuccessfulExit: false}`) and boots the job out
  and back in. `launch_agent.uninstall()` boots it out and deletes the plist.
  Called from inside the running extra, bootout sends this process SIGTERM.
  `resolve_program()` pins the in-bundle `Contents/MacOS/OpenSwap` when frozen.
- `bundle.py` already tells "this process is the launchd job" apart with
  `os.environ.get("XPC_SERVICE_NAME") == launch_agent.LABEL`
  (`_repoint_menubar_agent`, `_retire_legacy_menubar_labels`).
  `runs_from_temporary_location()` detects a translocated or mounted copy.
- `cli._frozen_without_terminal()` turns a bare Finder launch into `menubar`.
  `cli.py` (menubar dispatch) calls `bundle.migrate_from_uv_install()` and
  then `menubar.run`.
- Nothing stops a second extra. Opening the app from Finder while launchd's
  copy runs may start a second status item; this has not been checked.
  rumps' app delegate does not handle a reopen event.
- `MenuBarSettings` (`menubar_display.py`) persists display settings in
  `menubar_settings.json`. `settings_page_rows()` builds the Settings page
  model with no AppKit; `SETTINGS_SECTIONS` is General, Automation, Remote.
- `provider_empty_state()` already gives a first-run account step:
  **Sign in with Claude** and **Save current login**.
- `widget_install._install_bundled_widget()` registers the bundled appex
  (`pluginkit -a`) and retires a uv widget host; `widget_status()` reports
  registrations when frozen.
- `statusline.install()` / `uninstall()` back `openswap statusline
  --install/--uninstall`; `paint_command()` pins the app's executable.
- `uninstall.remove_integrations()` and the helpers behind
  `openswap uninstall [--zap]` live in `uninstall.py`. `_uninstall_command`
  in `cli.py` needs a TTY unless `--yes`, and ends in
  `os.execv(brew, ["brew", "uninstall", "--cask", ...])` for a cask install.
- The cask's caveat says to run `openswap setup`.

## Decisions

1. **One login mechanism.** The LaunchAgent stays the only way OpenSwap
   starts at login, for the app and for a uv install. The plist on disk is the
   setting: "Open at login" is on when the plist exists. No separate boolean
   is stored, so `setup`, `uninstall`, `brew uninstall` and the toggle can
   never disagree.
2. **`SMAppService` stays deferred.** It would give a nicer Login Items
   entry, but it only works for the app (not a uv install), puts the plist
   inside the bundle under a new registration, and means moving every
   existing login item plus rewriting the cask's kickstart and the uninstall
   hook. Revisit once the LaunchAgent path is complete everywhere.
3. **One copy of the extra.** The launchd job is the copy that should run,
   because it restarts after a crash and is what the cask's upgrade step
   restarts. A copy started from Finder hands over to it.
4. **The app asks first.** First launch shows a welcome page with **Open at
   login** switched on. Nothing is written until the user clicks Continue.
5. **The CLI and the app share each action.** The app calls the same
   functions as `setup`, `statusline --install`, `widget --install` and
   `uninstall`. Terminal output and flags do not change except where noted.

## Scope

In: `launch_agent.py`, `bundle.py`, `menubar.py`, `menubar_panel.py`,
`menubar_display.py`, `widget_install.py`, `uninstall.py`, `cli.py` (setup
output, uninstall refactor), `packaging/homebrew/openswap.rb.in` (caveat
only), their tests, `README.md`, `docs/menubar.md`, `docs/widget.md`.

Out: `SMAppService`, in-app updates, putting `openswap` on PATH for a copy
not installed by Homebrew, Windows and Linux, the Remote tasks and Automation
pages, account and credential code.

## Phase 1: start at login from the app

### Step 1: one extra at a time

First check how macOS behaves today, on the notarized build: with the
launchd job running, open the app from Finder, from Spotlight and with
`open -a OpenSwap`. Record whether a second status item appears.

- Before anything else, a copy launchd did not start (Finder, Spotlight,
  a terminal `openswap menubar`) whose login item runs this program and has
  no running process hands over to it: `launch_agent.start_login_item()`
  (Step 2) starts the job and this copy exits 0 once the job's pid exists.
  First it leaves a show request (below), so opening the app shows the
  popover here too.
  When the job is already running, skip the hand-over and take the lock
  path below, so the running copy is told to show its popover. The
  hand-over covers a login item that is on but not running, for example
  after Quit
  (`KeepAlive: {SuccessfulExit: false}` does not restart a clean exit).
  Without it the Finder copy would hold the lock, and the cask's upgrade
  kickstart would start a launchd copy that waits behind the old build. If
  the hand-over fails, log it and carry on as below. A terminal
  `openswap menubar` prints that it started the login item, and where the
  logs are.
- Add `menubar_lock(backup_dir)` (in `menubar.py` or a small new module):
  an exclusive `fcntl.flock` on `<backup_root>/menubar.lock`, taken in
  `menubar.run` before the status item exists.
- The launchd job (`XPC_SERVICE_NAME == LABEL`) waits up to 10 s for the
  lock (it may be starting during a hand-over, Step 2). Any other copy does
  not wait: it leaves a show request and exits 0. A terminal
  `openswap menubar` also prints "OpenSwap is already running in the menu bar."
- A show request: write `<backup_root>/menubar.show-request`, then post the
  distributed notification `com.opensoft.openswap.menubar.show`. The lock
  holder consumes the file in two places: on that notification, and once
  its popover and observer are attached at start-up. If the file was under
  30 s old it opens the popover. The holder may have the lock but no
  observer yet, or may not be started yet (a hand-over), and the file
  carries the request across either gap, which a notification alone cannot.
  Consuming deletes the file, so one request shows the popover once.
- The running extra also opens its popover on a reopen event
  (`applicationShouldHandleReopen:hasVisibleWindows:` on rumps' delegate),
  so opening the app again always shows it.
- If the job cannot get the lock in 10 s it exits 1, and launchd's
  `KeepAlive` retries it. Exception: when its plist is gone (Open at login
  was turned off), it unloads its own job (`unload_own_job()`, Step 2) and
  exits 0. Otherwise a `kickstart -k` from the cask would revive the job
  behind another copy and `KeepAlive` would restart it in a loop.

**Verify**: unit tests for the lock (two file descriptions in one process
conflict under `flock`), the wait-only-when-launchd rule, the start-up
hand-over (login item on and idle, which leaves a show request before
starting the job; on and running, which leaves a show request and never
calls `start_login_item`; off; running another program), the show request
(consumed once whether the notification or start-up gets there first,
ignored when stale, honoured when it arrives before the holder's observer
is attached), and the notification name. Manual: open the app three ways while
the job runs; one status item, and the popover opens each time. Quit, then
open the app from Finder: `launchctl print` shows the job running it, and
its popover opens.

### Step 2: one function that turns login on and off

Add to `launch_agent.py`:

```python
def login_item_enabled(home=None) -> bool: ...
def login_item_runs_this(home=None) -> bool: ...
def start_login_item(*, home=None, uid=None) -> int | None: ...
def unload_own_job(*, home=None, uid=None) -> bool: ...
def set_login_item(enabled: bool, *, home=None, uid=None) -> str: ...
```

`login_item_runs_this` is true when the plist's program is
`resolve_program()`; only then may a copy hand over (a plist for another
copy of the app must not start it). `start_login_item` kickstarts the job
when it is loaded and bootstraps the plist when it is not, then returns
launchd's pid (`_wait_for_pid`). A job may be mid-unload: the start-up
migration (`bundle._repoint_menubar_agent`) boots out a uv extra just
before this runs. So when the kickstart fails or no pid appears, it waits
for the job to unload (`_wait_until_unloaded`) and bootstraps the plist
once. It returns None only when that fails too. `_repoint_menubar_agent`
also waits for its own bootout to finish before it rewrites the plist.

Every start that fails records why in `<backup_root>/login_item_status.json`
(time and launchctl's detail). Any successful start clears it, and the
launchd job clears it when it starts. Step 3 reads it.

`set_login_item` returns what happened, so the UI can say it. What it does
depends on which process calls it:

| Caller | Turn on | Turn off |
|---|---|---|
| The launchd job itself | Write the plist when it is missing or runs another program; no `launchctl` | Delete the plist; this copy keeps running. On Quit it calls `unload_own_job()` before it exits, so the job is not left loaded for the cask's `kickstart -k` to revive. It does not come back at the next login |
| A copy opened from Finder, Spotlight or `openswap menubar` | Write the plist, `start_login_item()` (`_wait_for_pid` moves here from `cli.py`), then return `"handoff"`: the caller quits and launchd's copy takes the lock | Boot out the job if it is loaded (it is not this process), delete the plist |

The CLI keeps `install()` and `uninstall()` as they are; they already run
outside the extra. Refuse with a clear message from a translocated or mounted
copy (`bundle.runs_from_temporary_location()`): "Move OpenSwap to Applications
first." Never boot out the calling process from inside the call.

`unload_own_job()` is for the launchd job on its way out. It starts
`launchctl bootout gui/<uid>/<label>` as a detached process
(`start_new_session=True`, so launchd stopping the job's process group does
not take it along) and returns; the caller exits right after. It runs only
when the plist is gone, so turning login off and then quitting leaves
nothing loaded, while a plain Quit with login on keeps today's behaviour
(loaded, not running, started again at the next login).

The rule depends on the plist, not on which process turned login off, so
it covers every launchd-started copy. Between turning login off and
quitting, the job is still loaded, and a `brew upgrade` in that window has
its `kickstart -k` restart the running extra on the new build. That is the
right result: the extra was running and stays running, now upgraded. The
restarted copy still has no plist, so its Quit unloads the job, and it is
gone at logout either way. The cask's kickstart cannot be gated on the
plist, because `postflight_steps` run with `deny_read_home`.

Check on macOS 14 and 15 what happens when the user switched OpenSwap off in
System Settings › General › Login Items ("Allow in the Background"), and
whether `launchctl print` shows it. Whatever the signal, it ends up as a
failed start in `login_item_status.json`, and Step 3 shows it. Don't wait
for a toggle change to report it.

**Verify**: `tests/test_launch_agent.py` covers each cell of the table with a
fake `_launchctl` and `XPC_SERVICE_NAME` set or unset, including "never
boots out itself", the temporary-location refusal, and `unload_own_job()`
(detached, new session, only when the plist is gone). Quit and the
lock-timeout path call it in exactly those cases.

### Step 3: Open at login in Settings

- `settings_page_rows()` gets a new group at the end of General, **This
  Mac**, with an **Open at login** toggle (`id: open_at_login`). Its value
  comes from `login_item_enabled()`, read when the page is built, not from
  `MenuBarSettings`.
- While the toggle is on and `login_item_status.json` records a failed
  start, and this copy is not the launchd job, a warning row follows the
  toggle. It reads "macOS isn't starting OpenSwap at login" with an
  **Open Login Items** button
  (`x-apple.systempreferences:com.apple.LoginItems-Settings.extension`). The
  toggle never claims it works when macOS refused the last start.
- From a temporary location the toggle is disabled and a hint row says to
  move the app to Applications.
- The controller calls `set_login_item` off the main thread and shows the
  result: on a hand-over the copy quits after launchd's copy is up; after
  "off" from the job, a hint says it will not open at the next login.
- Works in a uv install too (it pins the console script, as `setup` does).

**Verify**: `settings_page_rows` tests for the new rows, the temporary-location
case, the failed-start warning (shown only when on, failed, and not the
launchd job), and the section filter. `start_login_item` tests: a kickstart
that fails while the job unloads falls back to one bootstrap; failures write
the status file and a start clears it. Manual on the notarized build: install with
`brew install --cask`, skip `openswap setup`, open the app, switch the toggle
on. `launchctl print gui/$(id -u)/com.opensoft.openswap.menubar` shows the
bundle path, there is one status item, it is back after logging out and in,
and `brew reinstall --cask openswap` restarts it with a new pid.

## Phase 2: first run, widget and status line

### Step 4: welcome page

- New `MenuBarSettings.welcome_done: bool = False`.
- On start, when the app is frozen, runs from a stable location, and
  `welcome_done` is false: if the login item already runs this app
  (`openswap setup` ran), set `welcome_done` and show nothing. Otherwise open
  the popover once, by itself, on the welcome page. An LSUIElement app shows
  no window, so without this a first launch looks like nothing happened.
- The welcome page (a new popover page, model in `menubar_display.py`): one
  line on what OpenSwap does, the account step from `provider_empty_state`
  when there are no Claude accounts, an **Open at login** switch preset on,
  and **Continue**. Continue saves `welcome_done`, then applies the switch
  through `set_login_item` (the hand-over, if any, is the last thing).
- Closing the popover without Continue changes nothing; the welcome page is
  shown again the next time the popover opens.
- A uv install never shows it; `openswap setup` is that path.

**Verify**: model tests for the page in each state (no accounts, accounts,
temporary location, login item already on). Manual: a clean user account,
cask install, open the app: welcome opens, Continue with the switch on
leaves one launchd-started extra; with it off, no plist.

### Step 5: the widget registers itself

- On start, frozen and from a stable location, run the bundled registration
  in the background unless this bundle's own appex (`bundle.appex()`,
  resolved) is among the paths `widget_status()["registrations"]` lists.
  `registered` alone is not enough: it is true when only an old or copied
  app with the same extension id is registered. When pluginkit cannot
  answer, register anyway (`pluginkit -a` is idempotent). Log a failure; do
  not alert.
- Settings › General › This Mac gets a hint row: "Add the widget: Edit
  Widgets, search OpenSwap" (frozen only).
- `openswap setup` drops "Add the desktop widget: openswap widget --install"
  for the frozen app and prints the Edit Widgets hint instead. A source
  install keeps the Xcode line. `openswap widget --install` keeps working.

**Verify**: a test that start-up registers when nothing is registered,
when only another copy's appex is, and when pluginkit fails; skips when this
bundle's appex is listed; and never runs from a temporary location; `test_cli.py` setup output
for frozen and source installs.

### Step 6: status line toggle, caveat and docs

- **Claude Code status line** toggle in This Mac: `statusline.install()` /
  `uninstall()` with `paint_command()`, the same calls `openswap statusline
  --install/--uninstall` makes (move the shared part into one function both
  call). The value is `statusline.installed_command(...) is not None`.
  From a temporary location, turning it on is disabled with the same hint
  as Open at login: `paint_command()` would pin a path that disappears and
  break every status-line render. Turning it off stays possible.
- Cask caveat: "Open OpenSwap from Applications to finish setting up, or
  run: openswap setup". Keep the HOMEBREW_PATH logic that names the cask's
  command when another `openswap` comes first.
- README Install and Quick start, `docs/menubar.md` (Process, a new "First
  run" paragraph), `docs/widget.md`: describe both paths in the same terms.

**Verify**: settings model tests for the toggle (including on-disabled,
off-allowed from a temporary location), a CLI test that the shared
function is what `statusline --install` calls,
`tests/test_packaging_homebrew.py` for the caveat. `brew style` and
`brew audit --cask --strict` on the rendered cask in a local tap.

## Phase 3: uninstall from the app

Optional. The owner approves the dialog copy before it ships.

### Step 7: Uninstall OpenSwap…

- Move the body of `_uninstall_command` (after argument parsing and the TTY
  prompt) into `uninstall.run(zap: bool, say) -> int`, which the CLI calls
  unchanged.
- The More… menu gets **Uninstall OpenSwap…**: a dialog that lists what is
  removed, with **Also delete saved accounts, settings and logs** (zap)
  unchecked. With zap, `refuse_live_sessions()` runs first and its message
  is shown as an alert.
- On confirm the app starts its own executable as
  `uninstall --yes [--zap]` in a new session (`start_new_session=True`,
  output to `~/Library/Logs/com.opensoft.openswap.uninstall.log`), then
  quits. Removing the login item boots the extra out, so the work must not
  run inside it. The child ends in `brew uninstall --cask` for a cask
  install, as the CLI does today.
- A copy not installed by Homebrew is moved to the Trash by the child as its
  last step (`NSFileManager.trashItemAtURL`), or revealed in Finder when
  that fails.

**Verify**: `tests/test_uninstall.py` runs `uninstall.run` through the
existing CLI harness for both `zap` values. A test that the menu action
starts a detached child with the right argv and never calls
`remove_integrations` in-process. Manual: on a test user, cask install,
Uninstall from the app with and without zap; `brew list --cask` no longer
shows openswap, no plist is left, and the Keychain items are gone only with
zap.

## Test plan

- Extended: `test_launch_agent.py`, `test_cli.py`, `test_uninstall.py`,
  `test_packaging_homebrew.py`, the `settings_page_rows` and empty-state
  tests, `test_widget_install.py`.
- New: the lock (Step 1) and the welcome page model (Step 4).
- `uv run pytest` green after each phase.
- Manual on the notarized build: Steps 1, 3, 4 and 7, and one
  `brew upgrade --cask openswap` across a release, on a clean user account.

## Done criteria

- [ ] Opening the app with no terminal, clicking Continue, and logging out
      and in leaves the extra running from launchd
- [ ] Opening the app while it runs shows the popover, never a second item
- [ ] Open at login, the status line and (Phase 3) uninstall work from
      Settings and from the terminal, through the same functions
- [ ] `openswap setup`, `openswap uninstall` and their flags behave as before
      except for the setup "Next" lines in Step 5
- [ ] `uv run pytest` passes; cask style and audit pass
- [ ] README, `docs/menubar.md`, `docs/widget.md` and the cask caveat
      describe both paths
- [ ] `plans/README.md` row updated

## STOP conditions

- The extra would boot itself out (SIGTERM) while turning login on or off.
- Two status items can be on screen at once after Step 1.
- macOS shows a Login Items or Background Items prompt that Step 2 cannot
  report back to the user.
- Anything would switch the live Claude login, or delete a Keychain item
  outside an explicit zap.
- The hand-over leaves no extra running.

## Maintenance notes

- Without Open at login, a `brew upgrade` leaves a Finder-started copy on
  the old build until it quits. Docs should say to reopen it; Phase 1 makes
  that the rare case.
- Revisit `SMAppService` (Decision 2) only for something the LaunchAgent
  cannot do, and plan the move of existing login items with it.
