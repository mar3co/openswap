# Plan 017: Remote Agent Host progress

Plan 017 is the canonical scope and phase order. Supporting research informs
the work but does not override the plan. This log tracks the current gated
execution; it does not mean that remote execution is implemented or enabled.

## Current status

**Phase 1: signoff PR merged, live-evidence exit gates still BLOCKED.**
PR #59 merged to `main` on 2026-09-30 as merge commit `39438ca`. Merging it is
the review signoff only; it does not substitute for the technical evidence
gates below, all of which still require an owner-authorized Codex account
context and an authenticated `codex exec` run. On 2026-09-28 the owner
explicitly authorized Phase 2 local-worker infrastructure to proceed in
parallel with their Phase 1 work. This changed sequencing only: no Phase 1
gate is waived or marked passed. PR #60, the local-only Phase 2 worker, merged
to `main` on 2026-09-30 as merge commit `225211d` with CI green on macOS,
Linux and Windows and a clean Codex review (56 resolved threads). Remote
access remains default-off and live Codex execution stays disabled. Phase 3
credential-free scaffolding is implemented in the five-PR stack
#63–#67 under the owner's explicit authorization to overlap the open Phase 1
gates. On 2026-09-30 the stack was fully reviewed (six sub-reviews) and the
fixes were pushed to every branch. PRs #63–#67 merged on 2026-10-01 (main
`ab737eb`) with five P2 Codex threads left open; PR #68 fixed all five and
most of the Windows/macOS timing flakes, merging on 2026-10-02 (`2e714a3`).
[mar3co/openswap#71](https://github.com/mar3co/openswap/pull/71) (merged 2026-10-02 as `c9eccfb`) fixed the reference-store starvation
behind the Windows failures that remained. Its local
protocol/service/client evidence does not pass Phase 1 or the Phase 3 network
exit gate. The production Codex adapter remains disabled. The Phase 4 OpenTag
connector merged to OpenTag `main` on 2026-10-03 (mar3co/opentag#137–#141,
follow-ups #143–#144) and is deployed; its exit is still open (see Phase 4).

The next unblocking step is Phase 1 live evidence, and the code for it is now
in review (see [Live path prepared](#live-path-prepared-2026-10-07)): the
owner pins a Codex account, installs the pinned CLI, signs that account in to
its isolated Codex home and runs `openswap worker live-check` on their
MacBook Pro. That command runs real `codex exec --json` jobs, records
pass/fail evidence for every Phase 1 gate and, only if all pass and the owner
says yes, enables live execution. Until it has run, every result recorded here
is synthetic.

The baseline `uv run pytest` completed before this branch's changes: 2870
passed, 4 skipped, 1 failed, 3 warnings (15.15s). The failure is
`tests/test_menubar.py::test_launch_claude_login_missing_claude`: its injected
`which` reports no executable, but the resolver also probes hard-coded
install locations and finds this machine's `/opt/homebrew/bin/claude` (2.1.274).
The isolated test still fails with a temporary HOME and `PATH=/usr/bin:/bin`.
A narrow test-only correction now simulates the missing-resolver result without
changing product behavior; the isolated test passes (`1 passed in 0.74s`).
The latest Phase 1-only assembled suite was green: `3016 passed, 5 skipped, 3
warnings in 47.84s`; it includes 144 phase-one harness tests and two direct
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
- [x] Both decision memos delivered; control service decided. Claude path
  decided by the owner on 2026-10-07 (see "Claude accounts" below).
- [x] The phase-one evidence, adapter contract and reproducible harness are
  reviewed and PR #59 is merged as Phase 1 signoff (2026-09-30, `39438ca`;
  Codex review clean on `7d1f9c8`, 78 resolved threads). Signoff is not
  evidence: the unchecked gates above still block real Codex execution.
- [x] Full OpenSwap pytest suite is green on the assembled phase-one branch
  (`3016 passed, 5 skipped, 3 warnings in 47.84s`).

The control-service decision is recorded: the protocol specification, worker
client and MIT reference server live in this repository, OpenTag implements
the same protocol in its own repository, and the worker selects a backend by
URL. The phase-3 reference server is therefore a product deliverable, not a
movable placeholder. The Claude memo's auth-path answer was recorded by the
owner on 2026-10-07: unmodified Claude Code with the owner's native login, on
the owner's own paired Macs, for tasks the owner starts (see "Claude accounts"
below).

## Phase status

| Phase | Status | Exit evidence / blocker |
| --- | --- | --- |
| 1. Feasibility spike and authentication gate | SIGNOFF MERGED (#59) / LIVE PATH IN REVIEW / EVIDENCE PENDING OWNER RUN | A hash-verified stable 0.157.1 passes the synthetic low-level Seatbelt wrapper probe, but no authenticated `codex exec` proves account selection, refresh behavior, structured provider events, complete process-tree cancellation/recovery, or model/tool enforcement integration. The fake `setsid()` reproduction showed the wrapper could return success while a detached helper remained alive; the harness now detects and terminates tracked escaped descendants and reports `interrupted`, but cannot close the fork/reparent race. No owner-authorized Codex slot or exclusive live-auth ownership is established. Control-service decision is recorded. PR #59 merged 2026-09-30 (`39438ca`) as review signoff; merging does not satisfy the remaining technical exit gates. Next step: an owner-authorized account context and an authenticated `codex exec` run. The live adapter, per-job launchd containment with a coalition stop proof, and the `live-check` evidence harness are in review (2026-10-07); the owner's `live-check` run is what clears these gates. |
| 2. Local worker, remote access off | MERGED (#60) — local-only | Owner authorized local infrastructure to overlap Phase 1; no Phase 1 gate is waived. PR #60 merged 2026-09-30 (`225211d`); CI green on macOS, Linux and Windows; Codex review clean on `556ca11`. Fake-only validation is recorded below. Remote access stays off, the production Codex adapter still refuses, and live Codex stays disabled. The follow-up that lease release must also prove the provider process tree has stopped was closed on 2026-10-07 (see the lease release paragraph below). |
| 3. Private remote pilot | MERGED (#63–#67, follow-ups #68) / EXIT OPEN | Owner explicitly authorized credential-free Phase 3 overlap. The protocol, reference server, polling/heartbeats, enrollment/status and guarded test CLI have loopback fake-adapter evidence. Reviewed in full on 2026-09-30; three HIGH bugs (launch after abandon, artifact-failure wedge, dying remote thread) and the MEDIUM findings were fixed before #63–#67 merged on 2026-10-01 (`ab737eb`). #68 (2026-10-02, `2e714a3`) fixed the five P2 Codex threads left open at merge and most CI timing flakes; [mar3co/openswap#71](https://github.com/mar3co/openswap/pull/71) (2026-10-02, `c9eccfb`) fixed the reference-store starvation behind the rest. Real owner-controlled HTTPS deployment, submission from another network, backend replacement, and retention/purge plus an audit trail in the reference server still need the owner. Production Codex remains disabled pending Phase 1. |
| 4. OpenTag connector | MERGED (mar3co/opentag#137–#141, follow-ups #143–#144) / DEPLOYED / EXIT OPEN | Credential-free overlap authorized by the owner; fake-adapter evidence only. Merged 2026-10-03; production migrations applied and agent/portal deployed the same day; worker dispatch stays off unless a workspace grants the `worker-dispatch` scope. Scope tracked in mar3co/opentag#135. Exit needs both an authorized OpenTag request that completes live research on the owner's Mac and returns citations/artifacts through a short initial tool call (blocked on Phase 1's live-adapter gates; fake-adapter runs do not count) and the staging Slack scenario, including non-owner refusal, cited private results and state-only shared updates. See the Phase 4 section below. |
| 5–6 | NOT STARTED | Require phase 4; its code is merged but its exit is still open. |

## Phase 2 local-worker plan (owner-authorized overlap)

The owner authorized local Phase 2 infrastructure to overlap the open Phase 1
review. The authorization does not pass or waive any Phase 1 gate. Phase 2 is
limited to an opt-in local worker: remote access remains off, the production
Codex adapter always reports unavailable, and no provider account or job is
used. Phase 3's control server, enrollment, polling and result upload were not
part of the Phase 2 slice; the later Phase 3 work is recorded below.

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
3 scaffolding is implemented; its owner network pilot remains open.

The Phase 2 full-suite validation on the currently integrated PR #59 snapshot
`f684ebf` is green: `3082 passed, 4 skipped, 3 warnings in 20.02s`. The run
used the existing test environment with narrowly elevated permissions for
disposable AF_UNIX sockets and fake-process supervision. The final PR #59
head (`7d1f9c8`) was merged into this tree before PR #60 merged. After the
PR #60 review-fix pass below, the full suite on this tree remains green:
`3221 passed, 5 skipped, 3 warnings in 47.97s`. The merged `main` at
`225211d` passed CI on all three jobs (`test`, `test-windows`,
`macos-keychain`).
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
The same live-`auth.json` resolution applies when the menu bar passes the
engine's own Codex home instead of `None`; only a slot directory uses the
roster record.
A personal Claude roster row with a null `organizationUuid` matches the
live personal login, normalized to `""` as the engine's identity paths do.
A kickoff `OSError` releases its lease as unlaunched only when the default
runner shows `Popen` itself failed; an `OSError` after launch, or from an
injected runner, leaves the lease `uncertain`.
A blocked `worker disable` now pauses without changing the opt-in and
restores the previous `enabled` value, so retrying it on an already
disabled worker never re-enables Remote tasks. The same holds when the
LaunchAgent unload fails: the prior opt-in is restored, paused.
Provider `probe()` and `start()` now run outside the control lock, so a
stop during a slow start is acknowledged at once. Journal and lease steps
stay under the lock, the cancellation is re-read right before launch, and
a stop that lands during `start()` makes the RUNNING write stale, so the
started run is interrupted rather than replayed.
`start()` itself runs on a tracked thread, so a stop, shutdown, opt-out or
the runtime limit is enforced while it runs. After a stop, start has a
2 s grace to return a handle that can be interrupted with proof; a start
that still has not returned is abandoned: the job is interrupted, the
lease is quarantined, admission stays blocked until the thread exits, and
a late handle is interrupted best effort (never stop proof).
The runtime limit starts when `start()` is called, so time spent starting
counts against it instead of the running loop granting a fresh limit.
A shutdown or opt-out that arrives while `probe()` runs is rechecked before
any lease or launch, so the job fails as `worker_disabled` without starting.
`worker lease release --confirm-stopped` now also works while the recording
worker is still running, once the journal shows its job terminal: the
worker has then dropped the lease and admits no work on the quarantined
account, so an unproven interrupt no longer requires killing the worker.
A still-active job keeps refusing.
The start thread re-reads the cancellation, expiry and shutdown fences
under the control lock immediately before calling `start()`; if any fired,
nothing is launched and the lease is released as unlaunched. An abandoned
start keeps its lease `start_pending` until the call returns, and a
live-worker release is refused until then.
A kickoff that runs without a lease (Remote tasks off) holds a per-provider
unleased-run lock for its whole run, and enabling Remote tasks must win
both providers' locks before writing the opt-in, so the worker can never
be enabled and lease an account while such a kickoff is using it (enable
reports `kickoff_in_progress`). A kickoff that waited on an enable re-reads
the policy and takes the leased path.
Codex OAuth onboarding (`add_oauth_account`) now holds the same lease
mutation guard as every other Codex roster write.
Purge holds both unleased-run locks too, refusing while a kickoff runs, and
keeps their lock files (like the other lock anchors) while it removes the
lease documents.
The start thread marks the launch committed atomically with its final
fence; a stop after that point is still journaled and enforced by
interrupting the run, but is acknowledged as `stop_after_launch_committed`
rather than implying it prevented the launch. Making the boundary itself
atomic would mean holding the control lock across `start()`, which the
earlier responsiveness fix removed.
The read-only status fallback opens a fully checkpointed journal with
`immutable=1`, so it never recreates WAL sidecars and works in a directory
it cannot write. WAL content with its `-shm` present is read in place with
`mode=ro`; WAL content without `-shm` (no live writer) is read from a
private temporary copy, so status never recreates a sidecar.
Claude `remove_account` now runs the legacy org-field roster migration under
the lease guard, as move already did, so a blocked removal cannot rewrite
`sequence.json` first.
The worker's pid liveness probe no longer uses `os.kill(pid, 0)` on Windows,
where signal 0 is `CTRL_C_EVENT` and interrupts every console process; it
uses `OpenProcess`/`GetExitCodeProcess` and treats anything unclear as alive.
The control-socket path no longer needs `os.getuid`, so worker status and
purge work on Windows (where the worker never runs); the AF_UNIX IPC tests
are POSIX-only and wait for the server's ready signal instead of the socket
file, which exists before `listen()`.
The IPC client reports the worker unavailable on hosts without `AF_UNIX`,
so status falls back to the read-only snapshot and purge works on Windows.
A blocked `worker disable` now reports the persisted opt-in instead of
always saying the worker remains enabled.
The CLI now rejects a stale `cswap` launcher before dispatching to the worker
subcommand, so `cswap worker status` gets the removed-command message instead
of reaching the worker CLI. Codex `set_account_disabled`, `set_alias`, and
`unset_alias` now hold the same lease mutation guard as the engine's other
roster mutations. `_resolve_workspace` now refuses an approved read-only
source that others can write to (readable by others remains allowed).
The lease release command now also requires proof that the provider process
tree stopped (closed 2026-10-07). For a worker job lease whose recording
worker is gone and whose job is terminal, it releases without
`--confirm-stopped` only when the journal holds a `provider_finished` event
with `execution_stopped` for that job. The runtime journals that event from
the adapter's own report before the job turns terminal. Every other path to a
terminal state releases or quarantines the lease itself first, so a terminal
state alone (`succeeded`, `failed`, `cancelled` or `expired`) no longer
releases a stranded lease; the owner must confirm. Fake-adapter tests cover
the journaled proof, terminal states without it, an expired job, and a crash
between the provider's proof and the lease release. This check is only as
strong as the adapter's `execution_stopped` report: a real adapter may set it
only after verifying its whole process tree has exited, which is Phase 1's
still-blocked complete-process-tree cancellation gate.
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

- Phase ordering overlaps because the owner explicitly authorized local
  Phase 2 infrastructure and subsequently credential-free Phase 3 scaffolding
  while Phase 1 remains in progress. Neither authorization waives Phase 1
  evidence gates or authorizes live provider execution or provider credentials.
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
  between snapshot and open is still recognised as escaped. A probe that
  fails mid-run scans descendants before signalling its group, while a
  helper that detached since the last scan is still the leader's child. A
  directory raced in during creation must pass the ownership and permission
  check before anything is created beneath it. When tracking was incomplete,
  cleanup also sweeps live pidfd descendants still cached in the leader's
  group, since one may have called `setsid()` after its last scan. A timeout
  terminates the group before journaling `cancel_requested`, so stalled
  storage cannot keep a provider running; a failed append leaves the
  recoverable `running` row. macOS group scans during termination are
  bounded by the grace deadline, so a stalled `ps` cannot delay SIGKILL.
  Every other group-liveness check is bounded too: by the probe's runtime
  deadline while it runs, by the post-KILL wait deadline, and by a short
  window for the final checks, where a stalled scan counts as still running.
  A probe that times out, overflows or leaves group members scans
  descendants once more before its group is signalled, so a helper that
  detached since the last periodic scan (or in a run too short for one) is
  still found and swept. `cancel_requested` is journaled only after both the
  group and every tracked escaped descendant were signalled. Only a
  recognised terminal state proves an outcome: any other journal state (a
  newer worker's `queued`, a malformed string, or a non-string) is recovered
  as interrupted and blocks new launches until then. The `running` row is
  written off the supervising thread, so a stalled fsync cannot stop the
  deadline from being enforced; later rows wait for it to land, and every
  exception path joins it before the state lock is released. Whether a run
  timed out is decided at the original deadline: an exit observed after it,
  whether by a later check or an in-loop check that ran past it, is still a
  timeout, never a success. Probes apply the same rule to the leader's exit
  and to the quiet-group check, timing out as soon as either observation
  crosses the deadline. Both runtime budgets start just before `Popen`, so
  time spent creating the process counts toward them.
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
- PR [#59](https://github.com/mar3co/openswap/pull/59) merged to `main` on
  2026-09-30 (`39438ca`) as Phase 1 signoff; it does not clear the remaining
  technical evidence gates. PR [#60](https://github.com/mar3co/openswap/pull/60)
  merged the same day (`225211d`) with the local-only Phase 2 worker.
- Windows CI on PR #60 found three bugs that the macOS-only development had
  missed, all fixed before merge: `os.kill(pid, 0)` on Windows sends
  `CTRL_C_EVENT` to the whole console, so pid liveness now uses
  `OpenProcess`/`GetExitCodeProcess` and treats an unclear answer as alive;
  the IPC socket path no longer needs `os.getuid`; and the worker client
  raises `IpcError("worker_unavailable")` when `socket.AF_UNIX` is missing,
  which had broken `worker status` and, through it, `purge` on Windows. The
  plan still targets macOS; these fixes keep the shared code and tests
  honest on the other CI platforms. A macOS IPC test race (connection
  refused before the fake server listened) was fixed with a readiness event.

## Phase 3: private remote pilot scaffolding

**Implementation merged (#63–#67, main `ab737eb`); network exit gate remains OPEN.** The owner
explicitly authorized credential-free Phase 3 work to overlap the still-open
Phase 1 gates. This is a sequencing deviation, not a provider-safety waiver.
The five branches were stacked, each on the one below, and retargeted to
`main` before merging bottom-up on 2026-10-01:

| Slice | Pull request | Scope |
| --- | --- | --- |
| 3a | [#63](https://github.com/mar3co/openswap/pull/63) | [v1 protocol](../docs/worker-protocol.md), strict wire types, default-None service URL |
| 3b | [#64](https://github.com/mar3co/openswap/pull/64) | single-owner SQLite/http.server control service and operator CLI |
| 3c | [#65](https://github.com/mar3co/openswap/pull/65) | outbound HTTPS transport, durable claim bindings, heartbeat/lease/event/result reconciliation |
| 3d | [#66](https://github.com/mar3co/openswap/pull/66) | local pairing, Keychain-only key, unpair, daemon configuration, CLI/menu connectivity |
| 3e | [#67](https://github.com/mar3co/openswap/pull/67) | guarded test-only submit, loopback end-to-end tests, progress documentation |

Tests start the reference HTTP server on loopback in-process, approve pairing
through the CLI with a mocked login Keychain, submit through `submit-test`, run
one inert fake-adapter job, retrieve cursor events and SHA-256-verified result
bytes, and prove unlisted files stay local. A second test drives configured
background polling using readiness events. This proves the credential-free
transport and durable integration, **not** live research, provider isolation,
real TLS termination or submission from another network. No provider binaries,
provider credential stores, real Keychain items or launchctl state are used.

Unit tests additionally cover offline admission rejection, duplicate creation,
claim/worker fences, lease loss without killing running local work, revocation,
pre-launch expiry/cancel/authorization checks, reconnect cancellation and true
outcomes after service interruption, upload-response replay, unknown outcome
recovery without relaunch, explicit artifact limits and non-loopback HTTPS
requirements. Production execution still uses `UnavailableCodexAdapter`.

| Branch head | Full suite | Phase 3 tests serially |
| --- | --- | --- |
| 3a (as opened) | 3242 passed, 5 skipped | 21 passed |
| 3b (as opened) | 3251 passed, 5 skipped | 30 passed |
| 3c (as opened) | 3267 passed, 5 skipped | 46 passed |
| 3d (as opened) | 3280 passed, 5 skipped | 59 passed |
| 3e (as opened) | 3291 passed, 5 skipped | 70 passed |
| 3d fixes merged into 3e (`c9ef38d`) | 3373 passed, 5 skipped | 152 passed |
| 3e fixed head (`f74c6dc`) | 3419 passed, 5 skipped | 198 passed |
| 3d round-two head (`e53d4b3`) | 3393 passed, 5 skipped | 172 passed (five modules; the e2e module lives in 3e) |
| 3e round-two head (`0031697`) | 3450 passed, 5 skipped | 229 passed |
| 3e round-three head (`9153334`) | 3482 passed, 5 skipped | not re-run serially |
| 3e round-four head (`ffe0994`) | 3502 passed, 5 skipped | not re-run serially |
| 3e round-five head (`b5bd3b6`) | 3509 passed, 5 skipped | not re-run serially |
| 3e round-six head (`ad612d1`, merged as `ab737eb`) | 3516 passed, 5 skipped | not re-run serially |
| #68 follow-ups (`95b3bc3`) | 3527 passed, 5 skipped | not re-run serially |

Full suites use `uv run pytest -q`; serial Phase 3 runs use `-n0` with the
six Phase 3 test modules (the review pass added
`tests/test_worker_launch_abandon.py`). The inherited desktop `CODEX_CLI_PATH` override
is removed for test isolation and the uv cache stays under `/private/tmp`.
Each completed full run has the same three existing pytest fixture warnings.
Socket and synthetic-process tests require local execution permission. These
are macOS results. While stacked, only 3a received CI; each slice then ran CI
(`test`, `test-windows`, `macos-keychain`) after retargeting to `main`, and
#68 passed all three on its final head. Linux/Windows behavior uses portable
HTTP/SQLite paths and mocked macOS enrollment.

### Review and fix pass (2026-09-30)

The implementing agent opened PRs #63–#67. The reviewing session then reviewed
the whole stack in six sub-reviews (one per PR plus a security pass over the
stack), confirmed all twelve pre-existing Codex bot comments against the code,
and reproduced the HIGH findings with scratch tests before any fix. Fixes were
applied bottom-up on the existing branches, each merged forward into the next
(no force-push, no rebase), with a regression test per behavioural fix:
3a `f210c97`, 3b `2e65e5e`, 3c `1df969c`, 3d `424d74e`, 3e `f74c6dc` in the
first round. A second round answered the Codex re-review of those fixes the
same way (each branch merged forward, one regression test per fix), with heads
3a `967b35f`, 3b `1994296`, 3c `64c7b02`, 3d `e53d4b3`, 3e `0031697`. A third
round answered the next Codex re-review (3a `6c1b5ed`, 3b `eb13955`, 3c
`b4aee58`, 3d `29d1da0`, 3e `9153334`), and a fourth the one after that (3a
`412e25d`, 3b `fc9bdae`, 3c `c5747d3`, 3d `74c30db`, 3e `ffe0994`), and a
fifth after that (3a unchanged with a clean Codex review, 3b `f35892e`, 3c
`c435f8a`, 3d `cb4dc6b`, 3e `b5bd3b6`). Codex then approved 3a, 3b and 3e; a
sixth round closed the last 3c/3d findings (3c `1f5f1c9`, 3d `18ea392`, 3e
`ad612d1`). All five PRs are now merged to `main`; the network and live-provider evidence gates below remain open.

HIGH bugs fixed:

- **Launch after abandon (3d).** Moving the remote authorization guard off the
  launch lock let a stop or deadline that landed while the guard was blocked
  abandon the job to `interrupted`, after which the guard returned and the
  provider still started. The launch fence now re-checks the abandoned flag
  and requires `starting` under the lock, releasing the lease `unlaunched`.
- **Artifact-failure wedge (3c).** A permanently refused artifact (oversized,
  symlinked, conflicting) repeated every tick, never wrote `done=1`, and
  blocked every later claim while the state flipped to offline. Per-artifact
  validation failures are now terminal for that artifact only (skipped, with
  an `artifact_rejected` diagnostic) and the claim completes.
- **Dying remote thread (3c).** `tick()` caught a fixed exception tuple, so a
  `RuntimeError` subclass from the adapter probe or a journal write ended
  heartbeating for the process lifetime. `run()` now guards every tick and the
  probe is wrapped like the runtime does.

MEDIUM findings fixed across the stack: the local job ID is reserved in the
binding before admission (the root of the 3e e2e flake, whose `admitted`
signal now comes from the durable binding write); the remote idempotency key
hashes the remote ID so 200-character IDs cannot overflow; `cancel_requested`
must be a real boolean and `state` a known value; `stale_epoch` on heartbeat
re-registers instead of reporting online; the heartbeat cancel list is
filtered to enforceable states; multiline tasks are valid while other control
characters are refused; size-0 artifacts are fully validated; `validate_url`
degrades non-strings and whitespace to disabled with a warning and returns a
normalized origin; `runtime_limit_s` 600 and 600.0 share one canonical form;
`unpair [url]` recovers an orphaned Keychain item; `submit-test` gained
`--idempotency-key`/`--expires-at` with a printed retry hint and validates
the service response; the transport surfaces every documented error code; a
`cursor_conflict` ends the claim with an uncertain outcome instead of an
endless offline retry. `docs/worker-protocol.md` was updated wherever wire
behaviour changed.

Round two (Codex re-review of the fixes, same day) closed:

- **Heartbeat thread (3c).** `RemoteClient.run` heartbeats on its own thread at
  the fixed cadence and synchronizes on `openswap-worker-remote-sync`, so a
  slow pass (probe, event pages, uploads) can no longer cost liveness and get a
  running job spuriously `interrupted`; the heartbeat thread also renews the
  admitted claim's lease. `ConfiguredRemote.run` (3d) drives the same two halves,
  re-reading the enrollment on the heartbeat thread, and both threads write
  `remote-status.json` from the shared connectivity.
- **Confirmed interruptions (3b).** A reconciled `interrupted` with
  `execution_stopped: true` or `unlaunched: true` is terminal like every other
  confirmed outcome and leaves `cancel_job_ids`.
- **Register liveness (3b).** `register` no longer marks the worker live; only
  a heartbeat does, so `submit`/`poll` answer `offline_worker` until then.
- **Unpair/launch boundary (3d).** `unpair` clears the URL before deleting the
  key; the launch guard returns the URL and worker ID it verified, and the
  runtime re-reads `settings.json` under the launch lock before committing, so
  after `unpair` returns no uncommitted remote launch can start.
- **Registration kept on local faults (3d).** A failed status write or locked
  Keychain no longer discards the client and re-registers (which interrupted
  the service-side jobs of the replaced registration); it reports `offline`.
- **Retry after expiry (3e).** An explicit `--idempotency-key` with an explicit
  `--expires-at` is sent even after that expiry, so the service's idempotent
  replay returns the original job; the retry hint is shell-quoted.
- Smaller items: connectivity keeps all five values with `expired` final like
  `revoked`; `validate_url` lowercases scheme and host and every enrollment
  path hashes the normalized origin; the timestamp grammar bounds fields and
  refuses leap seconds; `runtime_limit()` enforces the 14,400 s bound and
  overflowing timestamps map to `invalid_request`; terminal `state_changed`
  events are stored, never applied.

Round three (the next Codex re-review, same day) closed:

- **Heartbeat deadline (3c, 3d).** Heartbeats are due on an absolute schedule
  and lease renewal moved to its own `openswap-worker-remote-renew` thread, so
  consecutive heartbeats reach the service at most one period plus one request
  timeout apart. `ConfiguredRemote.run` does the same.
- **Bindings per enrollment (3c, 3d).** The remote journal is keyed by service
  URL and enrollment (worker ID, or a key digest without one), so a replacement
  device paired against the same URL never inherits an unreadable binding.
- **Provisional interruptions (3c).** An `interrupted` outcome without proof
  keeps its binding pending while the job's account lease is still held or
  quarantined; a later `confirmed_stopped` release is reconciled as confirmed.
- **Re-pair of the same URL (3d).** Pairing records the worker ID in
  `settings.json` and the launch commit refuses an authorization from any other
  worker; the configured guard authorizes only while its captured client is
  still the active one.
- **Reference server (3b).** `register` clears the previous incarnation's
  liveness; a late cancel leaves a confirmed interruption alone; only
  `state_changed` events move the job; `cancel_job_ids` is capped at 100 (live
  jobs first, then the newest unconfirmed interruptions).
- Smaller items: `waiting_for_approval` fails closed at admission and in the
  launch fence; service responses with duplicate keys or non-finite numbers
  are refused; a `job` response must match the claim's ID and epoch; claim
  expiry is judged on the service clock; offsets through `±23:59` are accepted;
  `validate_url` drops default ports; `unpair <url>` says when only an orphan
  was removed; a `submit-test` retry has no age limit on a past expiry.

Round four closed:

- **Key renewal keeps the worker (3b).** `pair-code --renew <worker_id>` issues
  a renewal code; pairing with it rotates that worker's key and expiry in place,
  so its jobs, events and artifacts (and the Mac's journal bindings, keyed by
  worker ID) carry over. Revoked workers cannot be renewed.
- **Cancellation off the heartbeat path (3c).** Heartbeat `cancel_job_ids` and
  renew's `cancel_requested` are queued and applied by the renewal thread to
  any still-running local job, including one whose binding ended on a
  `cursor_conflict`; a blocked synchronization pass no longer delays a cancel.
- **Pairing (3d).** Saved connectivity status is scoped to the paired worker
  ID, and `pair`/`unpair` run as whole transactions under a cross-process lock.
- Smaller items: a malformed artifact digest is `invalid_request`; an
  unadmitted claim that is both interrupted and cancelled reconciles as
  `cancelled`; the retry hint uses `--flag=value` so dash-prefixed keys survive.

Round five closed: key rotation clears liveness like registration; a
`cancel_requested` event after a heartbeat interruption keeps the cancel flag;
the Mac's journal is keyed by worker ID so renewal keeps bindings; heartbeat
cancel lists over 100 are malformed; the `remote:` idempotency prefix is
reserved for remote admission; the configured renewal loop drains queued
cancellations; `pair`/`unpair` migrate legacy data first; and a binding that
ended on a `cursor_conflict` still reports its proven outcome once the run
stops.

Round six closed: a binding retires only on a reconcile acknowledgement that
names the same job and state, and an upload only on one that echoes the sent
artifact's name, size and digest; saved connectivity status is judged fresh on
the Mac's receipt time (the service timestamp stays for display), and every
pairing change, including a renewal, deletes the old status (#68 later
narrowed this to changes of the configured enrollment).

### Follow-ups after merge (#68, 2026-10-02)

Five Codex P2 threads were unresolved when the stack merged. PR #68
(`2e714a3`) fixed each with a regression test, replied on the thread and
resolved it:

- **Rejection diagnostic after a restart (#65).** A job that ended under an
  earlier runtime epoch could not take the `artifact_rejected` diagnostic, so
  the binding retired with neither the artifact nor the record.
  `LocalJobStore.append_terminal_diagnostic` now records it on any terminal
  row, fenced by the current epoch and the row generation. If the write
  fails, the binding stays pending. The diagnostic is recorded once per job.
- **Lost reconcile response for an unadmitted claim (#65).** A terminal
  remote state other than a provisional `interrupted` is replayed as is, so
  the retry no longer conflicts with the confirmed `failed`. A `succeeded`
  held by the service is retired without a reconcile.
- **Saved status (#66).** `remote-status.json` is deleted only after the
  configured enrollment changes (a successful pair, or an unpair of the
  configured URL). A refused pair, a failed exchange or an unpair of an
  orphan keeps it.
- **Abandonment acknowledgement (#67).** The `cursor_conflict` abandonment
  reconcile goes through `_reconcile()`; an empty or mismatched
  acknowledgement keeps the binding pending.
- **`stale_epoch` during abandonment (#67).** It propagates and the binding
  stays pending. The next heartbeat detects a superseded worker epoch and
  re-registers. `_abandon` does not reset the epoch itself: the same error
  can mean only the claim's epoch is stale, and re-registering every tick
  would interrupt the device's other jobs.

#68 also steadied the Windows CI timing flakes. The expiry-during-launch test
moves the deadline inside `probe()` instead of racing a 0.3 s clock. The
launch-readiness waits are longer, the heartbeat cadence test checks that
heartbeats keep coming while poll is hung, and each wait phase has its own
deadline. It also raised the reference server's listen backlog from 5 to 64:
a real limit (a burst past 5 connections is reset even on macOS), but not the
cause of the Windows e2e failures, as first thought. Full suite on macOS:
3527 passed, 5 skipped.

The cause was found after #68 merged and was fixed in [mar3co/openswap#71](https://github.com/mar3co/openswap/pull/71),
merged 2026-10-02 as `c9eccfb`. The e2e test kept
failing on Windows with the launch guard's `renew` timing out, and the
server log showed WinError 10053 when the server wrote its reply: the client
had already given up. `ControlStore` makes two SQLite write transactions
per request. Under steady writes from several threads, SQLite's busy
handler, which polls with growing sleeps, can starve one waiter for its whole
5 s busy timeout, and the client's 5 s socket timeout expires first. Requests
in the server process now queue on an in-process lock. With eight threads
sending heartbeats on macOS, the slowest request drops from 1.1–2.5 s to
5 ms at the same throughput. That PR also fixes
`test_run_loop_survives_a_journal_error_and_keeps_ticking`, which now waits
for the retried cancel instead of a fixed number of sync passes.

[mar3co/openswap#72](https://github.com/mar3co/openswap/pull/72) bounds the
wait for that lock, closing #71's remaining Codex P2. A request that cannot
start within 2.5 s, half the client's 5 s deadline, is refused with 503
`service_unavailable` before it touches the database. Before this, a late
request ran for a client that had already given up; a late `pair` consumed a
code nobody received. The same PR widens the remaining 2 s launch-readiness
waits in the worker tests to 15 s, after one failed on Windows.

Still open after this pass (design gaps, not regressions):

- The reference service has no retention/purge or audit trail although the
  plan requires both; the PR bodies do not claim them.
- No real HTTPS deployment has been exercised; TLS is loopback-exempt only in
  tests, and truststore certificate checks are unverified against a real
  owner-controlled endpoint.
- No submission from another network with no inbound listener on the Mac.
- Backend switching by URL against an independently deployed compatible
  server is undemonstrated.
- Phase 1 live-evidence gates remain blocked; every recorded result is
  synthetic fake-adapter evidence.
- The production Codex adapter remains disabled (`UnavailableCodexAdapter`).

Remaining owner exit evidence:

- [ ] Deploy a real owner-controlled HTTPS service, with a private reference
  HTTP backend behind TLS termination; validate truststore certificate checks.
- [ ] Submit using the test CLI **from another network**, with no inbound
  network listener on the Mac, and observe outage/reconnect behavior there.
- [ ] Demonstrate backend replacement through URL configuration and enrollment,
  without code changes, against an independently deployed compatible server.
- [ ] Eventually clear Phase 1's live-adapter gates and run real research with
  the authorized local account; this stack does not enable that adapter.

Recorded scope/deviations: the reference service retains data until its owner
explicitly deletes the stopped database/backups; v1 has no remote retention or
delete API. Enrollment keys expire after 30 days and require local re-pairing.
The pilot exports only explicitly named `result.md` by default (at most eight
files, 1 MiB each), never directory discovery. The single-owner test CLI uses
the locally paired key; requester/provider accounts are not added. Pairing is
unsupported outside macOS, while HTTP/SQLite/fake-adapter tests are portable.
No third-party backend internals or connector implementation enter this stack.
The cancellation/process-tree gates and strict `execution_stopped is True`
lease-release checks remain intact.


## Phase 4: OpenTag connector (2026-10-02)

**Merged and deployed 2026-10-03; exit open; not a live-provider signoff.**
The owner explicitly authorized implementing the OpenTag connector while Phase 1
remains open, using the fake adapter for all execution evidence. The production
adapter stays `UnavailableCodexAdapter`. No provider authentication files, real
Keychain entries or launchctl state are accessed. Local database services are
not used; database validation runs in OpenTag PR CI.

OpenTag implementation and review stack:

- [Protocol server #137](https://github.com/mar3co/opentag/pull/137): durable v1
  operations, strict validation, owner-private audit and bounded retention.
- [Dispatch #138](https://github.com/mar3co/opentag/pull/138): dedicated capability,
  immutable private preview, atomic owner approval and idempotent submission.
- [Slack flow #139](https://github.com/mar3co/opentag/pull/139): owner-only command,
  private preview, redacted shared updates and durable two-tier notifications.
- [Owner portal #140](https://github.com/mar3co/opentag/pull/140): pairing, private
  approvals, job status/Stop and authenticated results.
- [Conformance #141](https://github.com/mar3co/opentag/pull/141): expanded fake-client
  outage/reconnect, approval races, renewal/revocation and backend-switch evidence.

The first two slices passed CI database regressions and a real pinned OpenSwap
client roundtrip against the independent server, using an inert injected adapter
and temporary settings/journals. This is synthetic loopback evidence, not staging
TLS, another-network operation, provider isolation or live research. OpenTag
imports the pinned OpenSwap Git package only as a test dependency; its server
implementation consumes the published protocol specification and does not vendor
OpenSwap source.

The owner chose host-owned device display labels and provider-usage annotations.
No v1 wire fields were added. The protocol also does not advertise approved local
workspace IDs; host-configured hints must not claim verified local availability.

One specification clarification arose from implementing a second server:
`interrupted` with both proof flags false is the provisional reconciliation already
required by the cursor-conflict path. Other terminal outcomes require stopped or
unlaunched proof, and success always requires stopped proof. This is a text
clarification, not a new operation or field.

Phase 3 exit evidence closed by this work so far: **none of the real-network
items**. The independent implementation adds cross-server synthetic conformance;
real HTTPS/truststore, another-network submission without an inbound Mac listener,
outage/reconnect on that network and backend switching against independent HTTPS
origins remain owner-controlled pilot work. Phase 4 exit also remains open until
an authorized OpenTag request completes live research on the owner's Mac and
returns citations/artifacts through a short initial tool call, which needs Phase
1's live-adapter gates and the production adapter; fake-adapter evidence cannot
close it. The staging Slack scenario must also succeed, including non-owner
refusal, cited private results and state-only shared updates.

### Merge and production rollout (2026-10-03)

The stack merged to OpenTag `main` by squash after about ten Codex review rounds:
#137 `53b1bd4`, #138 `e91ef8b`, #139 `912ba1f`, #140 `42f482c`, #141 `3f2f0d9`.
The review fixed, among others, a Slack-to-portal identity link that nothing
populated (every Slack dispatch would have been refused), an admin path to map
their Slack ID onto another member, a replayed Slack event that could start two
jobs, and the conformance harness never launching its second job. Once a lower
PR merged, later SQL changes shipped as forward migrations
(`20261003010000_remote_worker_approval_owner_idempotency`,
`20261003020000_remote_worker_jobs_get_confirmed`). Late findings were deferred
to follow-ups: mar3co/opentag#142 (closed by #143), #145 and #146.

Follow-ups merged on 2026-10-03:

- [#143](https://github.com/mar3co/opentag/pull/143) `842b9ac`: worker links
  return through sign-in, artifact sign-in redirect, approved-payload checks in
  the conformance harness, and a fix for a pre-existing `/login?next=/\host`
  open redirect in the shared `safeNextPath`.
- [#144](https://github.com/mar3co/opentag/pull/144) `4d84bbf`: an outbound HTTPS
  fake-adapter pilot runner and checklist for the owner-controlled network
  pilot; it refuses `opentag.me` origins unless `--allow-production` is passed.

The shared Supabase project's ledger was behind both OpenTag (7 migrations) and
OpenTicket (20 desk migrations), so neither existing push path could run.
OpenTicket mar3co/openticket#61 moved its OpenTag pin to `3f2f0d9` and #62 added
a guarded `db:catchup` that applies a missing suffix of both ledgers in version
order. `db:catchup` applied all 27 migrations; the ledger is now current and the
worker and Slack-link functions execute only as `service_role`. The OpenTag
agent was deployed to `api.opentag.me` and the portal deployed from `main`.
Worker dispatch remains off unless a workspace grants an agent the
`worker-dispatch` scope.

Still open for the Phase 4 exit: the live cited research run (blocked on Phase 1)
and the staging Slack scenario. The review follow-ups are closed:
mar3co/opentag#146 (pilot runner shutdown and switch edges) by #147 (`d88fe44`)
and #145 (transient auth errors in the `/app` guards) by #148 (`f7b4b63`), both
merged 2026-10-06.

## First-use account and folder step (2026-10-07)

The first-use flow's "choose an eligible local account and approved research
folder" step now exists; until now only tests called
`configure_worker_local_policy`, so a paired worker failed every job
`provider_unavailable` with no supported way to pin an account.

- `openswap worker account [slot|email|alias] [--clear] [--json]` lists Codex
  roster slots and pins one through the Codex engine's own `resolve_account`.
  The pin is `stable_account_identity("codex", accountId)`. It is **Codex
  only**: Claude accounts are listed as not eligible yet (Claude
  authentication gate) and a Claude selector is refused, as are API-key slots
  (no account ID) and accounts no longer in the roster. Only roster metadata is
  read, never auth files or tokens.
- `openswap worker workspace list|add <id> <folder> [--readonly-source DIR]|remove <id>`
  manages the approved research folders under the opaque IDs the owner enters
  as Local workspace IDs in the OpenTag portal, applying the launch-time folder
  checks when the folder is added.
- After `openswap worker pair` succeeds, an interactive terminal with no pin
  offers the eligible Codex accounts (Enter skips) and then the folder step;
  without a terminal it prints both next steps. Pairing succeeds either way.
- The menu bar's Remote tasks section gains an **Account** popup (eligible
  Codex slots with the pin checked, None, Claude entries disabled) that pins
  off the UI thread through the same function as the CLI.
- Pin and workspace changes take the worker lifecycle lock; pinning also holds
  the Codex mutation guard. The worker re-reads the pin for every launch,
  before the lease, so a change applies to the next job without a restart; a
  running job keeps the account recorded at `starting`. A pinned account
  missing from the roster at launch fails the job `provider_auth_unavailable`
  (already on the journal and protocol allowlist) before any lease or launch,
  and the remote client does not claim new work in that state.

Execution is still disabled: the production adapter remains
`UnavailableCodexAdapter` until Phase 1's live-evidence gates clear, so this
step lets the owner finish setup but does not run any provider job.

## Post-pair worker offer (2026-10-07)

An owner paired a Mac and the portal kept it Offline ("No heartbeat yet"):
pairing never starts the worker and nothing said `openswap worker enable` was
the next step. Enrollment still does not enable execution on its own. After the
account and folder steps, `worker pair` on a terminal now asks to start the
worker and, on yes, calls `enable_worker` (the function behind `worker enable`
and the menu toggle); no, EOF and non-terminal runs print the command; an
enabled worker is reported, never toggled, and pairing cannot fail here.
Human `worker status` adds a one-line "Paired with <origin> but the worker is
off" hint, and the menu bar shows "Paired, worker off" under **Enable local
worker**. A conftest guard keeps pairing tests non-interactive so `pytest -s`
on a terminal cannot reach the real launchctl.

## Optional per-job account choice, OpenSwap side (2026-10-07)

The "Optional per-job account choice (v1 extension)" section of
`docs/worker-protocol.md` is implemented in OpenSwap; the OpenTag side is in
progress.

- Settings hold a local allowlist (at most 20 Codex accounts): a random
  `account_ref` per entry (`secrets.token_hex(16)`), the local `codex:`
  identity and an owner label (default: slot alias or "Codex account N"). The
  pin is the default and is always allowlisted; a pre-allowlist pin migrates
  to a one-entry allowlist on the next write.
- `openswap worker account allow|disallow|label` (with `--json`, and
  `--clear-default` to withdraw the default), the list view and a menu bar
  **Web choice** popup manage it under the same locks as the pin.
- The remote client sends `accounts` after each registration and on a local
  fingerprint change; a 404 (`unsupported_version`, or `not_found` from older
  reference servers) disables it until the next
  registration. A claim's `account_ref` is accepted only once the enrollment
  advertised accounts, and the runtime resolves it against the current
  allowlist under the launch lock. A reference no longer allowed fails
  `provider_auth_unavailable` (no pin: `provider_unavailable`) before any
  lease, reconciled `failed` with `unlaunched=true`; nothing is substituted.
  No journal schema change: the choice is read from the durable claim in the
  remote journal.
- The reference service implements `accounts`, accepts an advertised
  `account_ref` on submission (part of the idempotency payload) and now
  answers unknown operations 404 `unsupported_version`, as the spec says;
  `submit-test` takes `--account-ref`.

Execution is still disabled until Phase 1's live-evidence gates clear.

## Live path prepared (2026-10-07)

Everything up to the owner's first real run is implemented, in three stacked
PRs: containment, the live Codex adapter, and the evidence harness. Live
execution stays **off by default**; nothing here has run real Codex. The
Phase 1 gates above remain unchecked until the owner's `live-check` passes.

**Containment (cancellation gate).** Measured on a development Mac with
synthetic processes ([cancellation-boundary.md](research/remote-agent-host/cancellation-boundary.md#per-job-launchd-job-plus-resource-coalition-sweep-measured-2026-10-07)):
launchd gives each bootstrapped job its own resource coalition, a
`setsid()`-daemonised descendant stays in it, and `launchctl bootout` alone
left that descendant running. `worker/containment.py` therefore runs each job
as its own launchd label whose `/bin/sh` wrapper waits for a `go` file until
the worker has recorded the label, coalition ID and boot session; Stop freezes
every coalition member until a full scan finds none running (a stopped
process cannot fork, which closes the fork/reparent race), kills them, and
unloads the label. Proof is "label unloaded and no live member"; a different
`kern.bootsessionuuid` is proof on its own. A 60-process forking `setsid()`
job was emptied in 0.07 s; a job whose launching process was `SIGKILL`ed kept
running and was later recovered with proof. Residual limit: work a job asks
launchd or another system service to start runs outside its coalition; the
live check measures a `launchctl submit` attempt from inside the sandbox.

**Live adapter (`worker/codex_exec.py`).**

- Pinned CLI: `openswap worker codex install` fetches the official
  `rust-v0.157.1` `codex-aarch64-apple-darwin.tar.gz`, refuses it unless its
  SHA-256 is the published digest, unpacks the single member into the private
  worker directory and records the binary's own SHA-256; every probe re-hashes
  it and checks `--version`. Apple silicon only (the only published digest we
  pin).
- Account isolation: one `CODEX_HOME` per account under the worker directory,
  signed in with `openswap worker codex login` (the pinned CLI's own login,
  `cli_auth_credentials_store = "file"`, under the Codex lease). Jobs never
  read, copy or write the default `~/.codex` login or roster snapshots; only
  that Codex process refreshes the isolated home. A launch refuses
  (`provider_auth_unavailable`, unlaunched) unless the home's signed-in
  account hashes to the leased identity.
- Sandbox: a named permission profile (deny `:root`, read `:minimal` and the
  approved read-only sources, write only the job folder, deny `$TMPDIR` and
  `/tmp`, no shell network) selected by `default_permissions`; no `--sandbox`
  flag, because the permissions doc says it makes Codex ignore the profile.
  Live web search on; apps, hooks, plugins, multi-agent, browser/computer use,
  code mode, unified exec and skill search disabled; `project_root_markers =
  []`, `project_doc_max_bytes = 0`. A launch refuses while any managed or system
  Codex layer exists that could override the profile. The job's environment is a fixed
  allowlist supplied by launchd.
- Events: `thread.started` becomes `provider_started`; the end of the run
  becomes one `provider_finished` (`succeeded` only for exit 0, a
  `turn.completed`, no error and a non-empty `result.md`; otherwise `failed`
  with `provider_rate_limited`, `provider_auth_unavailable` or
  `provider_unavailable`) whose `execution_stopped` is the coalition sweep's
  proof. No journal or protocol code was added.
- Runtime: `ProviderLaunchRefused` fails a job before anything ran and
  releases the lease as unlaunched; a restarted worker hands every recovered
  job (and the job behind an unreleased lease) to `adapter.recover`, and
  releases the lease `confirmed_stopped` only on proof.
- Switch: `openswap.worker.live.execution_mode(backup_root)` is the one place
  that answers `disabled` or `live`. The live adapter's `execution_mode`
  attribute (what the readiness report in
  [#80](https://github.com/mar3co/openswap/pull/80) reads from the adapter)
  and `WorkerRuntime.execution_mode()` both derive from it. The opt-in (`worker.liveExecution` in
  settings.json) records the passing evidence's SHA-256 and the measured
  binary's SHA-256; a different binary fails jobs `provider_unavailable`.
  `openswap worker live status|enable|disable` manage it.

**Evidence harness.** `openswap worker live-check` refuses while the worker is
running unpaused, a job is active or a lease is held, then records, for the
pinned (or `--account`) account: `pinned_cli`, `account_identity`,
`default_login_unchanged`, `tool_surface` (every disabled feature listed
and off, no MCP servers, no managed layer), `sandbox_wrapper`,
`research_run`, `sandbox_exec`, `stop` and `kill_recovery`. The adversarial
`codex exec` job is asked to read and write outside its folder, read a
sentinel in `CODEX_HOME`, read `auth.json` into `/dev/null`, follow a symlink
out, print its environment, use `curl` and `launchctl submit`; results are
checked on disk and in the JSONL with positive controls (it must read and
write inside its folder), so a refusal is a failure, not a pass. The stop and
kill jobs run a helper that daemonises a `setsid()` `sleep`. The evidence
file (0600, `live-evidence/live-check-<UTC>.json`) holds booleans and counts
only. It offers to enable live execution only when every gate passes.

Validation (no real Codex, no credentials): the full suite, plus opt-in tests
against real launchd (`OPENSWAP_LAUNCHD_TESTS=1`): the containment
`setsid()` test, and the whole harness driven through real launchd with a
fake, unsandboxed `codex` script. In that run `stop` and `kill_recovery`
passed with a real detached helper, `research_run`, `tool_surface` and the
identity gates passed, and both sandbox gates failed as they must against an
unsandboxed binary (outside reads and writes, `/tmp`, network and the
`launchctl submit` escape were all detected; the worker's environment
sentinel was absent because launchd supplies the allowlist).

**Owner steps on the MacBook Pro** (Apple silicon):

```sh
openswap worker pause                 # if the worker is running
openswap worker codex install
openswap worker account <slot>        # if no account is pinned yet
openswap worker codex login           # browser sign-in to the isolated home
openswap worker live-check            # ~5-15 min; answer y to enable if all gates pass
openswap worker pause --off
```

**Still unproven until that run:** that 0.157.1 `codex exec` honours
`default_permissions` and the disabled features under `--strict-config` (the
check fails closed if a key is rejected or a boundary leaks); the exact text
of `codex mcp list` and `features list` on 0.157.1 (parsed leniently, a format
change fails `tool_surface`); that the model runs the probe commands (a
refusal fails the gate); refresh-race behaviour of the isolated home during
a long run (only one Codex process uses it, but a token refresh under load is
not exercised); a default login held in the Keychain rather than
`~/.codex/auth.json` (recorded as absent, not compared); and the
launchd-mediated escape when the model does not run that step. Phase 4's
exit still needs the staging Slack run after this.

### Review hardening (2026-10-08)

Codex review rounds 13 to 21 on #81 to #84 tightened the live path further.
None of this changes the owner steps.

- **Stop proof.** Pids are bound to a stable identity:
  - A harmless zombie is identified by its start time from `kern.proc.pid`.
  - A freeze needs two consecutive complete scans.
  - A `SIGSTOP` that may have hit a recycled pid is undone, and `SIGKILL`
    only reaches members already observed stopped.
  - The leader pid is re-verified with launchd after the coalition lookup.
  - Ownership of a loaded label comes only from exactly one parsed plist
    path; anything else is unknown, and stop then proves nothing.
  - Run directories are keyed by their on-disk spelling (`F_GETPATH`), and
    paths with control characters are refused.
  - Recovery reads handles only after an in-progress launch releases them.
  - Locks and recovery mirrors moved from `~/Library/Caches` to
    `~/Library/Application Support/com.opensoft.openswap/`.
- **Codex adapter.**
  - `--ignore-rules` is passed.
  - A launch is refused while the job folder holds a `.codex` layer.
  - `codex login` and `logout` are refused under a managed Codex layer.
  - A failed login counts as failed even with the account's old credentials
    still in the home.
  - Login and logout resolve the slot and take the lease under one guard.
- **Live check.**
  - It holds the worker lifecycle lock throughout, and refuses a stale,
    unreadable or running worker.
  - The default login is fingerprinted by metadata only and is never
    opened. The baseline is taken before any sign-in prompt.
  - Denials count only from completed commands, and each has a positive
    control: an exit-0 `env`, a created symlink, writable `/tmp` and
    `$TMPDIR` outside the sandbox, and a curl that runs inside it.
  - The exec probe also covers an approved read-only source and the job's
    own `$TMPDIR`.
  - Leftovers from earlier checks, mirror-only ones included, must be proven
    stopped.
  - Helpers and the escaped probe job are always cleaned up, and sentinel
    files get fresh, exclusive names.
- **Opt-in per account.** Enabling records the account the passing check ran
  on, and a later check with the same binary adds its own. A job on an
  unchecked account is refused before launch, and the worker does not poll
  while any selectable account is unchecked.
- **Claude.**
  - Any managed policy refuses a launch: `managed-settings.d`, per-user
    managed preferences, or a non-empty server-managed `remote-settings.json`
    cached in the profile.
  - Profiles are signed in with Claude Code's own `claude auth login`, under
    the lease.
  - Grants overlapping the logins or the backup root are refused.

## Claude accounts (2026-10-08)

**Owner decision (2026-10-07).** Remote tasks may run on the owner's Claude
accounts: option "Unmodified Claude Code with the owner's native login",
limited to the owner's own paired Macs and tasks they start themselves.
Recorded in [decision-claude-auth.md](research/remote-agent-host/decision-claude-auth.md#owners-decision).
This lifts the "Claude authentication gate" in the account step above.

Stacked on the live-check PR:

- **Accounts and pins.** `openswap worker account`, `allow`, the post-pair
  offer and the menu-bar picker list and accept Claude slots. Pins stay typed:
  `stable_account_identity("claude", email, organizationUuid)` versus
  `("codex", accountId)`; `claude:<slot>` and `codex:<slot>` select a
  provider, and a bare slot means Codex first, then Claude. A job runs on the
  provider of its pinned or chosen account (`WorkerRuntime.adapters`), and
  claim gating asks that provider's adapter. One job per host still holds
  across providers (`ProviderLeases` spans the Codex and Claude lease
  stores).
- **Binary (`worker/claude_cli.py`).** The owner's installed `claude`
  (Homebrew cask or the native installer) is pinned with `openswap worker
  claude pin`. It is hashed, and jobs run a byte-identical read-only copy kept
  under Application Support, which every launch re-hashes and checks with
  `--version`. No published digest exists to verify against, so this is
  trust-on-first-use; jobs run with the auto-updater off, and an update needs a
  re-pin and a new live check (the opt-in is bound to the binary).
- **Profile.** The account's OpenSwap-managed session profile (plan 003,
  `CLAUDE_CONFIG_DIR=<backup>/sessions/<n>-<slug>`), signed in once by the
  owner with `openswap worker claude prepare`, which runs the pinned Claude
  Code's own `claude auth login` into that profile under the Claude lease
  (OpenSwap seeds no credential). A launch refuses unlaunched
  (`provider_auth_unavailable`) unless the profile is signed in as the leased
  identity, and (`provider_unavailable`) while an interactive live session
  uses that profile or while it mirrors the default profile's customizations
  (scheduled kickoff's sharing). The worker never reads, uploads,
  proxies or logs a credential, and never changes the default login; the CLI
  owns refresh.
- **Adapter (`worker/claude_exec.py`).** `claude -p --output-format
  stream-json --verbose --restricted --tools Read,Grep,Glob,WebSearch,WebFetch
  --allowedTools (same) --permission-mode dontAsk --permission-prompts none
  --strict-mcp-config --disable-slash-commands --no-session-persistence`,
  plus `--add-dir` per approved read-only source, inside `sandbox-exec` with a
  generated Seatbelt profile: writes only to the job folder, the profile, the
  run's temporary folder and this user's cache/temporary folders; the default
  login (`~/.claude`, `~/.claude.json`), `~/.codex` and the whole backup root
  (other accounts, worker state) unreadable and unwritable apart from this
  profile. Same launchd containment, stop proof, result publishing and
  allowlisted failure codes as Codex (`result` events map to
  `provider_rate_limited`, `provider_auth_unavailable` or
  `provider_unavailable`).
- **Opt-in and evidence.** `openswap worker live-check --provider claude`
  records `live-check-claude-<UTC>.json` with the same gate names (Read-tool
  probes in place of shell probes: inside read allowed, outside read, profile
  sentinel and `~/.claude.json` denied; stop and kill recovery against a long
  research task). `worker.liveExecutionClaude` is a separate opt-in;
  `execution_mode()` answers for the pinned account's provider.

Validation (no Claude login, no model call): new
`tests/test_worker_claude.py` (pin and verify, argv/env/Seatbelt, refusals,
stream-json mapping, provider routing and leases, per-provider opt-in, profile
preparation, a simulated Claude live check) plus a real `sandbox-exec` run of
the generated profile on this Mac (outside and backup-root reads and writes
denied; job folder and profile allowed); full suite green.

**Owner steps on the MacBook Pro** (after the Codex steps, or on their own):

```sh
openswap worker pause
openswap worker account claude:<slot>    # pin the Claude account
openswap worker claude pin               # pin the installed claude binary
openswap worker claude prepare           # Claude's own sign-in into that account's profile (browser)
openswap worker live-check --provider claude   # answer y to enable if all gates pass
openswap worker pause --off
```

**Still unproven until that run:** that `--restricted` with `--tools` leaves
only those five tools and no MCP servers (the check reads the `init` event
and fails otherwise); that Claude Code works under the generated Seatbelt
profile (any extra path it needs shows up as a failed research run, never a
silent widening); that the model performs the Read probes (a refusal fails
the gate); Keychain behaviour of a profile-scoped login when the token
refreshes during a long job; and that WebFetch/WebSearch reach the network
from inside the sandbox.


## Guided Mac setup and readiness report (2026-10-08)

The pilot owner found setup confusing: opaque workspace IDs typed into the
portal, and no way for the portal to know which folders the Mac approved or
whether it executes for real.

- `openswap worker pair` now runs a guided setup in a fixed order: start the
  worker, confirm (or pick) the Codex account, approve a research folder, then
  a summary of what is still missing before Slack can start tasks. The folder
  step offers `~/OpenSwap Research` (created 0700) as `research` in place of
  the built-in folder inside the backup root, and then other folders with a
  suggested ID. `openswap worker setup` reruns the steps on a paired Mac, and
  the menu bar's **Set up Remote tasks…** button runs them in dialogs, pairing
  first from the pasted pairing command. All three share
  `openswap.worker.guided_setup` and the CLI's own functions.
- Approved folders gain an optional owner label (`workspace add --label`,
  `workspace label`); the default is the folder's name.
- New optional protocol extension "Optional readiness report (v1 extension)":
  the worker sends `readiness` (folder IDs and labels, and execution mode
  `disabled`/`live`) after each registration and on change; registration and
  revocation clear it on the backend. The mode comes from one hook,
  `openswap.worker.adapter.execution_mode()`, which reports `disabled` while
  the production adapter is `UnavailableCodexAdapter`. The reference service
  implements it. The OpenTag side stores the report, shows it in the owner
  listing and feeds the reported IDs to the Slack/agent workspace list.

Execution is still disabled until Phase 1's live-evidence gates clear.


## Setup asks which folders tasks may read (2026-10-08)

Owner feedback on `openswap worker setup`: offering to "create and approve
~/OpenSwap Research as research folder" made no sense, because most owners
already keep their code in a repo or a GitHub folder.

- The folder step now asks one question: which folders remote tasks may read.
  It lists likely code folders in the home folder (`~/GitHub`,
  `~/Documents/GitHub`, `~/Developer`, `~/Code`, `~/Projects`, `~/src`,
  `~/repos`, `~/dev`, plus the repos of a folder holding three or fewer),
  with a GitHub folder first and marked "recommended" as the Enter default.
  The answer is numbers (`1 3`, `1,3`) or a path; with nothing found it asks
  for the path to the code folder.
- Each chosen folder becomes its own workspace: ID from the folder's name
  (unique), label the folder's name, the folder as its only read-only
  source, and results in `~/OpenSwap Research/<id>`, created 0700 without
  asking. The first replaces the built-in `research` workspace unless a job
  may still run or upload in it. The owner never approves a results folder.
- Readable folders refuse the home folder, system folders, `~/Library` and
  hidden folders, credential homes and the results folder, and must pass the
  read-only source checks the worker repeats at launch.
- `openswap worker workspace add --read <folder>` makes the same workspace;
  `add <id> <folder>` is unchanged. The summary lists "Readable folders" by
  ID and label; the readiness report is still IDs and labels only, so
  OpenTag's Slack folder hints follow the new IDs without a protocol or
  OpenTag change.

Review fixes (same PR): every folder comparison in the workspace policy,
the settings disjointness check and the Codex/Claude launch-time grant checks
now goes through `openswap.pathid`, which compares the on-disk spelling
(`F_GETPATH` on macOS) and filesystem identity along the ancestors. Before
this, a case variant on APFS (`~/library`, `~/LIBRARY/Application Support`,
the home folder in capitals) passed every refusal, and Seatbelt honours such
a grant. Settings store the on-disk spelling. Read-only sources may not
overlap another workspace's results folder (and results may not overlap a
readable folder); `--readonly-source` follows the readable-folder policy; and
the worker applies that policy again at launch.

Second review (same PR): a typed path that exists is used as typed, so
apostrophes in real names are never shell syntax; shell-splitting applies only
to text that starts with a quote or holds a backslash. Synced cloud drives in
`~/Library` (a `CloudStorage` provider folder, iCloud Drive) are readable,
compared by identity; the rest of `~/Library` is not. The worker checks the
folder rules, including the cross-workspace overlap, for every job before it
creates any folder; a refusal fails the job as the local `workspace_refused`
diagnostic (uploaded as `provider_unavailable`, since OpenTag's list is
closed), and `worker status` and the setup summary name the workspace and the
reason. Settings that break the rules still load, so they can be fixed.

Third review (same PR): the cloud-drive exemption compares the folder's own
resolved components with the real `CloudStorage` and `Mobile
Documents/com~apple~CloudDocs` under the canonical `~/Library`, and refuses a
base that is a symlink, so a base linked to `~/Library` opens nothing. The
readiness report leaves out workspaces the worker would refuse (or cannot
check), and reports them again once fixed.


## Setup: search the folders, and say less (2026-10-08)

Owner feedback on the merged folder step: ~/GitHub was listed, but typing did
not search. On a terminal the folder step is now #88's type-to-search picker
with the detected folders as numbered suggestions (GitHub first and
highlighted, so Enter picks it); typing searches them, then the home folder;
digits pick by number; after a pick it opens again as "Add another". Piped,
scripted and Windows runs and the menu bar keep the numbered question. Every
pick still goes through `add_readable_folder`.

A wording pass cut the guided setup to a step header, at most one line of
context, one short question with its default, one-line ✓ confirmations and
one `Next:` per step; the summary is the checklist plus one `Next:`. The
words "control service", "provider", "execution", "admission" and
"workspace" no longer appear outside commands. What tasks may do in a picked
folder is one constant (`guided_setup.FOLDER_USE`) while the owner decides
whether tasks work in it or only read it.


## Folders are where sessions work (owner decision, 2026-10-08)

The owner's goal: "trigger remote sessions to use a user's claude/codex
account on their local/remote machine… they need folders since claude and
codex need to be launched in a certain folder." A folder is where the
Claude or Codex session is launched and works, like running `claude` in a
repo, not a read-only source with output elsewhere.

Decision:
- Default: each task runs in its own git worktree of the chosen repo
  (`openswap/<task>` branch from `HEAD`, under
  `~/OpenSwap Research/.worktrees`), so it edits freely without touching the
  owner's working copy or another task.
- Opt-in direct mode: the session runs in the folder itself, exactly like
  local `claude`. Set only on the Mac (`openswap worker workspace mode <id>
  direct`, `workspace add --work --direct`, `setup --advanced`), never from
  the service, and not reported to it (that would need a protocol field).

Built (PR "Launch remote sessions in work folders"): repos and folders of
repos as work folders (rescanned per launch and report, collision-free IDs),
non-repos refused unless direct, the per-task worktree, the Codex and Claude
write scope (the worktree, `.git/objects`, the worktree's admin folder and
the `openswap/` branch namespace; reads of the shared `.git`), a real
`sandbox-exec` test of that scope, a new required live-check gate
(`worktree`: a commit in the task's worktree works; the owner's copy, branch
and git config stay unwritable), the lifecycle (branches kept, clean
worktrees removed, `openswap worker worktrees [prune]`), and the setup copy
("Folders where remote sessions can work"). Work folder tasks get Claude's
Edit, Write and Bash tools and a work prompt; research workspaces keep the
research profile. Existing live opt-ins stay enabled; the next live check
must pass the new gate.

Codex review of that PR (fixed in it): repo IDs are kept for good in
`repo-ids.json` (a new repo can never take an older one's ID); every git
command the worker runs disables filters, fsmonitor, signing and hooks and
targets a worktree from the worker's own record; a task writes objects to
its own folder (the shared store is a read-only alternate) and the worker
imports them verified; Claude work tasks get no Bash (it would share the
sandbox that reads the account's credentials), so the worker commits what a
task leaves; and results upload still works after a repo is removed.

## Sessions follow the account's own permission settings (owner decision, 2026-10-08)

The owner asked, about the fixed research tool list and the "no Bash for
Claude" rule: "Shouldnt shell commands be optional for the user to decide?
or stick with claude mode? like if claude is in auto … why are we deciding
that thats a system level setting no?" They then approved this design:

1. **Tools follow the account's own settings.** A remote session uses the
   permission mode and allow/deny rules from that account's Claude Code
   settings (its OpenSwap profile), exactly as running `claude` there would,
   covering shell, edits and web. Nobody is at the Mac, so anything that
   would ask is denied, unless the mode is `auto`, where Claude's own auto
   mode decides. The fixed `--restricted --tools` list is no longer the
   default.
2. **Codex gets the same treatment:** the account's approval policy and
   sandbox mode instead of a mode OpenSwap forces; approval prompts resolve to
   deny unless the policy never asks.
3. **Optional per-Mac limit, set only on the Mac, never from the web**
   (`openswap worker permissions follow|no-shell|read-only`, and an advanced
   setup choice). Default: follow the account's settings. Status and the setup
   summary show it only when it is not the default.
4. **OpenSwap keeps only system-level boundaries**, enforced by Seatbelt in
   every mode including `bypassPermissions`: the folder or per-task worktree
   write scope; no access to other accounts' credentials, the backup root,
   the journal or the owner's working copy; the identity and
   case-insensitive path protections.
5. **Keep the account's own sign-in away from shell commands where
   possible**, as protection rather than a restriction; the live check says
   honestly whether it held.

Built (PR #93, stacked on #90):

- **Claude.** `claude -p --setting-sources user --permission-prompts none`
  (flags checked in `claude --help`, Claude Code 2.1.286): the mode and rules
  come from the profile's `settings.json`; a repo's settings never apply
  (`-p` skips the trust dialog). Measured on the installed CLI with a
  signed-out profile: `init` reports the profile's `defaultMode`, project
  settings are ignored, and an invalid settings file is silently dropped, so
  a launch now refuses one (`provider_unavailable`) rather than lose the
  owner's deny rules. `openswap worker claude prepare --copy-settings
  --mode <mode>` copies the mode and the allow/deny/ask rules (never hooks,
  model or extra directories) from `~/.claude/settings.json`; interactively it
  asks for a profile with none. Managed or enterprise policy still refuses a
  launch.
- **Codex.** `permissions.json` in each isolated home (approval policy,
  reviewer, sandbox mode), set with `openswap worker codex settings
  [--copy-settings]` or offered after `codex login`; the managed
  `config.toml` carries it. `codex exec` refuses every approval request, so
  asking policies run as `on-request`; `auto_review` decides by itself;
  `untrusted` runs with no shell tool; `read-only` makes the folder
  read-only. Codex's own sandbox is the only boundary its shell has (it cannot
  be wrapped: no nested sandboxes), so the folder rules and the shell's lack of
  network stay, `danger-full-access` included. Checked with the Codex 0.160.0
  bundled in ChatGPT.app (`codex sandbox`, no sign-in): the read-only config
  denies a write, `--disable shell_tool` turns the shell off, `auto_review`
  parses.
- **Per-Mac limit.** `worker.permissionOverride` in settings.json (absent:
  `follow`; unreadable: `read-only`). `no-shell`: Claude gets
  `--disallowedTools Bash,PowerShell,Monitor,REPL,BashOutput,KillShell` and no
  hooks, Codex no shell tool; `read-only`: Claude's read and web tools only,
  Codex a read-only sandbox. No protocol, IPC or remote path names it.
- **Boundaries, new because a shell is now possible.** The Claude Seatbelt
  profile takes the Keychain away (`/usr/bin/security` cannot start, the
  Keychain's mach services cannot be looked up, `~/Library/Keychains` is
  unreadable). Without that, any shell command could read every
  `security`-created item without a prompt: other accounts' sign-ins, the
  default login and the worker's device key. Claude Code reads its own sign-in
  through `security` child processes, indistinguishable from a task's, so it
  now keeps it in the profile's `.credentials.json` (its own plaintext
  fallback; measured: a denied `security` exec fails fast and is not a
  "transient" Keychain error, so the fallback write happens). `claude prepare`
  runs Claude Code's own `auth login` with only the Keychain taken away, so the
  sign-in lands there; OpenSwap still never touches a credential. A profile
  signed in only in the Keychain refuses (`provider_auth_unavailable`) until
  `prepare` signs it in again. The profile's configuration files (settings,
  memory, agents, commands, skills, hooks, output styles, plugins) are
  unwritable from a job, so one task cannot widen the next.
- **Item 5, what works.** Claude Code's bash sandbox nested in OpenSwap's: no;
  real `sandbox-exec` inside `sandbox-exec` fails `sandbox_apply: Operation
  not permitted` (macOS 27.0.1). Passing the token at launch: the CLI accepts
  `CLAUDE_CODE_OAUTH_TOKEN` / `_FILE_DESCRIPTOR`, but never refreshes or
  persists such a token, so OpenSwap would have to read the credential and own
  its rotation, which the decision memo rules out; not adopted. Result: under
  `follow`, a Claude task's shell can read that account's own sign-in (and
  nothing else of the kind); `no-shell` prevents it. The setup summary says so
  in one line for a Claude account with the shell allowed. Codex's shell
  cannot read its `auth.json` or the Keychain (measured with `codex sandbox`).
- **Live check.** New required gates `permissions` and `sign_in_isolation`
  (see the module docstrings and [worker-protocol](../docs/worker-protocol.md#permissions-the-accounts-own-settings)).
  The Claude Read probes and both providers' worktree jobs run with the
  widest settings, so only OpenSwap's boundary can refuse. Opt-ins now record
  `policyVersion` 2; one recorded before (a fixed tool list, no shell) stays
  off and status says to run the check again.

Validation: full suite green; real Seatbelt tests of the new rules on this Mac
(`security` exits 126, profile settings and memory unwritable, state and the
credentials file reachable); an opt-in real test that a throwaway keychain
file's item is unreachable through the service rule alone
(`OPENSWAP_KEYCHAIN_SANDBOX_TESTS=1`, also on in GitHub Actions); simulated
live checks for both providers, including Macs that fail the new gates.

**Still unproven until the owner's live check:** a real sign-in and its
refresh into `.credentials.json` with the Keychain unreachable; that the
pinned Codex 0.157.1 accepts `approvals_reviewer` (only written when an
account uses `auto_review` or `guardian_subagent`) and the read-only
filesystem entries; the model performing the new permission probes.
