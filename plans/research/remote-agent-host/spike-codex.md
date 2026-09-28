# Phase 1 Codex feasibility spike

Research run on 2026-09-27; updated 2026-09-28 (stable-CLI recheck, detached-descendant reproduction). This records local binary/help evidence and
credential-free harness tests. No real Codex login, credential file, Keychain
item, or provider job was accessed; the synthetic sandbox probe creates a fake
`auth.json` sentinel in its temporary profile. It is not evidence of live account isolation,
web research quality, refresh behavior, or provider-run cancellation/recovery.

## Installed CLI evidence

The discovered binary is:

```text
/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex
codex-cli 0.158.0-alpha.2.1
```

This is a pre-release build, so it is not a release pin suitable for enabling a
worker. `codex exec --help` advertises `--json`, `--sandbox`,
`--skip-git-repo-check`, `--ignore-user-config`, `--ignore-rules`,
`--ephemeral`, and `--dangerously-bypass-hook-trust`. The last option is
explicitly marked dangerous by the CLI and is not used by the harness. Help
does not establish which user/project hooks, MCP servers, plugins, skills, or
environment values are loaded for a real run.

The version and help probes ran with temporary `HOME` and `CODEX_HOME`
directories. These probes do not authenticate and do not establish that an
actual Codex run would select that profile, avoid keychain-backed auth, avoid
fallback auth variables, or keep refresh writes isolated. No config or auth
files were read or created as part of this inspection.

Official OpenAI documentation describes `codex exec` as non-interactive,
read-only sandboxed by default, and capable of JSONL events with `--json`;
the event stream can include tool calls and web searches. It also states that
`codex exec` reuses saved authentication by default. These are documented
capabilities, not validation of this installed pre-release binary or a
particular account path: [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

## Harness and evidence limits

`scripts/remote_agent_host_spike.py` provides four credential-free probe/recovery commands and one disabled live command:

```bash
uv run python scripts/remote_agent_host_spike.py inspect \
  --codex-bin '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex' \
  --evidence-dir /private/tmp/openswap-codex-spike-evidence

uv run python scripts/remote_agent_host_spike.py demo \
  --state-dir /private/tmp/openswap-codex-spike-demo

uv run python scripts/remote_agent_host_spike.py sandbox-probe \
  --codex-bin '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex' \
  --evidence-dir /private/tmp/openswap-codex-spike-evidence

uv run python scripts/remote_agent_host_spike.py recover \
  --state-dir /private/tmp/openswap-codex-spike-demo
```

`--state-dir` and `--evidence-dir` create each missing ancestor as a
mode-0700 directory owned by the caller. On POSIX, existing directories must
already be owned by the caller with no group or world permissions; the harness
preserves their mode and refuses unsafe paths. Journal, evidence and lock files
are opened with `O_NOFOLLOW`, so a planted symlink is refused rather than
followed.

`inspect` invokes only `--version` and `exec --help` under a disposable home.
`demo` starts only this script's inert child process, cancels its process group,
and writes a mode-0600 JSONL journal/evidence file. `recover` changes unresolved
`starting`, `running`, and `cancel_requested` journal entries to
`interrupted` and records whether the journaled process group is still alive
(`group_still_alive`); it never signals or terminates an orphan, because the
pgid may have been reused. A start is refused while any job's latest state is
non-terminal, so recovery must run first; an existing job ID is refused on
future starts. It never replays work. Evidence keeps state names and event
type names restricted to lowercase dotted identifiers (anything else is
recorded as `unknown-event`), and the version probe stores nothing unless the
output matches `codex-cli <token>`. Raw output, task text, paths and
credentials are never stored. The fake child receives an allowlisted
environment (`PATH`, `HOME` set to the state directory, `SPIKE_*`). The test suite also supplies fake
executables to verify process-group signaling and no replay after uncertain
recovery.

`sandbox-probe` writes a new throwaway Codex profile with the tested
`research-test` filesystem profile and synthetic `auth.json` sentinel, then
runs only local shell commands through `codex sandbox`. It checks a workspace
read/write and denial of outside read/write plus the synthetic profile's auth
and config files. Its profile configuration is:

```toml
default_permissions = "research-test"

[permissions.research-test]
extends = ":workspace"

[permissions.research-test.filesystem]
":root" = "deny"
":minimal" = "read"
":tmpdir" = "deny"
":slash_tmp" = "deny"

[permissions.research-test.filesystem.":workspace_roots"]
"." = "write"
```

This tests the CLI's low-level Seatbelt wrapper and configured path boundary
only. It does not prove that `codex exec` applies the profile as expected, that
web research works under it, or that an LLM cannot induce disallowed actions.
The earlier probe in `spike_inheritance` required sandbox execution permission
from the host; if macOS refuses to run the local sandbox wrapper, that is a
host permission limitation, not a passing deny result.

The `live` command always refuses. It does not launch Codex, even if supplied a
task and a path called `CODEX_HOME`. The owner has not selected a roster slot,
and exclusive credential ownership has not been established. There is no
refresh-race or account-isolation result to report. Do not treat the fake
process tests as proof that Codex reacts to cancellation or that Codex JSONL is
complete under process termination.

## Recorded outcomes

The local harness previously reported:

```text
inspect: codex-cli 0.158.0-alpha.2.1; exec_json=true; sandbox_option=true;
         skip_git_repo_check=true; ignore_user_config=true; no_auth_performed=true
demo:    state=cancelled; returncode=-15; event_names=[thread.started]
sandbox: inside read/write allowed; outside read/write denied;
         synthetic CODEX_HOME auth/config reads denied; no exec/model run
tests:   40 passed (full assembled suite: 2,911 passed, 4 skipped, 3 warnings)
```

After the bounded process-group runner was added, the official stable ARM
macOS `0.157.1` binary was also checked from `/private/tmp`. `inspect` reported
`version=codex-cli 0.157.1`, `matches_discovered_pin=false`, and
`no_auth_performed=true`. A credential-free `sandbox-probe` using synthetic
workspace and `CODEX_HOME` sentinels allowed workspace read/write and denied
sibling read/write plus synthetic `auth.json` and `config.toml` reads. This is
wrapper-only Seatbelt evidence; it does not establish `codex exec` enforcement
or account selection. The helper-cleanup regression passed in isolation
(`1 passed in 1.19s`); all 60 harness cases are included in the full-suite
result above. The two recorded evidence rows from that recheck:

```json
{"kind": "codex_help_probe", "version": "codex-cli 0.157.1", "expected_version": "codex-cli 0.158.0-alpha.2.1", "matches_discovered_pin": false, "pre_release": false, "no_auth_performed": true, "exec_json": true, "ignore_user_config": true, "sandbox_option": true, "skip_git_repo_check": true}
{"kind": "low_level_sandbox_probe", "inside_read_allowed": true, "inside_write_allowed": true, "outside_read_denied": true, "outside_write_denied": true, "codex_home_auth_and_config_denied": true, "exec_or_model_run": false}
```

The recovery journal appender separates an unterminated trailing record before
writing a new JSONL row. The recovery regression preserves the malformed tail
as a separate ignored line and verifies the next recovery pass does not repeat
the interrupted transition.

The fake-process harness includes regression coverage for the Codex review
findings on [PR #59](https://github.com/mar3co/openswap/pull/59): finding
[#4121377781](https://github.com/mar3co/openswap/pull/59#discussion_r4121377781)
ensures a successful leader exit cannot hide a still-running descendant in the
supervised process group, and
finding [#4121377796](https://github.com/mar3co/openswap/pull/59#discussion_r4121377796)
ensures invalid UTF-8 becomes an `unstructured-output` event instead of
terminating the reader. These tests validate the fake-process harness only;
they do not establish provider execution or cancellation behavior.

Finding [#4121533420](https://github.com/mar3co/openswap/pull/59#discussion_r4121533420)
is covered by parameterized tests for `OSError` and timeout failures from both
the version and help probes. The operator receives a sanitized `refused:`
message, with no traceback or exception output; any other `OSError` or
`ValueError` reaching the entry point prints a fixed `refused: harness
failure (details withheld)` line. A corrupted journal (oversized numbers,
invalid bytes) is skipped row by row rather than crashing recovery. These are local CLI-probe
failure tests; they do not establish Codex provider execution.

Findings [#4121649254](https://github.com/mar3co/openswap/pull/59#discussion_r4121649254)
and [#4121649264](https://github.com/mar3co/openswap/pull/59#discussion_r4121649264)
are covered by noisy-output and subprocess-failure tests: output lines and
retained event names have explicit caps while the pipe continues draining, and
both sandbox probes return sanitized refusals for launch, timeout, or decode
failures.

Finding [#4121751784](https://github.com/mar3co/openswap/pull/59#discussion_r4121751784)
is covered with a 5,000-digit integer and below-cap nested JSON followed by a
valid event. Both parser failures are retained as `unstructured-output`, and
the next event is still collected. Python 3.14 parses this nesting depth, so
the test injects a `RecursionError` for the nested record to exercise that
failure path.

Finding [#4121829716](https://github.com/mar3co/openswap/pull/59#discussion_r4121829716)
is covered by a bounded process-group runner: timeout and normal leader exit
clean up observed members of the owned process group, and probe launch failures
remain sanitized. They do not guarantee cleanup of descendants that leave that
group.

The sandbox probe now gives every synthetic operation a unique start,
completion, and exit-status marker. Denials are counted only when that command
completed with a nonzero status, its own stderr contains `Operation not
permitted`, and the corresponding sentinel stayed hidden. The auth/config
check requires both `cat` operations to complete and be denied independently.
If Seatbelt initialization fails before the shell runs, or output contains only
partial/misleading sentinels, the harness refuses instead of recording a
passing boundary result. A hash-verified stable 0.157.1 run passed this stricter
probe again with workspace read/write allowed and sibling plus synthetic
`CODEX_HOME` reads denied.

Findings [#4122150605](https://github.com/mar3co/openswap/pull/59#discussion_r4122150605)
and [#4122150621](https://github.com/mar3co/openswap/pull/59#discussion_r4122150621)
are covered by bounded probe collection: stdout and stderr each retain at most
256 KiB, are read concurrently without unbounded buffers, and overflow stops
the owned group and returns a sanitized refusal. Cleanup waits on the process
without re-reading the pipes. Fake stdout-only, stderr-only, and dual-stream
floods verify refusal and stop a same-group helper. This does not close the
separately documented detached-descendant gap.

The later review finding
[#4122008642](https://github.com/mar3co/openswap/pull/59#discussion_r4122008642)
was reproduced credential-free with a disposable fake executable. It spawned a
helper that called `setsid()`, redirected stdout/stderr, wrote a heartbeat, and
had a hard four-second self-expiry. `_run_probe` returned success in 1.816s
while the helper heartbeat continued; explicitly signaling the recorded helper
PID stopped it. This demonstrates that process-group checks do not cover a
descendant that escapes the group and closes inherited pipes. The helper was
stopped before the temporary directory was removed; no process was left
running.

The fake harness now mitigates this best-effort: while the leader is alive it
snapshots `ps -axo pid=,ppid=,pgid=,stat=,lstart=` and attributes every
descendant by parent pid regardless of process group, keyed by pid plus a start-time,
group and command identity so a reused pid is dropped rather than signalled;
signals go through a non-reusable pidfd where the OS provides one (Linux),
and on macOS the identity is re-read before each signal while evidence
records `escaped_cleanup_certain: false` because check and signal are not
atomic; before deciding a terminal state it
re-snapshots, terminates any tracked descendant still alive outside the
group (TERM, grace, KILL), records them as `escaped_descendants` with
`escaped_descendants_terminated`, and forces the state to `interrupted`,
never `succeeded` or `cancelled`. The reproduction is retained as
`test_setsid_detached_descendant_forces_interrupted_and_is_terminated`. The
residual gap stands: a descendant that forks between the final snapshot and
the leader's exit is reparented before it can be attributed, so no complete
process-tree cleanup guarantee is claimed and the cancellation gate remains
blocked until a provider run is measured.

The `sandbox-probe` pass required an escalated but credential-free local run so
macOS could launch the sandbox wrapper. `codex exec` was never run under this profile;
the pass proves only the `codex sandbox` wrapper's boundary for synthetic
files. The live research-run gate remains
unmet.

## Current adapter feasibility

The local version/help surface is sufficient to prototype argument discovery,
but not to accept a provider adapter contract yet. Before a live run is
enabled, the owner must select and authorize a specific Codex profile and
confirm exclusive use for the run. Then the spike must verify actual
`CODEX_HOME` account selection, read/write restrictions and inherited tools,
JSONL event behavior, process-group cancellation, restart outcome, and refresh
behavior with that same profile. A release CLI version must be pinned and
repeated through those checks. Until then, account isolation, research
capabilities, and refresh safety remain unknown.
