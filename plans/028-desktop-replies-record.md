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
- **2026-11-15, revised copy after the VS Code ruling (owner, relayed, quoting
  their pick "Use both (Recommended)").** The Settings hint ("Replies work in
  Claude Desktop and terminal sessions started after you turn this on; VS Code
  sessions show notices only for now. …") and the CHANGELOG New line ("Claude
  Desktop sessions, and terminal sessions started without `openswap claude`, …")
  are approved as proposed.
- **2026-11-15, final-review CHANGELOG additions (owner, relayed, quoting their
  pick "Use both (Recommended)").** The Improved line on "Preview withheld" and
  `[REDACTED]`, and the restart sentence on the New line, are kept as written.
  The managing session also told the owner about the second R1 revisit
  (withhold plus Deny only) and the reopened fix rounds; no objection.
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
- **Terminal wake (Task 2 step 0, 2026-11-15).** Interactive `claude` 2.1.311
  under `expect`, with a project `Stop` hook set to `asyncRewake` that exits 2
  after 5 s. A new turn ran; the TUI showed "Stop hook feedback", then the reply.
  `UserPromptSubmit` fired with the task-notification prompt holding the hook's
  stderr, the woken turn's `Stop` came with `stop_hook_active: true`, and
  `idle_prompt` fired about 60 s later. Passes, so `cli` keeps typed replies.
- **Woken turn's `UserPromptSubmit`** (headless 2.1.325): the prompt is
  `<task-notification>…Stop hook feedback…</task-notification><system-reminder>Stop hook blocking error from command "Stop": <stderr></system-reminder>`.
  Task 2 settles a reply on this prompt containing the note line and the exact
  text sent.
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

### Task 1 review round 3 (5828809..1834993; four Opus seats)

| Finding | Seat(s) | Severity | Disposition |
|---|---|---|---|
| Channel sessions: a credential call pairs with Claude Code's preview, which overwrites the withheld line, and Allow is offered | API, silent, hook | Critical | H1 |
| Missing names: `passphrase`, `*_KEY`, `pw`, `auth`, `-u user:pass`, colon-less URL userinfo, `accesstoken` | silent, hook | Critical (data) | H2 |
| U+1D159 and the musical format characters aren't escaped | hook | Critical (rendering-dependent; same class as G2) | H3 |
| A token right after an escaped character in question text isn't masked | worker | Critical (data) | H4 |
| Tab runs in question text pass "whole" | API, hook, worker | Important | H5 |
| Prose headers in quoted or prefixed form aren't masked | hook | Important | H6 |
| Codex's description isn't withheld for credential calls | silent | Important | H1 |
| A hook Deny's grace (1.5 s) is shorter than the confirm window (2 s) | worker | Important | H7 |
| Three G1 lines are untested or tests can't fail for their reason | worker | Minor (tests) | H8 |
| A Mac Yes in the hook-read window, with a call longer than the grace, is credited to the watch | silent | Important; inside the accepted G1 imprecision | No change; known imprecision |
| A hostile child as sole waiter: a Deny closes the item early while the dialog waits | API | Important; denial of service only, needs a hostile process | Filed mar3co/openswap#212 |
| `CHECK_BUDGET` wording, `validate_notice` docstring, a partial `[RED` at the prose cap | API, hook | Minor | ride along |

Round 3 fix notes: planning session scratchpad `028-task1-fix3-notes.md`
(H1–H8). Ruling check: one Opus pass.

Round 3 ruling check (Opus, one pass), with amendments before dispatch:
- H1 is "deny only when the call pairs exactly" (unpaired calls take no reply);
  plan row 1 is amended (785c48e).
- H2's `auth`, `pw` and trailing-`key` rules apply only to UPPER_SNAKE env names
  and dash flags, so code edits (`ForeignKey`, `useAuth`, `auth/` paths) keep
  Allow, as R1(a) intends.
- H4 uses ASCII-only boundaries in the token and header patterns.
- H6 covers YAML-style quoted values and `Bearer <long token>` in prose.
- H8's third test pins the real lost-hand-over outcome.
- The partial-mask fix keeps masking before the cut.

### Task 1 fix round 3 (6925c6b)

- H1–H8 and the Minors, by a fresh Opus implementer; suite 7608 passed. It also
  fixed `_strings` reading lists in reverse, so a Codex command list now
  matches the flag rule (accepted). Uppercase env names such as `SSH_AUTH_SOCK`
  and `SORT_KEY` now lose Allow (accepted false positive).
- Review round 4: 275+/85− lines, below the 400 line, so one cold seat and the
  silent-failure seat, both Opus.

### Task 1 review round 4 (1834993..6925c6b; cold and silent-failure seats)

No Critical findings; the safety rules have converged. The Importants are
mostly needless withholds, plus two narrow leaks in prose. Made inline by the
controller (d715367, TDD, suite 7632 passed), since each fix only narrows a
false positive under a conservative condition or closes a gap:
- `pass` counts only in upper-case names, flags and header names, so Python's
  `pass` keyword and "tests pass" keep Allow.
- Login flags skip `uid:gid`, formats (`date -u +%H:%M`), substitutions, another
  flag, and a URL or path after the colon.
- `SSH_AUTH_SOCK` is exempt. The docs now say upper-case constants in code
  count (accepted).
- Header-style names before a colon (`X-Auth-Key:`) are read like flags.
- `Bearer`/`Basic` in prose needs a digit in the token.
- Prose masks a login flag's password, and quoted parts after `=` in an
  unquoted header value. URL userinfo excludes only ASCII whitespace.
- `max()` grace is pinned by a mutation-checked test.

Not changed: an early close by a hostile sole waiter (mar3co/openswap#212).

Review: one cold seat (112 changed lines).

### Round 4 follow-up: d715367 partly reverted (073ecdc)

The cold seat on d715367 found that its narrowing of false positives opened
real leaks (rated Critical by effect):
- `curl -u "$JIRA_EMAIL:hunter2"`, `curl -u 4021:5839`;
- a `pass` key, `db_pass=…`;
- a quoted prose password cut through;
- `Bearer <token without a digit>`.

It also found header-name false positives on YAML and CSS. All of these were
in code the controller had just added, so by the convergence rule the
narrowing was reverted to 073f429's broad rules rather than patched again.
Kept, because each can only reduce leaks:
- URL userinfo excludes only ASCII whitespace;
- quoted parts after `=` in a header value;
- a prose login password masked to its opening quote or the end of its word;
- the `SSH_AUTH_SOCK` exemption;
- header names before a colon and value, limited to `X-…` and Title-Case;
- the safe `.get` in `observe`, and the `max()` test.

The remaining false positives, and the leak cases a future narrowing must not
reopen, are tracked in mar3co/openswap#213. Suite 7625 passed. One cold seat
on 073f429..073ecdc.

**Task 2 pre-dispatch ruling check** (Opus, one pass): wrong states in T1, T3,
T4, T5, T7, T8, T9, T10, T11 and T12. The notes are being amended after
measuring the woken turn's `UserPromptSubmit` and the terminal wake.
Headless 2.1.325: the woken turn's prompt is
`<task-notification>…Stop hook feedback…</task-notification><system-reminder>Stop hook blocking error from command "Stop": <stderr></system-reminder>`.

### Task 2 dispatched (2026-11-15)

Notes amended after the ruling check:
- T3: a replacement must pair with its own `Stop`.
- T4/T5: a reply settles only on the woken turn's own prompt (note plus text);
  the owner's typing first means `answered_on_mac`; background tasks and `busy`
  decide nothing; `Stop` never supersedes a handed-over item.
- T8: wall-clock hour, enforced by the worker with a 60 s margin.
- T9: release by the session file, not by item pairing.
- T11: exact stderr bytes.
- T12: separate caps for wake hooks (1 per process, 32 in all).
- Accepted: after a no-turn slash command, no reply box shows until the next
  turn; a reply handed over just before a restart stays `sent`.

Implementer: a fresh Opus agent.

### Cold seat on 073f429..073ecdc: no Critical; Task 1's per-task loop stops

Nothing caught at 073f429 is missed now; every pattern stays linear. By the
convergence rule (the findings are in code the last round added, or predate
it), these go to the whole-branch final review's single fix dispatch instead
of another per-task round:
- **I-a:** a quoted password right after a login flag's colon
  (`curl -u admin:'p@ss!'`). Predates; the fix only catches more:
  `["']?` after the user.
- **I-b:** prose over-masks `-u 1000:1000`, `rsync -a deploy@host:/srv`,
  `grep -a foo:bar`. Prose only, nothing leaks.
- **I-c:** header names ending in `Key` (`Idempotency-Key`, `X-Cache-Key`) are
  withheld. A false positive; candidate: count only `auth`/`pw` for header names.
- **I-d:** header-name check misses `X-Auth-Key:0123` (no space) and the quoted
  JSON key form.
- **I-e:** prose cut-throughs: escaped quote, text after a closing quote,
  backtick, Unicode space in branch 3.
- **I-f:** `_USERINFO` matches across U+3000 (CJK prose).
- **Minors:** revert the defensive `.get`; the `SSH_AUTH_SOCK` docs placement;
  `holds_credential`/`mask_shapes` docstrings; the comment at redact.py:40; a
  backtick-exclusion test.

### Task 2 implemented (746043c)

Fresh Opus implementer; suite 7690 passed. Controller rulings on its four
decisions, all accepted:
- **Pairing first.** A wake hook takes `message` only after pairing with its
  own `Stop`, so a command posing as a hook never gets the text. Cost: a lost
  `Stop` notice means no reply box that turn.
- **One per process.** Strictly one wake hook per process; a paired newcomer
  releases the older one.
- **Wrapper exit codes.** The wrapper maps only exit 90 to 2, and the text goes
  out on fd 3, so a broken or older program can never start a turn.
- **Digest match.** The woken prompt is matched by a digest that ignores
  trailing whitespace.

Review: the full set (first review, 941+/63−): cold worker, cold hook,
silent-failure, API surface; all Opus.

### Task 2 review round 1 (746043c; four Opus seats)

| Finding | Seat(s) | Severity | Disposition |
|---|---|---|---|
| A command the session runs pairs as the wake hook (a forged `Stop`, or winning the race with the real `Stop`), takes the owner's text, and can forge the woken prompt so the reply reads `applied` | API (rated Critical), worker, silent | Important. By effect, the text was bound for the same session, which a same-user process can already read; nothing is approved and no one acts as another. Same class as plan 025's accepted channel limit (mar3co/openswap#181) | K1: pair only the newest hook around the latest `Stop`; never a hook whose process under Claude Code started before the turn; residual noted on #181 |
| An unpaired hook (a prompt queued at `Stop`, or the hook/notice race) blocks later turns' hooks for up to an hour | worker, hook | Important | K1 |
| An out-of-order older `Stop` overwrites the latest | worker | Important (low reach) | K1 |
| No staleness check on the hook path: text can land after a turn the owner didn't see | API, hook, silent, worker | Important | K2 |
| A held item never settles if the woken prompt never matches (format drift, a whitespace-only text, a failed write) | hook, silent, worker | Important | K3 + K5 |
| Desktop: a woken turn whose prompt notice arrives after its `Stop` leaves no idle item | hook, silent, worker | Important | K4 |
| A lone-surrogate text makes the hook fail after taking it | silent | Important | K5 |
| Forged notices block typed replies | API | Important; denial of service only | K6: accepted and documented |

Task 2 fix round 1, pre-dispatch ruling check (Opus, one pass). Amendments:
- **K1:** pairing at either order of the hook and its notice; exactly one
  eligible hook pairs (two or more pair none); a hook whose process started more
  than 2 s before the `Stop` is never eligible; a typed prompt releases only
  unpaired hooks and hooks paired to an older `Stop`; per-process caps of one
  paired and two unpaired.
- **K2:** staleness only by events (a newer prompt, a busy or waiting status, a
  newer `Stop`), never a time window, which would break Desktop's slow `Stop`
  hooks.
- **K3:** a mismatch is a note-carrying prompt with a wrong digest.
- **K5:** refuse text containing the closing tag; trim U+FEFF.
- **K6:** "any local process".

Implementer: a fresh Opus agent. The Task 2 implementer is not reused, to stay
under the context line.

### Task 2 fix round 1 (d50057e)

K1–K6 done by a fresh Opus implementer; suite 7728 passed. 31 new tests fail on
746043c. Pairing is now computed from the latest `Stop` and the hook's launch
time (`peers.hook_launch`). All six of its judgment calls are accepted:
- K5's gate applies to the hook path only;
- K2 counts a status by its own `status_at` (assumes Claude Code doesn't
  rewrite `statusUpdatedAt` while busy; unverified);
- T6's `refused` is kept for a fresh non-idle read;
- a mismatch must be dated at or after the hand-over, and a match settles
  whenever the item is open;
- registration is refused when the launch time is unknown or the hook can never
  pair;
- two qualifying hooks are kept waiting, unpaired.

Review: cold, silent-failure, API surface (701 lines), all Opus.

### Task 2 review of d50057e (cold, silent-failure, API surface): per-task loop stops

- **API seat's Critical:** a session command started mid-turn forges a `Stop`
  and suppresses the real `Stop` notice, so it pairs and receives the typed
  text. **Adjudicated Important** by effect, consistent with Task 2 round 1:
  the text was bound for that same session, nothing is approved, and it is the
  mar3co/openswap#181 class. The docs must stop saying such a shell "never
  pairs" or that forged notices "never send text elsewhere".
- **API Important:** a forged note-carrying prompt (unlimited back-dated
  guesses, or the impostor that holds the text) makes a reply read `applied`.
  Fix: a woken prompt counts only if dated at or after the hand-over, so a wrong
  guess after it is a K3 mismatch (one guess); document that an impostor can
  report `applied`.
- **Silent and cold:** the launch check compares the notice's late `time.time()`
  with the wrapper's `ps` start, which can drop the real hook on a slow start.
  Fix: date the `Stop` by the notice hook's own launch (peer `hook_launch`), so
  both sides use the same clock.
- **Silent:** a background subagent's `waiting` dialog counts as a newer turn
  (K2). Fix: count only `busy`.
- **Silent:** a lost notice plus a fast background turn (about 100 ms race,
  predates this round). Filed mar3co/openswap#214.
- **Minors:** doc wording for an older `Stop` ("pairs nothing") and the 2 s
  shell case; the `_actions`, `channel_link` and `wake_main` docstrings; the
  `_gate` order.

All of these are in code the last round added, or are narrow, so by the
convergence rule they go to the whole-branch final review's single fix
dispatch, with Task 1's items I-a to I-f.

### Whole-branch final review (origin/main..HEAD; cold and silent-failure, Opus)

No Critical or Important finding needing another round.
- **Cold:** Minors only: the dead `answerable` waiter field; the CHANGELOG
  missing the masking and withholding change (which also affects
  `openswap claude` sessions) and the restart note; docstrings.
- **Silent:**
  - A backward wall-clock step keeps a wake hook paired through an unseen turn
    (low reach). Filed mar3co/openswap#216.
  - After a worker restart, idle sessions take no typed reply until their next
    turn (fails safe). Filed mar3co/openswap#215, plus a docs limit.
  - Defense in depth: the worker itself refuses Allow on a withheld item (B5).
- **CHANGELOG addition** sent to the owner for approval (relay, 2026-11-15);
  the implementer uses the draft.

Final single fix dispatch notes: planning session scratchpad
`028-final-notes.md`:
- A1–A6: Task 1's routed items I-a, I-c to I-f, and the docstrings. I-b, the
  prose over-masking, is dropped: narrowing it would let a numeric prose
  password through.
- B1–B5: Task 2's routed items and B5.
- C1–C5: docs, leftovers and the CHANGELOG.

After it: ONE cold seat, the gates, then the PR. Every non-Critical finding
from that seat becomes a tracked issue.

Final pre-dispatch ruling check (Opus, one pass), with amendments:
- A2 keeps a header's trailing `key` withheld, except a short non-secret list
  (Idempotency, Cache, Partition, Sort, Primary, Foreign, Sec-WebSocket).
- A4 ends the prose password at ASCII whitespace or U+3000, U+2028, U+2029.
- A5 applies to prose only, so the call check is unchanged.
- B2 stores the Stop hook's launch separately, uses it only in the three launch
  comparisons, compares strictly (`stop_launch - launched <= 1`), and falls back
  to the notice's `at`.
- B4: "held" means already in `_sent`.

Final fix dispatch: c7bac8e (redact) and 7998766 (worker, ipc, docs,
CHANGELOG), by a fresh Opus implementer; suite 7776 passed. Its three
prose-masking decisions are accepted (each masks the same or more of the
password itself). One cold seat follows; per the budget, every non-Critical
finding becomes a tracked issue.

### Final cold seat (74f327f..7998766): a Critical reopens the loop once

- **Critical (data):** prose masking showed part of a single-quoted login
  password the parent masked (`-u admin:'p\'q'r st'uv`). The shell reads no
  escapes inside single quotes. It also fixes a pre-existing leak of the
  `'it'\''s secret'` idiom.
- **Important, a regression:** B2 let a session's background shell vouch for
  itself with its own forged `Stop`, widening the mar3co/openswap#181 class
  from "started in the last 2 s" to "any background command".
- The owner was told before the loop reopened (relay, 2026-11-15).
- Fix made inline by the controller, f3e2808 (38+/8−, TDD, suite 7780 passed):
  - the shell's quoting rules in prose;
  - a Stop hook's launch vouches only for a `Stop` reported within
    `STOP_START_SECONDS` (5 s) of it, otherwise today's 2 s rule;
  - the docs updated.
- One scoped cold seat on f3e2808.

Scoped seat on f3e2808: the shell reading verified (no shell-password
character newly shown in about 3M fuzzed inputs), linear time, and the real
hook pairs for `Stop` hooks starting up to 4 s late. Its findings:
- **A backdated forged `Stop` after an Esc-ended turn** (no real `Stop`) lets a
  session's own background shell pair. Not a regression; the #181 class,
  adjudicated Important. The docs limit is corrected and noted on
  mar3co/openswap#181.
- **A `Stop` hook taking over 5 s to start** drops that turn's reply box.
  Fails safe. Filed mar3co/openswap#217 and added to the docs.
- **Non-shell quoting in prose** (`'admin:it\'s secret'`) showed `secret`,
  which the parent masked. Critical by effect, so the loop reopened once more
  (the owner was told). Fix b866021: a backslash inside a single-quoted value
  masks the rest of the line, more than either reading (suite 7782 passed).
  One scoped cold seat on b866021.

Scoped seat on b866021 found one more prose case in code b866021 added: a
second login flag ending its line, after a backslashed single-quoted password,
had its next-line password shown. Doc Minors too. Patching each reading kept
producing narrower cases, so prose login-password masking is now **the union**
of the four readings this branch has used (the shell's, a writer's escapes, a
backslash swallowing the rest of the line, quotes alone). That masks at least
as much as each by construction (60b38b7, suite 7784 passed). The timing test
now times `holds_credential` and `mask_shapes` separately (0.5 s each). In
production `mask_shapes` only sees prose cut to its cap plus a 1000-character
margin. Doc wording fixed. One scoped cold seat on 60b38b7.

Scoped seat on 60b38b7: no Critical or Important (fuzzed 200,000 inputs, the
union verified, linear time). Minors:
- the docs understate the prose login-password extent: fixed;
- adding `"-u=" * 87_000` to the hostile timing list: **ruled not added**. It
  already takes 0.27–0.34 s against the 0.5 s budget, so it would make the test
  flaky on slower CI runners, and production prose is capped at its limit plus
  1000 characters.

### PR

- 2026-11-15: the owner chose "Create PR" in this session (auto mode had refused
  `gh pr create` on the managing session's relay alone). Opened
  mar3co/openswap#218 from claude/charming-montalcini-f4134e (base main).
  Auto-fix is on. Requested a Codex review. Merge and release stay with the
  owner, through the managing session.

### PR follow-ups and convergence

- **Codex finding (P1):** prose masked only 1000 characters past its cap, so
  a construct needing its end (a URL password's `@`) could show its first part.
  Fixed in abccfca: `MASK_MARGIN` raised to 64 KiB; thread answered and
  resolved.
- **CI `macos-keychain`:** the 260k hostile timing test flaked at 0.6 s.
  Fixed in 235ea0f: each check is timed at its production size.
- **Seat follow-ups:**
  - c66fcf4: escape only the part of a question that can be shown, because
    the hook's time could overrun on crafted input.
  - 5f20032: strip leading blanks before that cut, because a `[REDACTED]`
    fragment or dropped field could appear; fuzz-verified by the seat.
- **Convergence ruling** (managing session, applying the owner's review
  rules, 2026-11-15). Every round since the PR opened found issues only in code
  the previous round added. From the fuzz check on 5f20032 on:
  - any non-Critical finding is a tracked issue, with no new fix round;
  - only a Critical (a credential shown on the watch, or a hidden command that
    could be allowed) reopens the code, and the managing session is told first;
  - Codex findings are handled the same way: fix Criticals, and fix Importants
    only when reachable on production and not in code this PR's fix rounds
    added.
- **Codex on 6d90b0a:**
  - two P1s, each sending a credential part off the Mac: an apostrophe in a
    URL password (call check and prose), and an escaped quote inside a JSON
    header value (prose). Treated as Critical; the managing session was told
    first. Fixed in bd0856e (widening only, tests); threads answered and
    resolved;
  - the earlier P1 on text from past the cap: tracked as mar3co/openswap#219.
- **Masking-scope ruling** (managing session, applying the owner's rules,
  2026-11-15). On main, Claude's last message and question text already reach
  the watch unmasked whenever details are on (plan 025 D1), so prose masking in
  this PR only reduces exposure, and pattern masking can never be complete; the
  hard boundary is the "Send details" switch. From here on:
  - a new prose-masking miss becomes a tracked issue, not a fix round;
  - only a gap that lets a hidden command be allowed from the watch, or that
    shows a credential main would not show, reopens the code. Call previews are
    new in this PR, so a credential-check miss counts.
  
  The PR body says so.
- **Cold check of bd0856e:** `holds_credential` no findings; linear time.
  Prose-masking interplay (one pattern's mask hiding another credential's
  start, partly predating it): tracked as mar3co/openswap#220 under the
  masking-scope ruling.
