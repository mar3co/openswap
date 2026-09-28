# Codex research adapter contract (provisional)

Status: provisional. On 2026-09-28 the owner authorized phase-2 local worker infrastructure to proceed in parallel with the ongoing phase-1 review. This permits implementing the local protocol, journal, queue, leases and IPC against fake or disabled adapters; it does not pass any phase-1 evidence gate. Real Codex execution remains disabled until the stable-version, authentication, effective-tool, filesystem, refresh-race, cancellation, and recovery gates are met. Plan 017 is canonical. This contract is intentionally one-shot: `probe`, `start`, `events`, `interrupt`. It has no `resume` or approval operation.

## Contract boundary

The worker may call only these operations:

| Operation | Responsibility |
|---|---|
| `probe` | Readiness check for the pinned local Codex installation and locally selected identity. Return typed availability, supported version and safe diagnostic codes. Do not start a conversation, change authentication, or return secrets. |
| `start` | Start one admitted job in the already-approved research workspace using the fixed Codex adapter. Return the provider process/session reference privately to the local journal, plus a safe initial event. Do not resume or fork another provider session. |
| `events` | Consume the running process's JSONL stream, normalize it into sequenced local job events, and redact credential-like material before persistence or relay. Cursor reads replay journaled events only; they do not alter execution. |
| `interrupt` | Signal the supervised provider process group, observe termination, and report stop-requested versus execution-stopped distinctly. Never report completion before the process is stopped. |

The phase-one probe supervises the initial process group and, in the fake
harness, tracks descendants by parent pid so a `setsid()` child that escapes
the group is still found, terminated and reported as `interrupted`. A
credential-free fake `setsid()` child originally escaped, closed inherited
output and kept running after `_run_probe` returned success; that case is now
a regression test. Tracking is best effort: a fork between the final snapshot
and leader exit is reparented before attribution. Thus the wrapper does not
satisfy the complete-process-tree interruption gate; do not claim an
execution-stopped result until detached-descendant handling is measured
against a real provider run.

The phase-1 adapter is Codex-only. A remote job cannot choose an executable, executable arguments, provider binary path, environment variable, account, model, auth file, arbitrary path, or tool set. The only locally pinned provider identity is selected before a job and is stable for its lifetime; OpenSwap uses a stable opaque local identity, not a movable slot number. A submission has no account or model field. It carries a task and a registered opaque workspace identifier, which the host resolves to the approved directory.

The host admits **one active provider job per host**. It does not rotate credentials to work around limits. It will not overwrite live auth state, import token snapshots, or copy token files to another profile. If the local identity cannot be used without racing with an interactive Codex process or another refresh writer, `probe` reports unavailable and `start` refuses.

## Filesystem and provider-state boundary

Two kinds of writes exist and must stay distinct:

1. **Model/tool filesystem access:** the approved research directory is the only writable root. Task output lives under that directory. The launcher supplies the canonical working directory, never caller text, omits `--add-dir`, and rejects jobs whose workspace ID does not map to the locally approved root. The plan's command is `codex exec` in that directory with `--skip-git-repo-check`; that flag only skips the repository guard and must not be treated as disabling project configuration discovery.
2. **Provider-managed process state:** Codex itself may need its selected local provider profile for sign-in and token refresh, and may write provider-owned runtime state there. This is not a model/tool writable root. The adapter must keep that state outside the model/tool sandbox roots and prove the CLI can complete required auth/state writes without exposing the files to model-directed commands. The path, storage mode, refresh writes, session files, and OS keychain behavior need a version-pinned test; this spike does not define or weaken the boundary.

A broad `workspace-write` mode is insufficient by itself: the installed help exposes `--add-dir`, and the official sandbox documentation says the built-in `:workspace` profile includes temp roots. A named permission profile can express a single workspace root while denying `:tmpdir`, `:slash_tmp`, and reads outside minimal runtime files. Credential-free tests on the installed pre-release and hash-verified stable 0.157.1 CLI builds proved these rules at the `codex sandbox` wrapper only; see the [inheritance spike](./spike-inheritance.md) and [stable CLI spike](./spike-stable-cli.md). They did not prove `codex exec` launches model tool commands under the same profile.

Codex configuration and tools are separate from this shell boundary. The effective run must positively establish that no unapproved user, project, system, plugin or desktop integration is available. In particular, user/project MCP, command or MCP hooks, plugin-bundled MCP/hooks, admin/system skills, browser/app tools, and inherited environment values cannot be accepted merely because the working directory is sandboxed. If an unapproved surface cannot be disabled and verified on the pinned supported version, research-profile dispatch is not allowed.

## Lifecycle and job states

The adapter does not own the remote job state machine. The host maps execution into exactly these canonical states:

`queued`, `claimed`, `starting`, `running`, `waiting_for_approval`, `cancel_requested`, `succeeded`, `failed`, `cancelled`, `interrupted`, `expired`.

This contract never emits `waiting_for_approval`; in-run approval and resume are out of scope. If Codex requires an unsupported approval or interactive action, fail closed. Do not wait for or infer owner approval, and do not silently approve.

- A pre-launch expired or cancelled job never starts.
- After launch, a remote stop request first records `cancel_requested`. The host sends `interrupt` once, then uses `cancelled` only after it proves the provider process stopped.
- A nonzero provider exit or readiness/tool failure becomes `failed` with a redacted diagnostic.
- A normal, confirmed terminal provider result becomes `succeeded`.
- If the worker loses the process or cannot determine whether work already occurred, mark `interrupted`. **Never automatically relaunch an interrupted job.** No adapter resume operation exists in this phase.
- Worker connectivity (`online`/`offline`, last seen) is separate from job state.

Events are append-only and cursor-addressable. Each normalized event includes the local job ID, monotonic cursor, timestamp, and safe event kind/data. Event/log/artifact processing must redact provider credentials and avoid uploading local absolute paths or raw provider auth/session files.

## Launch constraints

The adapter launcher, not the remote caller, fixes all process arguments. It passes the approved workspace as cwd (and `-C` only if the pinned CLI requires it), `--json`, and the plan-approved `--skip-git-repo-check`. It must not pass `--add-dir`, remote `-m/--model`, caller `-c/--config`, dangerous bypass flags, `--remote`, or `resume`/`fork` subcommands. The web research capability may use the provider's supported search surface only after it is explicitly approved and verified; shell network policy is not proof that search, MCP, apps, or plugins are disabled.

The subprocess environment is built from an explicit allowlist. The current documented default for command subprocesses is inheritance of `all`; do not rely on that default for dispatch. Remove unrelated desktop/session/MCP tokens, API keys, and proxy credentials. Pass only provider-required variables plus minimal runtime variables. Verify the actual tool subprocess environment with synthetic sentinels on the pinned CLI before claiming it is clean. Environment filtering alone does not protect filesystem-readable secrets.

## Phase-1 acceptance evidence still required

This contract remains provisional until all of the following are measured on a supported, pinned CLI and the locally authorized Codex identity:

- `probe` sees the intended local identity without revealing credentials and without mutating the user's default login.
- An authenticated `codex exec --json` research run starts headlessly, produces structured events and citations, and uses the selected account.
- An adversarial model/tool read of synthetic files placed at the exact auth/config/profile paths is denied; model/tool writes succeed only under the research workspace, including attempts at `/tmp`, `$TMPDIR`, sibling roots, symlinks, and alternate absolute paths.
- No user/project/admin/system/desktop MCP, unapproved hooks, plugins, skills, browser/apps, or unexpected environment variables enter the effective run. The approved research capability list is observable and allowlisted.
- Provider-owned token refresh and session/runtime writes remain possible under the local provider process while the model/tool boundary remains closed; concurrent UI activity and refresh races are tested. No credential snapshots or in-flight account switches are used.
- `interrupt` terminates the complete provider process tree. Kill/restart after launch leads to `interrupted`, with no automatic new launch. No resume or approval call is made.
- The spike demonstrates that the adapter can run one serialized job against a stable locally pinned identity without accepting an account or model override. Worker-level one-active-job enforcement and protocol rejection of remote account/model fields are phase-2/3 implementation gates, not phase-1 acceptance criteria.

The installed `0.158.0-alpha.2.1` prerelease and public stable `0.157.1` CLI help and synthetic Seatbelt wrapper tests are preliminary observations only. They do not meet these acceptance criteria. For documented inheritance limits and raw local command results, see [spike-inheritance.md](./spike-inheritance.md) and [spike-stable-cli.md](./spike-stable-cli.md).

## Official references

- [Non-interactive Codex CLI](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex configuration and hooks](https://learn.chatgpt.com/docs/config-file/config-advanced)
- [MCP server configuration](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Codex skills](https://learn.chatgpt.com/docs/build-skills)
- [Codex permission profiles and limits](https://learn.chatgpt.com/docs/permissions)
- [Codex authentication](https://learn.chatgpt.com/docs/auth)
