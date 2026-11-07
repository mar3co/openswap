# Plan 025: S1 review round (OpenSwap notices)

Branch reviewed: `claude/p025-s1-attention-notices-b693e7`, base 1329499,
head 1b4264e (about 2,450 lines of code plus 1,700 of tests). Four Opus seats
on 2026-11-06, each given only its part of the diff: cold and silent-failure
on the hooks installer and settings, cold and silent-failure on the worker's
item lifecycle, IPC, protocol and reference server. No Critical findings.

Held until S2 settles: S2 builds on this branch and edits the same worker
files, so this round goes to one implementer together with S2's own review
round, on S2's branch, to keep one writer per file.

An Opus adversarial reader checked the first draft's rows on 2026-11-06; the rows below are the corrected version. Each row states the required outcome for a state, not a mechanism. Add a
test per row that would fail without the fix.

## Important: hooks installer and settings

| # | State | Required outcome |
| --- | --- | --- |
| H1 | Turning notices on or off, or uninstalling, when a home cannot be listed, the Desktop registry is unreadable, or a home OpenSwap once wrote to is no longer discovered (a dropped profile, an unset `CLAUDE_CONFIG_DIR`) | Removal covers every file OpenSwap ever wrote hooks into, listed or not. Any it cannot reach is named to the caller. Uninstall never reports the notices step done while hooks may remain |
| H2 | Removing hooks (notices off, worker start, uninstall) | A file OpenSwap never wrote to is never parsed into a refusal, rewritten, reformatted or deleted, and uninstall never fails over it. An unreadable file OpenSwap may have written to is never rewritten, and is reported. Any `<openswap> attention …` or launcher entry counts as OpenSwap's, whatever its event (a downgrade). While OpenSwap's record of turning the Codex feature on exists, removal restores the feature as H5 says, even with none of OpenSwap's entries left |
| H3 | `hooks.json` (or a Claude `settings.json`) is a symlink, or OpenSwap created the file in an earlier cycle and the owner has since replaced it | Removal writes the remaining document through the link and never unlinks the link. A file is deleted only if OpenSwap created it at the latest turn-on and it still holds only OpenSwap's content. Settings reads trust under the path Codex itself uses (check which) |
| H4 | Removing OpenSwap's Codex group shifts the owner's own hooks for the same event | Every owner hook keeps its exact state: trusted, disabled or untrusted. None gains trust or starts running. If that cannot be ensured, the owner is told which hooks changed; the worker's start-up cleanup, which has no one to tell, never makes such a shifting removal |
| H5 | Codex's hooks feature is effectively off (false, or unset with Codex's default off) and other hooks exist | Only the owner's own turn-on may enable the feature, and it says those hooks will start running and needs the owner's OK. The worker never enables it at start once other hooks exist or the owner has set it false since. The original value is kept until it is actually restored or the owner changes it, and removal restores it whenever the only hooks added since are OpenSwap's. Settings never advises "Trust all and continue" while other untrusted hooks are present |
| H6 | `features.hooks` is missing, or the legacy `codex_hooks` key is set | Settings shows the effective value, legacy key included, and never "trusted" for a feature that is effectively off |
| H7 | The app, the launcher, the named program or the backup root is missing, or OpenSwap is present but cannot run the hook (an older build, an import failure), including entries left after an uninstall | No Claude Code or Codex event ever fails or is blocked: anything short of a working hook exits 0 and prints nothing. Settings shows hooks that point at a missing program |
| H8 | An account profile has sharing off, a profile is added while the worker runs, or the switch is turned off during a periodic install | Sessions in every profile get hooks without a worker restart, or Settings names the profile as not covered. A file that holds only OpenSwap's hooks never blocks turning sharing on, and turning sharing off never leaves the profile without hooks. No home gets hooks after the switch went off |
## Important: worker lifecycle and protocol

| # | State | Required outcome |
| --- | --- | --- |
| W1 | OpenTag answers an `attention` report with 403 `forbidden`, or with a persistent 400 or 413 | 403: retry on a long backoff, resume once accepted, and say why in status; never every 5 seconds. 413: the report shrinks. 400 caused by one item: that item is repaired or dropped. Closes always get through. The protocol doc says so. Failures are logged, rate-limited |
| W2 | One session ID is open in two live processes (`claude --resume` in a second terminal), or a PID is reused | No hook or record from one process closes or suppresses another process's items. An item with no process recorded closes only when every live record of its session agrees. "Alive" means the same process, not a reused PID |
| W3 | A `permission_prompt` arrives while the session file shows a status set before or after the dialog | The status counts as the answer only if it was set after the recorded `PermissionRequest` time; otherwise the prompt is published. A prompt already answered is never published |
| W4 | A background subagent's prompt is open | It closes when that prompt or that subagent ends (a Deny or Esc on it, `SubagentStop`), never because the main thread's status changed. A brief `busy` between queued dialogs closes only the prompt that was shown, and only if nothing waits when the grace ends |
| W5 | Codex approvals on one thread and logged answers | Each logged answer closes at most one approval, once; the 0.5-second re-read never closes another |
| W6 | Closes pile up (after an outage), including past the 200 cap | Every close reaches the backend with its reason while under the cap. Past the cap, the item still leaves the backend's open set. The reference server keeps close reasons for any item it has accepted since registration |
| W7 | `settings.json` cannot be read, or holds a key this build does not know (a downgrade) | Unknown keys are ignored. Open items are never wiped for that reason. Waiting closes are still sent. Status says the setting cannot be read |
| W8 | A crash or power loss right after saving `attention.json`, or a passing read error | The per-Mac key and open items survive (fsync before replace). Only a file that parses wrong is moved aside; a passing I/O or permission error never moves it or replaces the key |
| W9 | Text holds a lone surrogate | It is replaced, both on intake and when reporting, including in items already saved. The item is never dropped |
| W10 | `test_another_processs_record_for_the_session_is_ignored` | The test publishes the item first, then observes the other process's record, and asserts the item is still open with its detail and no `session_ended` close. Add the "left waiting" case |
## Minor (ride along)

- `PostToolUse` for `AskUserQuestion` closes the question item with the
  matching tool key and agent, never just the first.
- Keep one or two IPC slots for status, pause and stop, and log a refused
  connection. Handle `OSError` from `accept()` without killing the IPC thread.
- Log attention-path failures, rate-limited per error code.
- `session_label` uses the HMAC `session_id`'s first characters, never the
  raw ID's.
- Skip the session-file refresh when notices are off and the store is empty.
- fsync and keep file modes in `_write_text`. Keep a trailing TOML comment on
  the `hooks` line. Don't leave an empty `config.toml` that was absent before.
- `test_hook_stops_at_its_deadline_whatever_happens` holds stdin open so the
  alarm is what ends it.
- Stale comments: `uninstall.py:421-422` and the `cli.py` comment still say
  `notify`.

## Dispositions

- 2026-11-07: every row and Minor is fixed on `claude/p025-s-fixes-ed1ac5` in b6fd5be. That fix session's own review fixes are in 0e7211b. Each row has a test; no row was disputed. Gates: 6,981 passed.
- Rows met more strictly than written:
  - H4: the fallback is used. The worker never makes a removal that shifts the owner's hooks, and the owner is told per hook.
  - H5: the worker never enables Codex's feature. Only the owner's turn-on does, with a dialog that lists their other hooks.
  - H8: profiles without sharing get their own settings file. This supersedes S1's earlier choice to leave such profiles without hooks.
- Live evidence (Codex 0.166.0-alpha.6):
  - H6: Codex's hooks feature is on by default, so OpenSwap no longer writes `hooks = true` when the key is unset. `features.hooks` wins over the legacy `codex_hooks` key in either order.
  - H3: trust is keyed under `realpath(CODEX_HOME)`.
- Live evidence (Claude Code 2.1.311):
  - W4: the session file stays `waiting` while subagent prompts wait. A "No" fires `SubagentStop`, now hooked.
- Found in review: Claude Code writes `procStart` in UTC. A local-time `ps` was discarding every session record on non-UTC Macs. Fixed.
- Final cold seat on the fix diff (installer area): no Critical. Three Important findings and the leftover Minors are tracked in mar3co/openswap#182. S1 is review-settled.
