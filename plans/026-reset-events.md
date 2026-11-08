# OpenSwap feature proposal: reset events for people, webhooks and OpenTag routines

Status: ACCEPTED 2026-11-07 with every call in Decisions. Extends kickoff (the 5-hour start timer) and
reuses plan 017's worker protocol, plan 022's pairing and plan 025's local
handoff from other OpenSwap processes to the worker.

## Problem

Every account's quota runs in fixed windows: a 5-hour window and a weekly
(7-day) window. Quota left unused when a window resets is gone. OpenSwap
already knows each account's usage and reset times, and kickoff already
starts the 5-hour window on a schedule. It tells no one else about it. A
person has to look at the menu bar to notice that the weekly window ends in
two hours with 40% left, or that a fresh 5-hour window just opened.

The owner wants that moment to reach:

- **the person**, as a nudge on the Mac or on their iPhone and Apple Watch;
- **OpenTag routines**, so, for example, a Slack agent can start a bug hunt
  or a security review on the account whose quota is about to lapse;
- **the user's own automations** (ntfy, Zapier, n8n, home scripts), through
  a plain webhook.

Success: tokens that would have expired get spent on work the owner chose
ahead of time, and someone who plans their day around 5-hour windows knows
exactly when one starts and ends without watching the menu bar.

## Goals, non-goals, anti-goals

Goals:

- One event model, delivered to any mix of three destination kinds: a Mac
  notification, generic webhook URLs, and OpenTag over the paired worker.
- Each destination chooses its own filter (owner ruling: "flexible").
- Every event carries the numbers a listener needs to decide for itself.

Non-goals:

- OpenSwap does not decide whether there is enough quota for a bug hunt.
  OpenSwap reports the numbers, and the routine or script makes the call.
- No "combined across all accounts" event in this plan (see Decisions).
- No delivery while OpenSwap is not running. The Mac app is the source,
  the same as kickoff.
- No new Windows or Linux support.

Anti-goals (it fails even if it technically works when):

- It floods. A 5-hour "ending soon, 3% left" every five hours across six
  accounts makes people turn it off. The defaults must be quiet.
- An event lies, for example "window started" after a stale fetch, or
  "ending soon" for a window that already reset.
- A webhook leaks account credentials, tokens or session content.
- A webhook receiver can't tell a real OpenSwap call from a forged one.

## Events

Three moments per window, for each account that reports that window (Claude
Code and Codex accounts alike):

| Event | Meaning | When it fires |
| --- | --- | --- |
| `window.started` | A new 5-hour window's clock is running (5-hour only) | The first time a live reading shows a `resets_at` later than the window OpenSwap last knew about. After a kickoff this is the store's next fetch of that account, carrying the real `resets_at`, never an estimate. |
| `window.ending_soon` | The window ends within the lead time and quota is left | Once per window, when `resets_at - now <= lead time` and the remaining percentage is at or above the destination's minimum. |
| `window.reset` | The window ended and its quota is fresh | At the stored `resets_at`, without waiting for a fetch. For the weekly window this is also the start of the next window: weekly limits roll on a fixed 7-day schedule (see `_rolled_weekly_window`, `menubar_display.py`), so `reset` is the only weekly boundary event. A 5-hour window has no schedule, so after its `reset` the next window starts only on the account's next request, and that start is reported as a separate `started`. |

Windows: `five_hour`, `seven_day`. Per-model weekly limits (the usage data's
`scoped` list, for example a Fable weekly cap) get no events of their own.
They ride along in every payload as `scoped_limits` so a routine can see that a
model it needs is already capped.

"Started" is observed, not exact. The 5-hour window starts on the account's
first request, which OpenSwap sees only at its next usage fetch. The event
says when OpenSwap observed it (`observed_at`) as well as the window's
`resets_at`, so the start can be derived as `resets_at - window length`.

**Staleness.** Usage fetches are rationed by the usage store: every
surface, including an explicit refresh, is capped by its serve TTL (180 s) and
per-account poll plans (`snapshot_source.py`, `usage_store.reserve`), so the
extra has no supported way to force a fetch, and adding one would spend the
budget plan 002's pacing protects. Revised 2026-11-07 when planning S1 (this
supersedes the earlier "request one fresh fetch" call): `ending_soon` fires
with the freshest numbers the store has and never forces a fetch. The
payload's `measured_at` says how old they are. `started` fires only from a
live reading (never last-known-good), at the store's next natural fetch.

Each `(account, window, resets_at, event)` fires at most once. That must
survive an app restart: a restart must not re-send `ending_soon` for the
same window. A small state file under the backup root records what has fired.

### Payload

The same JSON body goes to every destination kind:

```json
{
  "id": "evt_…",                 // derived from (provider, account, window, resets_at, type); see below
  "type": "window.ending_soon",
  "provider": "claude",          // or "codex"
  "account": { "label": "Work", "email": "a@example.com", "org": "Acme" },
  "window": "seven_day",
  "resets_at": "2026-11-09T17:00:00Z",
  "seconds_until_reset": 7200,
  "used_pct": 61.0,
  "remaining_pct": 39.0,
  "is_active_account": false,
  "scoped_limits": [ { "name": "Fable", "used_pct": 100.0, "resets_at": "…" } ],
  "measured_at": "2026-11-09T14:58:40Z",   // when the usage numbers were fetched
  "observed_at": "2026-11-09T15:00:04Z",   // when OpenSwap fired the event
  "mac": "Yohan's MacBook Pro"
}
```

**One moment, one `id`, across Macs.** An owner can run OpenSwap on two Macs
that share accounts. Each Mac fires on its own, but the `id` is a hash of
`(provider, account identity, window, resets_at, type)`, so both Macs send the
same `id` for the same moment. Webhook receivers drop repeats by `id` (the
docs say so), and OpenTag drops repeats per owner before it fires a routine.
A routine never starts twice because two Macs saw the same reset.

Email and org are included because a routine needs to know which account to
target. Nothing else about the account is sent: no tokens, no credential
paths, no session content.

## Destinations

Settings → Automation gains a **Reset events** section beside kickoff. It
lists destinations, and each destination has the same filter:

- **Accounts:** all, or a chosen set.
- **Windows:** 5-hour, weekly, or both.
- **Events:** any of started, ending soon, reset.
- **Ending-soon lead time:** default 30 minutes for 5-hour, 3 hours for weekly.
- **Ending-soon minimum left:** default 20%. Below that, `ending_soon` does
  not fire for this destination.

Three destination kinds:

1. **Mac notification.** Uses the existing `_notify` path and
   `NotificationCopy` style (for example, "Work: weekly ends in 3h, 39% left").
   Defaults to off.
2. **Webhook.** URL plus a filter; any number of them. Delivery is an HTTPS
   `POST` of the payload. `http://` is allowed only for `localhost`, so a
   home script on the same Mac works without TLS. Each webhook gets a
   generated secret; every request carries `OpenSwap-Signature:
   t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<body>">` so receivers can verify it
   and reject replays. Retry: three attempts with backoff over about two
   minutes, then drop and record the last failure in the destination's row.
   A delivery failure never blocks or delays other destinations or the app.
   The secret lives where OpenSwap keeps its other local secrets, not in the
   plain settings file.
3. **OpenTag.** Available when the Mac is paired. The menu bar hands each
   event to the local worker, and the worker sends it to OpenTag over a new
   optional protocol extension (`"reset_events"` capability), the same
   additive pattern as `"attention"`: a new `"reset_events"` string in the
   `capabilities` operation's `features` array (`docs/worker-protocol.md`).
   A backend that doesn't advertise it never receives events. Delivery
   follows the webhook rule: a short retry while the worker is connected,
   then drop. No queue is kept across a disconnect, because a late
   `ending_soon` is worse than none. Because the worker is already
   authenticated, OpenTag knows which Mac and owner an event belongs to.

What OpenTag does with an event is OpenTag's side (separate phase, private
repo `mar3co/opentag`):

- make each event type available as a **routine trigger**, filterable by
  account, window and remaining percentage;
- optionally push it to the iPhone and Apple Watch app from plan 025 (the
  push carries an ID and a generic line, the same data boundary as plan 025);
- a routine can send work back through the existing remote-tasks path
  (plan 017) with the per-job account choice extension, so the bug hunt runs
  on the account whose quota is ending.

The OpenTag destination has the same filter as the others. OpenTag may filter
further per routine.

A matching CLI covers headless use and testing:

- `openswap events list`
- `openswap events add-webhook <url> [filter flags]`
- `openswap events remove <id>`
- `openswap events test <id>`, which sends a sample event marked `"test": true`.

## Decisions

Owner rulings (2026-11-07):

- Support **both** OpenTag (product to product, over the paired worker) and
  generic webhooks for the user's own automations.
- **Flexible** scope: per-destination choice of accounts, windows and events.
- **Flexible** person notification: Mac banner and/or iPhone and Watch
  through OpenTag, each a destination the owner turns on.
- This is an extension of the kickoff feature.

Calls made in design (owner may override):

- Events are always per account. No combined event: a listener that receives
  all accounts can add them up, and a combined "pool reset" would have to
  invent one reset time across accounts that each reset at different times.
- Only `ending_soon` has a minimum-remaining filter. `started` and `reset`
  are rare enough not to need one.
- Webhooks are signed with HMAC, `localhost`-only `http`, and use bounded
  retries with no persistent queue. Events are time-sensitive, so an
  `ending_soon` delivered an hour late is worse than none.
- Mac notification defaults to off, so nothing new appears until the owner
  turns it on.
- No weekly `started`. Weekly `reset` is that moment, and firing both would
  double every weekly event.
- `started` after kickoff waits for the store's next natural fetch, so it
  carries the real `resets_at` (an estimate would not match the fetched value
  and would fire twice). No forced fetch (see Staleness).
- `ending_soon` uses the freshest stored numbers, and every payload says when
  they were measured.
- Deterministic event `id` across Macs, with OpenTag deduping per owner.
- Per-model weekly limits are payload context, not events.
- OpenTag delivery has the same no-queue rule as webhooks.

## Left to the builder

- Where the scheduler lives in the menu bar process (next to `_maybe_kickoff`),
  and how it times `ending_soon` and `reset` between usage fetches.
- Exact Settings UI layout, within the existing Automation page style.
- The menu bar → worker handoff mechanism. Reuse plan 025's local IPC if it
  fits.
- The protocol extension's exact operation and field names. Document them in
  `docs/worker-protocol.md`.
- Phasing. Suggested order: S1, the event engine + Mac notification + webhooks +
  CLI (no OpenTag dependency); S2, the worker extension; O1, OpenTag triggers
  and push.

## Test focus

- Once-only firing across restarts, a stale cache, and a missed reset while
  the Mac slept (wake after `resets_at`: fire `reset`, never a late
  `ending_soon`).
- `started` must not fire from a stale last-known-good entry.
- Filter matching per destination.
- Signature format against a reference verifier.
- A failing or hanging webhook can't stall other destinations or the UI.
