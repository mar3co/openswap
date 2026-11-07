# Plan 025: S2 review round (OpenSwap replies)

Branch reviewed: `claude/p025-s2-attention-replies-52d693`, on S1's tip
1b4264e, head 4fb9349 (about 1,800 lines of code plus 1,200 of tests). Four
Opus seats on 2026-11-06, each given only its part of the diff: cold and
silent-failure on the channel, registration, launcher and question hook, and
on the worker's reply handling, reference server and doc.

One **Critical** (Q1). This fix round also carries S1's round
(`plans/025-review-s1.md`), on this branch, so one implementer owns the
worker files.

An Opus adversarial reader checked the first draft's rows on 2026-11-06; the rows below are the corrected version. Every correction was toward refusing, so no re-pass was run. Each row states the required outcome for a state, not a mechanism. Add a
test per row that would fail without the fix.

## Critical

| # | State | Required outcome |
| --- | --- | --- |
| Q1 | A report goes over its size budget and drops some items' `detail`, or a reply arrives for an action a later report stopped offering | No item is ever reported with `allow` or `answer` unless the detail that shows its call or its questions goes out in the same report. A reply is accepted only for an action that the last report the service accepted offered for that item, together with its detail |
## Important

| # | State | Required outcome |
| --- | --- | --- |
| Q2 | "Send details" is off, or any question or option would be cut or dropped (past `MAX_OPTIONS`, a label that cleans to nothing, text past the cap) | `answer` is offered only when every question and every option is sent whole; otherwise the item is notice-only (the same rule as Allow) |
| Q3 | A watch message arrives for an idle item while the session is no longer idle (the owner typed and the async `UserPromptSubmit` has not landed, a background-task turn runs, or a dialog waits), or a turn ended since the item opened | The message is never typed into a session that is not idle. A turn that ends while the session stays idle opens a new item (new ID); a reply to the old item gets `item_closed` and the new item takes messages. Otherwise a message for a non-idle session is answered `refused` |
| Q4 | A reply was marked for delivery but never handed over (the question hook left or timed out, the waiter ended, the send failed), or its hand-over is unknown after a worker crash | Never acknowledged `sent` unless it was handed over. A known non-hand-over settles as `item_closed` or `refused`, is queued for acknowledgment, and the item takes replies again if still open. When hand-over is unknown after a restart, the reply is never reported `refused` or `item_closed` (the owner must never be prompted to retry something that may have landed), and its item takes no further reply until it closes |
| Q5 | Saving the record of handled reply IDs fails, or the record cannot be read at start | A reply is not delivered when its handled record cannot be saved; it is answered `refused`. When the record cannot be read at start, replies issued before that start are refused, never delivered. A reply is never applied twice across a restart |
| Q6 | A heartbeat is refused with `invalid_request` while acks are queued | Liveness and reply delivery never wait on refused acks. The refused batch is retried with backoff, not on every heartbeat; only entries the service refuses are given up, and they are logged |
| Q7 | Any process other than the channel Claude Code itself started from its MCP config tries to register as the session's channel (another stdio MCP server, a Bash `exec`, including one faking the command line) | Only the channel Claude Code started holds the registration. If more than one candidate appears, or it cannot be told which is real, the session gets no replies and status says why. The real channel retries every few seconds while refused |
| Q8 | The session's channel restarts (`/mcp` → Reconnect, or it died) | A new channel takes over only once the holder's process (pid and start time) is gone, and replies resume only if a live check shows Claude Code still honors a reconnected channel; until that is shown, the item offers no reply actions after a reconnect |
| Q9 | Any process other than the channel's own Claude process is seen for the session after the item opened (a hook with another pid, another session file, pid 0, a headless `--resume`) | No reply is delivered; the item offers no reply actions. This is checked at report time and again at delivery |
| Q10 | A permission verdict | It goes only to the channel of the process that relayed that request, and only while that process still holds the session |
| Q11 | An answer for a question | It reaches a waiter only when exactly one waiter exists for that item's own occurrence and that waiter is the session's synchronous hook; otherwise the item offers no `answer`. Waiters are capped per Claude process |
## Minor (ride along)

- The question waiter's registration is documented like the channel's caveat
  (a command the session runs can register as a waiter; it gains no
  approval), or limited to the session's own synchronous hook command line.
- Channel sends to the worker get a short timeout, so a stalled worker never
  stops the channel answering Claude Code.
- `ps` runs with `LC_ALL=C` so `lstart` parses the same in every locale.
- The malformed-reply branch queues its `refused` ack again like the valid
  branch.
- The heartbeat thread takes a small lock of its own for acks and incoming
  replies, never the store lock held across disk writes.
- `test_attention_replies.py:176`: use a captured nested preview, or drop the
  claim that the shapes were copied from real messages.
- Remove the repeated `runs_from_temporary_location()` check.

## Dispositions

- 2026-11-07: every row and Minor, including the Critical Q1, is fixed on `claude/p025-s-fixes-ed1ac5` in 0e7211b, together with the review fixes for both rounds. Each row has a test; no row was disputed.
- Q8 was shown live on Claude Code 2.1.311: after `/mcp` → Reconnect, the new channel is relayed to and obeyed. Replies therefore resume once the old channel's process is gone, instead of failing closed.
- Q3: a background turn that ends while the session stays idle opens a new item, so the owner gets a second push.
- Q7 residual: a faker that registers before the real channel ever connects still holds the registration. It can read messages but cannot approve anything. Tracked in mar3co/openswap#181.
- Beyond the brief: an HTTP error with a non-JSON body now keeps its real status as `service_unavailable`, which matches the protocol doc.
- Final cold seat on the fix diff (worker and replies area): no Critical. Four Important findings and the leftover Minors are tracked in mar3co/openswap#183. Every finding fails safe.
- mar3co/openswap#178 and mar3co/openswap#179 are fixed on that branch; close them at merge. S2 is review-settled.
