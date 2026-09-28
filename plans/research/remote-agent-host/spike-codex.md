# Phase 1 Codex feasibility spike

Research run on 2026-09-27. This records local binary/help evidence and
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

`inspect` invokes only `--version` and `exec --help` under a disposable home.
`demo` starts only this script's inert child process, cancels its process group,
and writes a mode-0600 JSONL journal/evidence file. `recover` changes unresolved
`starting`, `running`, and `cancel_requested` journal entries to
`interrupted`; an existing job ID is refused on future starts. It never
replays work. Evidence keeps state names and JSON event type names, not raw
output, task text, paths, or credentials. The test suite also supplies fake
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

The local harness reported:

```text
inspect: codex-cli 0.158.0-alpha.2.1; exec_json=true; sandbox_option=true;
         skip_git_repo_check=true; ignore_user_config=true; no_auth_performed=true
demo:    state=cancelled; returncode=-15; event_names=[thread.started]
sandbox: inside read/write allowed; outside read/write denied;
         synthetic CODEX_HOME auth/config reads denied; no exec/model run
tests:   21 passed
```

The fake-process harness includes regression coverage for the Codex review
findings on [PR #59](https://github.com/mar3co/openswap/pull/59): finding
[#4121377781](https://github.com/mar3co/openswap/pull/59#discussion_r4121377781)
ensures a successful leader exit cannot hide a still-running descendant, and
finding [#4121377796](https://github.com/mar3co/openswap/pull/59#discussion_r4121377796)
ensures invalid UTF-8 becomes an `unstructured-output` event instead of
terminating the reader. These tests validate the fake-process harness only;
they do not establish provider execution or cancellation behavior.

Finding [#4121533420](https://github.com/mar3co/openswap/pull/59#discussion_r4121533420)
is covered by parameterized tests for `OSError` and timeout failures from both
the version and help probes. The operator receives a sanitized `refused:`
message, with no traceback or exception output. These are local CLI-probe
failure tests; they do not establish Codex provider execution.

Findings [#4121649254](https://github.com/mar3co/openswap/pull/59#discussion_r4121649254)
and [#4121649264](https://github.com/mar3co/openswap/pull/59#discussion_r4121649264)
are covered by noisy-output and subprocess-failure tests: output lines and
retained event names have explicit caps while the pipe continues draining, and
both sandbox probes return sanitized refusals for launch, timeout, or decode
failures.

The `sandbox-probe` pass required an escalated but credential-free local run so
macOS could launch the sandbox wrapper. The actual `exec` and `sandbox` wrappers
did not share this same provider execution: this proves only the local Seatbelt
profile boundaries for synthetic files. The live research-run gate remains
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
