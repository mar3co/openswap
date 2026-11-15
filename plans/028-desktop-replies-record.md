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

Pre-dispatch ruling check (Opus adversarial reader, one pass, 2026-11-15). It
found wrong states in F1, F2, F4, F5, F6, F7 and F8. The notes were amended
before dispatch:
- JSON escapes count as exact; Zl/Zp and runs of spaces make a preview
  deny-only.
- Masking is done with cut-straddling secrets in mind; quoted values are masked
  whole; only substitutions and variable references stay readable. The token
  shapes also apply to the Bash `description` and `last_message` (a fold-in).
- A question's wholeness comes from the waiting hook.
- Plan row 3 is amended for queued prompts.
- F8 decides only on the call's own signals.

F8's premise was measured: on 2.1.325, neither a hook deny nor a host deny
fires `PostToolUseFailure`. Both continue the turn and end with `Stop`. **Known
imprecision, accepted:** a Mac "No" within 2 s of a watch Deny can be credited
to the watch; the call is denied either way. No re-pass: no ruling on money or
entitlement changed.

Fix-round implementer: fresh Opus agent, with the notes file and the Task 1
report as inputs.

### Task 1 fix round 1 (3eac7df, 5828809)

- 3eac7df: F1–F8 and the Minors, by a fresh Opus implementer (suite 7479
  passed). Its context reached 471k, past the 400k line, so it is not resumed.
- Controller rulings on its report (2026-11-15):
  - **Space runs.** "4 or more spaces → deny only" would make indented code
    deny-only, against F1's aim. Ruled: only a run of more than 16 spaces is
    marked and deny-only; indentation stays exact.
  - **R1 revisit.** No objection from the owner yet; F1 stands as written.
  - **Suggestion 1 (credit a hook Allow only on its own `PostToolUse`).**
    Declined. It is the same reporting-only imprecision as the accepted Deny
    case: in Desktop a Mac "No" also continues the turn, and the call is denied
    either way.
  - **Suggestion 2 (release a never-recorded hook early).** Adopted, amending
    F5: a hook whose prompt is never recorded is released once its session
    file says `idle` after it, past `PAIR_SECONDS`. Such a hook can't take a
    reply, and releasing it frees about 36 MB.
- 5828809: both rulings, made inline by the controller (24 lines changed, TDD);
  suite 7482 passed.
- Review round 2 dispatched on 983c44b..5828809: the same four Opus seats.

### Task 1 review round 2 (983c44b..5828809; four Opus seats)

| Finding | Seat(s) | Severity | Disposition |
|---|---|---|---|
| Non-ASCII blanks (U+3000, NBSP, Hangul fillers), variation selectors and combining marks count as exact, so a tail can be pushed out of sight while Allow is offered | API, hook, silent | Critical (approves something not visibly shown) | G2: a safe character set; everything else is escaped and deny-only |
| A credential used as a nested dict key goes out unmasked with `exact` | hook, silent | Critical (data) | G3 |
| Masker quadratic on `token=$(`-style input (192 s at 262K) | hook, silent | Important | G3: linear, under 0.5 s |
| Masker false positives (`git add src/auth.py …`, `fix-auth`, pytest `::`, `grep SECRET_KEY .env`) | hook | Important | G3: scope narrowed |
| Substitution or variable values masked in quoted, header and `-u` branches | hook | Important | G3 |
| A value inside an outer quoted string masked to the end of the text | hook | Important | G3 |
| "Bearer" masked in prose | hook | Important | G3 |
| F8 holds a hook-denied item open until `Stop`: blocks a retried call, keeps later prompts' hooks alive, sticks after Esc | worker, silent | Important (regression from round 1) | G1: settle at the grace (re-rules F8) |
| Notices from v0.4.2 hooks rejected after an update | worker | Important | G4 |
| The watch shows option labels without their descriptions | API | Important, predates plan 028 | Filed as mar3co/openswap#211 (owner decision) |
| Docstring says 4 spaces; F6 test can't fail for its reason | API, worker, hook | Minor | ride along |
| A subagent's hook Deny credited when a Mac Yes lands in the hook-read window | silent | Not reachable (millisecond race) | No change |

Controller notes:
- **The masker is narrowed (G3).** Round 1's F2 asked for more heuristics than the data boundary needs. Plan 025 D1 accepted masking that catches recognizable credentials only, and the heuristics produced most of round 2's findings. `mysql -pX` and `login -p X` drop out of scope as a documented limit.
- **Convergence.** Most round 2 findings are in code round 1 added. Two Criticals reopen the loop, as the review rules require. Round 3 rewrites the masker to a narrower scope rather than patching it, to converge.

Round 2 pre-dispatch ruling check (Opus, one pass) found wrong states in every
ruling. Amendments:
- **G3 replaced by "withhold, don't mask"**: a call holding a recognizable
  token shape or a secret-sounding name gets no preview, a fixed line, and Deny
  only. Masking in place needed shell-accurate parsing and kept leaking or
  hiding ordinary commands. Second revisit flag on R1 sent to the owner (relay
  2026-11-15); proceeding unless they object. Documented limit: a password with
  no recognizable shape and no secret-sounding name nearby (`sshpass -p x`).
- **G1**: the confirm bound is kept; a denied item still queues prompts
  recorded before its exit; a session still `waiting` at the end of the grace
  means `answered_on_mac`; allows are unchanged from 983c44b.
- **G2**: bidi classes R, AL and AN make a preview deny-only and a question not
  whole; line breaks separated only by whitespace count as consecutive; space
  runs in questions are covered; confusables are an accepted residual.
- **G4**: the worker masks prose on every notice; an old question hook's
  `answerable` is ignored.

Fix round 2 implementer: fresh Opus agent. The round 1 agent is over the 400k
line.

### Task 1 fix round 2 (dd2bce4, 1834993)

- dd2bce4: G1–G4 and the Minors, by a fresh Opus implementer (suite 7533
  passed). `redact.py` is now a credential check plus prose masking.
- Controller rulings on its report:
  - **Scope note** (the withheld line also replaces Claude's description on
    notice-only items): accepted, the safer direction.
  - **Suggestion** (match `password`, `passwd`, `secret` and `credential`
    anywhere in a name, so `PGPASSWORD` is caught): adopted. A false positive
    costs only Allow; a miss sends a password off the Mac. Made inline
    (1834993); suite 7536 passed.
- Review round 3 dispatched on 5828809..1834993: the same four Opus seats.
