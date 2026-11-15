# Plan 028 record: replies to Claude Desktop and VS Code sessions

Running ledger for [plan 028](028-desktop-replies.md): status, rulings, step-0
results, review dispositions, hand-overs. Newest last within each section.

## Status

- 2026-11-15: plan written, R1–R4 ruled, plan and copy approved. Task 1 next.

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

## Review dispositions
