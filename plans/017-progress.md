# Plan 017: Remote Agent Host progress

Plan 017 is the canonical scope and phase order. Supporting research informs
the work but does not override the plan. This log tracks the current gated
execution; it does not mean that remote execution is implemented or enabled.

## Current status

**Phase 1: IN PROGRESS, exit BLOCKED.** Only the feasibility spike and its
written decisions are in scope. There is no product code. PR #59 is ready for
review; opening or merging it does not substitute for the remaining technical
evidence gates. Phases 2 and 3 have not started.

The baseline `uv run pytest` completed before this branch's changes: 2870
passed, 4 skipped, 1 failed, 3 warnings (15.15s). The failure is
`tests/test_menubar.py::test_launch_claude_login_missing_claude`: its injected
`which` reports no executable, but the resolver also probes hard-coded
install locations and finds this machine's `/opt/homebrew/bin/claude` (2.1.274).
The isolated test still fails with a temporary HOME and `PATH=/usr/bin:/bin`.
A narrow test-only correction now simulates the missing-resolver result without
changing product behavior; the isolated test passes (`1 passed in 0.74s`).
The latest assembled-branch suite is green: `3011 passed, 5 skipped, 3 warnings
in 42.01s`; it includes 139 phase-one harness tests and two direct tests of the
Claude binary resolver's fallback directories. The helper-cleanup
regression also passed in isolation (`1 passed in 1.19s`).

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
  reviewed; phase-one PR is merged before phase 2 begins.
- [x] Full OpenSwap pytest suite is green on the assembled phase-one branch
  (`3011 passed, 5 skipped, 3 warnings in 42.01s`).

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
| 2. Local worker, remote access off | NOT STARTED | Requires every phase-1 exit item above and the phase-1 PR merged. |
| 3. Private remote pilot | NOT STARTED | Requires phase 2's exit criteria and merged PR. Builds the protocol specification, configurable backend URL and the MIT reference server as product code; the pilot runs against a self-hosted instance. |
| 4. OpenTag connector | NOT STARTED | OpenTag repository, mar3co/opentag#135; requires phase 3. Out of scope for this PR. |
| 5–6 | NOT STARTED | Require phase 4. Out of scope for this PR. |

## Deviations and verification

- No deviation from plan 017's scope or ordering.
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
  timed out is decided at the original deadline: an exit seen only by a
  later, slower check is still a timeout, never a success.
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
  review against `main`. Its merge remains a phase-2 prerequisite, not a
  substitute for the unchecked technical evidence above.
