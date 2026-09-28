# Plan 017: Remote Agent Host progress

Plan 017 is the canonical scope and phase order. Supporting research informs
the work but does not override the plan. This log tracks the current gated
execution; it does not mean that remote execution is implemented or enabled.

## Current status

**Phase 1: IN PROGRESS, exit BLOCKED.** Only the feasibility spike and its
written decisions are in scope. There is no product code. A draft PR can carry
the evidence and decisions, but opening it does not satisfy a technical or
owner-decision gate. Phases 2 and 3 have not started.

The baseline `uv run pytest` completed before this branch's changes: 2870
passed, 4 skipped, 1 failed, 3 warnings (15.15s). The failure is
`tests/test_menubar.py::test_launch_claude_login_missing_claude`: its injected
`which` reports no executable, but the resolver also probes hard-coded
install locations and finds this machine's `/opt/homebrew/bin/claude` (2.1.274).
The isolated test still fails with a temporary HOME and `PATH=/usr/bin:/bin`.
A narrow test-only correction now simulates the missing-resolver result without
changing product behavior; the isolated test passes (`1 passed in 0.74s`).
The final assembled-branch suite is green: `2876 passed, 4 skipped, 3 warnings
in 13.52s`; the pytest header reported `2879 items` and 18 workers. The baseline
header and summary also differed by one, so the exact final summary is retained
here rather than attempting to reconcile the runner's item count.

The ChatGPT app-bundled Codex CLI is `0.158.0-alpha.2.1` (pre-release). A
separate official stable ARM macOS release, `0.157.1`, was downloaded to
`/private/tmp` and verified against its published SHA-256 before testing. Its
`codex sandbox --permission-profile research-test` wrapper allowed synthetic
workspace read/write and denied sibling read/write plus synthetic
`CODEX_HOME/auth.json` and `config.toml` reads. This narrows the stable-version
uncertainty for that low-level wrapper only; it does not prove `codex exec`
applies the profile. The spike harness and synthetic tests cover local helper
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
| Decisions and integration | `memo_writer` | Keep this progress record current, prepare the two owner-pending decision memos, reconcile spike evidence into an adapter contract and phase gates, and coordinate final review. No provider implementation. |

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
- [ ] Evidence for structured events, cancellation of the supervised process
  group, and restart/kill recovery; no automatic relaunch after an ambiguous
  side effect.
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
- [ ] The owner records the control-service operator, hosting, and repository.
  No phase-3 transport or service code starts before that answer.
- [x] Both written decision memos have been delivered for owner review.
- [ ] The phase-one evidence, adapter contract and reproducible harness are
  reviewed; phase-one PR is merged before phase 2 begins.
- [x] Full OpenSwap pytest suite is green on the assembled phase-one branch
  (`2876 passed, 4 skipped, 3 warnings in 13.52s`).

The control-service operator/hosting/repository decision is still pending and
blocks phase 1 exit. The Claude memo is delivered and its auth-path answer is
still pending, but that does not block Codex-only work; no Claude code starts
until the owner records a permitted path or exclusion. Current recommendations
are Codex-only for this phase and OpenTag as the service operator/repository
with hosting chosen by the owner; keep any phase-3 reference package clearly
separated and movable until ownership is recorded. These recommendations are
not owner decisions.

## Phase status

| Phase | Status | Exit evidence / blocker |
| --- | --- | --- |
| 1. Feasibility spike and authentication gate | IN PROGRESS / BLOCKED | A hash-verified stable 0.157.1 passes the synthetic low-level Seatbelt wrapper probe, but no authenticated `codex exec` proves account selection, refresh behavior, structured provider events, cancellation/recovery, or model/tool enforcement integration. No owner-authorized Codex slot or exclusive live-auth ownership is established. Control-service decision is pending; branch publication is separately blocked pending explicit authorization for the GitHub destination. |
| 2. Local worker, remote access off | NOT STARTED | Requires every phase-1 exit item above and the phase-1 PR merged. |
| 3. Private remote pilot | NOT STARTED | Requires phase 2's exit criteria and merged PR, plus the recorded control-service owner, hosting and repository. Keep a minimal reference implementation behind an interface in a movable package only after that gate. |
| 4. OpenTag connector | OUT OF SCOPE | Tracked in the separate OpenTag repository. |
| 5–6 | OUT OF SCOPE | Do not start. |

## Deviations and verification

- No deviation from plan 017's scope or ordering.
- The test-only missing-Claude fixture correction is a test determinism fix;
  it will preserve the `ClaudeSwitchError` assertion and will not change the
  resolver or executable discovery behavior.
- Credential-free harness tests passed (`5 passed in 3.69s`); `inspect`,
  inert-child `demo`, and synthetic Seatbelt `sandbox-probe` outcomes are in
  [the Codex spike record](research/remote-agent-host/spike-codex.md). This
  establishes only helper and local sandbox behavior, not provider execution.
- `uv` was not installed on `PATH`; version 0.12.19 was installed only under
  `/private/tmp/openswap-uv-test`, with its Python, cache, and project test
  environment under `/private/tmp`. The full suite used that isolated runtime.
- No Docker, Postgres or Supabase was run. No real provider credentials or
  Keychain entries were touched; synthetic auth sentinels were used only under
  `/private/tmp` for the Seatbelt boundary probe.
- Phase-one publication is blocked by auto-review. Exact rejection: “Pushing
  the branch exports repository contents to the unverified GitHub origin; the
  user authorized implementation/stacked PRs in general but did not
  specifically authorize this payload to this destination.” The requested
  authorization is to push the phase-one branch/commits to
  `https://github.com/mar3co/openswap` and create a draft PR. This is a
  publication authorization blocker, not a technical security finding; no
  push retry or workaround was attempted.
