# OpenSwap feature proposal: pair a Mac by signing in to OpenTag

Status: DONE 2026-10-16 (accepted with every default). S1 OpenSwap #118, S2 OpenSwap #119,
O1 OpenTag #185, O2 OpenTag #186. OpenTag's migrations and connect page are merged but
not yet pushed or deployed. Depends on plan 017 (pairing, worker protocol)
and reuses parts of plan 018 (OpenTag's sign-in and consent pages). Facts about
OpenTag were read from `mar3co/opentag` `main` at `9f350ab`. Recheck them
before phase O2, because migrations that are already merged cannot be edited.

## Problem

Pairing a Mac with OpenTag takes two chores today.

1. **First pairing.** The owner opens the portal's Workers page and selects
   **Pair your Mac**. They copy `openswap worker pair <origin> <code>`, which
   carries a one-use `pair_…` code that is valid for 10 minutes. Then they
   paste it into Terminal or into the menu bar's **Set up Remote tasks…**
   dialog. Each Mac needs another trip to the portal and a fresh code. A code
   that dies while the owner switches windows means starting again.
2. **Renewal every 30 days.** Device keys expire 30 days after enrollment
   (`remote_workers.key_expires_at`). To renew, the owner opens **Re-pair this
   Mac** in the portal, runs `openswap worker unpair` on the Mac, makes a
   renewal code and pairs again. The worker ID and jobs survive. Still, every
   paired Mac stops taking tasks once a month until the owner notices and
   repeats the steps. This is the worse of the two.

Neither chore adds security. The Mac already proves it holds a live key on
every request. The browser session already proves who the owner is. The
copy-paste step only carries a code from one to the other by hand.

## Recommendation

Do two independent things:

1. **In-place key rotation (phases O1, S1).** Add an optional protocol
   operation, `rotate_key`. A worker that still holds a live key swaps it for
   a new one with a new 30-day expiry, and keeps its worker ID. The worker
   does this by itself when 7 days or fewer remain. A Mac that stays online
   never expires again. Ship this first: it removes the monthly chore with no
   UI at all.
2. **Connect to OpenTag (phases O2, S2).** Add **Connect to OpenTag…** to the
   menu bar, and `openswap worker connect` to the CLI. The Mac opens the
   browser on a portal page. The owner signs in if needed, picks the workspace
   and selects **Pair this Mac**. The browser then returns to the Mac on a
   loopback address. The Mac redeems the result for a device key through the
   existing `pair` operation, then continues with the guided setup as today.
   The same button reconnects a Mac whose key has expired, which turns today's
   unpair-and-re-pair into one click.

The pasted command keeps working everywhere. It stays the way to pair over SSH,
from a Mac without a browser, and with the reference server, which has no
human sign-in.

### Why not a full OAuth client, or a device code

OpenTag already runs an OAuth 2.1 server for MCP connectors (plan 018,
mar3co/opentag#178). Making OpenSwap one more OAuth client of it looks natural
but fits badly:

- Its tokens are bound to the `/mcp` resource (RFC 8707) and appear under
  **Connected assistants**. A device key is neither: it is a worker protocol
  credential with its own expiry, revocation and audit (`remote_workers`).
- Redirect URIs must match exactly, in the authorize check and in both SQL
  functions. A native app listening on an ephemeral port would have to
  re-register on every launch, and dynamic client registration purges clients
  that never connected after a day.
- The Mac would still need a second call to swap an OAuth token for a device
  key, so OAuth adds a token type without removing a step.

Instead, phase O2 reuses only the **shape** of RFC 8252 (an authorization code
with PKCE, returned to a loopback redirect). The code it returns is an ordinary
pair code, bound to the PKCE challenge, and the Mac redeems it through
`/v1/pair`. Codes, renewal codes, audit and expiry all stay where plan 017 put
them.

A device code (RFC 8628: the Mac shows a short code and the owner types it
into the portal) is rejected because it can be phished. An attacker can start
the flow on their own machine and persuade the owner to approve the attacker's
code, and the attacker's machine then becomes a worker in the owner's
workspace. With a loopback redirect the result can only reach the machine that
started the flow.

## Owner decisions this plan keeps

- **Only the Mac approves its own enrollment.** Today, running the pair
  command on the Mac is the approval and no network caller can pair a Mac.
  After this plan the approval is two steps, both by the owner: starting
  Connect on the Mac, and then selecting **Pair this Mac** in a browser that
  hands the result back to that Mac's loopback listener. Nothing on the
  network can finish the flow without the PKCE verifier, which never leaves
  the Mac.
- **Setup choices stay on the Mac.** The portal page pairs and nothing more.
  Folders, direct mode, the account and the session permission override are
  still chosen by the guided setup on the Mac (plan 017's "Folders are where
  sessions work" and "Sessions follow the account's own permission
  settings").
- **OpenSwap never handles the owner's OpenTag password or session.** The
  owner signs in inside their own browser. The Mac sees only a pair code and
  then its device key.
- **Keys stay in the login Keychain only,** under service `openswap` with the
  account `worker-device:<sha256(url)>` (`openswap.worker.pairing`).

## Phase O1 (OpenTag): `rotate_key`

**Protocol.** Add an optional operation, used only when the backend accepts the
`key_rotation` feature in `capabilities`:

| Operation | Request fields | Success response |
| --- | --- | --- |
| `rotate_key` | `worker_epoch` | `worker_id`, `device_key`, `expires_at` |

The name is chosen so it is never confused with `renew`, which renews a job
lease.

**Server rules.**

- **Who may call.** The call is authenticated with the current key, and only
  while that key is live. `revoked` and `device_expired` are refused exactly as
  for any other operation, so a revoked or expired Mac can never rotate. A
  `stale_epoch` is refused like any other mutation.
- **Lost responses.** Rotation must survive a response that never arrives,
  because a Mac whose new key was lost in transit would otherwise be locked
  out. The server stores the new hash as `pending_key_hash`, with its own
  expiry. The old key stays valid until the first request that authenticates
  with the pending key. That request makes the pending key current and drops
  the old one. A second `rotate_key` sent with the old key replaces the
  pending key, which covers a retry after a lost response. No more than one
  pending key exists per worker.
- **Requests already in flight.** The worker's heartbeat, claim-renewal and
  synchronization threads share one transport. A request sent with the old key
  can therefore still be in flight when another thread's request promotes the
  new key. So after promotion the old hash moves to `previous_key_hash` and is
  still accepted for 2 minutes, then dropped. The grace never outlives the old
  key: `previous_key_until` is the earlier of the promotion time plus 2
  minutes and the old `key_expires_at`. If the old key has already expired,
  for example after a Connect renewal of an expired Mac, the previous slot
  stays empty. Without this, the straggler would
  get `unauthorized`, which the worker treats as a revocation
  (`openswap.worker.remote`).
- **Registration is untouched.** `rotate_key` and promotion never register,
  never bump `worker_epoch` and never touch claims or leases. A rotation must
  not interrupt a running job the way a new registration does.
- **Rate.** Replacing a pending key is always allowed, because that is how a
  retry after a lost response works. After a pending key is promoted, the next
  rotation is refused for one hour with 409 `rotation_too_soon`. That is a new
  error code, sent only to workers that advertised `key_rotation`. Rotating
  early only moves the expiry later, so the limit stops churn, not abuse.
- **Audit.** Write `worker.key_rotated` when the pending key is created and
  `worker.key_promoted` when it becomes current. Neither row contains a key.
  The `last_seen_at` handling matches renewal.
- **Revocation.** Revoking a worker also clears `pending_key_hash` and
  `previous_key_hash`.
- **A pasted renewal replaces every key.** The renewal branch of `pair`
  (a pasted renewal code) sets the new `key_hash`. In the same transaction it
  must clear `pending_key_hash`, `pending_key_expires_at`, `previous_key_hash`
  and `previous_key_until`. Otherwise someone holding an old pending key could
  still sign in after the renewal, promote that key and lock out the newly
  issued one. So O1's migration changes the `pair` branch too.

**Migration.** Add one forward migration, after the newest one at the time
(today `20261014120000_worker_origin.sql`). It does the following:

- adds `pending_key_hash` (unique, sha256 hex) and `pending_key_expires_at`
  (the new key's 30-day expiry, which becomes `key_expires_at` on promotion),
  plus `previous_key_hash` and `previous_key_until` for the 2-minute grace, to
  `remote_workers`;
- updates `worker_protocol_auth` to accept a pending key and promote it, and to
  accept the previous key until `previous_key_until`;
- adds a `rotate_key` branch to `worker_protocol_call`;
- adds `key_rotation` to the features the capabilities branch accepts.

Functions are copied verbatim with a header comment saying what changed, as
the repo's earlier forward migrations do. pgTAP covers these cases:

- rotate, then authenticate with the new key, which promotes it;
- an old-key retry replaces the pending key;
- a pasted renewal while a pending or previous key exists leaves only the new
  key working: both old slots get `unauthorized` and cannot promote;
- the old key works for 2 minutes after promotion, then gets `unauthorized`;
- promoting over an expired key leaves the previous slot empty, and a grace
  that would pass the old key's expiry ends at that expiry;
- rotation and promotion leave `worker_epoch`, claims and leases unchanged;
- a revoked worker cannot rotate;
- an expired key cannot rotate;
- the hourly limit after a promotion, and that it never blocks a retry;
- a pending key from a superseded registration still promotes;
- the tables stay unreachable through the Data API.

**Portal.** The worker card shows "Key renews automatically" when the
registration accepted `key_rotation`. When the key expires within 7 days and
the Mac has not rotated it, the card shows a reconnect hint that points to the
Mac. Docs go in `docs/remote-workers.md`.

## Phase S1 (OpenSwap): rotation on the Mac

- **Protocol docs.** Add "Optional key rotation (v1 extension)" to
  `docs/worker-protocol.md`, styled like the readiness and origin sections.
  Older backends answer 404 `unsupported_version` (or `not_found`); the worker
  records that and stops asking until its next registration, as it does for
  `capabilities`.
- **When to rotate.** `WORKER_FEATURES` gains `"key_rotation"`. After a
  registration where the backend accepted it, the remote loop rotates when 7
  days or fewer remain. It asks at most once per pass and backs off for an hour
  after a failure. Rotation never holds back heartbeats, claims or launches.
- **Saving the new key without re-registering.** Today a changed key re-registers:
  `ConfiguredRemote._resolve` (`openswap.worker.remote_state`) compares the
  whole `Enrollment`, key included. When it differs, it builds a new
  `RemoteClient`, and that client registers again. A new registration bumps the
  worker epoch and interrupts every claimed or running job. So rotation runs
  inside the active client and updates everything in place:
  1. The remote client calls `rotate_key` with its current transport and
     epoch. It checks that the response is a closed object, and refuses a
     `worker_id` that differs from its own.
  2. Under the pairing lock, a new `pairing.store_rotated_key(root, url,
     enrollment)` writes the new key to the Keychain item. It first checks that
     the item still holds the key the client started with. If the owner
     unpaired or reconnected meanwhile, it writes nothing. If the Keychain
     write fails, the client keeps the old key; the server still accepts it
     because the new one is only pending.
  3. Under `ConfiguredRemote._client_lock`, the client swaps the key on its
     transport, and `ConfiguredRemote.identity` is set to the new
     `(url, enrollment)` in the same step. The next `_resolve` and
     `launch_allowed` then see an unchanged identity, and the client is kept.
     The transport reads its key once per request, under a small lock, so each
     request carries one key or the other in full.
  4. The next request from any thread promotes the new key. A request already
     in flight with the old key is covered by the server's 2-minute grace.

  Any other key change still rebuilds the client, as today. That covers an
  unpair and pair, and the reconnect in S2, whose old key may already be dead.
  Rotation keeps the saved status, because the worker ID, service and
  registration are unchanged.
- **Reference server.** Implement `rotate_key` with the same pending-key rules,
  so the client is tested end to end on loopback.
- **Status.** Status and the menu say "Key renews automatically" or "Key
  expires in N days". The second one appears only when the backend lacks
  rotation, and it names the fix.

## Phase O2 (OpenTag): the connect page

**Discovery.** Serve `GET /.well-known/openswap-worker` as public JSON:

```json
{"protocol": "v1", "connect_endpoint": "https://<origin>/workers/connect"}
```

A backend without it (the reference server, an older OpenTag) answers 404, and
OpenSwap falls back to the pasted command. The field set is closed, so that
adding a field later is a deliberate version change.

**The page.** `GET /workers/connect` takes these query parameters, all
required except `worker_id` and `label`:

| Parameter | Rule |
| --- | --- |
| `redirect_uri` | `http://127.0.0.1:<port>/openswap/callback` or `http://[::1]:<port>/openswap/callback`, with a port from 1024 to 65535. Any port is allowed, as RFC 8252 §7.3 recommends; the host and path are fixed. `localhost` is refused, following RFC 8252 §8.3. |
| `state` | 22–128 base64url characters, sent back unchanged |
| `code_challenge`, `code_challenge_method` | PKCE, `S256` only (the same rule as `mcp-oauth.ts`) |
| `label` | The Mac's suggested name, at most 64 printable characters. It is self-asserted and shown as such. |
| `worker_id` | A reconnect: this Mac's existing worker |

**Checks before anything is shown.**

- **Bad parameters.** If any parameter is invalid, the page shows a plain
  error and redirects nowhere. The page never redirects to a host other than
  the loopback.
- **Signed out.** A signed-out owner goes through `/login?next=…`, which
  already refuses open redirects (`safeNextPath`, mar3co/opentag#143).
- **Workspace.** It uses the same workspace picker as the OAuth consent page.
  With no membership the page says "Join a workspace first".
- **Reconnect.** A `worker_id` must name a worker the signed-in owner owns in
  the chosen workspace that is not revoked. Otherwise the page refuses with
  "This Mac was removed; pair it as a new Mac."

**Consent copy.** The title is **Pair "<label>" with OpenTag**, or **Reconnect
"<label>"**. The page says:

- the name came from the Mac;
- the owner should continue only if they just selected Connect on that Mac;
- tasks still need the owner's approval in Slack or the portal;
- the folders and account are chosen on the Mac.

The buttons are **Pair this Mac** and **Cancel**. Cancel redirects to the
loopback with `error=access_denied&state=…`, so the Mac stops waiting.

**On approval.** The page issues a pair code through the existing `issue_code`
paths: a new pairing, or a renewal when `worker_id` is set. The code row also
stores `code_challenge`, `redirect_uri` and `label`. The page then redirects
to `<redirect_uri>?code=<pair code>&state=<state>`. The code keeps the 10-minute
lifetime and single use. It is issued with the same per-owner limits as the
**Pair your Mac** button; phase O2 adds a limit if that button has none.

**`/v1/pair` changes.**

- **Request.** The request takes an optional `code_verifier` (43–128
  characters).
- **Verifier rules.** A code that stores a challenge requires a verifier whose
  S256 matches. A missing or wrong verifier returns `invalid_code` and consumes
  nothing, so a stolen code cannot be burned by guessing. A code without a
  challenge (the pasted flow) refuses a verifier. Both checks happen inside the
  same atomic consume as today.
- **Response for new clients.** Only when the request carried a verifier, the
  response adds `workspace_label` and `owner_label`, so the Mac can say where
  it was paired. Older clients never send a verifier, and they keep the exact
  closed response shape that `fields()` in `openswap.worker.protocol` checks.
- **Name.** The new worker's display name defaults to the code's `label`.
- **Reconnect keeps the old key until the new one is used.** Today the
  renewal branch of `pair` replaces `key_hash` as soon as the code is
  consumed. If the Mac then failed to save the new key, a Mac whose old key
  still worked would be cut off. So a renewal code redeemed with a verifier
  stores the new key as `pending_key_hash`, using the same pending, promotion
  and grace rules as `rotate_key` from O1. The old key keeps working until the
  new key is first used. The new pending key takes the one pending slot,
  replacing any older one, and clears `previous_key_hash`. The pending key
  works even when the current key has already expired, because its expiry is
  `pending_key_expires_at`. A renewal code redeemed without a verifier (the
  pasted flow) keeps today's immediate replacement, so older clients see no
  change. It also clears both old slots, as O1 requires.

**Migration.** Add one forward migration. It adds `code_challenge`,
`redirect_uri` and `label` (all nullable) to `remote_worker_pair_codes`, plus
the `pair` branch changes. pgTAP covers these cases:

- the verifier match, mismatch and missing cases, and that a failure consumes
  nothing;
- a verifier sent on a code without a challenge;
- a renewal through connect keeps the worker ID, leaves the new key pending,
  keeps the old key working until the new key is used, and promotes on first
  use;
- a pasted renewal code still replaces the key at once and clears both old
  slots;
- a connect renewal replaces an older pending key, and that older key no
  longer works;
- a connect renewal for a Mac whose current key has expired works once the
  new pending key is used;
- a revoked worker is refused;
- the code still expires after 10 minutes;
- the redirect URI and challenge never leave service-only tables.

**Workers page.** The page leads with "Open OpenSwap on your Mac and choose
**Connect to OpenTag…**". **Pair with a command instead** keeps today's code
and steps. **Re-pair this Mac** becomes "On that Mac, choose **Reconnect** in
OpenSwap", with the command steps kept behind the same disclosure.

## Phase S2 (OpenSwap): Connect in the app and the CLI

**A new module, `openswap.worker.connect`.** It holds the connect flow:

- **Lock.** Take `<backup root>/.worker-connect.lock` without waiting before
  discovery, and hold it until redemption ends or the flow is cancelled. If
  the lock is held, refuse with `connect_in_progress`. This must be a lock of
  its own: holding the pairing lock for up to 10 minutes would block unpair
  and key rotation. Redemption still takes the pairing lock briefly, as `pair`
  does today. A process that crashes releases the lock with its file
  descriptor.

- **Discover.** Read `<origin>/.well-known/openswap-worker`, with the same
  bounded timeout and URL rules as the worker transport (`validate_url`). A
  `connect_endpoint` on a different origin is refused.
- **Listen.** Bind a one-shot HTTP listener to `127.0.0.1` on port 0 (the OS
  picks a free port). Generate `state` (16 random bytes) and a PKCE verifier
  (32 random bytes).
- **Open the browser.** Open
  `connect_endpoint?redirect_uri=…&state=…&code_challenge=…&code_challenge_method=S256&label=…`.
  The label is the Mac's Computer Name (`scutil --get ComputerName`), cut to
  64 characters. A reconnect also sends `worker_id`.
- **Wait.** Accept requests only for `GET /openswap/callback`. A request with a
  wrong `state` is answered with an error page and the listener keeps waiting.
  This stops another web page the owner has open from sending a code from its
  own account to the listener. The first request with the right `state`
  closes the listener. The wait lasts up to 10 minutes, the code's lifetime.
- **Redeem.**
  - A new pairing calls `pairing.pair(root, url, code, code_verifier=…)`.
  - A reconnect calls a new `pairing.reconnect(root, url, code, code_verifier=…)`.
    It replaces the key for the configured URL in one locked transaction,
    without the `unpair_before_pairing` refusal. It refuses a returned
    `worker_id` that differs from the configured one.
  - **When a reconnect fails partway.** If saving the new key fails after
    redemption, the Mac keeps its old key. While that key is still live it
    keeps working, because O2 leaves the server's new key pending. When the
    old key has already expired, nothing usable is left. Then the Mac says to
    run Connect again, and the new code replaces the unused pending key.
- **The browser tab.** It gets a small page: "Paired. You can close this tab
  and go back to OpenSwap" when redemption succeeded, or the error and the
  next step when it failed. That page is sent only after redemption, so the tab
  never says "paired" when the Mac is not.

**Menu bar.** Settings → Remote shows **Connect to OpenTag…** when the Mac is
not paired, and **Reconnect** when the key has expired or is about to expire
without rotation.

- **Starting.** The URL defaults to the OpenTag portal origin. **Use another
  server…** lets the owner type a URL, and it is the only way to name a
  backend other than OpenTag.
- **While waiting.** The menu bar shows one dialog, "Finish in your browser…",
  with **Cancel**. The flow runs on a worker thread, as `pair_once` does, and
  Cancel closes the listener.
- **After pairing.** The menu bar says "Paired with <workspace_label>", then
  runs the guided setup (`guided_setup.run`) as it does today after a pasted
  command.
- **Fallback.** **Set up Remote tasks…** still accepts a pasted command when
  the Mac is not paired.

**CLI.**

- `openswap worker connect [url]` runs the same flow and opens the browser with
  `open`.
- `--no-browser` prints the URL instead. That only works when the browser runs
  on the same Mac, because the redirect goes to that Mac's loopback. Over SSH
  the command says to use `openswap worker pair` with a code from the portal.
- On a paired Mac with an expired key, `connect` reconnects. On a healthy
  paired Mac it says the Mac is already paired and stops.
- `openswap worker pair` without arguments suggests `connect`.

**Interrupted flows.**

| Case | What happens |
| --- | --- |
| Browser tab closed, or the owner never approves | After 10 minutes, or when the owner selects Cancel, the listener closes. Nothing is paired and no code was issued. |
| Owner selects Cancel in the portal | The Mac gets `access_denied`, stops and says so. |
| App quits or crashes after approval, before redemption | The listener is gone and the code goes unused, then expires. Nothing is paired. Run Connect again. |
| Redemption fails (network, a refused code) | The tab and the dialog show the error. The code stays unconsumed unless the server consumed it. Run Connect again. |
| Keychain or settings write fails | A new pairing behaves like today's `_pair`: no orphaned key and no half-configured URL. A reconnect keeps the old key, which still works while it is live because the server's new key is only pending. |
| Two Connects at once (app and CLI, or two terminals) | The connect lock is taken before discovery. The second Connect is refused at once with `connect_in_progress`, before it opens a listener or a browser page. |

**Several Macs, and pairing the same Mac again.**

- Each Mac runs Connect on its own and becomes its own worker, named from its
  Computer Name.
- A reconnect keeps the worker ID, jobs and history.
- After a deliberate `unpair`, Connect pairs as a new worker, because an
  unpaired Mac keeps no worker ID. See open question 4.

## Delivery plan

The phases are independent stacks. O1 then S1 can ship without O2 and S2.

| Phase | Repo | One PR | Exit |
| --- | --- | --- | --- |
| O1 | opentag | Migration, `/v1/rotate_key`, the `key_rotation` capability, portal card line, docs, pgTAP | pgTAP (`pnpm db:test`) and the node tests green on the preview branch. The conformance job runs a pinned OpenSwap that rotates. |
| S1 | openswap | `rotate_key` client, refserver, status, docs | `uv run pytest`, including a loopback test where the response is dropped and the old key still works, then the new key promotes |
| O2 | opentag | Migration, well-known document, `/workers/connect`, `/v1/pair` verifier, Workers page copy | pgTAP and node tests of the page's checks: a bad redirect host, `localhost`, a missing challenge and a revoked `worker_id` each show an error and redirect nowhere. |
| S2 | openswap | `worker.connect`, `pairing.reconnect`, CLI `connect`, menu bar item, docs | `uv run pytest` with a fake browser that follows the redirect against a stub connect endpoint. Manual pairing and reconnect of a real Mac against a preview deployment. |

Production `db:push` for O1 and O2 stays the owner's call, through OpenTicket's
composed scripts, as for plan 017 and plan 018.

## Test plan (OpenSwap)

- **Connect lock.** A second Connect is refused before it opens a listener,
  and a crashed holder frees the lock.
- **Listener.** It binds only to `127.0.0.1`, accepts one request, refuses a
  wrong `state` and keeps waiting, refuses other paths and methods, and closes
  on timeout and on Cancel.
- **PKCE.** The verifier and challenge match RFC 7636's test vector. The
  verifier is never logged or written to disk.
- **Discovery.** A 404 or a malformed document falls back to the pasted flow.
  A document whose `connect_endpoint` is on another origin is refused.
- **Pairing.** `pair` with a verifier accepts the response's extra labels;
  without a verifier it still refuses them. `reconnect` refuses a different
  `worker_id` and keeps the old key on every failure.
- **Rotation.** It keeps the same `RemoteClient` and worker epoch. A job
  that is running when the key rotates finishes and uploads normally. A
  concurrent unpair during rotation wins, and nothing is written. It starts at
  7 days left and not before. A lost response
  recovers. A Keychain write failure keeps the old key. A 404 stops it until
  the next registration. It never blocks a heartbeat. It is refused when
  revoked.
- **Menu bar.** It shows Connect, Reconnect and the waiting dialog. Cancel
  closes the listener. The guided setup runs after a connect. This runs on
  Windows CI with fake AppKit, so nothing new is imported at module top level
  on Windows.

## Acceptance criteria

- A new Mac pairs from the menu bar with one click on the Mac and one
  approval in the browser, with no copy-paste.
- A Mac that stays online for 60 days never stops taking tasks for an expired
  key.
- A Mac whose key did expire is back with **Reconnect** and one browser
  approval. It keeps its worker ID and does not need `unpair`.
- An approval link sent by someone else cannot pair anyone else's machine:
  without that machine's loopback listener and PKCE verifier the code cannot
  be redeemed.
- The pasted command, the reference server and older OpenSwap versions work
  unchanged.

## Deliberately outside this plan

- Signing the Mac in to OpenTag for anything other than pairing, such as
  listing jobs in the app.
- Picking folders or the account from the portal (an owner decision keeps
  these on the Mac).
- Connect from Windows or Linux. Pairing is macOS-only because of the Keychain
  (`require_macos`).
- Connect in the reference server, which has no human sign-in.

## Open decisions for the owner

Decided 2026-10-16: the owner took the default for all five questions below.
Connect offers OpenTag with **Use another server…** beside it; keys keep 30
days and rotate at 7 days left; **Pair this Mac** needs no recent sign-in; an
unpaired Mac keeps no install ID; and the portal hides the pasted command
behind **Pair with a command instead**.

1. **Default server.** Should the app offer OpenTag (the portal origin, today
   `https://opentag.me`) as the default for Connect, or always ask for a URL?
   Default: offer OpenTag, with **Use another server…** beside it.
2. **Key lifetime with rotation.** Keep 30 days, rotating at 7 days left, or
   shorten to 14 days now that renewal is automatic? Default: keep 30, so a Mac
   that was off for a fortnight still works.
3. **Recent sign-in for pairing.** Should **Pair this Mac** require a sign-in
   within the last N minutes, as some services do for adding a device?
   Default: no, because the PKCE and loopback checks already tie the result to
   the Mac.
4. **Pairing the same Mac again after an unpair.** Keep a local install ID
   after `unpair`, so a later Connect can offer to reuse that Mac's old worker
   (and its history)? Default: no. Unpair means forget, and the portal can
   remove the old card.
5. **The pasted command in the portal.** Keep it visible as a secondary
   option, or hide it behind **Pair with a command instead**? Default: hide it
   behind the disclosure, since SSH and the reference server still need it.

## Sources

- RFC 8252, OAuth 2.0 for Native Apps: §7.3 loopback redirection with any
  port, and §8.3 why to use the IP literal and not `localhost`.
- RFC 7636, PKCE: S256, and the test vector in appendix B.
- RFC 8628, Device Authorization Grant: §5.4 covers remote phishing, which is
  why the device code flow is rejected.
- OpenTag `main` at `9f350ab`:
  - `packages/core/src/mcp-oauth.ts` and `apps/portal/src/lib/mcp-oauth.ts`
    (exact redirect matching, PKCE S256, `/mcp` resource);
  - `apps/portal/src/app/v1/[operation]/route.ts` and
    `packages/core/src/worker-protocol.ts` (`pair` and the operation list);
  - `supabase/migrations/20261002162724_remote_worker_protocol.sql`
    (`remote_workers`, `remote_worker_pair_codes`, `worker_protocol_auth`);
  - `supabase/migrations/20261014120000_worker_origin.sql` (the latest `pair`
    branch, and capabilities);
  - `apps/portal/src/app/app/workers/worker-controls.tsx` (the Pair and
    Re-pair steps).
- OpenSwap: `src/openswap/worker/pairing.py`,
  `src/openswap/worker/guided_setup.py` (`pair_once`,
  `parse_pairing_command`), `src/openswap/worker/remote.py`
  (`WORKER_FEATURES`, `sync_capabilities`), `docs/worker-protocol.md`.
