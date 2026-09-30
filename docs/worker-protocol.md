# OpenSwap worker protocol v1

This MIT protocol describes a single-owner research pilot. A compatible server
needs only this specification. The worker makes outbound requests; the Mac has
no inbound network listener. Provider authentication stays local. Production
Codex execution remains disabled until Plan 017's live-evidence gates pass.

## Transport, identity, and limits

Use HTTPS with certificate verification against the operating system trust
store. HTTP is allowed only for literal loopback IPs or `localhost`. Redirects
are forbidden. The service URL is an origin, without credentials, path, query,
or fragment. All operations below are `POST /v1/<operation>`, with UTF-8 JSON
objects and `Content-Type: application/json`. Every field listed is required
unless described as optional. Reject unknown fields, duplicate JSON keys,
non-finite numbers, unsupported versions, and oversized bodies. Responses are
JSON objects, status 200 on success, with `Cache-Control: no-store`. Maximum
encoded request/response size is 1,500,000 bytes; event pages contain at most
200 events. Requests have a bounded timeout (reference client: 5 seconds).

Except `pair`, every request uses `Authorization: Bearer <device_key>`. Keys
are random 256-bit-or-stronger opaque secrets, stored hashed by the server and
in the Mac's login Keychain under service `openswap`. Keys expire 30 days after
enrollment; re-pair to renew. Never log keys, codes, prompts, or request bodies.
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

IDs are opaque strings of 1–200 characters. Epochs and cursors are JSON
integers (never booleans), bounded at 2^53-1. Timestamps are RFC 3339 with an
explicit UTC offset; the server returns UTC. Server time determines deadlines.

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

Task is 1–32,000 characters; idempotency key/workspace ID are 1–200; profile
is at most 80. v1 accepts only `codex`/`research`. Runtime is finite, greater
than zero and at most 14,400 seconds. Expiry must be in the future and no more
than 24 hours away at first admission. A submission names **no account, model,
path, environment, executable or argv**. The Mac resolves the opaque workspace
ID and pins the locally approved account. Reusing an idempotency key with the
identical normalized payload returns the same job even if the worker later
goes offline; changing its payload returns `idempotency_conflict`. A new
submission to a worker without a heartbeat in the last 15 seconds is refused.

A claim contains `job_id`, `epoch` (monotonically increasing fencing generation),
`lease_until`, and `submission` (the original closed object). The worker first
registers; registration increments its durable worker epoch and interrupts any
prior claimed/active job, without requeueing it. Every worker mutation carries
that epoch. Poll atomically grants one job and a 20-second lease, and repeated
polls replay the same unexpired claim. At most one nonterminal claim per worker
is permitted. Renew before expiry; an expired lease cannot be revived. All
uploads carry the original job epoch; old worker/job epochs return
`stale_epoch`. Leases govern admission, **not termination of a running job**.

## State, loss of connectivity, and cancellation

Canonical states are `queued`, `claimed`, `starting`, `running`,
`waiting_for_approval`, `cancel_requested`, `succeeded`, `failed`, `cancelled`,
`interrupted`, `expired`. Connectivity is separate: `disabled`, `online`,
`offline`, `revoked`, with a remote last-seen timestamp. `waiting_for_approval`
is reserved; v1 provides no approval/resume operation and fails closed.

Queued expiry becomes `expired`; queued cancel becomes `cancelled`. Claim
moves `queued` to `claimed`. Uploaded safe `state_changed` events report
`starting`/`running` or stop request; final outcomes use `reconcile`. Cancel
of claimed/starting/running sets `cancel_requested`, propagated by heartbeat
and renew/job reads. The separate `cancel_requested` flag persists even when
heartbeat loss changes the state to `interrupted`; reconnect must enforce it.
Repeated cancellation is idempotent. A terminal outcome
is not overwritten by a late cancel. A cancel request is never stop proof.

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
failure/cancellation/expiry. `succeeded` always requires stopped proof. An
uncertain outcome stays `interrupted`. Reconciliation and event/result uploads
are allowed after lease loss for the same worker/job epoch, but never after
revocation or device expiry. Confirmed terminal reconciliation is idempotent;
conflicting terminal outcomes return `invalid_state`.

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
`runtime_limit_reached`, `provider_auth_unavailable`, `provider_rate_limited`.
No free-form provider payloads, local paths or account/session identifiers are
accepted. Upload with `events`, `worker_epoch`, `epoch` together; read by
omitting all three. Upload at most 200 events; identical cursor replay is
idempotent, differing replay or gaps return `cursor_conflict`. Responses return
up to 200 events strictly after `after_cursor`; `next_cursor` is the last
returned cursor (or the supplied cursor). Persist acknowledgements locally;
a lost response may replay already stored events.

Artifact contains exactly `name`, `size`, `sha256` (lowercase hex digest),
`content_base64` (RFC 4648 padded base64). Name is a basename of 1–100 ASCII
letters/digits/underscore/dot/hyphen, beginning with a letter or digit. Maximum
size is 1 MiB each, at most eight artifacts per job. Validate decoded size and
SHA-256 before atomic storage. Same name/content replay is idempotent; a
changed artifact returns `artifact_conflict`. Upload only for successful jobs.
The worker exports an explicit list (pilot default: `result.md`) from that
job's approved output directory. Never glob, traverse symlinks, upload source
checkouts, logs, auth/session files, or recursively archive directories.
Operator and owner must ensure selected results contain no provider secrets.
The service supplies metadata lists and bounded authenticated downloads.

## Errors, persistence, and operating the reference service

Errors are JSON `{"error":"code"}` without exception details. HTTP 400:
`invalid_request`, `invalid_code`, `hash_mismatch`, `invalid_state`; 401:
`unauthorized`, `device_expired`; 403: `revoked`, `forbidden`; 404: `not_found`,
`unsupported_version`; 409: `offline_worker`, `idempotency_conflict`,
`stale_epoch`, `lease_lost`, `cursor_conflict`, `artifact_conflict`, `queue_full`;
413: `body_too_large`, `artifact_too_large`, `artifact_limit`; 503:
`service_unavailable`. Retry transport/503 failures with bounded polling, not
mutations under new idempotency keys. Treat 401/403 as no new admission.

Enrollment, codes, epochs, jobs, events and artifact bytes are durable in
SQLite. Reference queue limit: 20 queued jobs per worker. The reference service
retains data until its operator deletes the stopped service's private database
and backups; there is no automatic retention or network deletion API in v1.
Revocation removes access, not data. Operators own retention, deletion, backups
and TLS termination. Run with `umask 077` in an owner-private state directory:

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
unsupported outside macOS; there is no secret-file fallback. Unpair before
pairing a different backend or renewing an expired enrollment. New enrollment
at the same URL receives separate claim/upload journal bindings.

`openswap worker unpair` removes the key and clears the URL. New launches also
recheck Keychain availability. Unpair/revocation do not stop an already running
local job. The operator can revoke its worker ID separately. After locally
enabling and configuring a permitted account/workspace, `worker run` maintains
outbound connectivity. With no configured URL it performs no network or Keychain
access. The production adapter still refuses jobs in this phase.

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

It generates a fresh idempotency key and sends only the canonical submission.
It prints JSON job ID/state and returns promptly; it never supplies an adapter,
model, account, path or command. A new submission requires the worker online.
The command is **test-only** and does not bypass the disabled production Codex
adapter or local owner policy. Fake adapters are Python test injections, not a
CLI feature. A production paired worker can heartbeat, but it will not execute
provider jobs until its live adapter gates are cleared.

Run the credential-free roundtrip and deterministic failure tests with:

```sh
uv run pytest -q -n0 tests/test_worker_protocol.py tests/test_worker_refserver.py \
  tests/test_worker_remote.py tests/test_worker_pairing_status.py \
  tests/test_worker_remote_e2e.py
```

Tests mock Keychain and use an inert adapter. They prove loopback pairing,
guarded submission, background polling, one launch, replayable safe events and
explicit hash-verified result retrieval. They do not establish a real HTTPS
deployment, another-network submission, provider permissions or live research.
