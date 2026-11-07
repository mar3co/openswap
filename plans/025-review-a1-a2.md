# Plan 025: A1/A2 review round (Apple app)

Branch reviewed: `claude/p025-a1-apple-app-4bbbf8` in OpenTag, base 43934c0,
head bd24d98 (4,125 added lines, 45 files under `apps/apple`). Five Opus seats
on 2026-11-06, each given only its part of the diff: cold and silent-failure
on sign-in and tokens, cold and silent-failure on replies, cold on
notifications and UI. No Critical findings.

This is the fix round's brief. An Opus adversarial reader checked the first draft's rows on 2026-11-06, and the rows below are the corrected version. Server fact it confirmed: a repeated `idempotency_key` returns the first reply before any `item_closed`, `replies_off` or `reply_exists` check, so a retry with the same key is always safe. Each row states the required outcome for a
state, not a mechanism. Design the mechanism with the code in front of you,
and add a test per row that would fail without the fix. Where a row's logic
lives in a view or the app model, extract it so it can be unit-tested.

## Important

| # | State | Required outcome |
| --- | --- | --- |
| P1 | A sign-in refresh is in flight and its caller is cancelled (the owner leaves a view, a `.task` ends) | The refresh and the saving of the rotated tokens still finish; a cancelled caller only stops waiting. If saving the rotated tokens fails, the app signs out with a message saying why and never presents the used-up token again |
| P2 | The app or the extension is suspended, killed or at its deadline while a refresh is in flight | No process presents a refresh token whose earlier send has an unknown answer while the sender could still save it; meanwhile the extension shows the generic line and the app waits. Every refresh ends with time left to save before the sender's own deadline. If the answer is lost for good, the owner is signed out with a message that the sign-in was interrupted (until the grace-period issue lands). The app is never killed for holding a lock while suspended |
| P3 | `ASWebAuthenticationSession.start()` returns false | Sign-in ends with a visible error, and the button is usable again |
| P4 | Signing out | A delete that finds nothing counts as signed out (another process may have cleared the tokens). A delete that fails: the app says sign-out did not finish, stays signed in and revokes nothing. When OpenTag ended the sign-in itself, the app shows signed out regardless |
| P5 | The app is deleted and reinstalled | The first real launch after an install starts signed out. A launch before first unlock (a background wake after a reboot) is never taken for a fresh install. Before that first launch, the extension shows only the generic line, never session text from the old sign-in |
| P6 | A reply's POST fails in transport, or its 2xx body cannot be decoded | Transport failure: shown as "may not have been sent". Automatic retries reuse the same key and stop ten minutes after the tap by the device's own clock; after that the app only reads the item. A server reply stands in for this tap only if the app did not know that reply before the tap; tapping the same button again keeps the key. An undecodable 2xx did land: shown as "Sent; status unknown" with no buttons |
| P7 | A reply gets `409 reply_exists` | The app shows the existing reply and follows it. Buttons return only once that reply ends in a state after which OpenTag takes another reply (`item_closed`, `refused`, `expired`) and the item is open |
| P8 | The app has a local reply state and the server's item carries a reply | The newer of the two wins, by OpenTag's reply order. A local state wins only for a tap OpenTag cannot know about yet (sending or queued). An old server `expired` never overrides a new local failure, and an old local final state never hides another device's newer reply |
| P9 | Two sends for one item are in flight (an in-app tap and a watch tap, or the same notification on both devices) | Order is OpenTag's, never a device clock. The losing tap shows the reply that won, as not applied. A late response never replaces a reply OpenTag accepted after it. The same notification tapped on both devices is one reply |
| P10 | A watch reply cannot reach the iPhone at once | The watch says it will send when the iPhone is in reach. A request never goes out more than ten minutes after the tap, whatever the skew between the two clocks. A queued copy with the same key as a reply already sent is that reply, and its state is reported. A queued tap still shows as queued after a watch relaunch, and a newer tap for the same item replaces it |
| P11 | The phone cannot map a request from the watch (blank text, unknown action, a version mismatch) | The watch is told it failed and shows the controls again, or, for a notification tap, the owner gets a notice. It never stays on "Waiting for your iPhone". The watch drops blank text before sending |
| P12 | A watch notification action is tapped and the watch app may be suspended before the phone answers | The tap is stored on the watch when received and delivered once the session is up, on this launch or the next. A duplicate copy that arrives late gets the first copy's state |
| P13 | The watch receives snapshots in any order | The newer snapshot always wins, and the order survives relaunch, reinstall and clock changes. A signed-out snapshot always wins |
| P14 | A newer notification replaces the one the owner is reading (shared collapse ID) at the moment of a tap | The reply is for the item whose content was on screen when tapped, matched without comparing the watch's and the phone's clocks. Option buttons chosen from item A's options never answer item B. Otherwise the tap is not sent, the owner is told it was not sent because the notification changed, and the current item opens |
| P15 | The complication's data is more than about 15 minutes old | It stops showing a number (shows a dash). Age is counted from when the phone fetched the data, not from when the watch received it |
| P16 | A question has zero options, any question lacks options, or `answer` is offered with no stored detail | The phone and the watch both say "Answer this one on the Mac", never an empty or permanently disabled answer form |
| P17 | An open item's latest reply is `pending` | The item is fetched again until that reply is final. A `sent` reply shows "Sent" and is re-read only on normal refreshes |
| P18 | Reply and close wording | `answered_from_watch` reads "Answered from your iPhone or watch". `item_closed` reads "Not applied: already answered or gone", worded from the item's `close_reason` where known. A watch that cannot read the phone's answer says "Sent to your iPhone; status unknown" |
## Minor (ride along in this round)

- `registerDevice` guards on `signInGeneration`, not `refreshGeneration`.
- `kSecAttrAccessible` is set on update as well as add.
- A sign-out seen in `item(id:)` (`AttentionError.signedOut`) signs the
  model out; `becomeSignedOut` logs a failed local sign-out instead of
  dropping it.
- Signing out offline tells the owner the disconnect did not reach OpenTag
  and that Your Macs can disconnect the app.
- Drop the window-less `ASPresentationAnchor()` fallback.
- Tests that cannot fail or pin a bug: `ReplyTests.swift:47-51`, `:201-204`
  (pins the `.rejected` bug), `:237-272` (tests the mock against itself),
  `ItemTests.swift:925` (`inputPreview` fixture), `keepsSameNamedMacsApart`
  (use three or four same-named Macs), `PushDisplayTests.showsTheFetchedItem`,
  and the overclaiming name `lockTimesOutRatherThanRefreshingUnlocked`. Add a
  deliver-once test for the extension.
- `Pushes/watch-question.apns` is labeled as a simulator-only shortcut that
  puts session text in the payload, which real pushes never do.
- `Reply.swift:225-227` doc comment: say what is checked, not "if both
  receive the tap".

## Filed as issues, not in this round

- A short grace period for reusing a just-rotated refresh token, so a lost
  refresh response does not end the whole sign-in. This is a change to
  OpenTag's OAuth server (shared with plan 018), and a security decision.
- A push (or `content-available`) when an item closes, so the complication
  and notifications clear without opening the app. An OpenTag push change.

## Dispositions

(filled in after the fix round)

- 2026-11-06: all 18 rows and the Minors fixed in `bd24d98..5000cb4`
  (p025-a1-apple); its own seats' leftovers are mar3co/opentag#265, and the two issues the
  brief asked for already existed as mar3co/opentag#248 and mar3co/opentag#249.
- Final cold seat on 5000cb4 found one Critical (P14 only half fixed: the
  iPhone's and the watch's static notification buttons answered whatever item
  the notification held when tapped). Reported to the owner and reopened for
  that alone: fixed in `5000cb4..7acff67`. The seat's other findings were
  appended to mar3co/opentag#265.
- Cold seat on 7acff67: no Critical; the P14 fix holds. Its Important findings
  (category races, eviction of on-screen buttons, the watch's buttons
  unproven, quick-reply ordering) are filed as mar3co/opentag#266. A1/A2 is
  review-settled. Watch notification taps (mar3co/opentag#265 item 1): the
  owner ruled on 2026-11-07 to leave the buttons as they are until the device
  check shows where a tap lands (see the plan's Implementation rulings).
