# OpenSwap worker protocol v1

This MIT protocol describes a single-owner research pilot. A compatible server
needs only this specification. The worker makes outbound requests; the Mac has
no inbound network listener. Provider authentication stays local. Production
Codex execution remains disabled until Plan 017's live-evidence gates pass.

## Transport, identity, and limits

Use HTTPS with certificate verification against the operating system trust
store. HTTP is allowed only for literal loopback IPs or `localhost`. Redirects
are forbidden. The service URL is an origin, without credentials, path, query,
or fragment; scheme and host are canonicalized to lowercase. All operations below are `POST /v1/<operation>`, with UTF-8 JSON
objects, `Content-Type: application/json` and an exact `Content-Length`;
`Transfer-Encoding: chunked` is rejected. Every field listed is required
unless described as optional. Reject unknown fields, duplicate JSON keys,
non-finite numbers, unsupported versions, and oversized bodies. Responses are
JSON objects, status 200 on success, with `Cache-Control: no-store`. Maximum
encoded request/response size is 1,500,000 bytes; event pages contain at most
200 events. Requests have a bounded timeout (reference client: 5 seconds).
The reference client sends heartbeats from a thread of their own, on a fixed
cadence, so probes, event pages and artifact uploads (each bounded, but not a
whole pass) can never delay liveness past the 15-second deadline; that thread
also renews the lease of an admitted claim that is still waiting for its
launch fence.

Except `pair`, every request uses `Authorization: Bearer <device_key>`. Keys
are random 256-bit-or-stronger opaque secrets, stored hashed by the server and
in the Mac's login Keychain under service `openswap`. Keys expire 30 days after
enrollment. To renew, the operator issues a renewal code for the existing
worker ID; pairing with it (after a local unpair) rotates that worker's key and
expiry in place and keeps its ID, so its jobs, events and artifacts stay
reachable, and the old key stops working. A revoked worker cannot be renewed. Never log keys, codes, prompts, or request bodies.
The backend derives owner identity from authentication. The reference server
has one owner: its operator. A key is scoped to its enrolled worker and that
worker's jobs, events and artifacts; it cannot select another worker. A more
capable implementation must preserve owner-only access and may use separate
scoped requester credentials. Workspace membership alone conveys no access.

The operator issues a random one-use code, valid for 10 minutes. The owner
approves pairing on the Mac by invoking `openswap worker pair <url> <code>`;
no network caller can approve it. The server atomically consumes the code and
returns a key. Unknown, consumed and expired codes all return `invalid_code`.
Enrollment does not enable local execution or select an account/workspace.

## Wire operations

| Operation | Request fields | Success response |
| --- | --- | --- |
| `pair` | `code` (string, at most 200 characters) | `worker_id`, `device_key`, `expires_at` |
| `register` | none (`{}`) | `worker_id`, `worker_epoch` (positive integer), `heartbeat_seconds` (5), `lease_seconds` (20) |
| `heartbeat` | `worker_epoch` | `last_seen_at`, `cancel_job_ids` (array of IDs) |
| `submit` | submission object below | `job_id`, `state` |
| `poll` | `worker_epoch` | `claim` (claim object below, or null) |
| `renew` | `worker_epoch`, `job_id`, `epoch` | `lease_until`, `state`, `cancel_requested` (boolean) |
| `job` | `job_id` | `job_id`, `state`, `epoch`, `event_cursor`, `expires_at`, `cancel_requested` (boolean) |
| `cancel` | `job_id` | `job_id`, `state` |
| `events` | `job_id`, `after_cursor` (integer >= 0); optional `worker_epoch`, `epoch`, `events` | `events` (array), `next_cursor` |
| `upload` | `worker_epoch`, `job_id`, `epoch`, `artifact` (below) | `name`, `size`, `sha256` |
| `artifacts` | `job_id`; optional `name` | `artifacts` (metadata array), or `artifact` (content object when name supplied) |
| `reconcile` | `worker_epoch`, `job_id`, `epoch`, `state`, `execution_stopped` (boolean), `unlaunched` (boolean) | `job_id`, `state` |

IDs are opaque strings of 1–200 characters; no string field may contain a
control character (below U+0020) other than the newline, carriage return and
tab permitted in `task`. Epochs and cursors are JSON integers (never
booleans), bounded at 2^53-1. Timestamps are strict RFC 3339 date-times,
`YYYY-MM-DDThh:mm:ss[.fraction](Z|±hh:mm)`, with `T`/`Z` accepted in either
case and at most six fractional digits; a space separator, basic format, week
dates, hour `24`, leap seconds (`:60`), longer fractions and offsets with
seconds are refused. The server returns UTC with the `Z` designator. Server
time determines deadlines.

Submission is a closed object containing exactly:

```json
{
  "idempotency_key": "unique-request-id",
  "worker_id": "paired-worker-id",
  "provider": "codex",
  "task": "Research the described problem and write a cited result.",
  "capability_profile": "research",
  "workspace_id": "research",
  "expires_at": "2026-10-01T00:00:00Z",
  "runtime_limit_s": 600
}
```

Task is 1–32,000 characters and may span lines; idempotency key/workspace ID
are 1–200; profile is at most 80. v1 accepts only `codex`/`research`. Runtime
is a finite JSON number greater than zero and at most 14,400 seconds. Expiry
must be in the future and no more than 24 hours away at first admission. A
submission names **no account, model, path, environment, executable or
argv**. The Mac resolves the opaque workspace ID and pins the locally approved
account. A backend implementing the optional account choice extension (below)
may add an `account_ref` the worker itself advertised; nothing else changes. `worker_id` must be the authenticated key's own worker; any other
value returns `forbidden`. Reusing an idempotency key with the identical
normalized payload returns the same job even if the worker later goes
offline; changing its payload returns `idempotency_conflict`. Normalization
renders `runtime_limit_s` as an integer when it is integral (`600` and
`600.0` are the same payload) and as a float otherwise, and `expires_at` as
UTC with `Z`; every other field compares verbatim. A new submission to a
worker without a heartbeat in the last 15 seconds is refused with
`offline_worker`.

A claim contains `job_id`, `epoch` (monotonically increasing fencing generation),
`lease_until`, and `submission` (the original closed object). The worker first
registers; registration increments its durable worker epoch and interrupts any
prior claimed/active job, without requeueing it, and does not by itself mark
the worker live: it clears the previous incarnation's liveness, so only a
heartbeat carrying the new epoch makes the worker live again. Every worker mutation carries
that epoch. Poll atomically grants one job and a 20-second lease, and repeated
polls replay the same unexpired claim. At most one nonterminal claim per worker
is permitted. Renew before expiry; an expired lease cannot be revived: `renew`
after `lease_until`, or on a terminal job, returns `lease_lost`, and `poll`
returns `lease_lost` while the worker's active claim has an expired lease
rather than granting another job. Only `heartbeat` counts as liveness; `poll`
from a worker without a heartbeat in the last 15 seconds returns
`offline_worker`. All uploads carry the original job epoch; old worker/job
epochs return `stale_epoch`. A worker whose `heartbeat` or `poll` returns
`stale_epoch` has been superseded by a newer registration; it must report
`offline` and register again before any further mutation. Leases govern
admission, **not termination of a running job**.

`renew` and `job` responses carry `cancel_requested` as a JSON boolean and
`state` as one of the canonical states below. A worker treats any other value
(for example the string `"false"`) as a malformed response: it neither cancels
nor launches local work on it, and retries as it would after a transport
failure.

## State, loss of connectivity, and cancellation

Canonical states are `queued`, `claimed`, `starting`, `running`,
`waiting_for_approval`, `cancel_requested`, `succeeded`, `failed`, `cancelled`,
`interrupted`, `expired`. Connectivity is separate: `disabled`, `online`,
`offline`, `revoked`, `expired` (the enrollment's 30-day key lifetime ended,
locally or as reported by the service; re-pair to continue), with a remote
last-seen timestamp. `waiting_for_approval` is reserved; v1 provides no
approval/resume operation and fails closed.

Queued expiry becomes `expired`; queued cancel becomes `cancelled`. Claim
moves `queued` to `claimed`. Uploaded events are the worker's replayable
journal and may record any state, but a service applies a `state_changed`
event to the job only for `starting`/`running`/`cancel_requested`; a terminal
state in an event is stored, never applied. Final outcomes change job state
only through `reconcile` with stopped or unlaunched proof. Cancel
of claimed/starting/running sets `cancel_requested`, propagated by heartbeat
and renew/job reads. The separate `cancel_requested` flag persists even when
heartbeat loss changes the state to `interrupted`; reconnect must enforce it.
A worker-uploaded `state_changed` event to `cancel_requested` sets the same
flag. `heartbeat` lists only jobs whose flag still needs enforcement (claimed,
starting, running, cancel_requested or unconfirmed interrupted), never
finished ones or confirmed interruptions, and returns at most 100 IDs: jobs
that are still live first, then the newest unconfirmed interruptions.
Repeated cancellation is idempotent. A terminal outcome
is not overwritten by a late cancel; an `interrupted` job confirmed with
proof counts as terminal here too. A cancel request is never stop proof.

After three missed five-second heartbeats the service reports the worker
`offline` and active jobs `interrupted`. It evaluates this deadline on every
request; no stale status is served. Queued jobs retain their own deadlines.
The worker continues any already running local job within its runtime limit,
journals events locally, and stops claiming new work on lease loss. It never
releases a local account lease merely because a remote claim expires.

On reconnect, authenticate again, heartbeat, read every outstanding job, relay
cancel requests, and renew before admitting work. Recheck expiry, cancellation,
authorization, locally pinned account readiness and local policy immediately
before launching. A previously launched or interrupted job is never relaunched.
Reconcile the journal's true outcome even when the server already marked it
interrupted: a terminal `state` with `execution_stopped: true` establishes a
confirmed stopped result, while `unlaunched: true` establishes pre-launch
failure/cancellation/expiry. `reconcile` accepts only a terminal `state`;
`succeeded` always requires stopped proof, so `succeeded` with
`unlaunched: true` (or without `execution_stopped: true`) returns
`invalid_state`, as does any other terminal state carrying neither proof, except
`interrupted` with both proof flags false. That exception explicitly reports an
uncertain provisional outcome (including the cursor-conflict path below), releases
the service-side admission slot without requeueing or proving local termination,
and remains eligible for later reconciliation with the true stopped outcome. Reconciliation and event/result uploads
are allowed after lease loss for the same worker/job epoch, but never after
revocation or device expiry. Confirmed terminal reconciliation is idempotent;
conflicting terminal outcomes return `invalid_state`. This includes
`interrupted` confirmed with `execution_stopped: true` or `unlaunched: true`:
it is terminal like the other confirmed outcomes, a later different terminal
`state` returns `invalid_state`, and `heartbeat` no longer lists the job in
`cancel_job_ids`.

Revoking a device denies **all** subsequent data access, submissions and claims
immediately, interrupts its service-side active jobs, and causes the Mac to
show `revoked`. It does not kill an already running provider: it finishes or
hits its runtime limit unless the owner cancels locally. Unpair removes the
local key and URL; server revocation is a separate operator action.

## Events and explicit artifacts

Each uploaded event has exactly `job_id`, `cursor` (positive, contiguous per
job), `timestamp`, `kind`, `state` (or null), `diagnostic_code` (or null),
`execution_stopped` (boolean). Kinds: `state_changed`, `provider_started`,
`provider_finished`, `stop_requested`, `diagnostic`. Stopped proof is only
allowed on `provider_finished`. Diagnostics: `live_adapter_disabled`,
`provider_unavailable`, `job_expired`, `cancel_requested`, `execution_uncertain`,
`lease_conflict`, `worker_restarted`, `invalid_transition`, `worker_disabled`,
`runtime_limit_reached`, `provider_auth_unavailable`, `provider_rate_limited`,
`artifact_rejected` (a `diagnostic` event: one explicit artifact was refused by
the size, hash, limit, conflict or export-safety checks and was skipped; the
job outcome stands), `remote_sync_conflict` (local only: the service refused
this job's event history with `cursor_conflict`, so the worker stopped
syncing it; see below), `workspace_refused` (local only: the job's workspace
breaks the folder rules at launch; it is uploaded as `provider_unavailable`,
because the service's diagnostic list is closed).
No free-form provider payloads, local paths or account/session identifiers are
accepted. Upload with `events`, `worker_epoch`, `epoch` together; read by
omitting all three. Upload at most 200 events; identical cursor replay is
idempotent, differing replay or gaps return `cursor_conflict`. Responses return
up to 200 events strictly after `after_cursor`; `next_cursor` is the last
returned cursor (or the supplied cursor). Persist acknowledgements locally;
a lost response may replay already stored events. `cursor_conflict` is
permanent for that job: no replay can repair a diverged history, and the
service keeps re-offering an unreconciled active job on every `poll`. The
worker therefore reports the outcome as uncertain (`reconcile` with
`interrupted`, `execution_stopped: false`, `unlaunched: false`), records a
local `remote_sync_conflict` diagnostic and ends the claim, so later claims
are not blocked by an endless "offline" retry. Only an unreachable service
defers that to the next tick. Local execution and the local journal are
unaffected; the job's later local events and results are not uploaded.

Artifact contains exactly `name`, `size`, `sha256` (lowercase hex digest),
`content_base64` (RFC 4648 padded base64). Name is a basename of 1–100 ASCII
letters/digits/underscore/dot/hyphen, beginning with a letter or digit. Maximum
size is 1 MiB each, at most eight artifacts per job. Validate decoded size and
SHA-256 before atomic storage. Same name/content replay is idempotent; a
changed artifact returns `artifact_conflict`. Upload only for successful jobs:
`upload` for a job whose state is not `succeeded` returns `invalid_state`, and
decoded content above 1 MiB returns `artifact_too_large` before any hash check.
The worker exports an explicit list (pilot default: `result.md`) from that
job's approved output directory. Never glob, traverse symlinks, upload source
checkouts, logs, auth/session files, or recursively archive directories. An
artifact refused by these checks (`artifact_too_large`, `artifact_limit`,
`artifact_conflict`, `hash_mismatch`, or a local export-safety failure) is
skipped and reported with an `artifact_rejected` diagnostic event; the job's
reconciled outcome is unaffected and the claim completes without it. Only
transport, lease and authorization failures defer the upload for retry.
Operator and owner must ensure selected results contain no provider secrets.
The service supplies metadata lists and bounded authenticated downloads.

## Optional per-job account choice (v1 extension)

By default a submission names no account and the Mac uses the account its owner
pinned locally. An owner may also approve a short local **allowlist** of
eligible accounts so a backend can offer a per-job choice among them. The Mac
stays the authority: it advertises only allowlisted accounts, validates every
choice again before launch, and never falls back to another account. In this
release only Codex roster accounts are eligible.

The extension is additive. A worker that never sends `accounts` sees no
change, and a backend that does not implement it rejects the operation like any
unknown operation (404 `unsupported_version`).

| Operation | Request fields | Success response |
| --- | --- | --- |
| `accounts` | `worker_epoch`, `accounts` (array below) | `account_count` |

`accounts` holds 0–20 entries, each a closed object with exactly `account_ref`
(opaque ID, 1–200 characters), `label` (1–100 characters, no Unicode control
characters: U+0000–U+001F or U+007F–U+009F) and `default` (JSON boolean).
References are unique within the
request and at most one entry is the default. The request atomically replaces
the worker's whole advertised set; an empty array withdraws it. It carries the
current worker epoch like any other mutation and returns `stale_epoch` from a
superseded registration. The worker sends it after each new registration and
whenever its allowlist, labels or default change. A new registration and a
revocation clear the worker's advertised set on the backend. A job carrying
`account_ref` is offered by `poll` only after the current registration has sent
`accounts` (an empty set counts); until then it stays queued and later jobs
without the field may be claimed first. A worker that receives 404
`unsupported_version` (or 404 `not_found`, which reference servers predating
this extension return for an unknown operation) records that the backend
offers no account choice and keeps working without it; no other response
disables the feature.

`account_ref` is a random value the Mac generates once per allowlist entry and
stores locally. It is never derived from a provider account ID, email or token,
so a backend cannot correlate it across workers or owners. `label` is chosen by
the owner (by default the OpenSwap alias or "Codex account N"); an email is
sent only if the owner explicitly makes it the label. No credential, token,
account ID or usage data is advertised.

A backend that implements the extension accepts one more optional submission
field, `account_ref`. It must equal an `account_ref` the target worker
currently advertises; otherwise submission returns 400 `invalid_request`.
When present, it is part of the normalized idempotency payload and appears in
the claim's `submission` exactly as submitted. A backend never sends
`account_ref` to a worker that has not advertised accounts, so a worker without
the extension never receives the field.

On a claim, a worker resolves `account_ref` against its **current** local
allowlist under its launch lock. An absent field selects the local default. A
reference that is no longer allowlisted (the owner removed it after the backend
recorded the choice) or an absent field with no default pinned fails the job
before launch: the worker reconciles it `failed` with `unlaunched=true` and
never substitutes another account. The resolved local account is recorded on
the job when it starts and never changes for that run.

## Optional readiness report (v1 extension)

Without this extension a backend cannot know which research folders a Mac
approved or whether it runs jobs for real, so owners type workspace IDs into
the backend by hand. With it, the Mac reports both. The report is
informational: it never authorizes anything, and the Mac still checks every
claim's `workspace_id` against its own registry, under its launch lock, before
launch.

The extension is additive. A worker that never sends `readiness` sees no
change, and a backend that does not implement it rejects the operation like any
unknown operation (404 `unsupported_version`).

| Operation | Request fields | Success response |
| --- | --- | --- |
| `readiness` | `worker_epoch`, `folders` (array below), `execution` (string below) | `folder_count` |

`folders` holds 0–20 entries, each a closed object with exactly `id` and
`label`. `id` is the workspace ID a submission's `workspace_id` names for that
folder: 1–200 characters, ASCII letters, digits, `_`, `.` and `-` only (the
reference client's IDs are narrower: 1–64 lowercase letters, digits, `_` and
`-`). IDs are unique within the request. The reference client leaves out a
workspace whose jobs it would refuse at launch (`workspace_refused`, see
[Choosing the folders sessions work in](#choosing-the-folders-sessions-work-in)), and one
it cannot check; the report changes, and is sent again, once that is fixed.
`label` is 1–100 characters with no
Unicode control characters (U+0000–U+001F or U+007F–U+009F); the owner chooses
it, and by default it is the folder's own name. No path, read-only source,
account, credential or usage data is reported.

`execution` is `"disabled"` or `"live"`. `"disabled"` means the Mac refuses
every job before launch, whatever else is configured; this release always
reports it, because the production adapter is still disabled
(`live_adapter_disabled`). `"live"` means the Mac runs approved jobs with its
local provider. The value describes how the Mac is built and configured, not a
moment's availability: a paused worker, a missing account or a signed-out
provider is not reported here.

The request atomically replaces the worker's whole report; an empty `folders`
array reports that no folder is approved. It carries the current worker epoch
like any other mutation and returns `stale_epoch` from a superseded
registration. The worker sends it after each new registration and whenever an
approved folder, a label or the execution mode changes (detected locally by
fingerprint, never by polling). A new registration and a revocation clear the
report on the backend, which then shows it as not reported (distinct from an
empty list) until the worker sends it again. A worker that receives 404
`unsupported_version` (or 404 `not_found`, from reference servers predating
this extension) stops sending it until its next registration; other failures
are retried on the next pass and never hold back heartbeats or claims.

A backend may offer the reported folder IDs wherever it offered owner-entered
workspace IDs, and fall back to those entries for a worker that has not
reported. It must not treat a report as approval of a job or as proof that a
folder still exists: a job naming an ID the Mac no longer approves fails before
launch (`provider_unavailable`, `unlaunched=true`), exactly as without the
extension.

## Errors, persistence, and operating the reference service

Errors are JSON `{"error":"code"}` without exception details. HTTP 400:
`invalid_request`, `invalid_code`, `hash_mismatch`, `invalid_state`; 401:
`unauthorized`, `device_expired`; 403: `revoked`, `forbidden`; 404: `not_found`,
`unsupported_version`; 409: `offline_worker`, `idempotency_conflict`,
`stale_epoch`, `lease_lost`, `cursor_conflict`, `artifact_conflict`, `queue_full`;
413: `body_too_large`, `artifact_too_large`, `artifact_limit`; 500/503:
`service_unavailable`. The worker surfaces exactly these codes and reports any
other error body as `service_unavailable`, so service text never reaches the
owner. Non-`POST` methods return 405 `invalid_request`. A job
belonging to another owner is reported as `not_found`, never `forbidden`, so
job IDs cannot be probed for existence. Except for `pair`, a missing or
malformed `Authorization` header is refused before the body is read. Retry
transport/503 failures with bounded polling, not mutations under new
idempotency keys. Treat 401/403 as no new admission.

Enrollment, codes, epochs, jobs, events and artifact bytes are durable in
SQLite. Reference queue limit: 20 queued jobs per worker. The reference service
runs one request at a time; a request that cannot start within 2.5 seconds is
refused with 503 `service_unavailable` before it touches any state. The reference service
retains data until its operator deletes the stopped service's private database
and backups; there is no automatic retention or network deletion API in v1.
Revocation removes access, not data. Operators own retention, deletion, backups
and TLS termination. Run in an owner-private state directory; the CLI applies
`umask 077` itself so SQLite journal sidecars stay owner-private:

```sh
openswap worker refserver pair-code --database /private/path/pilot.sqlite3
openswap worker refserver serve --database /private/path/pilot.sqlite3
openswap worker refserver revoke --database /private/path/pilot.sqlite3 WORKER_ID
```

Default listener is `127.0.0.1:8765`. Non-loopback bind requires
`--behind-owner-controlled-tls`; its plain HTTP listener must sit on a private
backend link behind owner-controlled TLS termination. Never expose it directly.
Pair codes are printed only to the operator's terminal. Testing with fake
adapters proves transport/journaling, not provider safety or a real HTTPS pilot.

## Local enrollment and status

`openswap worker pair <url> <code>` is the local approval: it exchanges the
one-use code and stores enrollment in login Keychain under service `openswap`,
using a URL-scoped worker-device account name. It writes only the origin URL to
`worker.controlServiceUrl` in `settings.json`, leaving enabled/paused policy,
the pinned account and workspace registry unchanged. Pair/unpair are explicitly
unsupported outside macOS; there is no secret-file fallback. While settings
name a URL, pairing (at that URL or another) is refused with
`unpair_before_pairing`: unpair first to change backends or renew an expired
enrollment. When settings name no URL, a Keychain item left behind at the
requested URL (after a settings reset, or a pairing interrupted after the key
was stored) is an orphan and `pair` replaces it. New enrollment at the same
URL receives separate claim/upload journal bindings, scoped by the worker ID;
nothing key-derived is written to disk.

`openswap worker unpair [url]` clears the URL, then removes the key. With an
explicit URL it removes that enrollment's item even when settings no longer
reference it, clearing the configured URL only when it is the same origin.
New launches also recheck Keychain availability, and the guard's authorization
(the normalized URL and worker ID it verified) is re-verified against
`settings.json` alone, under the launch lock, immediately before the launch
commits: after `unpair` returns, no remote launch that was not already
committed can start (it fails `worker_disabled` and releases its lease
unlaunched). An already committed run continues: unpair/revocation do not stop
an already running local job. The operator can revoke its worker ID
separately. After locally enabling and configuring a permitted
account/workspace, `worker run` maintains outbound connectivity: it re-reads
the enrollment and heartbeats on one thread and synchronizes on another, and
both write the durable status from the client's shared connectivity. The
registration (worker epoch) is replaced only when the enrollment changes or is
gone/expired locally; a locked Keychain, a transport fault or a failed status
write reports `offline` and keeps it, since re-registering interrupts the
service-side jobs of the registration it replaces. With no configured URL it
performs no network or Keychain access. The production adapter refuses jobs
(`live_adapter_disabled`) until the owner enables live execution; see
[Running jobs live](#running-jobs-live-codex).

### Guided setup after pairing

Pairing does not start the worker or choose an account or folder, so right
after pairing `openswap worker pair` walks the owner through the remaining
steps, in this order; `openswap worker setup` runs the same steps again on a
paired Mac, and the menu bar's Settings → General → Remote tasks → **Set up
Remote tasks…** button runs them in dialogs (pairing first, from the pasted
`openswap worker pair <url> <code>` command, when the Mac is not paired yet).
Every step calls the same function as its own command, and nothing in them can
fail pairing: a step that fails prints its command and the next step still
runs.

Each step says at most one short line of context, asks one short question
with its default visible (`[Y/n]`, `[1]`), confirms a choice in one `✓` line
and ends with at most one `Next:` command.

1. **Start the worker.** "Start the worker now? [Y/n]". Yes (or Enter) runs
   the same function as `openswap worker enable`: on success "✓ Worker
   started. This Mac shows online in about 15 seconds."; a refusal prints
   `enable`'s message, its diagnostic code when it is one of `enable`'s own,
   and "Next: `openswap worker enable` …". No, any other answer, or EOF leaves
   the worker off. An already enabled worker is never toggled: the step says
   "✓ Worker running.", or that it is on but not running.
2. **Pick the account.** "Tasks run on the account you pick." and a numbered
   menu of the eligible Codex and Claude accounts (menu number, provider,
   email, alias, slot; the pinned one marked `✓ … current`), then "Account
   [N]" with the pin as the default, or "Account (1-N, Enter skips)". A number
   picks by menu position (the two providers may share slot numbers); an
   email, alias or `claude:<slot>` still works.
3. **Pick the folders.** One line ("Folders where remote sessions can work.
   Each task gets its own copy (a git worktree), so your files aren't
   touched.") and the folder search; see
   [Choosing the folders sessions work in](#choosing-the-folders-sessions-work-in).
   `openswap worker setup --advanced` also offers, after each repo, to run
   its sessions in the folder itself (direct mode).
4. **Summary.** A checklist (`✓`/`✗`/`•`, each with its words): Paired (the
   service URL and its link state), Worker, Account, Folders (ID and label)
   (", direct" after a folder in direct mode) and Live tasks (on or off), then one line: "Next:" with the first thing
   still missing, else the live-check command for the pinned account's
   provider while live tasks are off (`openswap worker live-check`; for a
   Claude account `openswap worker claude pin`, `openswap worker claude
   prepare`, then `openswap worker live-check --provider claude`; see
   [Running jobs live](#running-jobs-live-codex)), else "✓ Ready: Slack can
   send tasks to this Mac." Live tasks, here and in the readiness report,
   follow the pinned account's provider. With `--advanced` the step first
   asks for the per-Mac permission limit ("Limit every remote task on this
   Mac? (follow, no-shell, read-only)", Enter keeps it); the checklist shows
   a Permissions row only when the limit is not the default, and for a
   Claude account whose shell is not limited one line says that shell
   commands can read or replace that account's own sign-in (see
   [Permissions](#permissions-the-accounts-own-settings)).

Each step is headed `Step N of 4 · <name>`; on a terminal the headers are
bold unless `NO_COLOR` is set or stdout is not a terminal, and the menu bar
shows the same lines in its dialogs. Without a terminal, `pair` asks nothing
and prints each step's command. While a URL is configured and the worker is
disabled or not running, human `worker status` adds "Next: paired with
<origin> but the worker is off; run `openswap worker enable`" (`--json` is
unchanged), and the menu bar's Remote tasks section shows "Paired, worker
off" under **Enable local worker**.

### Choosing the folders sessions work in

A folder is where a remote Claude or Codex session is launched and works,
like running `claude` or `codex` in a repo (owner decision, 2026-10-08). It
is not a read-only source with output elsewhere.

**What can be chosen.** A git repo becomes one folder; its ID comes from its
name. A folder that is not a repo but holds repos (`~/GitHub`) is saved once
and offers each git repo directly inside it as a folder of its own (e.g.
`opentag`, `openswap`). It is scanned again for every launch and readiness
report, so a new repo appears without changing settings. A repo's ID comes
from its name the first time it is offered and is kept for good (in the
worker's `repo-ids.json`), so it never moves to another repo when repos are
added, removed or renamed around it; new IDs avoid every approved ID and
every ID already given, and an approved folder never takes a repo's ID. A
repo already added on its own is not listed twice; the folder of repos
itself is never a place a task can name. A task's results stay reachable
for upload under its ID even if its repo is moved or deleted after it ran. A folder that is
neither a repo nor holds one is refused in the default mode ("That folder
isn't a git repo and holds none."): falling back to a read-only launch would
silently give one folder a different capability, so the one way to work in a
plain folder is the explicit direct mode. A folder added as read-only by an
earlier release keeps its ID and becomes a work folder when it is picked
again.

**Each task gets its own worktree** (the default). Before launch the worker
runs `git worktree add -b openswap/<first 8 of the task id> <path> HEAD`.
Every git command the worker runs (outside the sandbox) runs nothing from
the repo: hooks, checkout and clean filters (every driver any config file
or include could define is emptied, an `includeIf` included whatever its
condition, so a filter that only an `onbranch:openswap/**` or `gitdir:`
include turns on never runs either; the task's copy is checked out as a
command of its own, never by `worktree add`'s child git), `core.fsmonitor`,
commit signing and submodule recursion are off,
and on a task's worktree git is pointed at it from the worker's own record
(`GIT_DIR`/`GIT_WORK_TREE`), never through the task-writable `.git` file,
and only after checking the task did not repoint its admin folder or switch
branches. `HEAD` is the commit the owner has checked out,
the same one local `claude` sees; it works with no remote and no
`origin/HEAD`. The owner's uncommitted changes are not carried over and
never touched. The worktree is
`~/OpenSwap Research/.worktrees/<folder id>/<task id>`, owner-only (0700).
Claude or Codex runs with it as the working directory. The task's git writes
new objects to its own object folder (`<task id>.objects`, with the repo's
store as a read-only alternate), so a task can never delete or rewrite the
objects the owner's checkout and other tasks use; it also gets `gc.auto=0`,
`maintenance.auto=false` and the repo's `user.name` and `user.email` (read by
the worker; the sandbox may not read `~/.gitconfig`). Codex passes these to
its shell commands through the profile's `shell_environment_policy`. A task
never writes a ref, not even its own branch: it edits with whatever tools the
account's own permission settings allow (see [Permissions](#permissions-the-accounts-own-settings)),
and when it ends, by finishing, Stop, a
timeout or a worker restart, the worker commits what it left for the
task's branch, imports only the objects that commit needs from the task's
object folder (packed by `git pack-objects` and checked by `git index-pack
--strict`, as a fetched pack would be; nothing else the task left there ever
reaches the repo's store), and only then moves the branch, which it keeps. A worktree whose work could not be
committed is kept, and so is one where the task left files the repo ignores
(build output, `.env` files): those are never committed, so the worktree
stays until `openswap worker worktrees prune --force`. The task's copy is a
full checkout even when the owner's is sparse (sparse rules are not carried
over); a task that makes its copy sparse keeps the worktree uncommitted,
since paths it left out would read as deleted. Submodules are not checked out in the
task's copy.

**Direct mode** (advanced, this Mac only): the session works in the folder
itself, exactly like local `claude`. Set it with `openswap worker workspace
mode <id> direct` (or `worktree` to go back), `openswap worker workspace add
--work <folder> --direct`, or `openswap worker setup --advanced`. The control
service can never set it, and the readiness report does not carry it (that
would need a protocol field). A folder of repos stays one in direct mode: each repo in it is still a folder of its own, whose sessions then work in that repo itself (never in the parent folder as a whole); a repo found in it takes the
folder's mode.

Either way, the task's results (`result.md`) still go to its own results
folder, `~/OpenSwap Research/<id>/<task id>`, and artifacts are uploaded from
there.

**Sandbox.** For both Codex (its permission profile) and Claude (the Seatbelt
profile) a worktree task may write only:

- the worktree;
- its own object folder `<task id>.objects`;
- this worktree's admin folder `<repo>/.git/worktrees/<name>` (its HEAD,
  index and logs).

No ref is writable, so a task can neither move the owner's branches nor
delete or rewrite another task's.

It may also read `<repo>/.git`, never the owner's working copy. A direct-mode
task may write the folder. The owner's working copy, the repo's shared
object store, `.git/config`, hooks and every ref stay unwritable. Every git
command the worker runs re-reads the configured filter drivers (a name may
contain dots) before emptying them. The protections below
(credential homes, `~/Library`, the home folder, path identity) apply to
every work folder and every grant.

**When a task ends** its branch is always kept. Its worktree (and object
folder) is removed as soon as the worker has committed what it left, and kept
for inspection when that could not be done: it is locked, or the task
repointed its admin folder or switched branches; finished tasks are swept before each new worktree launch. `openswap
worker worktrees` lists them (folder, branch, state, path); `openswap worker
worktrees prune` removes finished tasks' clean worktrees (`--force`: every
finished one), then runs `git worktree prune`. A repo moved or deleted
between tasks refuses the next launch (`workspace_refused`) and its orphaned
worktrees are removed; a repo without commits is refused. Two tasks in one
repo get two worktrees and two branches.

**The setup step.** Most owners keep their code in one folder, so the step
starts there. It looks for likely code folders in the home folder:
`~/GitHub`, `~/Documents/GitHub`, `~/Developer`, `~/Code`, `~/Projects`,
`~/src`, `~/repos` and `~/dev`, keeping only existing folders that pass the
checks below, once each (a symlink to a listed folder is not listed twice).
Repos and folders of repos are listed first. A folder that holds three git
repos or fewer is followed by those repos, so the owner can pick just one. A
GitHub folder (any found folder named GitHub) is listed first and marked
"recommended"; folders already added are marked `✓`.

On a terminal the step is a type-to-search picker. The detected folders are
its numbered suggestions, with the recommended one highlighted so Enter
picks it:

```text
Step 3 of 4 · Folders
Folders where remote sessions can work. Each task gets its own copy (a git worktree), so your files aren't touched.
Folders:
❯ • 1  ~/GitHub           (recommended)
  • 2  ~/GitHub/openswap  (git repo)
  • 3  ~/GitHub/opentag   (git repo)
type or 1 3 · ↑↓ move · Tab complete · Enter pick · Esc skip
```

Typing filters the suggestions first, then the folders of the home folder;
text that looks like a path (`~/…`, `/…`, or anything with `/`) completes
inside that folder; digits with spaces or commas (`1 3`, `1,3`) pick
suggestions by number. After a pick ("✓ ~/GitHub: openswap, opentag" for a
folder of repos, "✓ ~/Code/site (site)" for a repo) the same picker opens
again as "Add another (Enter to finish)", nothing highlighted and the picks
ticked `✓`; Enter on nothing (or Esc) finishes. When folders are already set
up, nothing is highlighted and Enter on nothing keeps them. A refused folder
prints one line (what is wrong, what to do) and the same picker opens again.

The menu bar, and a terminal run that is piped, scripted or on Windows, keep
the numbered list and one question instead, "Folders (numbers or a path)
[1]" (or "Folders (Enter keeps current)"); with nothing detected they ask
"Type the path to your code folder, for example ~/GitHub" through the native
folder chooser (menu bar) or a typed line. The answer is one or more numbers
(`1 3` or `1,3`), or one folder path (a path dragged into Terminal may be
shell-escaped). Choosing adds folders; it never removes one (`openswap
worker workspace remove <id>` does). Only each folder's ID and name reach
the control service; paths never leave this Mac. `openswap worker workspace
add --work <folder> [--direct] [--label TEXT]` does the same from the command
line.

Each chosen folder becomes a workspace:

- **ID and label:** the folder's name, lowercased, with anything other than
  letters, digits, `-` and `_` turned into `-` (`site.io` → `site-io`), made
  unique with `-2`, `-3`, …. It is one of the service's `[A-Za-z0-9_.-]`
  folder IDs. The label the service shows is the folder's own name.
- **Results:** `~/OpenSwap Research/<id>`, created owner-only (0700) without
  asking, as is `~/OpenSwap Research`. An existing results folder that
  others can access is refused, never re-permissioned.
- **The built-in folder:** the first chosen folder replaces the built-in
  `research` workspace in the OpenSwap backup root, unless a job may still run
  or upload its results in it; then `research` stays beside the new one until
  `openswap worker workspace remove research`. Without a chosen folder,
  `research` stays as a results-only research workspace.

**Folder checks.** A work folder (and a research workspace's read-only
source) may not be, or contain, the home folder; be a system folder
(`/System`, `/Library`, `/usr`, `/etc`, `/Applications`, …) or contain one;
be `~/Library` or a hidden folder in the home folder (or inside one), except
a synced cloud drive there: a provider's folder in `~/Library/CloudStorage`
(Dropbox, Google Drive, OneDrive, …) or iCloud Drive (`~/Library/Mobile
Documents/com~apple~CloudDocs`), and the folders inside them, but never
`CloudStorage` or `Mobile Documents` themselves, and never when one of those
bases is a symlink; be, contain or sit inside the OpenSwap backup root, Codex
home or Claude config home; or overlap `~/OpenSwap Research`. It must be a
real directory owned by the owner and not writable by group or others. A
work folder never overlaps a folder another workspace only reads, and an
existing results or worktree folder that became reachable by others (or was
replaced by a file or a link) refuses its workspace too. No
work folder or read-only source may overlap any workspace's results folder,
and no results folder may sit inside one. The worker checks these rules
again for every job before it creates any folder, so settings saved earlier
(or edited by hand) that break them still load and can be listed and fixed,
but a job in such a workspace fails as `workspace_refused` and never reaches
the sandbox. `openswap worker status` (and `--json`, as
`refused_workspaces`) and the setup summary name each refused workspace and
the reason.

Paths are compared by their on-disk spelling and by filesystem identity
(device and inode along the ancestors), never by the typed string: on a
case-insensitive volume (APFS by default) `~/library` is `~/Library`, and a
firmlink or other alias is the folder it names. Settings store the on-disk
spelling. The same comparison guards what the Codex and Claude sandboxes may
be granted at launch (the private worker directory, the default Claude and
Codex homes and the backup root).

Research workspaces from earlier releases keep working: `openswap worker
workspace add --read <folder>` lets research tasks read a folder, and
`workspace add <id> <folder>` approves a results folder; their tasks work in
the results folder as before.

### Choosing the account and research folders

Pairing never selects an account or a folder; the submission names neither.
The guided setup above, or these commands, choose them. A control service that
implements the [readiness report](#optional-readiness-report-v1-extension)
learns each approved folder's ID and label (never its path) and offers those
IDs, so the owner no longer types them into the service.

```sh
openswap worker account                    # list Codex and Claude slots; ✓ marks the pin
openswap worker account 2                  # pin by slot, email or alias (or --json)
openswap worker account claude:4           # a Claude slot (codex:2 names a Codex slot)
openswap worker account --clear            # remove the pin
openswap worker account allow 3 [--label "Team research"]   # allow for a per-job choice
openswap worker account label 3 "Team research"             # rename (slot, email, alias or ref)
openswap worker account disallow 3 [--clear-default]        # withdraw (slot, email, alias or ref)
openswap worker workspace list             # approved research folders
openswap worker workspace add --work ~/GitHub  # sessions work in each repo in ~/GitHub, a worktree per task
openswap worker workspace mode openswap direct  # advanced: work in the folder itself (this Mac only)
openswap worker worktrees [prune]          # task worktrees; prune removes finished clean ones
openswap worker workspace add tag-research ~/Research/opentag \
  [--readonly-source ~/src/project] [--label "Tag research"]   # label default: the folder's name
openswap worker workspace label tag-research "Tag research"   # or --reset to the folder's name
openswap worker workspace remove tag-research
```

Eligible accounts are Codex roster slots with a ChatGPT account ID and Claude
roster slots with an email (owner decision 2026-10-07, see
[decision-claude-auth.md](../plans/research/remote-agent-host/decision-claude-auth.md#owners-decision)).
The pin is stored as the opaque, typed identity of that account (`codex:` from
the account ID, `claude:` from the email and organization), so moving or
swapping slots does not change it, and a job runs on the provider of its
account. A bare slot number means the Codex slot when one exists, otherwise
the Claude slot; `claude:N` and `codex:N` are explicit. The commands read
roster metadata only (slot, email, alias, account or organization ID), never
auth files or tokens. Pin and workspace changes take the worker lifecycle
lock; pinning also resolves the slot under both providers' account locks, so
`remove`, `swap` and `move` cannot interleave. The menu bar's Settings →
General → Remote tasks → **Account** popup uses the same function and lists
both providers ("Codex N · …", "Claude N · …").

The worker re-reads the pin for every launch, before it takes the account
lease, so a new pin applies to the next job without a restart; a running job
keeps the account recorded on it at `starting`. If the pinned account is no
longer in its provider's roster at launch, the job fails with
`provider_auth_unavailable` before any lease or launch, and the remote client
does not claim new work until a present account is pinned.

**Per-job account choice.** The `allow`, `disallow` and `label` subcommands
manage the local allowlist behind the [optional account choice
extension](#optional-per-job-account-choice-v1-extension): at most 20 Codex or
Claude accounts, each stored as a random `account_ref` (generated once, never
derived from the account), its local `codex:` identity and a label (by default
the slot alias or "Codex account N" / "Claude account N", never the email
unless you pass it). The
pin is the default and is always allowlisted: pinning adds the account if
needed, `--clear` keeps it allowed without a default, and disallowing the
default is refused unless `--clear-default` is passed. A pin saved before the
allowlist existed becomes a one-entry allowlist on the next settings write.
The list view shows the allowed accounts with their references and labels and
marks the default. The changes take the same locks as pinning. The menu bar's
**Web choice** popup, under **Account**, toggles each eligible account
(the default is shown checked and cannot be withdrawn there).

After each registration, and whenever the allowlist, a label or the default
changes (detected locally by fingerprint, never by polling), the worker sends
`accounts` with only the references, labels and default flag. A 404
`unsupported_version` records that the backend offers no account choice until
the next registration; other failures are retried on the next pass and never
hold back heartbeats or claims. A claim's `account_ref` is accepted only once
this enrollment has advertised a non-empty set (recorded durably in the remote
journal); before that it is a malformed claim. Under the launch lock the
runtime resolves it against the current allowlist: no field means the pin, a
reference that is no longer allowed fails the job `provider_auth_unavailable`
and an absent field with no pin fails it `provider_unavailable`, in both cases
before any lease, reconciled `failed` with `unlaunched=true`; another account
is never substituted. With no pin, the remote client still claims work while
the backend acknowledged a non-empty set and an allowed account is in the
roster. The worker polls only while every provider a claim could
need (the pin's and each allowed account's) can run, since the service may
hand out a claim for any of them. The reference service implements `accounts`, accepts `account_ref`
only when the worker currently advertises it, and `submit-test` takes
`--account-ref`.

`workspace add` creates a missing folder owner-only (0700) and applies the
launch-time checks up front: a real directory owned by you with no group or
other access, and read-only sources owned by you and not writable by others.
It refuses a folder that is, or contains, your home folder, the OpenSwap
backup root, Codex home or the Claude config folder. At least one workspace
must stay approved. Jobs still fail `live_adapter_disabled` until the owner
runs the live check and enables live execution (next section).

### Running jobs live (Codex)

Live execution is off by default, and nothing turns it on except the owner's
explicit opt-in after a passing live check on this Mac. The steps, on an Apple
silicon Mac:

```sh
openswap worker pause                 # if the worker is running; reopen later with --off
openswap worker codex install         # official Codex CLI 0.157.1, SHA-256 verified
openswap worker account 2             # pin the account remote jobs run on, if not yet
openswap worker codex login           # sign that account in to its own isolated Codex home
openswap worker live-check            # 4 short real jobs, evidence file, then offers to enable
openswap worker pause --off           # reopen admission
```

`openswap worker live status` shows the mode, `openswap worker live disable`
turns it off again, and `openswap worker live enable --evidence FILE` enables
it from a passing evidence file without re-running the check.
`openswap worker codex status` re-verifies the binary and lists which accounts
have an isolated sign-in.

**Pinned CLI.** `codex install` downloads the `codex-aarch64-apple-darwin.tar.gz`
asset of the official `rust-v0.157.1` release (or reads `--archive PATH`) and
refuses it unless its SHA-256 is the published
`3c45b162b7a76f51325015b1d0a8112c73219b7a9b59cd5762c37c9ba55894fa`. Its single
binary goes into the private worker directory with a manifest of its own
SHA-256; before every job the worker re-hashes it and checks that
`--version` prints `codex-cli 0.157.1`. A Codex on `PATH` or inside
ChatGPT.app is never used, and live execution stays bound to the binary the
check measured.

**Account isolation.** Every eligible account has its own `CODEX_HOME` under
the private worker directory. `codex login [SLOT|EMAIL|ALIAS]` runs the pinned
CLI's own `login` (browser, or `--device-auth`) with that home and file-backed
credentials, while holding the Codex account lease. The default `~/.codex`
login and the roster's saved copies are never read, copied or written by a
job; only the Codex process running in that home refreshes its tokens. A
sign-in to a different account than the one selected is signed straight back
out. Before each launch the home's signed-in account must be the job's leased
account, or the job fails `provider_auth_unavailable` without launching
(`unlaunched=true`). A per-job account choice needs that account signed in the
same way.

**Sandbox.** The worker rewrites the home's `config.toml` before every run. It
carries the account's own approval policy and reviewer (see
[Permissions](#permissions-the-accounts-own-settings)) and selects a named
permission profile that denies `:root`, reads `:minimal` plus the
workspace's approved read-only sources, writes only the job's output folder
(nothing at all under a read-only sandbox), denies `$TMPDIR` and `/tmp`, and
has no shell network; no `--sandbox` flag is ever passed (that would make
Codex ignore the profile). Research uses
Codex's live web search. Apps, hooks, plugins, multi-agent, browser and
computer use, code mode, unified exec and skill search are disabled; project
config discovery and `AGENTS.md` are off. A job whose folder contains a `.codex` entry (a
project-local Codex layer) refuses to launch. A job refuses to launch while any
managed or system Codex layer that could override this exists (requirement or
managed files in the isolated home, `/etc/codex`, the machine or per-user
managed preferences, or their payload keys). The job's argv is fixed:
`codex --strict-config --disable … exec --json --ephemeral
--skip-git-repo-check --ignore-rules --cd <output> --output-last-message <output>/result.md -`,
with the task on stdin (`--disable shell_tool` too when the shell is off).

**Containment and Stop.** Each job runs as its own launchd job
(`com.opensoft.openswap.worker.job.<id>`) with an allowlisted environment, so
the worker's own environment never reaches it. Stop, completion and recovery
all end the same way: every process in the job's resource coalition is
frozen, then killed, and the label is unloaded. `execution_stopped` is
reported only when no member is left and the label is gone; otherwise the job
is `interrupted` and the account lease stays quarantined until
`openswap worker lease release`. A worker that crashes leaves the job
running under launchd; the next worker start stops it and frees the lease
only on that proof. Events reduce to `provider_started` and one
`provider_finished`; model text, commands and URLs stay in the job's private
run directory and in `result.md`.

**The live check** runs, against the pinned account and with the live adapter
exactly as jobs use it: the static tool-surface checks, a `codex sandbox`
probe, one web-research job, one adversarial `codex exec` job that is asked to
read and write outside its folder, read `CODEX_HOME`, print its environment,
read an approved read-only source (and try to write it and follow a link out
of it), write its own `$TMPDIR`, use the network and submit a launchd job (each outcome checked on disk and in
the event stream, with positive controls so a refusal cannot pass; it runs
under the `on-request` policy, so any escalation the model asks for is
refused headless), one job stopped while a `setsid()` helper runs, one job
whose launching worker process is killed and then recovered, and one work
folder job under the widest settings an account can have
(`danger-full-access`, `never`), so only OpenSwap's write scope keeps the
owner's copy, branch and git config unchanged. The `permissions` gate checks,
without another model turn, that the config a launch writes carries the
account's approval policy, that a read-only sandbox denies a write in the
folder and that `no-shell` turns the shell tool off; the `sign_in_isolation`
gate runs, through `codex sandbox`, a `security` lookup of a throwaway login
Keychain item (added for the check, found outside the sandbox, removed after)
and a read of the isolated home's `auth.json`: both must fail. It refuses while the worker is running
unpaused (or its state cannot be read), a job is active or a lease is held, and
holds the worker lifecycle lock until it finishes, so `pause --off`, `enable`,
a worker start or a pin change waits instead of reopening admission mid-check. The evidence file (mode 0600,
under the worker directory's `live-evidence/`) holds pass/fail booleans and
counts only, never model output or secrets, plus a hash binding it to this
Mac and install (evidence from another Mac is refused when enabling); each job's folder (with its
`result.md`) stays under the backup root's `live-check/<UTC time>/` for
inspection. The network probes need `http://example.com/` and
`http://1.1.1.1/` reachable from this Mac outside the sandbox (plain HTTP, so
the result does not depend on certificates). Only when every gate passes does
it offer to enable live execution (`--enable` does so without asking,
`--no-enable` never). It uses some of the account's quota. Enabling records
the account the check ran on: jobs on any other account (another pin, or an
allowed account a per-job choice selects) are refused `live_adapter_disabled`
until a check passes on that account too (`live-check --account <slot>`), and
the worker does not claim work while any selectable account is unchecked. A
new binary starts the list over. An opt-in also records which job permission
behaviour its check measured (`policyVersion`); one recorded before jobs
followed the account's own settings stays off (status says to run the check
again) until a new check passes.

### Running jobs live (Claude)

The owner decided on 2026-10-07 that Remote tasks may run on their own Claude
accounts with the unmodified Claude Code binary and its native login, on their
own paired Macs, for tasks they start
([decision](../plans/research/remote-agent-host/decision-claude-auth.md#owners-decision)).
Live execution for Claude is a separate opt-in from Codex's, off by default.

```sh
openswap worker pause                          # if the worker is running
openswap worker account claude:4               # pin the Claude account (or allow it for a per-job choice)
openswap worker claude pin                     # record the installed claude binary's version and SHA-256
openswap worker claude prepare                 # sign that account in to its OpenSwap profile (Claude's own login),
                                               # and offer to copy your permission settings into it
openswap worker live-check --provider claude   # short real jobs, evidence file, then offers to enable
openswap worker pause --off
```

`openswap worker claude status` re-verifies the binary and lists which Claude
accounts have a prepared profile; `openswap worker live status --provider
claude` (and `enable`/`disable`) manage the opt-in.

**Binary.** `claude pin` takes the `claude` the owner installed (Homebrew cask
or the native installer), hashes it and keeps a byte-identical, read-only copy
under `~/Library/Application Support/com.opensoft.openswap/claude-cli/`. Jobs
run that copy, so an update or replacement of the installed binary can never
run unverified. Before every job the worker re-hashes the copy, checks
`--version`, and refuses a changed file. Anthropic publishes no digest to
verify against, so the first pin is trust-on-first-use. Jobs run with the
auto-updater off. To adopt an update, re-pin and re-run the live check, since
the opt-in is bound to the measured binary.
A `claude` installed inside `~/.claude` (the old npm-local layout) is refused,
because jobs cannot read that folder. So is a Claude Code older than 2.1.7,
which lets a symlink get around permission deny rules (GHSA-4q92-rfm6-2cqx).

**Account.** Each Claude account runs from its OpenSwap session profile
(`CLAUDE_CONFIG_DIR=<backup>/sessions/<n>-<slug>`, the same folders live
Claude sessions use). If that profile is not signed in as the account, or
its sign-in is only in the Keychain (which jobs cannot reach, below),
`claude prepare` runs the pinned Claude Code's own `claude auth login
--claudeai --email <account>` with `CLAUDE_CONFIG_DIR` set to it, while holding
the Claude account lease, under a Seatbelt profile that only takes the
Keychain away; Claude Code then keeps the sign-in in the profile's own
credentials file (`.credentials.json`, its plaintext store when the Keychain
is unreachable). OpenSwap never reads, copies or seeds a credential for
this, and your default Claude login (`~/.claude`, `~/.claude.json` and its
Keychain item) is never changed. A sign-in to a different account is signed
straight back out. A profile already signed in there is left as it is. Only
Claude Code refreshes tokens, and the worker never reads, uploads, proxies or
logs a credential. A launch refuses without starting
anything (`provider_auth_unavailable`, `unlaunched=true`) unless the profile
is signed in as the job's account with its sign-in in that file, and
(`provider_unavailable`) while an interactive session is using that profile,
while the profile mirrors customizations from your default profile (scheduled
kickoff's sharing; `claude prepare` removes those mirrored items), or while
its `settings.json` is one Claude Code would ignore (see
[Permissions](#permissions-the-accounts-own-settings)).

**Tools and sandbox.** The argv: `sandbox-exec -f <profile.sb> claude -p
--output-format stream-json --verbose --setting-sources user
--permission-prompts none --strict-mcp-config --disable-slash-commands
--no-session-persistence`, plus the per-Mac limit's arguments (none by
default), `--settings` with deny rules that keep the file tools (`Read`,
`Edit` and the search tools) off the profile's credentials file in every
mode, and `--add-dir` for each approved read-only source, with the task on
stdin and an allowlisted environment (no API keys). No tool list or mode is
OpenSwap's: Claude Code takes them from the profile's `settings.json`.
The Seatbelt profile holds whatever the mode, `bypassPermissions` included.
It allows writes only to the job's output folder (or the task's worktree and
what git needs beside it), the account's profile, the run's temporary folder
and this user's cache and temporary folders, and never to the profile files
that configure or instruct later sessions (`settings.json`,
`settings.local.json`, `CLAUDE.md`, the cached server policy (`remote-settings.json`,
`policy-limits.json`), `scheduled_tasks.json`, `rules/`, `agents/`, `agent-memory/`,
`commands/`, `skills/`, `hooks/`, `output-styles/`, `plugins/`, and `projects/`,
where each project's auto-memory lives, entries included; jobs also run with
auto-memory off). It makes the default login, `~/.codex` and the rest of the
backup root (other accounts, worker state) unreadable and unwritable, and
takes the Keychain away: `/usr/bin/security` cannot start, the Keychain's
services cannot be looked up and `~/Library/Keychains` cannot be read, so no
command the session runs can reach another account's sign-in, the default
login or the worker's device key. Nothing it starts can leave the sandbox or
the job's coalition either: LaunchServices (`open`; an app it opens runs
unsandboxed), Apple Events, launchd job creation (and `launchctl`), local
Unix sockets other than name resolution's, connections to this Mac
(`localhost`; `sshd` would run a command outside the sandbox) and ssh to any
address (port 22) are denied. So a task cannot reach a server on this Mac,
not even one it started, or use ssh (git over HTTPS still works).
Containment, Stop, recovery, `result.md`
and failure codes are the same as for Codex. A job also refuses while any
managed Claude Code policy applies (the system `managed-settings.json` or
`managed-settings.d/`, managed preferences, or server-managed policy cached
in the profile as a non-empty `remote-settings.json`): it could add hooks,
permission rules or an API key helper the owner did not choose.

**The Claude live check** records the same gates as the Codex one. Its
`sandbox-exec` probe also tries to bootstrap a launchd agent, to open an
app through LaunchServices and to connect to a loopback listener the check
holds (reachable outside) from inside the job's profile; all must fail and
nothing may be left loaded or running. The Read
probes run in `bypassPermissions` with every read allowed, so only the
Seatbelt profile can refuse (a read in the job folder must work; reads
outside it, of a sentinel in the profile and of `~/.claude.json` must fail).
The tool surface gate reads Claude Code's `init` event (no MCP server, no API
key, a reported permission mode). The `permissions` gate runs four short
jobs with the profile's own settings left out (`--setting-sources ""`) so the
owner's rules cannot decide them: in `default` mode a shell command and a
write, which would ask, must both be refused; in `acceptEdits` the write must
work; under `no-shell` (in `bypassPermissions`) no shell tool may be listed
and a Read of a stand-in file guarded by the same kind of deny rule as the
credentials file must be refused without its content appearing anywhere;
under `read-only` only the read and web tools may be listed; and the research
job's mode must be the profile's.
The `sign_in_isolation` gate runs a shell directly under the job's Seatbelt
profile and environment: `security` must not start, a throwaway login
Keychain item (found outside, removed after) must stay unreachable through
the Keychain's services alone, and the profile's settings and memory must be
unwritable while its state stays writable; whether the shell can read the
account's own credentials file is recorded as it is (it can). The work folder
job runs in `bypassPermissions` with the shell. Its evidence file is
`live-check-claude-<UTC>.json`.

### Permissions: the account's own settings

The owner decided on 2026-10-08 that remote sessions follow the permission
settings of the account they run on, like running `claude` or `codex` there,
instead of a tool list OpenSwap picks
([plan](../plans/017-progress.md#sessions-follow-the-accounts-own-permission-settings-owner-decision-2026-10-08)).

- **Claude.** The mode (`default`, `acceptEdits`, `plan`, `auto`, `dontAsk`,
  `bypassPermissions`) and the allow, deny and ask rules come from the
  profile's own `settings.json` (user settings; a repo's
  `.claude/settings.json` never applies, since `claude -p` skips the trust
  dialog that would approve it). `openswap worker claude prepare
  --copy-settings` copies the mode and those rules (never hooks, the model or
  extra directories) from `~/.claude/settings.json`, `--mode MODE` sets the
  mode, and run in a terminal on a profile with none it asks. A settings file
  Claude Code would silently ignore (not JSON, a symlink, an unknown mode,
  rules that are not `Tool` or `Tool(specifier)`, a Bash `:*` not at the end)
  refuses the launch instead of dropping your
  deny rules, and the validated permission keys are passed again on the
  command line (`--settings`), so a schema problem elsewhere in the file, which
  makes Claude Code skip the file, cannot drop them. `openswap worker claude status` shows each account's mode and
  rule counts.
- **Codex.** Each account keeps `approval_policy`, `approvals_reviewer` and
  `sandbox_mode` in its isolated home (`permissions.json`), set with
  `openswap worker codex settings [SLOT] [--copy-settings] [--approval P]
  [--sandbox M] [--reviewer R]` (`--copy-settings` reads `~/.codex/config.toml`
  and its selected profile; `codex login` offers it after a sign-in). Without
  any, Codex's defaults apply (`on-request`, `workspace-write`). Codex's own
  sandbox is the only boundary its shell commands have (it cannot be wrapped
  in another: macOS refuses nested sandboxes), so OpenSwap keeps its folder
  rules and the shell's lack of network whatever the sandbox mode says;
  `read-only` makes the folder read-only too. `untrusted`, which asks before
  every command not known to be safe, runs with no shell tool.
- **Nobody can approve.** Anything that would ask is denied: Claude runs with
  `--permission-prompts none`, and `codex exec` refuses every approval
  request. An account's own automatic mode still decides: Claude's `auto`,
  Codex's `auto_review` reviewer.
- **Per-Mac limit, set only on the Mac.** `openswap worker permissions
  follow|no-shell|read-only` (also `openswap worker setup --advanced`). The
  default, `follow`, adds nothing. `no-shell`: Claude gets no shell tool
  (`--disallowedTools Bash,PowerShell,Monitor,REPL,BashOutput,KillShell`) and
  no hooks; Codex no shell tool. `read-only`: Claude keeps only Read, Grep,
  Glob, WebSearch and WebFetch (and no hooks); Codex a read-only sandbox. It
  applies from the next task. No wire operation, IPC request or remote
  message can set it, and the readiness report does not carry it. `worker
  status` (human and `--json`) and the setup summary show it only when it is
  not `follow`; an unreadable value reads as `read-only`.
- **What OpenSwap keeps.** The folder or per-task worktree write scope; no
  access to other accounts' sign-ins, the backup root or the journal, and no
  writes to the owner's working copy; the identity checks and
  case-insensitive path checks. These hold in every mode, including
  `bypassPermissions` and `danger-full-access`. Outside them a session can do
  what `claude` or `codex` could do there, for example read files in your
  home folder that its mode allows.
- **The account's own sign-in.** Claude Code reads it from the profile's
  credentials file, so the Seatbelt profile cannot hide that file from the
  session. Claude's file tools are kept off it by deny rules OpenSwap adds to
  every job, for its path and for its name in any folder and any letter case
  (so `/System/Volumes/Data/Users/…`, a case variant or, from Claude Code 2.1.7,
  a symlink is covered too; deny
  rules hold in every mode, and the live check measures them in
  `bypassPermissions`). A shell command in a Claude task is not bound by
  them and can read it, or replace it with another sign-in that later tasks
  on this account would then use (the launch checks the account the profile
  records, which a shell can rewrite too), because everything Claude Code
  starts runs in the same sandbox and Claude Code must be able to write the
  file to refresh its token. This is what a shell command run by local
  `claude` can do to its Keychain item as well. Claude Code's own bash
  sandbox, which could hide it, cannot
  start inside OpenSwap's (macOS refuses a nested `sandbox-exec`:
  `sandbox_apply: Operation not permitted`), and handing the CLI its token
  at launch (`CLAUDE_CODE_OAUTH_TOKEN`) would mean OpenSwap reads the
  credential and takes over its refresh, which the decision memo rules out.
  `no-shell` (or `read-only`) prevents it; the setup summary and the live
  check say so. A Codex shell command cannot read its account's `auth.json`.

**Readiness report.** After each registration, and whenever an approved
folder, its label or the execution mode changes (by local fingerprint, like
`accounts`), the worker sends `readiness` with each approved workspace's ID
and label and the execution mode. A label is the owner's (`--label` or
`workspace label`) or, by default, the folder's own name, which is the only
part of the path that leaves the Mac; set a label if the folder's name is
private. The execution mode comes from one hook,
`openswap.worker.adapter.execution_mode()`: it is `live` only when the adapter
in use declares `execution_mode = "live"`, and the production adapter
(`UnavailableCodexAdapter`) declares `disabled`. A 404 `unsupported_version`
(or `not_found`) stops the report until the next registration. The report is
sent at the end of a synchronization pass, after results are delivered and new
work is claimed, so a slow `readiness` route never delays either. The reference
service implements `readiness` and keeps the latest report per worker.

`worker status --json` includes `remote_connectivity` and
`remote_last_seen_at`; `last_seen_at` remains the local process heartbeat.
CLI and menu-bar status expose connectivity and service last seen without
revealing keys, tasks or paths. Old heartbeats become offline after 15 seconds,
and a stopped local worker is never reported as online.

## Test-only submission and local evidence

The owner test command requires a paired device at the selected URL and an
explicit acknowledgement on every invocation:

```sh
openswap worker submit-test --url https://control.example \
  --task 'Research this bounded topic' --workspace-id research \
  --runtime-limit 600 --expires-in 3600 --i-understand-this-is-a-test-tool
```

It sends only the canonical submission (plus `account_ref` when
`--account-ref` names an account the worker advertised) and prints the validated JSON job ID
and state (any other response shape, unknown state or extra field is refused
as `invalid_response` with exit status 1 and nothing echoed). It never supplies
an adapter, model, account, path or command. A new submission requires the
worker online. The idempotency key is generated unless `--idempotency-key`
(1–200 characters, no control characters) is given. Because the service may
have committed the job before a response was lost, every failure prints the
exact `--idempotency-key` and absolute `--expires-at` to retry with; resending
them repeats the identical payload, so the retry returns the same job instead
of admitting a second one. `--expires-in` (relative seconds) and `--expires-at`
(RFC 3339) are mutually exclusive and one is required; a retry must use the
printed absolute form, since a recomputed relative expiry changes the payload
and the service answers `idempotency_conflict`. The hint is shell-quoted, so a
key containing spaces can be pasted back. When both `--idempotency-key` and
`--expires-at` are given explicitly, the tool sends the payload even after
that expiry has passed: the service's idempotent replay runs before its expiry
validation, so it returns the original job (or `idempotency_conflict`), while a
new key with a past expiry is refused by the service as `invalid_request`. A
generated key or a relative expiry is still refused locally unless the expiry
lies in the future. Errors from the service (for
example `queue_full`, `offline_worker`, `idempotency_conflict`) are printed by
code with exit status 1.
The command is **test-only** and does not bypass the disabled production Codex
adapter or local owner policy. Fake adapters are Python test injections, not a
CLI feature. A production paired worker can heartbeat, but it will not execute
provider jobs until its live adapter gates are cleared.

Run the credential-free roundtrip and deterministic failure tests with:

```sh
uv run pytest -q -n0 tests/test_worker_protocol.py tests/test_worker_refserver.py \
  tests/test_worker_remote.py tests/test_worker_pairing_status.py \
  tests/test_worker_launch_abandon.py tests/test_worker_remote_e2e.py
```

Tests mock Keychain and use an inert adapter. They prove loopback pairing,
guarded submission, idempotent retry, background polling, one launch,
replayable safe events and explicit hash-verified result retrieval (including
refusal of tampered bytes or digests). They do not establish a real HTTPS
deployment, another-network submission, provider permissions or live research.
