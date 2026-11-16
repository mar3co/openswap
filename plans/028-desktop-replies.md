# OpenSwap feature plan: answer Claude Desktop and VS Code sessions from the watch

Status: IN PROGRESS. Accepted by the owner 2026-11-15, with its copy, after
rulings R1–R4 the same day. Progress, rulings and review dispositions:
[028-desktop-replies-record.md](028-desktop-replies-record.md). Extends plan 025 (session attention), whose
Safety rules, data boundary and required outcomes still hold except where this
plan says otherwise. The evidence is
[research/025-desktop-replies.md](research/025-desktop-replies.md). Claude Code
facts were measured on 2.1.325, the version Claude Desktop bundles on
2026-11-15.

## Problem

Replies from the iPhone and Apple Watch (Allow or Deny, a question's answer, a
typed reply) reach only sessions started with `openswap claude`, which loads
OpenSwap's channel. The owner runs almost every session in Claude Desktop. Those
sessions show on the watch, but they can't be answered there. Desktop starts
Claude Code itself and gives no way to name a channel. VS Code's extension is
the same.

## What the research showed

- A synchronous `PermissionRequest` hook that returns a decision answers the
  prompt. Claude Code shows the prompt at the same moment it starts the hook, so
  a waiting hook never delays Desktop's dialog. Whichever answer comes first
  wins:
  - **Hook first:** Claude Code sends `control_cancel_request`, and Desktop
    closes its dialog. Measured live: "Permission request … aborted" in the
    hook's own second.
  - **Mac first:** Claude Code uses the Mac's answer and ignores the hook's later
    one. It does not kill the hook.
- The same holds for `AskUserQuestion` with `updatedInput.answers`. OpenSwap's
  question hook already does this and is already installed in Desktop's config
  homes. It gives up at once there only because the worker finds no channel.
- A `Stop` hook with `asyncRewake: true` that exits 2 starts a new turn in an
  idle session, Desktop included. The text reaches Claude inside a task
  notification ("Stop hook feedback"), not as a user message.
- A Desktop session's `sessions/{pid}.json` says `waiting` (`waitingFor:
  "permission prompt"`) while a prompt is open, and `idle` after a turn. The
  worker's existing gate therefore works for Desktop sessions as it is.
- One waiting OpenSwap hook process uses about 36 MB.

## Owner decisions (2026-11-15)

The owner took the recommended option for all four on 2026-11-15. Each is
binding for the tasks below; reopening one goes back to the owner.

- **R1. What the watch shows before you tap Allow on a Desktop prompt.**
  Without a channel, the watch has only Claude's own one-line summary of a
  command. Claude writes that summary, so it can be wrong. Options:
  - (a) Show the actual command or file edit, cut to plan 025's caps, with
    recognizable secrets masked by OpenSwap. More of the session's text passes
    through OpenTag, and only while "Send details" is on. *(recommended)*
  - (b) Deny only from the watch; Allow stays at the Mac.
- **R2. Typed replies to a Desktop session.** The text reaches Claude as a note
  from OpenSwap's hook, not as a message from you. Claude followed it in both
  tests. Options:
  - (a) Offer them, with the note worded so Claude reads it as your reply.
    *(recommended)*
  - (b) Leave typed replies out for Desktop and VS Code; Allow, Deny and answers
    still work.
- **R3. How long a finished Desktop turn can take a typed reply.** Each session
  waiting for one keeps a small OpenSwap process (about 36 MB) running. When the
  wait ends, the watch shows the session without a reply box. Options: 10
  minutes; **1 hour** *(recommended)*; 4 hours.

**Rulings:** R1(a) actual command, masked and capped; R2(a) offer typed
replies; R3 one hour; R4(a) every session, each surface once its step-0 check
passes. Why: replies are what the owner asked for in the sessions they actually
use, and the watch must show what Allow approves. These supersede, for sessions
without a live channel only, three parts of plan 025:
- Safety, "only channel-registered items carry actions": a waiting hook now
  receives replies under required outcomes 1–15 below.
- Data boundary, "never the raw `tool_input`": under R1(a) a preview of it
  leaves the Mac, masked by OpenSwap and capped like `input_preview`, only while
  "Send details" is on.
- "Deliberately outside this plan: answering in Claude Desktop's Code tab".
- **R4. Which sessions get replies without `openswap claude`.** The hooks run in
  every Claude Code session on the Mac. Options:
  - (a) Every session (Desktop, VS Code and plain `claude` in a terminal), each
    once the step-0 check below shows its dialog closes. `openswap claude`
    stays, and still gives typed replies as your own message. *(recommended)*
  - (b) Desktop and VS Code only; terminal sessions still need `openswap
    claude`.

## Design

The rule that changes: where a session has no live channel, a waiting
synchronous hook takes the channel's place. A hook process belongs to exactly
one prompt (one tool call or one `Stop`), so a reply can reach only the prompt
whose hook receives it. A session with a live channel keeps the channel path
unchanged: the new hooks exit at once there, so nothing is ever answered twice.

Every hook below is installed only while the replies switch is on, in every
managed config home, like today's question hook. Claude Code reads hooks when a
session starts, so turning replies on reaches sessions started afterward.

### Allow and Deny

- **Hook:** a new synchronous `PermissionRequest` hook for every tool except
  `AskUserQuestion` (`openswap attention permission`, timeout 600 s). It sends
  the worker its session, its tool key, its agent and, under R1(a), a preview of
  its own `tool_input`. It then waits for a verdict or a release. It prints
  `{"behavior": "allow"}` or `{"behavior": "deny", "message": "The owner denied
  this from their phone or watch."}`, or nothing.
- **Waits for:** the watch's verdict, its item closing (any answer at the Mac,
  the session ending, `/clear`), the worker going away, Claude Code ending the
  hook, or 590 s, whichever comes first.
- **Worker hand-over:** the waiter is paired with its item the way question
  waiters are today (same process, session, agent and tool key, within
  `PAIR_SECONDS`). `_deliver` sets the waiter's verdict where today it sends the
  channel a `request_id` verdict.
- **What `_actions` offers without a channel:**
  - `allow` and `deny` when exactly one waiter waits for the item, "Send
    details" is on, and the item shows the waiter's own preview (R1a).
  - `deny` alone under R1(b), or while details are off.
  - Nothing when no waiter waits, or more than one does.
- **Detail:** under R1(a) the item's `input_preview` comes from the waiter, so
  Allow names exactly the call that receives it. Plan 025's required outcome 8
  becomes: Allow never approves a call other than the one shown, and the preview
  shown is the one the receiving hook sent.

### Question answers

- **Hook:** the existing `openswap attention question`, unchanged on the Claude
  Code side.
- **Worker:** `question_waiter` no longer requires a registered channel. It finds
  the session's config home from the Claude Code process (the home whose
  `sessions/{pid}.json` lists it). `waiter_over` stops treating "no channel" as
  the end of the wait. `_actions` offers `answer` under today's rule (details
  on, the question shown whole, exactly one waiter) whether or not a channel is
  live. `_deliver` reads the session record through the waiter's home.

### Typed replies (under R2a)

- **Hook:** a new `Stop` hook with `asyncRewake: true` (`openswap attention
  wake`), whose timeout is R3 plus a margin. At each `Stop` it registers with the
  worker for its session and process, then waits for:
  - a message: it writes the note to stderr and exits 2, which starts a turn;
  - a release (the owner typed, the session ended, the wait passed R3, replies
    turned off, the worker went away): it exits 0 and prints nothing.
- **Note wording** (copy for the owner to approve): "Reply from the owner's phone
  or watch:" followed by the text.
- **What `_actions` offers without a channel:** `message` on an idle item only
  while exactly one wake hook waits for that session's process and the session
  file says `idle`.
- **Worker hand-over:** `_deliver` gives the text to that wake hook (today:
  `link.send` of a channel message). The woken turn is not a `UserPromptSubmit`
  with OpenSwap's channel tag, so the idle item closes as `answered_from_watch`
  when the hook took the text and the session left `idle`.

### Settings and the CHANGELOG (copy for the owner to approve)

- Settings › Remote › Sessions, the hint under "Allow replies from my phone and
  watch" (replaces `REMOTE_REPLIES_HOW`). Under R4(a): "Replies work in Claude
  Desktop, VS Code and terminal sessions started after you turn this on. Start
  Claude with {launcher} to have typed replies arrive as your own message."
- CHANGELOG, Unreleased › New: "Claude Desktop and VS Code sessions can now be
  answered from your iPhone and Apple Watch: allow or deny a tool, answer a
  question, or reply to a finished turn. Turn on Settings › Remote › Sessions ›
  Allow replies from my phone and watch on the Mac first; sessions started
  after that take replies." Adjust it to R1–R4.
- `docs/` that say replies need `openswap claude`: the same correction.

## Required outcomes

States and outcomes. These state behavior, not mechanism; the implementer
designs the mechanism, and its review seats check these.

| # | State | Required outcome |
|---|---|---|
| 1 | A prompt in a session with a live channel | Answered through the channel exactly as today; the new hooks exit at once and take no reply (amended 2026-11-15: a call that may hold a credential shows no preview there either, and takes Deny only when its channel request pairs) |
| 2 | A prompt in a session without a channel, one waiter | Takes the actions above; a watch verdict that arrives first decides it, and the Mac's dialog closes |
| 3 | The owner answers at the Mac first | The item closes as `answered_on_mac`; the waiter is released within 2 s of the worker seeing the session leave `waiting` (amended 2026-11-15: when another prompt of the same process is queued behind it, or its item isn't recorded yet, within 2 s of its own item closing); a verdict already handed to the hook has no effect and is never reported `applied` |
| 4 | The watch's verdict reaches the hook just as the Mac answers | `applied` and `answered_from_watch` only when plan 025 row 12's rule shows the watch won; otherwise `answered_on_mac`, and the reply settles as not applied |
| 5 | Two waiters (or none) claim one item | The item takes no reply (a command the session runs can block replies, never approve a call) |
| 6 | The session ends or `/clear`s while a hook waits | The item closes as `session_ended`; the hook exits without output; no OpenSwap process outlives its Claude Code process by more than 2 s |
| 7 | The worker restarts while a hook waits | The hook exits without output and the prompt stays with the Mac. The item stays open, takes no reply until a new hook waits, and a reply already `sent` is never applied twice |
| 8 | Replies turned off while hooks wait | Every waiting hook is released within 2 s, without output |
| 9 | A typed reply arrives while the session is not `idle` (the owner typed, a turn is running, a dialog shows) | It is refused and starts no turn |
| 10 | The owner types at the Mac while a wake hook waits | The idle item closes as `answered_on_mac` and the hook is released; no watch text is injected into that turn |
| 11 | A woken turn ends | Its `Stop` starts a new wake hook, so a second typed reply works (checked in step 0) |
| 12 | The R3 wait passes | The hook exits without output; the idle item stays open, with no `message` action |
| 13 | A Deny from the watch | Claude sees the deny message above; the item closes as `answered_from_watch` and the reply is `applied` |
| 14 | VS Code or a terminal, where step 0 finds the dialog does not close when the hook decides | That surface takes no replies (the worker gates it by `entrypoint`), and the plan goes back to the owner |
| 15 | More than `MAX_WAITERS` (64) hooks wait at once | The newest exit at once and their prompts stay with the Mac |

## Tasks

Fewest tasks: both share the same files, so they cannot run as parallel lanes.

### Task 1: Allow, Deny and question answers without a channel (split rule: size)

Steps:

1. **Step 0 checks.** Record the results in this plan before any code:
   - In a VS Code session, does a hook's decision close the extension's dialog?
   - In a plain terminal `claude` session, does it close the permission dialog?
     The question dialog is already known to close.
2. Lift the channel requirement from `question_waiter`, `waiter_over`,
   `_actions` and `_deliver` for questions.
3. Add the permission hook, its waiter and its hand-over; add the R1 detail.
4. Install and remove the hook with the question hook.
5. Settings hint, CHANGELOG and docs.

Exit: on a paired Mac, from the watch, allow one Bash call and answer one
question in a Claude Desktop session. Then answer one at the Mac first, and the
watch shows "Answered on the Mac".

### Task 2: typed replies without a channel (split rule: size)

Steps:

1. **Step 0 checks:**
   - Is a wake-hook timeout of R3 plus a margin honored?
   - Does a woken turn's `Stop` start a new hook (row 11)?
   - What happens when the hook exits 2 while a turn is running?
   - What does the woken turn look like in Desktop's and VS Code's UI?
2. Add the wake hook, its registration and the release paths in rows 8–12.
3. Add `message` for idle items without a channel, and close the item when the
   text was taken.
4. Settings and CHANGELOG lines for typed replies.

Exit: from the watch, send one typed reply to an idle Desktop session and see
the turn run. Then type at the Mac while a wake hook waits, and see the reply
refused.

## Review seats

Both tasks are reply paths (security). Each gets the cold diff-only seat and the
silent-failure seat on Opus. The API-surface lens applies to the worker's hook
connections: a socket peer must not be able to take a verdict or text meant for
another prompt.

## Test plan

- `_actions`: each kind, with and without a channel, zero, one or two waiters,
  details on or off, R1(a) and R1(b).
- Waiter pairing: wrong process, session, agent or tool key, or too late, never
  pairs.
- Rows 3–10 and 12–15 as unit tests on `AttentionStore`; row 11 and the dialog
  closing in step 0 and the exit runs.
- Hook entry points: no worker, malformed stdin, a worker refusal, a release:
  each exits within its bound and prints nothing.
- Install and remove: the new hooks appear only while replies are on, beside the
  owner's own hooks, which stay untouched.

## Self-review

1. Every reply kind names what its hook waits for, how long, and how the worker
   hands over the reply: yes (Design).
2. Mac answer first, session end and worker restart each have a required
   outcome: rows 3, 4, 6, 7 and 10.
3. Everything that changes what the owner sees is an owner decision or
   approved copy: R1–R4, the note, the deny message, the Settings hint, the
   CHANGELOG line.
4. Nothing changes on the wire or in OpenTag: the actions and detail fields
   already exist.
5. Each task names its split rule: Task 1 size, Task 2 size (the two together
   would pass the 800-line ceiling).

## Deliberately outside this plan

- Retiring the channel: `openswap claude` keeps working as it does.
- A hook that reconnects after a worker restart (row 7 settles it instead).
- "Always allow" or mode changes from the watch (plan 025: one call only).
- Codex sessions.
