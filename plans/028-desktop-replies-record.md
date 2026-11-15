# Plan 028 record: replies to Claude Desktop and VS Code sessions

Running ledger for [plan 028](028-desktop-replies.md): status, rulings, step-0
results, review dispositions, hand-overs. Newest last within each section.

## Status

- 2026-11-15: plan written, R1–R4 ruled, plan and copy approved. Task 1 next.
- 2026-11-15: Task 1 implemented (Opus), commit 983c44b, suite 7305 passed.
  Review seats dispatched: cold (worker side), cold (hook side and masker),
  silent-failure, API surface; all on Opus, run in parallel because the diff
  is over 550 lines across two areas.

## Rulings

- **2026-11-15, R1–R4 (owner, in the planning session).** Recommended option on
  each: R1(a) the watch shows the actual command, masked and capped; R2(a)
  typed replies offered; R3 one hour; R4(a) every session, each surface once its
  step-0 check passes. Recorded in the plan with the plan 025 rules they
  supersede.
- **2026-11-15, plan approval (owner, relayed by the managing session, quoting
  their pick "Approve (Recommended)").** Plan 028 and its copy: the deny message,
  the typed-reply note, the Settings hint, the CHANGELOG line.
- **2026-11-15, implementer model (owner, relayed: "Opus (Recommended)").** Opus
  for every implementer dispatch and fix round of this plan.
- **2026-11-15, VS Code (owner, relayed, quoting their pick "Notices only for
  now (Recommended)").** VS Code sessions stay notice-only; Desktop and terminal
  sessions take hook replies. VS Code was not checked here and the extension is
  not to be downloaded. Tracked in mar3co/openswap#210 (the live check and the
  one-entry change). Makes the approved copy name VS Code wrongly: amended copy
  sent to the owner for approval, pending.
- **2026-11-15, merge and release.** Not this session's: the managing session
  gets the owner's approval for both.

## Step-0 results

Measured 2026-11-15 on this Mac; scripts and logs in the planning session's
scratchpad (not kept).

- **Terminal, permission (Task 1, R4).** Interactive `claude` 2.1.311 under
  `expect`, manual mode, a project `PermissionRequest` hook that denies after
  6 s. "Do you want to proceed?" showed while the hook waited, then the TUI
  replaced it with "Denied by PermissionRequest hook" and Claude went on. Passes.
- **Terminal, question.** The same with `AskUserQuestion` and an
  `updatedInput.answers` allow after 6 s: the menu showed, then "User answered
  Claude's questions: Pick a color → Blue". Passes.
- **Desktop.** Passed in the research (live probe 3 plus Desktop's code).
- **VS Code.** Not installed on this Mac: unchecked. Ruled notice-only for now
  (see Rulings); mar3co/openswap#210.
- **Second typed reply (row 11).** Desktop's 2.1.325 headless: a woken turn's
  `Stop` runs the wake hook again, with `stop_hook_active: true`; the second
  text started a second turn. Passes. The wake hook must not treat
  `stop_hook_active` as a reason to skip.
- **Wake during a running turn (rows 9–10).** The hook exited 2 while a
  20-second turn the host had started was running. The text was not injected into
  that turn; it started its own turn right after (24.7 s → 26.4 s). So a reply
  handed over just before the owner types runs after the owner's turn, never
  inside it.
- **Wake-hook timeout (Task 2 step 0).** Desktop's 2.1.325 headless: a `Stop`
  hook with `asyncRewake: true` and `timeout: 900` woke at 660 s, past the
  600 s default, and the turn ran (665.8 s). A configured timeout above the
  default is honored. The full hour (R3) was not waited out; Task 2's exit run
  covers it on a real session.

## Review dispositions

### Task 1 implementer departures (ruled by the controller, 2026-11-15)

- The permission hook's matcher is `""` and the hook exits at once for
  `AskUserQuestion`, instead of a matcher regex excluding it. Same behavior, and
  it doesn't depend on how Claude Code reads a regex. **Accepted.**
- A watch Deny handed to the hook while the owner presses Yes at the Mac within
  `CONFIRM_SECONDS`: both make the session `busy`, so the Deny is reported
  `applied` and the item closes `answered_from_watch` though the tool ran.
  Reporting only; no call is approved wrongly; it needs two answers within 2 s.
  This is a gap against plan 025 row 12 (never claim a win that isn't known).
  **Accepted as a known imprecision, pending the review seats.**
- Two parallel subagents running the identical call take no hook replies (the
  `_pair` taint marker is reused). Fails safe. **Accepted.**

### Task 1 review round 1 (983c44b; four Opus seats, 2026-11-15)

Mapped to Critical / Important / Minor by the controller. Fix notes (required
outcomes per state): planning session scratchpad `028-task1-fix-notes.md`,
items F1–F8.

| Finding | Seat(s) | Severity | Disposition |
|---|---|---|---|
| Masking a "credential" slot hides executable text that Allow runs (`--token $(…)`) | hook, API | Critical | F1: Allow only for an exact preview; deny only once anything is masked, cut or escaped. Revisit flag on R1 sent to the owner |
| Middle cut, bidi or zero-width characters still offered Allow | API, hook | Important | F1 |
| Masker misses `*_KEY`, quoted values, `Authorization`/`Cookie` JSON keys, space-separated names, `npm_`/`glpat-`/`hf_`/SendGrid, `-u user:pass`, `-pX` | hook | Important | F2 |
| Masker over-masks (`max_tokens`, empty values, `sed` expressions) and is quadratic on crafted input | hook | Important | F2 |
| A field shorter than the marker is cut silently | hook, silent | Important | F3 |
| Forged notice relabels a question's options while the real hook answers by position | API | Important | F4: item shows the hook's own questions; mismatch taints |
| `waiter_over` releases a queued prompt's hook on a due mark that is later lifted (regression for channel questions) | silent, worker | Important | F5 |
| `tick` releases hooks when settings are merely unreadable | worker | Important | F6 |
| A stale channel registration for a reused PID blocks hook replies | worker | Important | F7 |
| A hook Deny counted as won when the Mac said Yes within 2 s | silent (ledgered departure) | Important | F8: credited only if the call's `PostToolUse` never arrives |
| Permission waiter with empty `tool_name`/`tool_key` | API | Minor | rides along |
| Test `forged_first` doesn't change order; uncovered `_take_preview` branch; stale test name | worker | Minor | rides along |
| CHANGELOG and Settings hint promise typed replies that Task 1 doesn't deliver | hook | Rejected | Task 2 ships them in the same PR |

Implementer for the fix round: a fresh Opus agent. The Task 1 implementer
measured 371k of context, and a round this size would pass the 400k line.
