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
Phase 2 local-only implementation is underway under that owner authorization;
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
The last full-suite result before Phase 2 changes, at the assembled Phase 1
branch, was `2911 passed, 4 skipped, 3 warnings in 15.01s`; it included 40
Phase 1 harness tests. It is historical baseline evidence, not validation of
this Phase 2 branch. The helper-cleanup regression passed in isolation
(`1 passed in 1.19s`).

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
  `setsid()` reproduction shows the current probe wrapper can return success
  while a detached helper continues running, so process-group cleanup alone
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
- [x] Both written decision memos have been delivered for owner review.
- [ ] The phase-one evidence, adapter contract and reproducible harness are
  reviewed and PR #59 is merged as Phase 1 signoff. This remains required
  before real Codex execution, but does not block owner-authorized local-only
  Phase 2 infrastructure work.
- [x] Full OpenSwap pytest suite is green on the assembled phase-one branch
  (`2911 passed, 4 skipped, 3 warnings in 15.01s`).

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
| 1. Feasibility spike and authentication gate | IN PROGRESS / BLOCKED | A hash-verified stable 0.157.1 passes the synthetic low-level Seatbelt wrapper probe, but no authenticated `codex exec` proves account selection, refresh behavior, structured provider events, complete process-tree cancellation/recovery, or model/tool enforcement integration. The fake `setsid()` reproduction shows the current wrapper can return success while a detached helper remains alive. No owner-authorized Codex slot or exclusive live-auth ownership is established. Control-service decision is recorded. PR #59 is open and ready for review; it does not satisfy the remaining technical exit gates. |
| 2. Local worker, remote access off | IN PROGRESS — local-only core | Owner authorized local infrastructure to overlap Phase 1; no Phase 1 gate is waived. Remote access stays off and live Codex stays disabled. |
| 3. Private remote pilot | NOT STARTED | Requires phase 2's exit criteria and merged PR. Builds the protocol specification, configurable backend URL and the MIT reference server as product code; the pilot runs against a self-hosted instance. |
| 4. OpenTag connector | OUT OF SCOPE | Tracked in the separate OpenTag repository. |
| 5–6 | OUT OF SCOPE | Do not start. |

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

Current Phase 2 validation is green on the assembled branch: `2984 passed,
4 skipped, 3 warnings in 16.36s`. Focused core journal,
settings, disabled-adapter and fake lifecycle tests passed (`20 passed`); the
separate subprocess acceptance passed (`1 passed`). It verifies client
recreation/idempotency, singleton refusal, a stop acknowledgement remaining
nonterminal with the lease quarantined, and restart recovery to `interrupted`
without adapter relaunch. The first assembled run exposed a missing
`activeAccountNumber` in a live-kickoff test fixture; the fixture now declares
the managed account explicitly, and kickoff identity resolution remains
fail-closed. All provider behavior in these tests is synthetic; this result
does not clear Phase 1 gates or enable live Codex execution.

## Deviations and verification

- Phase ordering overlaps only because the owner explicitly authorized local
  Phase 2 infrastructure while Phase 1 remains in progress. This does not
  waive Phase 1 evidence gates or authorize Phase 3, provider execution, or
  credentials.
- The test-only missing-Claude fixture correction is a test determinism fix;
  it will preserve the `ClaudeSwitchError` assertion and will not change the
  resolver or executable discovery behavior.
- Credential-free harness tests passed (40 tests included in the full suite);
  `inspect`,
  inert-child `demo`, and synthetic Seatbelt `sandbox-probe` outcomes are in
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
  is **REPRODUCED / UNRESOLVED**. An inert disposable fake process showed
  `_run_probe` returned
  success while a helper that called `setsid()`, closed inherited output, and
  wrote a heartbeat remained alive. The helper had a four-second hard
  self-expiry and was explicitly stopped by its recorded PID immediately after
  observation. The group wrapper therefore does not prove cleanup of detached
  descendants; the complete-process-tree cancellation gate remains blocked.
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
