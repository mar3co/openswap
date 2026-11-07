# Plan 025: O1/O2 review round (OpenTag)

Branch reviewed: `claude/p025-o1-attention-34f6dc` in OpenTag, base 43934c0,
head 8aaaea5 (3,668 added lines, 28 files). Seven Opus seats on 2026-11-06,
each given only its part of the diff: cold and silent-failure on SQL, on auth
and the app API, and on the worker protocol; cold on push and the portal list.
No Critical findings.

This is the fix round's brief. An Opus adversarial reader checked the first draft's rows on 2026-11-06, and the rows below are the corrected version. Each row states the required outcome for a
state, not a mechanism: design the mechanism with the code in front of you, and
add a test per row that would fail without the fix. Severity is the
controller's adjudication on the Critical / Important / Minor scale.

## Important

| # | State | Required outcome |
| --- | --- | --- |
| R1 | A reply has not been handed to the Mac when its item closes, for any reason (a reason close, an `unknown` close, a purge) | At that moment the reply becomes final `item_closed`, whether or not any device has read it. It is never delivered, even if the item reopens, and it never blocks a new reply after a reopen |
| R1b | A reply whose sign-in ended before delivery (sign-out, or leaving the sign-in's workspace) | Becomes final at once, in a documented state the app understands, and does not block a reply from the new sign-in |
| R2 | A Mac whose owner is no longer a member of that Mac's workspace sends `attention` reports | Nothing is stored, reopened, re-reasoned (overrides R11) or switch-updated. The answer is named in the docs, so the Mac neither retries forever nor turns attention off. After the owner rejoins, reports work as usual, and an item still listed reopens without a second push. The check is per worker: a Mac paired in two workspaces keeps working in the one the owner still belongs to |
| R3 | A heartbeat carries `attention_acks`, from any registration of that worker (including one that has not yet re-sent `capabilities`) | The acks are applied (they are scoped to the worker). Only the delivery of `attention_replies` waits for the advertised registration. Acks never decide whether a heartbeat counts as a beat: repeated IDs, unknown reply IDs and unknown outcome values (a newer Mac) never cause a refusal |
| R4 | Several acks arrive, in one heartbeat or across several | The result equals applying them one at a time in order (`sent` then `applied` ends `applied`; `applied` then `sent` also ends `applied`). A reply acked in a heartbeat is never in that heartbeat's response. The docs say acks go to the worker pairing that delivered the reply, and that the Mac records a reply ID durably before applying it and acks a known ID rather than applying it again |
| R5 | Detail text holds DEL or a C1 control character (U+007F–U+009F) | OpenTag refuses it, as the contract says. The docs require the Mac to replace disallowed control characters before sending, so one character never blocks a whole report |
| R6 | APNs answers `400 DeviceTokenNotForTopic` | The device is not deleted, and the answer is logged as a deployment error that counts as neither received nor dead. A device is deleted only on 410, `BadDeviceToken` or `Unregistered`. If the app registers tokens under more than one bundle ID, each device carries its topic: a topic outside the deployment's allowed list is refused at registration, and an older app build that sends none gets the iPhone topic |
| R7 | One push goes to several devices, and some fail transiently while none receive it (dead tokens may be among them) | The push is claimable again later while the item is fresh. When every device was dead, the push stays unclaimed while fresh, so a device registered within ten minutes still gets it. Every retry re-checks that the item is still open and that notices are on |
| R8 | A sending or claiming step's database call returns `{error}` or throws (drop device, release push) | The failure is logged with a fixed message and counted, the claim is not silently kept, and the other in-flight sends finish before the connection closes |
| R9 | The cron run spends most of its time on worker retention, or worker retention throws | The attention purge and push delivery run in full whatever happens to worker retention. A claim not completed because a run was killed (cron or the worker route's `after()`) is claimable again while the item is fresh |
| R10 | Any question lacks stored options (zero options, no detail, or detail purged after 24 hours) and the item offers `answer` | `answer` is never offered unless every question is stored and has at least one option. Refuse it; do not accept free text |
| R11 | An item was closed as `unknown` and its real close reason arrives later, from an owner who is a member | The real reason replaces `unknown`. `closed_at` and its 7-day clock keep the first close. A reply already final `item_closed` stays so |
| R12 | The owner turns off "notices" or "replies" on the Mac | Notices off: no push for that Mac's items. An item first reported while notices were off never pushes, even if notices come back on while it is fresh. Replies off: a reply not yet handed out becomes final `refused` at that moment and is never delivered, even if replies come back on. A reply already handed out is never handed out again |
| R13 | Settings → Connected assistants shows the OpenTag app's grant with `attention:reply` | It says the app can answer sessions that need the owner, not only see them |
| R14 | The owner signs in to the app from the consent page | The page names the workspace the sign-in is bound to, and says leaving it signs the app out for the Macs in every workspace, and rejoining does not sign it back in. Correct the code comment that says otherwise |
## Minor (ride along in this round)

- An `answer` reply for an item with no stored questions (detail purged, or
  none sent) is bounds-checked or refused rather than forwarded unchecked.
- `'1.0'::jsonb` positions are cast safely in SQL.
- Items with no registered device are not claimed (`pushed_at` stays null),
  so a device registered within ten minutes still gets the push.
- Items of a Mac that never re-sends `capabilities` are still purged.
- Purge and report lock a worker's items in the same order.
- Tests that cannot fail: `session_attention_replies_test.sql:110` (set
  `last_seen_at` null first), `session_attention_test.sql:168` (use an
  unpushed item), `worker-protocol.test.ts:234` (compare with a
  `structuredClone` taken before the call), `mcp-oauth.test.ts:538` (assert
  no SQL call). Rename the stale "no actions before O2" test.
- `attention-api.ts:440`: a non-string `error` in a SQL answer becomes 503.
- `ATTENTION_KIND_LABELS`: an unknown kind gets a generic label.
- `packages/core/src/mcp-oauth.ts`: make `kind` required.
- `docs/attention-api.md`: "refused before the token is validated".
- `/oauth/apple/callback`: the same `Referrer-Policy: no-referrer` and frame
  headers as `/oauth/authorize`, and `code` scrubbed from Sentry URLs.
- AASA: drop `applinks` for the callback unless the app needs it. If it does,
  document that the app ignores callback URLs that arrive outside an active
  sign-in.
- APNs: drop the cached provider token only on `ExpiredProviderToken`;
  `apns-expiration` matches the ten-minute claim window; the cron logs purge
  counts and the error code.
- The "Join a workspace first" notice has wording for the app's sign-in.

## Not taken

- `register_device` moving a token between owners (auth silent, outside its
  diff): only reachable with the victim's device token, which no API exposes.
  Filed as an issue rather than fixed in this round.
- Duplicate alert sound after an APNs timeout (push cold minor 5): accepted;
  the collapse ID replaces the banner. Document it in the runbook.
- The portal list going stale until reload (push cold minor 8): the copy says
  "Refresh". Accepted.

## Dispositions

- 2026-11-06: all 15 Important rows and the Minor list fixed in one commit,
  `8aaaea5..5b16184` (p025-o1-opentag). R2's refused report answers HTTP 403
  `forbidden`. The session's own cold seat filed mar3co/opentag#260–mar3co/opentag#262.
- Final cold seat on the fix diff (one seat, per the review budget): no
  Critical. Its Important (Sentry Replay still records the callback code) is
  mar3co/opentag#260, whose body now carries the second review's details. Its four Minors
  are mar3co/opentag#264. No further round: O1/O2 is review-settled.
