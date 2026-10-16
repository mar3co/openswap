# Plan 021: Setup wizard and Move to Applications

> **Executor instructions**: Follow this plan step by step. Each step ends
> green (`uv run pytest`). If anything in "STOP conditions" occurs, stop and
> report. When done, update the status row for this plan in
> `plans/README.md`.
>
> **Drift check**: `git diff --stat fcfb8a9..HEAD -- src/openswap/menubar.py src/openswap/menubar_panel.py src/openswap/menubar_display.py src/openswap/menubar_dialog.py src/openswap/bundle.py src/openswap/launch_agent.py`
> On any change, compare the "Current state" notes with the live code first.

## Status

- **Priority**: P1 (the DMG makes a Finder install a first-class path)
- **Effort**: L
- **Risk**: MED (replaces the first-run surface; copies the app on disk)
- **Depends on**: 020 (welcome page, Open at login from the app), PR #114 (DMG)
- **Planned at**: commit `fcfb8a9`, 2026-10-16
- **Shipped**: 2026-10-16 in #115 (`dddc6f2`), after #114 (`259cacd`) added the
  DMG. The items under "Manual checks before release" have not been run yet;
  run them on a notarized build before the next release.

## Why this matters

Plan 020 made the app finish setting up by itself, through a welcome page
inside the menu bar popover. Two things still go wrong for someone who
installs from the new DMG:

1. **Opened from the disk image (or Downloads), nothing appears.**
   `welcome_on_start` returns `"none"` for a translocated or mounted copy, so
   the person sees only a menu bar icon. Open at login and the status line
   are disabled there, and nothing tells them why.
2. **The popover is a cramped place to set up.** It hangs off a small icon,
   closes when focus moves (to the browser for sign-in, for example), and has
   room for one account step and one switch. The widget, the status line and
   the terminal command are only reachable later in Settings.

A setup wizard in its own window fixes both: its first step moves the app
into Applications, and the rest walk through accounts, Open at login and
the extras, each through the same functions `openswap setup` and Settings
already call.

## Current state

- `menubar_display.welcome_on_start(settings, frozen, temporary,
  login_item_runs_this)` returns `"show"`, `"done"` or `"none"`.
  `menubar.py` `_welcome_on_start` opens the popover on `WELCOME_PAGE` for
  `"show"` and saves `welcome_done` for `"done"`.
- `welcome_page_model` and `MenuBarPanel._build_welcome` render the page;
  `_on_welcome_continue(open_at_login)` saves `welcome_done` and then applies
  the login item last, because `launch_agent.set_login_item(True)` from a
  Finder-opened copy returns `"handoff"` and that copy quits.
- Account actions: `_on_empty_action(provider, action)` (`start` → browser
  sign-in, `add` → save the live login, `capture` → save the Codex login).
  Sign-in finishes asynchronously; the 1 s `on_sync_tick` drains it.
- `_status_line_state()` (`on`/`off`/`other`) and
  `statusline.set_installed(config_home, backup_dir, enable)`.
- `menubar_dialog.BrandDialog` is a branded modal `NSPanel`
  (`_DialogSurface`, `_DialogButton`, `_label`).
- `bundle.runs_from_temporary_location()` is true under `/AppTranslocation/`
  and `/Volumes/`; `bundle.installed_by_homebrew()`; `uninstall._trash_app`
  trashes through `NSFileManager`.

## Design

### The steps

| Step | Shown when | Content | Primary / secondary |
|---|---|---|---|
| Move | the app runs from a disk image or Downloads | Why it must move; what is already in Applications | **Move to Applications** (or **Replace and Open**, **Open Installed Copy**) / **Not Now** |
| Welcome | always | What OpenSwap does, in three lines | **Get Started** |
| Claude | always | Saved accounts, or **Sign in with Claude** and **Save Current Login**; a sign-in in progress | **Continue** (or **Skip for Now**) / Back |
| Codex | the Codex provider is available | The same for Codex | **Continue** (or **Skip**) / Back |
| Open at login | always | The switch, on by default | **Continue** / Back |
| Extras | always | Claude Code status line (off unless it already is), the widget hint, the terminal command when it is not on PATH | **Continue** / Back |
| Done | always | Where OpenSwap lives now | **Open OpenSwap** |

- Account buttons act at once (they are explicit actions, as in the
  popover). Everything else is a choice that applies at **Open OpenSwap**,
  in this order: the status line, `welcome_done`, then Open at login, which
  may hand over (`_on_welcome_continue`, unchanged). A hand-over leaves a
  show request first, so the login item's copy opens the popover.
- Closing the window (or **Not Now**) changes nothing that was not already
  done; the wizard opens again at the next launch until it is finished.
- Settings › General › This Mac gets **Setup Assistant… › Open**, which
  opens the wizard at Welcome. Only in the app.
- The popover's welcome page goes away; the wizard replaces it. Until the
  wizard is finished, the popover shows its normal cards and empty states.

### Move to Applications

- Destination: `/Applications` when writable, else `~/Applications`
  (created). The copy is made with `ditto` to a temporary name beside the
  destination, the quarantine attribute is removed from the copy (as Finder
  would by moving it, so it is not translocated again), and it is renamed
  into place.
- An `OpenSwap.app` already there:
  - installed by Homebrew: never touched. **Open Installed Copy**, with a
    line that `brew upgrade --cask openswap` updates it;
  - the same or a newer version: **Open Installed Copy**;
  - an older version: **Replace and Open** moves it to the Trash first.
- Then a detached shell waits for this process to exit, opens the new copy,
  and ejects the disk image when this copy ran from one. This copy quits.
- A translocated copy's original in Downloads is left alone (macOS does not
  say where it was).
- `welcome_on_start` returns `"move"` for a frozen copy at a temporary
  location, finished or not: opening a newer DMG is then also how a Finder
  install updates.
- A DMG copy opened while the installed copy runs loses the menu bar lock;
  it leaves a move request with its path, and the running copy opens the
  wizard on Move with that copy as the source, replacing itself.

### Where the code goes

- `src/openswap/setup_wizard.py` (new, no AppKit): `WizardFacts`,
  `WizardChoices`, `wizard_steps`, `next_step`, `previous_step`,
  `wizard_page(step, facts, choices) -> dict`. Every string lives here.
- `src/openswap/bundle.py`: `applications_dir`, `bundle_version`,
  `move_plan(app, dest_dir)`, `copy_to_applications(app, dest_dir)`,
  `relaunch_after_exit(app, pid, eject)`.
- `src/openswap/menubar_wizard.py` (new, AppKit, imported lazily like
  `menubar_dialog`): `SetupWizard`, a non-modal branded window that renders
  `wizard_page` and reports actions.
- `src/openswap/menubar.py`: `_open_setup_wizard`, `_wizard_facts`,
  `_on_wizard_action`, a re-render on the 1 s tick when the facts change,
  `_welcome_on_start` opening the wizard, the hand-over show request, and the
  Settings action.

## Steps

1. **Model.** `setup_wizard.py` and its tests: step order for every
   combination of facts, each page's copy and buttons, account states
   (loading, error, signing in, Claude Code missing), the Move page for each
   `move_plan` outcome.
2. **Move to Applications.** `bundle.py` helpers and tests against a fake
   app in a temporary folder: fresh move, replace an older copy, keep a
   newer one, never touch a Homebrew copy, fall back to `~/Applications`,
   quarantine removed, relaunch script shape.
3. **Window.** `menubar_wizard.py`: one window, re-rendered per step;
   Return is the primary button, Escape and the close button are Not Now.
4. **Wire it in.** `menubar.py` and `menubar_display.py`: start-up opens the
   wizard (`"show"` at Welcome, `"move"` at Move), the actions, the tick
   re-render, Finish, the Settings row; remove the popover welcome page and
   its tests, keeping their coverage on the wizard.
5. **Docs.** README Install and Quick start, `docs/menubar.md` First run.

## Verify

- `uv run pytest` green; new tests in `tests/test_setup_wizard.py`,
  `tests/test_bundle_move.py`, and the first-run tests rewritten for the
  wizard.
- An offscreen render of every step for review (no running extra needed).

## Manual checks before release

Run on the notarized build, on a clean test user, after plan 020's list.

- [ ] Open the DMG, open OpenSwap from it: the wizard opens on Move. Move
      to Applications: the DMG ejects, the copy in Applications opens on
      Welcome, and `xattr` shows no quarantine on it.
- [ ] Same from a zip in Downloads (translocated): Move works; the Downloads
      copy stays.
- [ ] With the cask installed, open the DMG copy: Open Installed Copy opens
      the cask's app and never modifies it.
- [ ] With an older Finder install in Applications, a newer DMG: Replace
      and Open puts the old one in the Trash.
- [ ] Walk the wizard with no Claude Code login: Sign in with Claude opens
      the browser; the account appears in the wizard when it finishes.
- [ ] Finish with Open at login on: one launchd-started extra, its popover
      open. With it off: no plist.
- [ ] Close the wizard halfway, relaunch: it opens again; finished, it does
      not. Settings › Setup Assistant opens it.

## STOP conditions

- `ditto` or the rename into Applications fails in a way that could leave a
  half-copied `OpenSwap.app` in place: stop and report.
- Anything in the wizard would change a Homebrew-installed app on disk.
