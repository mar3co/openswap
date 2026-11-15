# Replies to Claude Desktop sessions from the phone or watch (plan 025 spike)

Followed by [plan 028](../028-desktop-replies.md). The owner chose to offer typed
replies too (ruling R2, 2026-11-15), so recommendation 3 below is superseded.

2026-11-15. Claude Desktop (Claude Code 2.1.325 bundled in the "Claude VS Personal"
profile), OpenSwap at `8fdd5c8`. No shipping code changed.

## The question

Replies (Allow/Deny, answering a question, a typed reply to an idle session) work
only for sessions started with `openswap claude`, which loads OpenSwap's MCP
channel. Desktop starts Claude Code itself (`--input-format stream-json
--output-format stream-json --permission-prompt-tool=stdio
--setting-sources=user,project,local`, no `--channels`), so
`AttentionStore._actions` offers nothing for Desktop sessions. Can hooks, which do
run in Desktop sessions, carry the replies instead?

## Answers

| Reply | Works for Desktop sessions? | How |
|---|---|---|
| Allow / Deny a permission prompt | **Yes** | a synchronous `PermissionRequest` hook returns the decision |
| Answer an `AskUserQuestion` | **Yes** | the same, with `updatedInput.answers` (OpenSwap's question hook already does this) |
| Typed reply to an idle session | **Yes, with caveats** | a `Stop` hook with `asyncRewake: true` exits 2 with the text |

A waiting hook never holds back Desktop's dialog: Claude Code shows the prompt and
runs the hook at the same time, and whichever answers first wins.

## What was run and what happened

Two setups:

- **Headless:** a script that plays Desktop's part. It starts Desktop's own
  bundled `claude` binary with Desktop's flags, sends `initialize` and a prompt, and
  logs every `control_request` (`can_use_tool`), `control_cancel_request` and tool
  result with timestamps. The test hook, in the scratch folder's
  `.claude/settings.json`, sleeps N seconds and then prints a decision or nothing.
- **Real Desktop:** throwaway side sessions in this repository ("Hook probe", 2
  and 3), in Manual mode. The test hooks went into the main checkout's untracked
  `.claude/settings.local.json`, because Desktop reads a worktree session's local
  settings from there (`--project-config-root /Users/yohan/GitHub/opensoft/openswap`).
  They were filtered to commands containing `probe-d` and were removed afterward.
  The evidence is Desktop's own log (`~/Library/Logs/Claude/main.log`), the hook's
  timestamped log, and the sessions' transcripts.

### 1. Permission prompts

Headless (times in seconds from start):

| Run | Hook | Desktop's part | What happened |
|---|---|---|---|
| A | denies at once | never answers | `can_use_tool` sent at 3.40, `control_cancel_request` at 3.44, tool result "probe: denied by hook" |
| B | waits 10 s, allows | never answers | `can_use_tool` at 1.70 (hook running), cancel at 11.75, command ran |
| C | waits 10 s, allows | denies at 3 s | host's deny applied at 4.65; the hook's later allow was not used |
| E | waits 10 s, says nothing | allows at 14 s | the prompt stayed with the host; host's allow applied |

Real Desktop:

- **Probe 2, Mac answers first.** The hook started its 30 s wait at 12:00:01. In
  the same second Desktop logged `Emitted tool permission request 4eacc77c… for
  Bash`, so the dialog showed while the hook waited. The owner clicked "Always
  allow" at 12:00:22 and the command ran. The hook was **not** killed: it finished
  at 12:00:31, and Claude Code ignored its deny.
- **Probe 3, hook answers first.** Request `4a8c137b…` was emitted at 12:01:36 and
  the hook started at the same time. The hook denied at 12:01:56, and in that same
  second Desktop logged `Permission request 4a8c137b… for Bash aborted`. The
  session reported "The command was refused: `probe: denied by hook`", and the
  directory was never created.

Why the dialog closes: in Desktop's `app.asar`, the `canUseTool` callback registers
an abort listener. Claude Code's `control_cancel_request` aborts it, and Desktop
then deletes the pending permission and emits `tool_permission_resolved` to the
UI, which takes the dialog down. The same "aborted" line already appears several
times in this Mac's logs for `AskUserQuestion` (11-09 to 11-14), so the path runs
routinely.

### 2. `AskUserQuestion`

Headless run D: the hook waited 5 s, then returned `behavior: allow` with
`updatedInput: {…, answers: {"Pick a color": "Blue"}}`. Claude Code sent
`control_cancel_request` to the host, and the tool result was `Your questions have
been answered: "Pick a color"="Blue"`. Desktop handles a question's cancel the same
way as a permission's (the abort path above). No live Desktop question was
answered by a hook in this spike; the owner's auto-mode check refused putting an
auto-answering hook into the shared settings file.

OpenSwap's question hook (`openswap attention question`, synchronous, 600 s) is
**already installed** in the Desktop profile's Code folder `settings.json`. It
gives up at once in Desktop sessions only because `question_waiter` finds no
registered channel.

### 3. Typed reply to an idle session

- **Claude Code `asyncRewake` hooks.** Claude Code 2.1.325's settings schema
  says: "If true, hook runs in background and wakes the model on exit code 2
  (blocking error). Implies async."
  - Headless: a `Stop` hook with `asyncRewake: true` waited 8 s and then exited 2
    with a message on stderr. The first turn ended at 3.2 s; at 13.8 s Claude Code
    started a second turn by itself and the model followed the text.
  - **Real Desktop, probe 3:** the first turn ended at 12:02:01 and the session went
    idle. At 12:02:16 the hook exited 2 with "Test reply from the owner's watch:
    answer with the single word PINEAPPLE…". Desktop logged a new turn, and the
    transcript shows a `<task-notification>` "Stop hook feedback" with a
    system-reminder `Stop hook blocking error from command "Stop": <text>`, followed
    by the assistant's "PINEAPPLE".
- **Desktop deep links don't work for this.** `claude://code/new?q=…&folder=…`
  opens a *new* session with a prompt, and `claude://claude.ai/epitaxy/<id>` opens
  an existing session. No link sends a message into an existing session.
- **Remote Control.** Desktop turns it on for each new session ("Enabling remote
  control for session …" in the log), so the Claude iPhone app can already type
  into Desktop sessions. That is Anthropic's path and has no API OpenSwap can use.

So the expectation "nothing but Remote Control" is **refuted**: an `asyncRewake`
Stop hook can start a turn in an idle Desktop session. The text arrives as hook
feedback, though, not as a user message.

## Smallest change per reply, and risks

The common idea: where a session has no live channel, a waiting synchronous hook
takes the channel's place. Each hook process belongs to exactly one prompt (one
tool call, one Stop), so a reply cannot land on the wrong prompt.

### Questions (smallest; worker only)

- `question_waiter` gets the session's config home from the Claude Code
  process (the home its `sessions/{pid}.json` is in) instead of from
  `self._channels`, and drops the channel requirement.
  - Desktop processes do write that file: the profile's `code/sessions/*.json` has
    `entrypoint: claude-desktop` and `status`.
- `waiter_over` ends the wait on the session leaving `waiting` instead of on
  "channel went away".
- `_actions`: an item with no live channel may still take `answer` when exactly
  one waiter waits for it (the rule that already applies).
- `_deliver` (`answer`) reads the record from the waiter's home, not `held.home`.

### Permissions

- Add a synchronous `PermissionRequest` hook for every tool except
  `AskUserQuestion` (`openswap attention permission`), installed only while replies
  are on, like the question hook. It waits on the worker the same way and prints
  `{"behavior": "allow"|"deny"}`.
- The worker pairs it with the item as `_waits_for` does for questions (same
  process, session, agent, tool key, within `PAIR_SECONDS`). That replaces
  `_attached` / channel `request_id` pairing for sessions with no channel.
- `_actions` offers `allow`/`deny` (Allow only while details are on, unchanged)
  when exactly one such waiter waits. `_deliver` sets the waiter's verdict
  instead of `link.send(verdict)`.
- Sessions that do have a channel keep the channel path. The hook exits at once
  there, so nothing is answered twice.

### Typed replies

- Add a `Stop` hook with `asyncRewake: true` (`openswap attention wake`) that
  waits on the worker for a message for its session. It exits 2 with the text on
  stderr, or exits 0 when released.
- `_actions` offers `message` on an idle item only while such a hook waits for
  that session. `_deliver` hands the text to it.
- Closing logic needs a new signal: the woken turn is not a `UserPromptSubmit`
  with `<channel source="openswap"`. Use the hand-over itself plus the next
  `Stop`.

### Risks

- **Mac and watch both answer.** Whichever reaches Claude Code first wins, and
  the later answer is ignored (probe 2, run C). The hook is *not* killed when the
  Mac answers, so the worker must release it when the session leaves `waiting`.
  `waiter_over` already does this for questions. A watch reply that arrives after
  the Mac answer can only be ignored, never applied to another prompt. The outcome
  OpenSwap reports still needs the existing `CONFIRM_SECONDS` attribution.
- **Timeouts.** When the hook's 600 s timeout passes, the dialog simply stays
  on the Mac, which is unchanged behavior. Each waiting hook is one OpenSwap
  process. Permission prompts are more frequent than questions, so this means more
  processes (capped by `MAX_WAITERS` / `MAX_WAITERS_PER_PROCESS`).
  - An idle session's wake hook waits for as long as replies should be possible
    (minutes to hours). Check how long a hook `timeout` Claude Code accepts.
- **Typed-reply framing.** The model sees "Stop hook blocking error from command
  …: <text>" inside a task notification, not a user turn. It complied in both runs,
  but an instruction framed as hook output may get less trust than the owner's own
  words.
  - The custom-prefix and summary fields that would fix the framing are marked
    `@internal` in the schema.
  - Whether `asyncRewake` is in Claude Code's public hooks docs was not checked.
- **A reply racing a turn.** If the owner types in Desktop while a wake hook
  waits, a later exit 2 would inject text into that turn. `_deliver` already
  refuses unless the session file says `idle`; the remaining window is the same one
  the channel has.
- **Existing channel path.** It is unchanged. A session with a channel takes
  channel replies; one without takes hook replies; never both.
- **Unverified:**
  - In Desktop, a session file's `status` going `waiting` during a prompt (seen
    only as `busy` here).
  - How a woken turn looks in Desktop's UI, beyond the transcript.
  - A hook-answered permission in a terminal `claude` session. The question hook
    already relies on this, and it would also give plain `claude` and VS Code
    sessions replies.

## Recommendation

1. **Do questions first.** The hook is already installed, and the change is in
   the worker only: `question_waiter`, `waiter_over`, `_actions`, `_deliver`.
2. **Then permissions** with a waiting `PermissionRequest` hook, as above. The
   evidence that Desktop closes its dialog is direct (probe 3 plus the app's
   code).
3. **Hold typed replies for an owner decision.** They work, but the text reaches
   Claude as hook feedback rather than as the owner's message, and they keep one
   process waiting per idle session. The Claude iPhone app's Remote Control
   already covers typed replies to Desktop sessions.

Decisions for the owner:

- Should the phone and watch answer Desktop prompts through a small OpenSwap
  process that waits beside each open prompt? The Mac's own dialog still shows
  and still works, and whichever answer comes first wins.
- Is it acceptable for a typed reply from the watch to reach Claude as a note
  from a hook ("Stop hook feedback: …") rather than as a message from you?
