# OpenSwap remote worker: architecture research

Researched 2026-09-27. This document separates observed provider capabilities from proposed OpenSwap design. It does not establish provider subscription eligibility or approve a particular credential integration; use the companion provider research for that release gate.

## Recommendation

Ship **Remote Worker as an opt-in OpenSwap feature**, with a separately testable host process and versioned protocol. OpenTag is the first remote client. Preserve the ability to distribute that host independently later; do not create a separate OpenServer product before the integration proves useful.

The value is a unified, permissioned way to dispatch work to users' existing machines and receive durable results across providers. Remote access alone is already offered by both major providers. The differentiation should be OpenTag coordination, account-aware local execution where permitted, a consistent run API, and reliable artifacts.

Use **host-initiated HTTPS polling for an initial deployment**, with an asynchronous public API/MCP facade for OpenTag. Add a persistent outbound connection only when the deployment platform supports a long-running relay service and measured latency justifies it. Run provider integrations on the host over local interfaces; never publish a raw provider control socket to the internet.

## What official products already establish

| Surface | Verified capability and practical implication |
|---|---|
| Claude Code Remote Control | Executes locally while Claude's mobile/web clients control sessions. Its server mode can create multiple sessions with a capacity limit and optional Git worktrees. It uses outbound HTTPS, scoped short-lived credentials, and server-stored transcripts. The local process must remain alive; network recovery differs by invocation mode. Mobile push and permission forwarding are documented. This validates the workflow and establishes substantial native competition. No third-party control API for this service was established by this research. [Official Remote Control docs](https://code.claude.com/docs/en/remote-control) |
| Codex app-server | Documented client integration includes conversation history, streamed events, authentication, approval requests, start/resume/fork, steering, and interruption. Local stdio is the default transport. Documentation marks app-server/WebSocket experimental and unsupported for production, warns non-loopback listeners can default to unauthenticated access, and describes explicit authentication/TLS. ChatGPT-managed and API-key modes exist. This is an adapter candidate behind OpenSwap's own boundary, subject to a version-pinned compatibility spike and provider review. [Official app-server docs](https://learn.chatgpt.com/docs/app-server) |
| Codex mobile Remote | OpenAI documents starting, steering, reviewing, and organizing tasks on connected development machines, including worktrees and approvals. The reviewed article does not establish a third-party remote-control API. [Official Remote field guide](https://developers.openai.com/blog/mastering-codex-remote-for-engineering) |
| OpenAI self-hosted Agents API environments | A different pattern runs the harness at OpenAI and `codex exec-server` locally. It registers with a restricted environment API key and connects outbound over WebSocket, reconnecting after drops. This is a useful architectural reference, but is API infrastructure, not evidence that consumer subscriptions can fund this integration. [Official self-hosted environments](https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted) |
| Tailscale | Devices attempt direct connectivity with encrypted relay fallback when NAT/firewalls prevent it. WireGuard protects both direct and relay paths; DERP cannot decrypt relayed payloads. It can simplify a private prototype, but requires client/network participation. [Connection types](https://tailscale.com/docs/reference/connection-types), [DERP](https://tailscale.com/docs/reference/derp-servers) |

## Transport decision

| Approach | Strength | Limitation | Recommended use |
|---|---|---|---|
| Outbound HTTPS polling to authenticated queue | No inbound port configuration; fits ordinary HTTP infrastructure; bounded request lifetimes; simple restart/retry semantics | Poll delay; queue operations and transcript storage cost; relay operator sees plaintext unless additional end-to-end encryption is implemented | MVP default |
| Outbound persistent WebSocket to relay | Low-latency progress, steering, approvals | Requires suitable persistent-connection hosting, backpressure, reconnect cursors, connection ownership and draining | Follow-on transport under same protocol |
| Mesh VPN / tailnet | Private addressing and existing device controls; less bespoke transport for technical users | Hosted OpenTag would need approved tailnet connectivity; users must install/manage network membership; network membership is not run authorization | Developer prototype / later self-hosted option |
| Direct inbound HTTPS | Less relay infrastructure in reachable environments | Port forwarding, CGNAT, TLS lifecycle, changing addresses, and broad internet attack surface become user problems | Avoid as consumer default |

OpenTag code findings from the companion repository review: custom remote MCP endpoints require public HTTPS and reject private/loopback/CGNAT destinations outside a dogfood exception. Defaults include a 15-second tool timeout, 32 KB content cap and 256 KB body cap. Therefore OpenTag should call a public asynchronous facade, not the laptop's private address, and should fetch artifact references instead of embedding large results. Its existing managed write gate covers particular billing integrations, so worker dispatch must be deliberately connected to mutation approval logic.

Do not place a permanent worker socket inside a short-lived request handler. A polling MVP can accept bounded HTTP requests; a later WebSocket implementation should use infrastructure designed to keep connections alive.

## Proposed components and data flow

1. **OpenSwap desktop UI** owns opt-in, enrolled controllers, authorized accounts/workspaces, host health, run history and a local disable/stop control.
2. **OpenSwap host process** owns execution, policy enforcement, account selection, adapter processes, approval state and a durable local event journal. Run it under the logged-in user initially; specify later behavior for logout/reboot explicitly.
3. **Relay/control service** owns controller authentication, device registration, a bounded command queue, transport delivery, durable metadata and notification fan-out. It never receives the user's provider refresh tokens or credential files. Provider credentials still travel to the relevant provider as required by its authentication protocol.
4. **OpenTag connector** discovers eligible hosts/workspaces, submits tasks and renders status/results. It does not choose arbitrary local paths, shell commands or provider executable arguments.

Execution: OpenTag submits a structured job → service validates controller scope and records an idempotent command → host fetches and authorizes it locally → provider adapter runs → host persists and forwards sequenced events → OpenTag reads status and artifact references. The host is authoritative about whether execution began or finished; the service is authoritative about submission receipt and queued delivery.

MVP operations: `list_workers`, `list_workspaces`, `submit_run`, `get_run`, `get_events(after_cursor)`, `cancel_run`, `list_artifacts`, `read_artifact`. Add `respond_to_approval` only with authenticated human approval routing and provider adapter support. Every mutating operation includes a unique operation ID. `submit_run` returns a run ID promptly; it never holds a tool invocation open until research finishes.

Suggested request fields: host ID, registered workspace ID, provider adapter ID, opaque account profile ID, prompt, policy preset, runtime budget, queue deadline and idempotency key. The host resolves workspace/profile IDs locally. Negotiate protocol and adapter capabilities so unsupported resume/approval modes are rejected before launch.

## Pairing and access design

- Enrollment begins on the host, displaying a short-lived, single-use pairing code/QR. An authenticated OpenTag user claims it and the local user confirms the controller identity and scope. Expired/replayed codes fail. Avoid pairing through a reusable link that grants blanket execution.
- Generate a device credential in OS-protected storage. Issue separate short-lived controller grants bound to owner, host, allowed workspace/profile IDs and allowed actions. Pairing, execution credentials, provider credentials and notification tokens are separate secrets.
- The relay checks identity and scope; the host independently checks every command against its local allowlist and policy. Do not trust an account/profile ID just because the relay supplied it.
- Revoking a controller immediately stops new submissions at the service, closes its access to existing run data, and invalidates queued commands. Host polling responses carry revocation state; a host that has not refreshed authorization must not start new remote work after its authorization lease expires. Cancellation of already-running work is a separate explicit choice.
- Run a single owner and one active job per host in the MVP. Select the account before launch and pin it for the job. Never change global provider credentials beneath a running process; do not promise concurrency until per-process credential/state isolation is proven.
- Register allowed directories locally. Canonicalize filesystem paths and defend against symlink/path traversal in artifact access. A worktree avoids file-edit conflicts; it is not a security sandbox.
- Apply OS/provider sandbox boundaries where supported, per-adapter tool restrictions and dedicated output directories. “Research” is a task description, not a security guarantee. Research presets may read approved project files and write outputs while shell/network/connector capabilities remain explicitly constrained.
- Treat prompts, repository instructions and tool output as untrusted content that cannot expand controller permissions. Broker secrets must not be available in child process environments. Redact sensitive data in logs; avoid collecting provider token files in artifacts.

MVP TLS protects transport, but does not justify calling the relay end-to-end encrypted. If opaque relaying is a product requirement, define exactly which endpoints decrypt: a hosted OpenTag orchestrator necessarily sees prompts/results it consumes. Defer blanket E2EE marketing until the actual threat model and implementation support it.

## Durable run lifecycle and recovery

Persist job identity, selected adapter/profile, workspace, policy snapshot, budgets, provider session ID, delivery attempt, host epoch, last event sequence, timestamps and terminal reason. Use a local transactional store plus an append-only event sequence; keep plaintext prompt/result retention configurable.

Suggested states: `queued`, `claimed`, `starting`, `running`, `waiting_for_approval`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, `interrupted`, `expired`. Track host connectivity separately (`online`, `offline`, last seen). A dropped connection must not turn a running job into success/failure. Do not describe offline as sleeping unless the host actually reported a sleep transition.

Events carry `run_id`, monotonically increasing per-run sequence, event ID, timestamp and typed payload. Reconnection resumes from a cursor and tolerates duplicates. Acknowledgements mean events are durably stored, not merely written to a socket. Coalesce token deltas, bound buffers and retain final status even if verbose logs are truncated.

Deduplicate submission retries using the stable operation ID, including across process restarts. Use a host-specific lease/epoch to prevent two host processes from claiming the same command. Do not promise exactly-once external side effects: if a process crashes after an action but before recording its result, mark the outcome uncertain and reconcile or require review instead of blindly rerunning.

If the phone disconnects, an authorized local job continues within its limits. If the machine loses the internet, provider work may also stall; persist available events and reconnect with jittered backoff. If it sleeps or powers down, computation stops. On restart, reconcile provider session/process state before reporting or resuming. If safe continuation cannot be established, report `interrupted` and offer a resume/retry that explains the risk of duplicated effects.

For MVP, reject new submissions to an offline host promptly. Later, optional offline queueing uses a clear deadline and rechecks credentials, policy, workspace and cancellation before execution. Do not silently execute stale prompts after a laptop wakes days later. “Keep awake while running” may be an explicit host setting; do not promise wake-from-sleep.

Artifacts are allowlisted output objects with ID, name, size, media type, hash and creation time. Relay copies are opt-in or tied to the run's disclosure settings, with bounded size and retention. Secure downloads require current run ownership; avoid arbitrary path reads and indefinite bearer links. Return a concise result plus citations/artifact references.

## Approval and notification routing

Keep approvals separate from model-generated messages. Bind each request to the exact run, tool/action, arguments digest, permission scope, host epoch and expiration. A human sees the concrete command/change and approves or rejects once. Local and remote UIs race through a single resolution record; stale or repeated responses fail safely. An unavailable approver leaves the run waiting or expires it under its preset; it does not silently enable unrestricted execution.

Push notifications should signal `needs_input`, selected failures, and optionally completion. Include an authenticated deep link, not sensitive prompts or credentials. Notification delivery is best-effort and never an approval. Deduplicate by approval/event ID, respect quiet hours, and display pending decisions reliably inside OpenTag even if mobile delivery fails. Implement actual push only after checking OpenTag's mobile/web delivery capabilities; this document does not claim the current research session can send custom push messages.

## Delivery phases and acceptance criteria

**Phase 0: compatibility and authorization spike.** Prove one provider adapter can start, stream, finish, interrupt and recover a task using an approved auth method. Verify account isolation and remote approval behavior on pinned versions. Produce a support matrix; an unsupported subscription path blocks that adapter, not the protocol work.

**Phase 1: private MVP.** Codex-first read-only research over approved source workspaces, with writes limited to a dedicated output directory; subject to the Phase 0 authentication/compatibility gate. Single user, single host, single active run; manual account selection; registered workspaces; polling relay; asynchronous OpenTag connector; terminal summaries/artifacts; explicit offline rejection and cancellation. Claude subscription-backed orchestration stays gated on the exact permitted credential and invocation path; a later BYOK offering is an explicit expansion of scope. Keep code in separate `host`, `protocol`, `adapter` and `connector` modules even if packaged through OpenSwap.

Acceptance tests:

1. A phone on a different network starts a run without inbound laptop ports; submission returns a run ID within OpenTag's 15-second timeout (target under two seconds in normal conditions).
2. Repeating the same submission before/after a response loss produces one run; restarting the host does not lose that mapping.
3. Disconnecting the client and reconnecting shows a consistent event history and the correct final result; duplicate events do not duplicate UI entries.
4. A disconnected host is visibly unavailable; no job is falsely reported started. Queue expiry/cancellation wins over a later delivery if queueing is enabled.
5. Revoked controllers cannot submit, fetch artifacts, answer approvals or execute previously queued work after authorization refresh/lease expiry.
6. Unknown workspace/profile IDs and artifact path escapes fail on the host. Provider and host secrets do not appear in relay payloads or artifacts.
7. Cancel is acknowledged promptly when online and eventually reports the provider's actual interruption outcome; unsupported cancellation cannot masquerade as success.
8. Crash during an ambiguous side effect produces `interrupted`/uncertain status, with no automatic duplicate action.
9. An approval cannot be answered by an agent tool call without the configured human approval path. Two clients cannot both apply the same decision.
10. A second run queues/rejects clearly; it does not switch credentials or write concurrently into the active workspace.

**Phase 2: reliability and broader execution.** Second provider after eligibility review, provider-version compatibility tests, per-process account isolation, concurrent worktrees, durable approval routing, notifications, explicit queued-offline behavior, and optional persistent relay connections. Add resource/concurrency quotas and support diagnostics before widening permissions.

**Phase 3: independent host distribution if justified.** Extract a headless installer and optional self-hosted relay when real users need Linux/devboxes, boot-time service operation, multiple host clients, or deployment without OpenSwap's credential UI. Consider an OpenServer brand only when that is a distinct product with an owner and support budget. The module boundary enables this without burdening the first release with a second product.
