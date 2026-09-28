# Plan 017: Remote Agent Host progress

Plan 017 is the canonical scope and phase order. Supporting research informs
the work but does not override the plan. This log tracks the current gated
execution; it does not mean that remote execution is implemented or enabled.

## Current status

**Phase 1: IN PROGRESS, exit BLOCKED.** The remaining technical evidence gates
are unresolved. PR #59 is ready for review; opening or merging it does not
substitute for those gates. On 2026-09-28 the owner explicitly authorized
Phase 2 local-worker infrastructure to proceed in parallel with their Phase 1
work. This changes sequencing only: no Phase 1 gate is waived or marked passed.
Phase 2 local-only implementation is implemented and under review under that owner authorization;
remote access stays off and live Codex execution stays disabled. Phase 3 has
not started.

The baseline `uv run pytest` completed before this branch's changes: 2870
passed, 4 skipped, 1 failed, 3 warnings (15.15s). The failure is
`tests/test_menubar.py::test_launch_claude_login_missing_claude`: its injected
`which` reports no executable, but the resolver also probes hard-coded
install locations and finds this machine's `/opt/homebrew/bin/claude` (2.1.274).
The isolated test still fails with a temporary HOME and `PATH=/usr/bin:/bin`.
A narrow test-only correction now simulates the missing-resolver result without
changing product behavior; the isolated test passes (`1 passed in 0.74s`).
The latest Phase 1-only assembled suite was green: `2996 passed, 4 skipped, 3
warnings in 30.31s`; it includes 123 phase-one harness tests and two direct
tests of the Claude binary resolver's fallback directories. The helper-cleanup
regression also passed in isolation (`1 passed in 1.19s`). This is Phase 1
evidence, not validation of the Phase 2 tree; the separate full Phase 2 result
is recorded below.

The ChatGPT app-bundled Codex CLI is `0.158.0-alpha.2.1` (pre-release). A
separate official stable ARM macOS release, `0.157.1`, was downloaded to
`/private/tmp` and verified against its published SHA-256 before testing. Its
`codex sandbox --permission-profile research-test` wrapper allowed synthetic
workspace read/write and denied sibling read/write plus synthetic
`CODEX_HOME/auth.json` and `config.toml` reads. This narrows the stable-version
uncertainty for that low-level wrapper only; it does not prove `codex exec`
applies the profile. The [bounded no-auth tool-surface experiment](research/remote-agent-host/tool-surface-spike.md)
reached the loopback mock with five advertised function tools but exited
without a last-message file; successful CLI completion and the research tool
surface remain unproven. The spike harness and synthetic tests cover local helper
behavior: inert child-process cancellation and uncertain-journal recovery do
not prove provider `codex exec` cancellation or recovery. No live account test,
refresh-race test, authenticated execution, or web-research run has been
performed. See the [Codex spike record](research/remote-agent-host/spike-codex.md)
and [stable CLI probe](research/remote-agent-host/spike-stable-cli.md).

No real provider auth, auth files, Keychain items or credentials have been
inspected or changed, and no authenticated `codex exec` provider job or Claude
run has been launched. There is no owner-authorized account slot or proof yet
of exclusive ownership of a live provider auth context, so any experiment
requiring credentials remains out of scope.

## Phase 1 work slices and interfaces

| Slice | Owner | Deliverable / boundary |
| --- | --- | --- |
| Codex feasibility | `spike_codex` | Pin the installed CLI version; a disposable-directory harness and evidence for `codex exec --json`, research/events, process-group cancellation, kill/recovery, pinned `CODEX_HOME`, default-login preservation, and refresh behavior during a run. No credentials without explicit slot and exclusive-ownership authorization. |
| Inheritance and restriction feasibility | `spike_inheritance` | Inventory config, MCP, hooks, environment and inherited tools, then establish which restrictions the pinned version enforces. Stable 0.157.1's hash-verified low-level wrapper probe passed the synthetic filesystem/auth-config boundary; effective `codex exec` enforcement and inherited-tool restrictions remain unproved. |
| Decisions and integration | `memo_writer` | Keep this progress record current; preserve the recorded control-service decision and pending Claude-auth decision; reconcile spike evidence into an adapter contract and phase gates; coordinate final review. No provider implementation. |

The [candidate adapter contract](research/remote-agent-host/adapter-contract.md)
defines `probe`, `start`, `events`, and `interrupt`; it is provisional, not yet
version-pinned or proven against an authenticated Codex run. Provider session
IDs remain distinct from OpenSwap job IDs. Launch and interruption results
must preserve uncertainty; a killed or ambiguous run is not silently
relaunched. The Codex account must be pinned for the job and refreshed only by
Codex in its authoritative live store. Credential copies or rollback
snapshots must not replace a refreshed live token.

Phase 1 exit requires all of the following:

- [ ] A reproducible disposable local research run with a supported,
  version-pinned Codex CLI and an explicitly approved account context.
- [ ] Evidence for structured events, cancellation of the complete process
  tree (including detached descendants), and restart/kill recovery; no
  automatic relaunch after an ambiguous side effect. The credential-free
  `setsid()` reproduction showed the probe wrapper could return success
  while a detached helper continued running. The fake harness now tracks
  descendants by parent pid and forces `interrupted` when one escapes, but
  the fork/reparent race remains, so process-tree cleanup is best effort and
  does not meet this gate.
- [ ] Evidence that the pinned Codex auth context is used without changing
  the user's default login, and that auth refresh remains authoritative while
  the process is live. This must be established without copying or rolling
  back tokens.
- [ ] Inheritance inventory and effective enforcement evidence for the
  research restrictions. Directory separation alone does not establish a
  sandbox. A low-level synthetic boundary test using the locally bundled
  pre-release Codex CLI `0.158.0-alpha.2.1` and a `research-test` seatbelt
  profile allowed a workspace read/write and denied reads/writes outside that
  workspace. A second probe used public stable Codex CLI `0.157.1`, downloaded
  from the official GitHub release and verified against its published ARM
  macOS archive SHA-256 (`3c45b162b7a76f51325015b1d0a8112c73219b7a9b59cd5762c37c9ba55894fa`).
  The same wrapper allowed workspace read/write and denied sibling read/write.
  An adversarial check also attempted to read synthetic `auth.json` and
  `config.toml` files at the disposable `CODEX_HOME` path; Seatbelt denied both
  and recorded `file-read-data` denials. Both results are limited to the
  synthetic low-level `codex sandbox` invocation: neither proves a supported
  Codex `exec` run applies the profile or that inherited config/tools meet the
  product restrictions. An `exec` run must prove the adapter applies the same
  boundary and effective tool restrictions.
- [x] The Claude authentication decision memo is delivered. The owner must
  record a permitted path before any Claude adapter or Claude-specific code
  depends on it; this separate Claude gate does not block Codex-only phase 1.
- [x] The owner records the control-service operator, hosting, and repository
  (2026-09-28): pluggable backend behind one URL; OpenTag-hosted, self-hosted
  MIT reference server in this repository, or any server implementing the
  published protocol. See the
  [decision memo](research/remote-agent-host/decision-control-service.md).
- [x] Both decision memos delivered; control service decided, Claude path pending.
- [ ] The phase-one evidence, adapter contract and reproducible harness are
  reviewed and PR #59 is merged as Phase 1 signoff. This remains required
  before real Codex execution, but does not block owner-authorized local-only
  Phase 2 infrastructure work.
- [x] Full OpenSwap pytest suite is green on the assembled phase-one branch
  (`2996 passed, 4 skipped, 3 warnings in 30.31s`).

The control-service decision is recorded: the protocol specification, worker
client and MIT reference server live in this repository, OpenTag implements
the same protocol in its own repository, and the worker selects a backend by
URL. The phase-3 reference server is therefore a product deliverable, not a
movable placeholder. The Claude memo is delivered and its auth-path answer is
still pending, but that does not block Codex-only work; no Claude code starts
until the owner records a permitted path or exclusion.

## Phase status

| Phase | Status | Exit evidence / blocker |
| --- | --- | --- |
| 1. Feasibility spike and authentication gate | IN PROGRESS / BLOCKED | A hash-verified stable 0.157.1 passes the synthetic low-level Seatbelt wrapper probe, but no authenticated `codex exec` proves account selection, refresh behavior, structured provider events, complete process-tree cancellation/recovery, or model/tool enforcement integration. The fake `setsid()` reproduction showed the wrapper could return success while a detached helper remained alive; the harness now detects and terminates tracked escaped descendants and reports `interrupted`, but cannot close the fork/reparent race. No owner-authorized Codex slot or exclusive live-auth ownership is established. Control-service decision is recorded. PR #59 is open and ready for review; it does not satisfy the remaining technical exit gates. |
| 2. Local worker, remote access off | IMPLEMENTED / UNDER REVIEW — local-only | Owner authorized local infrastructure to overlap Phase 1; no Phase 1 gate is waived. Fake-only validation is recorded below. Remote access stays off and live Codex stays disabled. |
| 3. Private remote pilot | NOT STARTED | Requires phase 2's exit criteria and merged PR. Builds the protocol specification, configurable backend URL and the MIT reference server as product code; the pilot runs against a self-hosted instance. |
| 4. OpenTag connector | NOT STARTED | OpenTag repository, mar3co/opentag#135; requires phase 3. Out of scope for this PR. |
| 5–6 | NOT STARTED | Require phase 4. Out of scope for this PR. |

## Phase 2 local-worker plan (owner-authorized overlap)

The owner authorized local Phase 2 infrastructure to overlap the open Phase 1
review. The authorization does not pass or waive any Phase 1 gate. Phase 2 is
limited to an opt-in local worker: remote access remains off, the production
Codex adapter always reports unavailable, and no provider account or job is
used. Phase 3's control server, enrollment, polling and result upload are not
part of this work.

| Slice | Boundary and proposed interface |
| --- | --- |
| Core policy and journal (`settings.py`, `worker/models.py`, `worker/journal.py`) | Add default-off `WorkerSettings(enabled=False, paused=False)` to shared `settings.json`; persist a validated opaque workspace map (registered output root plus disjoint read-only roots) and an optional stable opaque Codex account reference. Writes use a settings-specific cross-process lock; reads are side-effect-free. Store private state under `<backup_root>/worker`. Use stdlib SQLite transactions for jobs/events; no server database. |
| Typed runtime (`worker/runtime.py`) | Internal-only `JobSubmission(idempotency_key, provider='codex', task, capability_profile, workspace_id, expires_at, runtime_limit_s)`. Reject unknown fields; accept no account, model, path, environment or argv. `WorkerRuntime.submit/get/events/cancel/status` is callable only in-process; no submit RPC or CLI in Phase 2. |
| State, queue and fencing | Preserve plan states, including `waiting_for_approval` in the canonical enum, but never emit that unsupported state in Phase 2. Keep worker-process health separate. Bound pending jobs and active execution to one per host. Persist startup epoch and job generation; every state/event write compares both. Restart reconciliation marks uncertain work `interrupted` and never relaunches it automatically. Store job ID, locally derived owner reference, provider session ID if known, private pinned-account reference, epoch/generation and timestamps. Persist task text only in the private local DB; status/IPC exposes no raw task or provider text. Events use a typed safe-data allowlist and safe diagnostic codes. Output paths derive only from registered workspace IDs and local job IDs; no caller path, upload or artifact transfer in Phase 2. |
| Lease and stop boundary | Use the shared provider mutation lock and lease store; release only with explicit `UNLAUNCHED` or `CONFIRMED_STOPPED` evidence. Timeout, stale worker, ambiguous start, or detached-descendant uncertainty remains quarantined. An interrupted job alone does not prove that its lease may be released. |
| Provider boundary (`worker/adapter.py`) | Define `ProviderAdapter.probe/start/events/interrupt`. The production factory returns an unavailable Codex adapter with safe code `live_adapter_disabled`; no executable path or caller argv injection can enable it. A fake adapter is available only to tests. Provider-finished events carry explicit execution-stopped proof; no proof means `interrupted` plus retained account quarantine. Phase 1 sandbox, auth, tool-inheritance, cancellation and recovery gates remain required for any later real adapter. |
| Account lease integration | Lease owner API: `AccountLeaseStore(backup_root, provider).acquire(job_id, stable_account_id, worker_pid, worker_epoch, ttl_s) -> LeaseToken`, `renew(token, ttl_s)`, `mark_uncertain(token, reason)`, and `release(token, evidence)` where evidence is only `UNLAUNCHED` or `CONFIRMED_STOPPED`. `mutation_guard()` acquires the existing provider `FileLock` once and exposes `assert_unleased(account_ids)`; engine methods resolve stable identities under that guard, then mutate. Lease generation is distinct from worker epoch. Uncertain/expired leases remain quarantined; journal state is not a second lease authority. |
| Local control and UI (`worker/ipc.py`, sibling-owned) | A private versioned AF_UNIX socket exposes only `status`, `stop`, and `pause`; no submit RPC. `WorkerSnapshot(enabled, paused, process_state, remote_connectivity='disabled', provider, active_job, queue_depth)` and `ControlResult(accepted, job_id=None, diagnostic_code=None)` live in `worker/models.py`. Stop is request acknowledgement, never proof of execution stopped; `set_paused(False)` reopens admission only and never resumes a job. CLI verbs are `worker run|status|stop|pause|enable|disable`; the menu is a thin status/IPC client. Status reading is a pure read-only snapshot: it does not construct runtime/Engine, create default directories or read credentials. |

The local persistence surface is `LocalJobStore.create/claim/transition/append_event/get/list_events/start_epoch`; state changes use transactional expected-state + epoch/generation checks and events have per-job monotonic cursors. The daemon holds a process singleton lock before `start_epoch`; startup adopts only queued work and marks uncertain active work interrupted. IPC callback signatures are `status() -> WorkerSnapshot`, `stop(job_id: str | None) -> ControlResult`, and `set_paused(paused: bool) -> ControlResult`. Stop acknowledgments do not prove execution stopped, and stale/unavailable health or an unresolved lease blocks disable. The IPC and LaunchAgent/CLI seams are owned by the UI/inheritance slice; core models, journal, worker policy/configuration, adapter and runtime are owned by the core slice.

Phase 2 validation is fake-only: persistence across
worker/UI restart, bounded admission, state transitions, stale-epoch refusal,
lease enforcement, IPC allowlisting and early CLI routing. Acceptance includes
one subprocess-level fake worker test showing work survives status-client
recreation, duplicate admission is refused, stop acknowledgement is not
execution-stopped proof, and restart marks ambiguous work interrupted without
relaunch. It does not claim that Codex is executable under the required
boundary. No `launchctl` command is run against the user's login session;
LaunchAgent behavior is verified with mocks. Phase 1 remains blocked and Phase
3 remains unstarted.

The Phase 2 full-suite validation on the currently integrated PR #59 snapshot
`f684ebf` is green: `3082 passed, 4 skipped, 3 warnings in 20.02s`. The run
used the existing test environment with narrowly elevated permissions for
disposable AF_UNIX sockets and fake-process supervision. The newer PR #59 head
has not been merged into this tree. After the PR #60 review-fix pass below,
the full suite on this tree remains green:
`3153 passed, 4 skipped, 3 warnings in 30.37s`.
Focused core journal,
settings, disabled-adapter and fake lifecycle tests passed (`22 passed`); the
separate subprocess acceptance passed (`1 passed`). It verifies client
recreation/idempotency, singleton refusal, a stop acknowledgement remaining
nonterminal with the lease quarantined, and restart recovery to `interrupted`
without adapter relaunch. The first assembled run exposed a missing
`activeAccountNumber` in a live-kickoff test fixture; the fixture now declares
the managed account explicitly, and kickoff identity resolution remains
fail-closed. All provider behavior in these tests is synthetic; this result
does not clear Phase 1 gates or enable live Codex execution.

PR #60 review follow-ups #4124363480, #4124363497, #4124363514,
#4124442272, #4124700038, #4124700051, #4124766663, and #4124766673 are
addressed with regression coverage: Claude slot/session mutations and
refresh-token consumers honor
worker leases; purge refuses active or uncertain leases and retains provider
lock inodes while deleting other backup data; manually started workers stop
when opt-in is revoked; malformed pinned-account policy refuses enablement
before LaunchAgent installation; and menu status applies external policy
changes only with its generation-matched snapshot. Deadline/shutdown lease
release also requires an `InterruptResult` whose `execution_stopped` value is
literally `True`; a truthy string is tested as insufficient proof.
Cancellation/event races route through provider interruption, and malformed
released lease documents remain quarantined in read-only snapshots.
Follow-up #4124975047 is also addressed: worker `run`, `enable`, `disable`,
and `pause` use the canonical legacy-backup migration before creating worker
state; `status` remains read-only and `stop` remains IPC-only. A regression
checks migration precedes private worker-root creation and status leaves the
legacy directory untouched.
Follow-ups #4125129888 and #4125129902 are addressed: a durably journaled
provider-finished stop proof survives a stale terminal transition without
interrupting an already-finished run; purge now refuses while the worker is
enabled, running, installed/loaded, or unresolved and requires explicit
disable before retry. Purge keeps lifecycle/provider lock anchors while
removing worker data; it never disables or unloads the worker automatically.
Follow-ups #4125207619 and #4125207629 are addressed: default-login Claude
kickoff resolves the live identity from public global-config metadata while
holding the provider mutation guard and refuses unmatched or ambiguous roster
identities; failures in post-launch journal transitions interrupt the owned
run and retain an uncertain lease unless the adapter proves it stopped.
Follow-ups #4125504755 and #4125504772 are addressed: event reads run on one
tracked reader while the worker control/deadline driver remains responsive;
interrupt may run concurrently with a blocked read, no replacement read or
job is admitted while that reader remains active, and late results after
interruption are discarded. The fake-adapter regression covers stop,
shutdown, and deadline during a blocked read; it does not establish that a
real provider read is interruptible or pass the Phase 1 process-tree gate.
Claude transfer import now acquires the provider mutation guard before
migration or credential/roster writes, and refuses active, uncertain, or
corrupt lease state without changing those files.
Follow-ups #4125619349, #4125619361, and #4125619374 are addressed: after
provider interruption the runtime reloads the latest job generation while
preserving confirmed stop proof; automatic active-credential restoration
defers under a worker lease; and purge holds the canonical settings lock after
the lifecycle lock and before provider locks, preserving the settings-lock
inode through deletion. A concurrency regression holds the settings writer
lock while purge waits, then verifies purge removes managed data without
replacing the lock inode.
Follow-up #4125777799 is addressed: `statusline.save_wrap` now uses the shared
settings write lock across its read-modify-write, so status-line setup/removal
cannot restore stale worker pause/enable policy. A deterministic concurrent
writer test verifies the worker policy and unrelated settings survive. A
read-only audit found no other active `settings.json` read-modify-write path
outside the shared settings helpers and status-line writer.
Follow-up #4125894568 is addressed: a released lease is valid only when its
reason is exactly `unlaunched` or `confirmed_stopped`; absent or other
syntactically safe reasons remain quarantined. Existing malformed-lease test
cases cover both invalid forms.
Follow-up #4126005126 is addressed: disable retains the lifecycle lock while
waiting boundedly for the worker's process-lifetime instance lock to become
available after opt-out and service unload. Timeout leaves policy disabled
and paused, reports that stop is unconfirmed, and blocks re-enable while the
old process still owns the lock. The regression uses a fake lock holder and
mocked service, with no real LaunchAgent operation.
A follow-up review pass against this tree is addressed: a worker restart now
marks an `active` lease left by a crashed process `uncertain` (reason
`worker_restarted`), and an explicit `openswap worker lease release` command
releases an `active`/`uncertain` lease only after proving its recording
worker is gone (a newer journal epoch has started, or its pid is no longer
alive) and its job is terminal or absent from the journal; it never
auto-releases or relaunches anything. Scheduled kickoff no longer takes a
lease at all while Remote tasks are disabled, and a kickoff timeout now
releases its lease as confirmed-stopped (the child is already killed and
reaped by the time `subprocess.run` raises) instead of leaving it uncertain.
Default-login Codex kickoff resolves identity from the live `auth.json`
OAuth claims the same way `CodexEngine.current_account_number` does, rather
than the roster's possibly-stale `activeAccountNumber`; a now-unnecessary
`activeAccountNumber` seed in a Claude live-kickoff test fixture was reverted.
The CLI now rejects a stale `cswap` launcher before dispatching to the worker
subcommand, so `cswap worker status` gets the removed-command message instead
of reaching the worker CLI. Codex `set_account_disabled`, `set_alias`, and
`unset_alias` now hold the same lease mutation guard as the engine's other
roster mutations. `_resolve_workspace` now refuses an approved read-only
source that others can write to (readable by others remains allowed).
The lease release command proves only that the recording worker and its job
are finished; phase 2 never starts a provider process (the adapter refuses),
so there is no provider tree to check yet. Before phase 3 enables a real
adapter, release must also prove the provider process tree has stopped.
Release covers both providers (`--provider codex|claude`). A kickoff
timeout keeps its lease `uncertain`, because `subprocess.run` kills only the
direct child and a helper may survive. A kickoff lease, or a worker lease whose
job is missing from the journal or `interrupted`, carries no stop evidence, so
it is released only once the lease has expired (kickoff), its recording
process is gone while still `active`, and the owner passes `--confirm-stopped`.
A job whose expiry passes while it is being prepared now ends `expired`
before launch (STARTING may move to EXPIRED only before the provider
starts), and a non-stale journal failure while recording an interrupt now
propagates instead of returning the stale in-flight record, so the next
start recovers the row. The subprocess acceptance fixture now holds a stop
inside the fake interrupt, since event reads are interruptible; it had
flaked when the worker finished the interrupt before the parent's kill.
Enabling refuses (`worker_running_unmanaged`) while an unmanaged worker such
as a manual `openswap worker run` holds the instance lock, instead of
installing a managed service that could never start; re-enabling an already
loaded LaunchAgent stays idempotent.
Owner decision (2026-09-28): when a kickoff ping exits normally, the direct
child's exit is accepted as stop evidence and its lease is released. A
detached helper could outlive it; this is the best-effort process-tree limit
recorded in [the cancellation boundary](research/remote-agent-host/cancellation-boundary.md),
accepted so routine pings do not quarantine the account. A kickoff timeout
still leaves the lease uncertain. The same decision applies to the short
Codex usage read (`codex app-server`): its direct child's exit releases the
lease. Enable also kickstarts a LaunchAgent that is loaded but has no running
process, so it never reports success with no worker running. The
LaunchAgent starts the worker with a hidden `--managed` flag; a manual
`openswap worker run` refuses while the LaunchAgent is loaded, checked under
the same lifecycle lock as the policy check and singleton acquisition, so a
racing enable cannot leave two competing workers. A worker whose control
socket fails to start (or that fails after creating its runtime) clears its
own health record before exiting, so status and disable never see it as a
stale live worker. Release treats every non-worker lease (kickoff and Codex
usage reads alike) as a short-lived probe, so a lease left uncertain by the
long-lived menu process can be released after expiry with
`--confirm-stopped`. A journal read that fails right after an interrupt also
propagates, and the loop's error handler never interrupts a run a second time
once an interrupt has already handled it. Worker status (and so disable)
counts an unresolved lease in either provider's store, and a kickoff with
Remote tasks off still refuses to run while a leftover lease is unresolved,
using a lock-free read so it adds no contention with switching.

## Deviations and verification

- Phase ordering overlaps only because the owner explicitly authorized local
  Phase 2 infrastructure while Phase 1 remains in progress. This does not
  waive Phase 1 evidence gates or authorize Phase 3, provider execution, or
  credentials.
- A second independent review of the harness found and fixed: recovery
  crashing on a corrupted journal instead of refusing; tracebacks leaking
  paths for non-`SpikeError` failures; recovery unable to see a live orphaned
  group; ancestor directories created with default modes and journal/lock
  files following symlinks; the fake child inheriting the operator's full
  environment; raw version output and arbitrary event tokens reaching
  evidence; a slow post-KILL reap flipping `cancelled` to `interrupted`;
  directory entries not fsynced after creating the journal or its ancestors.
  Follow-up Codex findings on the tracker were fixed too: descendants are
  identified by pid plus start time so a reused pid is never signalled, and
  the exception path terminates tracked detached descendants as well.
  Signalling uses a non-reusable pidfd where the OS offers one; on macOS the
  identity re-check and the signal remain separate operations, so evidence
  records `escaped_cleanup_certain: false` there rather than claiming safety.
  An exited but unreaped descendant (a zombie) counts as terminated via pidfd
  readability or its `Z` process state.
  The group leader is observed, not reaped, until group cleanup finishes, so
  the process-group id it reserves cannot be reused while it is signalled; a
  pidfd opened after a snapshot is discarded if the identity no longer matches.
  One process-table snapshot per loop iteration serves both the leader check
  and descendant attribution, and evidence records
  `descendant_tracking_complete: false` if any snapshot failed, because an
  escaped descendant could then have been missed; such a run is always
  `interrupted`, never `succeeded` or `cancelled`.
  Only the immutable start time decides whether a pid was reused; a change
  of process group or command line never evicts a live descendant, and a
  tracked entry whose original process is known to be gone (start time
  changed, or its pidfd reads as exited) is evicted before attribution so a
  reused pid never seeds discovery of a stranger's children. The version
  probe persists only a digits-and-dots version token.
  A freshly opened pidfd is refused if the pid's start time changed, proven
  ours if its parent is still in the run (group and command changes do not
  matter), and otherwise kept as unverified: tracked and reported, never
  signalled, still forcing `interrupted` if alive. Group termination observes
  the leader's exit without reaping it; the leader is reaped only after
  descendant discovery and cleanup. Children of an unverified entry are
  tracked but inherit its unverified status, so a possibly reused pid never
  makes a stranger's children signalable. The `inspect` and `sandbox-probe`
  runner tracks descendants the same way: a helper that calls `setsid()` is
  terminated and the probe result is refused, and the probe checks for exit
  before reading so output written just before exit is never dropped. A
  supervision failure at any point after launch, including the `running`
  journal append, snapshots descendants, terminates the group and sweeps
  detached descendants before reaping the leader. Without a pidfd, a
  descendant counts as gone only when its pid is free, it is a zombie, or its
  start time changed; a failed snapshot or an unexplained identity change is
  recorded as uncertain cleanup, never as terminated. A handle-less trusted
  descendant whose command line changes, or whose parent becomes a process
  outside the run other than init, loses trust (a same-second pid reuse keeps
  the start time); a group change alone does not. Process-table scans in the
  supervision and probe loops are bounded by the job's remaining time, so a
  slow `ps` cannot postpone cancellation. A live pidfd whose re-validation
  scan fails or runs out of time is kept as an unverified entry, never
  dropped. The escaped-descendant check reads the caller's last scan rather
  than scanning again, so a failed scan always reaches
  `descendant_tracking_complete`. Recovery's `group_still_alive` ignores
  zombies. A probe whose periodic descendant scan fails is refused. The
  runtime budget starts at launch, before the first journal append. A
  periodic scan never runs past the deadline and none starts in its last
  0.25 s, and scans during cleanup are bounded as well. A state or evidence
  path that traverses a symlink not owned by root is refused. Probe cleanup
  waits, bounded, for killed group members to actually exit. A directory
  raced into existence during creation is revalidated, and the whole path is
  re-checked for symlinks afterwards. The event reader is stoppable: if a
  descendant still holds stdout, it is stopped and the pipe closed before the
  result is recorded. Only the exact macOS system links (`/tmp`, `/var`
  and `/etc` pointing at `/private/...`) are trusted in a state or evidence
  path, so running as root does not widen the exemption. Every existing
  directory on the path must be owned by the user or root and not be
  modifiable by others unless sticky, so no component can be swapped for a
  symlink after validation. The missing-path walk does not follow symlinks
  and the existing prefix is re-validated just before creation. Before any
  group signal, one short bounded scan attributes descendants, so even a run
  too short for a periodic scan records a helper that already detached.
  Without pidfds, escaped-helper cleanup takes one process-table scan per
  round, bounded by the cleanup deadline, instead of one unbounded scan per
  helper. Descriptor readiness uses `poll()` (no FD_SETSIZE limit), and a
  polling failure is never taken as process exit. The leader-exit fallback
  (`ps` state, where `waitid(WNOWAIT)` is unavailable) is bounded by the same
  deadline as the scans, and it never reaps: if no observation works the
  leader is treated as not known to have exited and the run ends
  `interrupted`. A failed final probe scan still sweeps
  descendants recorded by earlier scans. A pidfd verified by its re-read
  keeps that re-read's group and identity, so a child that called `setsid()`
  between snapshot and open is still recognised as escaped.
  Each has a regression test. None of this establishes provider behavior.
- The test-only missing-Claude fixture correction is a test determinism fix;
  it preserves the `ClaudeSwitchError` assertion and does not change the
  resolver or executable discovery behavior.
- Credential-free harness tests passed; `inspect`, inert-child `demo`, and
  synthetic Seatbelt `sandbox-probe` outcomes are in
  [the Codex spike record](research/remote-agent-host/spike-codex.md). This
  establishes only helper and local sandbox behavior, not provider execution.
- Codex PR review findings [P1 #4121377781](https://github.com/mar3co/openswap/pull/59#discussion_r4121377781)
  and [P2 #4121377796](https://github.com/mar3co/openswap/pull/59#discussion_r4121377796)
  are addressed in the harness for descendants in the supervised process
  group, and malformed UTF-8 is retained as an `unstructured-output` event.
  These fixes have regression tests but still do not demonstrate provider
  `codex exec` behavior.
- The additional Codex review finding [P2 #4121533420](https://github.com/mar3co/openswap/pull/59#discussion_r4121533420)
  is addressed: local version/help launch errors and timeouts are returned as a
  sanitized `refused:` result. Parameterized tests verify both calls and both
  failure types without exposing exception output.
- Codex review findings [P2 #4121649254](https://github.com/mar3co/openswap/pull/59#discussion_r4121649254)
  and [P2 #4121649264](https://github.com/mar3co/openswap/pull/59#discussion_r4121649264)
  are addressed: stdout draining now bounds each line and retained event names,
  and sandbox-probe launch/timeout/decode failures produce sanitized refusals.
  Regression tests exercise noisy output and each sandbox subprocess failure.
- Codex review finding [P2 #4121751784](https://github.com/mar3co/openswap/pull/59#discussion_r4121751784)
  is addressed: oversized integer and deep-JSON parser failures become
  `unstructured-output`, and the reader continues to record the next valid
  event. Python 3.14 parses the nested fixture, so the test injects a bounded
  `RecursionError` for that input to exercise the failure path.
- Codex review finding [P2 #4121829706](https://github.com/mar3co/openswap/pull/59#discussion_r4121829706)
  is addressed in the Slack example: an opaque workspace ID maps locally to a
  writable research/output workspace plus a separately approved read-only
  checkout; caller-supplied paths are not accepted. Finding [P2 #4121829716](https://github.com/mar3co/openswap/pull/59#discussion_r4121829716)
  is covered in credential-free CLI probes by cleaning observed members of
  their owned process groups after timeouts or leader exit. No provider
  execution is implied.
- New Codex review finding [P2 #4122008642](https://github.com/mar3co/openswap/pull/59#discussion_r4122008642)
  is **REPRODUCED / MITIGATED BEST-EFFORT**. An inert disposable fake process
  showed `_run_probe` returned success while a helper that called `setsid()`,
  closed inherited output, and wrote a heartbeat remained alive. The fake
  supervisor now snapshots the process table while the leader runs,
  attributes descendants by parent pid regardless of group, terminates any
  that escaped, records them in evidence and forces `interrupted`; the
  reproduction is retained as a regression test. A descendant that forks
  between the final snapshot and leader exit can still escape, so the
  complete-process-tree cancellation gate remains blocked.
  See the [cancellation boundary assessment](research/remote-agent-host/cancellation-boundary.md)
  for documented macOS mechanism limits and the current gate consequence.
- With the new bounded probe runner, the already-hash-verified stable `0.157.1`
  binary was rechecked: `inspect` reported `codex-cli 0.157.1` and
  `no_auth_performed=true`; the synthetic `sandbox-probe` allowed workspace
  read/write and denied sibling read/write plus synthetic `CODEX_HOME` auth and
  config reads, with per-operation start/completion/status/denial markers. A
  later Seatbelt-initialization failure or partial marker output now refuses
  instead of counting as a passing denial. This remains wrapper-only evidence;
  no `codex exec` or provider job was run.
- Codex review findings [P2 #4122150605](https://github.com/mar3co/openswap/pull/59#discussion_r4122150605)
  and [P2 #4122150621](https://github.com/mar3co/openswap/pull/59#discussion_r4122150621)
  are addressed in the probe harness: sandbox results require per-operation
  completion/status/denial evidence, and stdout/stderr collection is bounded
  at 256 KiB per stream with owned-group cleanup on overflow. Targeted harness
  tests passed (`40 passed`); the latest full suite passed on this assembled branch.
  The detached-descendant limitation remains unresolved.
- Codex review P1 [#4122476457](https://github.com/mar3co/openswap/pull/59#discussion_r4122476457)
  is addressed in the TLS plan/decision wording: public-facing control URLs use
  HTTPS; the reference HTTP listener is limited to loopback or owner-controlled
  TLS termination on a private link. P2 [#4122476468](https://github.com/mar3co/openswap/pull/59#discussion_r4122476468)
  is addressed in the Slack scenario: full task preview and approval stay
  private, with only a redacted approval reference, state, and link in the shared
  thread. P2 [#4122476465](https://github.com/mar3co/openswap/pull/59#discussion_r4122476465)
  is addressed in the credential-free fake harness with a POSIX state-directory
  lock shared by supervision and recovery; concurrent duplicate launches are
  refused. This is not product-worker admission or lease enforcement, which
  remains out of scope until later phases.
- Codex review [P2 #4122751355](https://github.com/mar3co/openswap/pull/59#discussion_r4122751355)
  is addressed in fake-harness evidence: launch, supervision-result, and
  recovery rows now carry `job_id`, with existing tests checking attribution
  across two recovered jobs. The detached-descendant cancellation gate remains
  unresolved.
- Codex review findings [P2 #4122860298](https://github.com/mar3co/openswap/pull/59#discussion_r4122860298)
  and [P2 #4122860316](https://github.com/mar3co/openswap/pull/59#discussion_r4122860316)
  are addressed in the fake harness: existing POSIX output directories are
  never chmodded and must already be private, and timeout/grace values must be
  finite before state creation or process launch. The targeted harness suite
  passed (`40 passed in 6.06s`).
- Codex review [P2 #4122976297](https://github.com/mar3co/openswap/pull/59#discussion_r4122976297)
  is addressed in the fake journal appender: before appending it separates an
  unterminated trailing record with a newline, preserving the existing bytes.
  The recovery regression verifies one successful recovery after a torn tail
  and no repeated recovery on the next pass.
- `uv` was not installed on `PATH`; version 0.12.19 was installed only under
  `/private/tmp/openswap-uv-test`, with its Python, cache, and project test
  environment under `/private/tmp`. The full suite used that isolated runtime.
- No Docker, Postgres or Supabase was run. No real provider credentials or
  Keychain entries were touched; synthetic auth sentinels were used only under
  `/private/tmp` for the Seatbelt boundary probe.
- PR [#59](https://github.com/mar3co/openswap/pull/59) is open and ready for
  review against `main`; its review/merge is Phase 1 signoff and does not clear
  the remaining technical evidence gates or block the owner-authorized local
  Phase 2 work.
