# Remote Agent Host: product boundary research

Research date: 2026-09-27. Read-only inspection of the user's local OpenSwap checkout at `mar3co/openswap`, HEAD `6ec5c71506313a101ae8a145a9a43b48f0f40ce0` (main). This is a proposed feature request, not an implemented capability. Provider support and subscription terms require the separate provider research; this document does not establish them. Where this report differs from plan 017 on scope, names, limits or phase ordering, the plan supersedes it.

## Recommendation

Ship **Remote Agent Host** as an optional OpenSwap feature, implemented by a **separate local worker process bundled with OpenSwap**. Let OpenTag be its first remote client. Keep a versioned worker protocol and provider adapters so the worker can later be packaged independently, without launching a new OpenServer product or repository now.

This separates three decisions that need not happen together: the user-facing product is OpenSwap, the runtime is a separate worker process, and the code initially belongs in the OpenSwap repository. A long-running worker should survive a popover reload or menu-bar crash, but users should have one installer and one account setup. The first release should target the Mac already supported by OpenSwap.

The promise is: “Send a research task to your Mac from OpenTag; use an approved local agent account; follow progress and receive the result.” It is not verified support for automating a ChatGPT desktop conversation or inheriting all ChatGPT product features.

## Evidence from the actual project

| Observed fact | Source | Product or implementation consequence |
| --- | --- | --- |
| OpenSwap is a macOS menu-bar account utility; Windows/Linux are explicitly not shipped. | `README.md:3`; `docs/architecture.md:79` | An independently supported Linux/Windows server is a separate commitment, not an incidental extra flag. |
| Accounts and switching are behind `Engine`/`AccountEngine`, with separate Claude and Codex engines. | `docs/architecture.md:34`; `src/openswap/engine/protocol.py:17` | Reuse the engine boundary. Add a local account-reservation/session contract; do not put credential logic in the OpenTag connector. |
| Existing scheduled kickoff invokes `claude -p` and `codex exec`. | `src/openswap/kickoff.py:282`, `:301`, `:340`, `:358` | Headless invocation is already an architectural precedent, but kickoff is a short ping, not a durable session runner. |
| `openswap run`, `map`, and `unmap` were deliberately removed; session-profile tests cover kickoff, not launching terminal sessions. | `src/openswap/cli.py:1453`; `docs/testing.md:9`; `docs/architecture.md:42` | Build and validate a new job runner; do not describe the old session feature as shipping or restore its former UX by accident. |
| Claude isolated profiles may share settings, skills, commands, agents, and MCP definitions from the user's ordinary environment. | `src/openswap/session.py:1`, `:71`, `:101` | Remote research must have an explicit tool/config inheritance policy. A fresh config directory alone does not prove restricted permissions. |
| Claude profile credentials and backups require stale markers, quiescence checks, and adoption of refreshed generations. | `src/openswap/engine/session_profile.py:164`; `src/openswap/session.py:91` | Avoid “copy credentials and launch many workers.” Start with one job per account and coordinated refresh ownership. |
| Codex slot directories also serve as `CODEX_HOME`; accounts can be moved/swapped between slots. | `docs/architecture.md:36`, `:83`; `README.md` command table | Long-lived remote bindings need stable opaque account IDs, not mutable slot numbers. |
| Existing process detection excludes `codex exec` and `codex app-server` from live Codex TUI detection. | `docs/architecture.md:53`; `docs/menubar.md:54` | Remote jobs need their own persisted liveness/account leases. Existing busy detection is insufficient. |
| OpenSwap already uses per-user launchd services. | `src/openswap/launch_agent.py:65`, `:141` | Use a separate per-user worker service, not a privileged system daemon. Test login, logout, lock, sleep, and upgrade behavior explicitly. |
| The app-bundle/install transition is still in progress in the project plan. | `plans/README.md` entries 007–008 | Coordinate helper packaging with that transition; do not create a second updater or independent distribution pipeline in the MVP. |
| OpenSwap is MIT; OpenTag is AGPL-3.0. | Both projects' README license sections | Keep the protocol narrow and repository ownership clear. Do not casually copy OpenTag implementation into the MIT worker. Licensing details would need review before code sharing. |

## Option comparison

These are design judgments informed by the checkout, not externally measured market scores.

| Option | Benefit | Cost | Verdict |
| --- | --- | --- | --- |
| Put jobs and networking directly in the menu-bar process | Fewest initial moving pieces | UI, credential utility, network control, and long-lived task failure modes become intertwined | Avoid except a disposable feasibility spike. |
| Bundle a separate OpenSwap worker in the existing product | One install; reuses local account roster; jobs have their own lifecycle; can later extract | Requires an explicit worker protocol and coordinated account ownership | Recommended. |
| Ship a separately installed OpenServer daemon now | Clean server identity and independent lifecycle | Another product, installer, onboarding, updater, support surface, and likely cross-platform expectations before demand is proven | Defer. |
| Put local execution entirely in OpenTag | Convenient control-plane ownership | OpenTag must gain desktop installation and duplicate or depend on OpenSwap account management | Keep OpenTag as client/orchestrator. |

## Ownership and boundaries

**OpenSwap owns** local provider discovery, locally stored credentials, explicit account enrollment for remote use, account reservation, profile creation, process lifecycle, local execution policy, job/event persistence, artifact staging, and visible pause/revoke/stop controls. Existing engine code owns refresh and credential writes; the worker must not duplicate it.

**OpenTag owns** identifying the requesting person/workspace, task creation, remote routing, presenting job progress and approval requests, delivery of results, and auditing remote requests. A connected device is a scoped execution target, not a credential pool for the whole OpenTag workspace. First release should restrict use to the device owner; team delegation needs explicit grants.

**The transport owns** authenticated device registration, bounded task delivery, acknowledgment, expiring leases, and reconnect/replay behavior. An outbound device connection is the preferred design. The concrete hosting/transport choice depends on the OpenTag stack research; “WebSocket relay” should not be assumed deployable on the current hosting platform. No public inbound port is needed for the intended product.

**The versioned contract** begins with the operations plan 017 names (`workers_list`, `workspaces_list`, `jobs_submit`, `jobs_get`, `jobs_events`, `jobs_cancel`, `artifacts_list`, `artifacts_get`), with `jobs_approve` and `jobs_resume` deferred. Carry opaque device and workspace IDs, idempotency key, permission profile, provider, protocol version, and time/size limits. The request names no account or model, and tokens and raw provider credentials do not belong in it. An asynchronous MCP adapter is the first OpenTag transport for this contract; MCP is not a substitute for job lifecycle or authorization.

**Commercial and approval boundary:** the companion repository review (recorded in [mar3co/opentag#135](https://github.com/mar3co/opentag/issues/135)) found that OpenTag's current hosted model spend remains gated regardless of who supplies the model keys, and that generic custom MCP writes do not supply a universal dispatch approval path. Local provider consumption, OpenTag orchestration model usage, and relay/storage costs must be presented separately; “uses your eligible local subscription” must not become “all work is free.” Remote job submission needs an explicit OpenTag tool scope and approval/policy decision, even if exposed through MCP.

## First-use experience

1. OpenSwap Settings → Remote tasks → Enable. Show the device name and clear explanation of who will be able to send work.
2. Pair with OpenTag using a short-lived authenticated flow that identifies the person and workspace. Approve on the Mac.
3. Choose which local accounts can run remote work; default to an explicitly selected account and show provider-specific availability. Do not promise that every account in the roster is remotely usable.
4. Select a task profile and approved working location. Start with restricted research and a dedicated output directory; broader code edits are a separate profile.
5. Send a test task from OpenTag. See the same job ID, state, requester, provider/account alias, and stop control in OpenSwap.
6. On later tasks, OpenTag shows the plan 017 states under friendly labels: queued, starting (`claimed`/`starting`), running, waiting for approval, completed (`succeeded`), failed, cancelled, interrupted and expired; device offline is distinct from task failure. An “unknown after disconnect” condition must not trigger a duplicate launch.

OpenSwap should have a compact activity view: current job, who requested it, elapsed time, account alias, pause new tasks, stop job, and disconnect workspace. Show unavailable/locked/sleeping/reauthentication states in plain language. Avoid foreground desktop restarts as part of task dispatch.

The product's notification design should send actionable approval/failure/completion signals through OpenTag's configured notification surfaces, with no repeated alerts on routine reconnects. Push delivery is an integration capability to verify, not a guarantee merely because a notification was created.

## Staged implementation

### 0. Prove the local provider path

Use disposable directories and a user-authorized account to verify a restricted research request, structured output, cancellation, timeout, authentication expiry, quota failure, and credential persistence. Verify both same-account foreground coexistence and profile inheritance. Record provider versions and auth methods. One supported provider is enough for an initial vertical slice; unsupported providers remain explicitly disabled.

### 1. Local worker, no remote dispatch

Introduce a worker package within OpenSwap, with no AppKit/rumps imports, and a local IPC surface. Add persisted jobs/events, account leases, stable account identity, one active job per host (which implies one per account), and child-process termination. MVP account selection is pinned at job start; no mid-session auto-rotation or silent provider fallback. Exercise ordinary switching/kickoff/removal concurrently with a worker reservation.

### 2. One-device OpenTag integration (plan 017 phases 3 and 4)

Pair a single owner's Mac, accept idempotent tasks via outbound transport, stream resumable status, and return a bounded final report/artifacts. Queued tasks have an expiry. After a crash, reconcile the recorded process/session and report interrupted/unknown when necessary; do not blindly resubmit work that may have already caused effects. Revoke access, cancel a job, and pause new work from the device.

### 3. Harden and package (plan 017 phase 5)

Package the optional helper with the current OpenSwap installer/app pipeline. Verify upgrade while busy, Keychain lock, network interruption, sleep/wake, logout/reboot, output retention, provider CLI compatibility, resource limits, and local versus remote account operations. Acceptance requires that existing account switching and menu-bar behavior remain usable when remote work is disabled or the helper is unavailable.

### Later expansion

Multiple devices, simultaneous jobs across independently enrolled accounts, workspace permissions, code-edit profiles, additional client integrations beyond the first MCP adapter, a headless installer, and Linux/Windows support are separate milestones. Subscription eligibility and provider authorization still apply per account/provider; more saved subscriptions does not by itself establish safe concurrency or transferable entitlements.

## Extraction criteria for OpenServer

Consider independent packaging when a concrete customer needs the worker without the menu bar, or a second integration needs the versioned protocol. Consider a separate repository/product only when there is also sustained independent ownership: a supported Linux/Windows host or always-on machine deployment, customers who do not need account swapping, independent release cadence, and enough adoption to fund separate installation and support.

These are decision gates, not market facts. A proposed pilot review can examine number of remotely active devices, repeat task usage, failure reasons, fraction of users requesting headless operation, and number of non-OpenTag clients. Extraction can begin as another entry point from the same repository; a new brand is not a prerequisite.

## Open questions that materially change scope

- Are remote tasks personal-only initially, or should teammates consume accounts registered by someone else? Recommend personal-only for the first version.
- Is the Mac expected to remain logged in and awake? Recommend making that requirement explicit, rather than implying unattended availability after logout or a cold reboot.
- Does “research” mean web research, repository analysis, local document analysis, or all three? Recommend a constrained initial profile and explicit opt-in to local data sources.
- Which provider/auth combinations permit and support the required path? Follow the independent provider report; never infer permission from technical invocation alone.
- Do selected accounts already have live foreground CLI sessions? Account leases must coordinate that case rather than swapping the global login to satisfy remote work.

No source repositories were edited, accounts accessed, sessions launched, or external feature issues created during this research.
