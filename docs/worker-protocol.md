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
account. `worker_id` must be the authenticated key's own worker; any other
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
syncing it; see below).
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
performs no network or Keychain access. The production adapter still refuses
jobs in this phase.

### Choosing the account and research folders

Pairing never selects an account or a folder; the submission names neither.
On an interactive terminal with no account pinned yet, `pair` lists the
eligible Codex accounts and asks for one (Enter skips), then points at the
folder step. Without a terminal it prints the same next steps. Pairing stays
successful whether or not this step completes.

```sh
openswap worker account                    # list Codex slots; * marks the pin
openswap worker account 2                  # pin by slot, email or alias (or --json)
openswap worker account --clear            # remove the pin
openswap worker workspace list             # approved research folders
openswap worker workspace add tag-research ~/Research/opentag \
  [--readonly-source ~/src/project]        # ID must match the portal's Local workspace ID
openswap worker workspace remove tag-research
```

Only Codex roster slots with a ChatGPT account ID are eligible; the pin is
stored as the opaque `codex:` identity of that account, so moving or swapping
slots does not change it. Claude accounts are listed as not eligible yet
(Claude authentication gate) and a Claude selector is refused. The commands
read roster metadata only (slot, email, alias, account ID), never auth files
or tokens. Pin and workspace changes take the worker lifecycle lock; pinning
also resolves the slot under the Codex account lock, so `codex remove`,
`swap` and `move` cannot interleave. The menu bar's Settings → General →
Remote tasks → **Account** popup uses the same function (Claude entries are
shown disabled).

The worker re-reads the pin for every launch, before it takes the account
lease, so a new pin applies to the next job without a restart; a running job
keeps the account recorded on it at `starting`. If the pinned account is no
longer in the Codex roster at launch, the job fails with
`provider_auth_unavailable` before any lease or launch, and the remote client
does not claim new work until a present account is pinned.

`workspace add` creates a missing folder owner-only (0700) and applies the
launch-time checks up front: a real directory owned by you with no group or
other access, and read-only sources owned by you and not writable by others.
It refuses a folder that is, or contains, your home folder, the OpenSwap
backup root, Codex home or the Claude config folder. At least one workspace
must stay approved. Execution remains disabled: the production Codex adapter
refuses every job until the Phase 1 live-evidence gates clear.

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

It sends only the canonical submission and prints the validated JSON job ID
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
