# macOS cancellation boundary: process groups and bounded research

Checked 2026-09-28 against Apple documentation and Codex CLI 0.157.1. This is architecture evidence only: no product code, provider job, account, or credential was used. The stable CLI artifacts and filesystem probe are recorded in [spike-stable-cli.md](./spike-stable-cli.md).

## Finding

The current process-group plan cannot guarantee that the whole Codex descendant tree stopped. PR #59 review #4122008642 at `341c49d` identified a child that calls `setsid()` and closes standard streams. The lifecycle-spike owner reproduced that inert case: the parent probe returned while the detached helper continued, then the test cleaned up its known fake PID. This is collaborator-reported test evidence, not a provider-run test. It contradicts any claim that process-group cleanup plus drained pipes proves full-tree termination.

Apple documents `setsid()` as creating a new session and process group whose ID is the caller's PID. Therefore, signaling only the original process group does not include a descendant that successfully creates a separate group. A closed pipe also cannot serve as a liveness signal for such a process. Polling parent PIDs or a process-table snapshot does not close the fork/reparent race and is not a complete remedy.

## macOS mechanisms and limits

| Mechanism | Documented behavior | Assessment for this contract |
| --- | --- | --- |
| POSIX process group | A signal sent to a process group targets that group. `setsid()` creates a new session and process group. | Useful best-effort cancellation for cooperative descendants that remain in the group. Not containment; a detached descendant escapes. |
| `kqueue` `EVFILT_PROC` / `NOTE_FORK` | Apple documents per-PID process event monitoring, including fork, exec, and exit notifications. | Observation, not containment or a kill-all primitive. The documentation does not promise atomic recursive enrollment or close the interval between a fork event and registering a watch on the new child. Not sufficient evidence for a race-free guarantee. |
| Foundation `Process.interrupt()` / `terminate()` | Apple says these send SIGINT/SIGTERM to the receiver and “all of its subtasks.” Apple also says interruption/termination is not always possible because a task may ignore the signal. The docs do not define “subtasks” coverage for a descendant that detaches or is reparented. | A supported API worth a targeted synthetic test; not yet evidence that escaped descendants are stopped or that termination is guaranteed. Do not report success solely from the leader's termination callback. |
| `launchd` LaunchAgent | Apple documents launching and managing per-user agents, relaunch policies, and lifecycle signaling. | Appropriate for worker lifecycle/restart; no Apple guarantee found that a LaunchAgent contains or reaps every process its worker launches. Not a descendant-cancellation guarantee. |
| XPC helper/service | Apple's AppKit guidance recommends XPC services/extensions where the system should track an app/helper relationship and terminate helpers as the app quits. | A possible service-lifecycle design to investigate, not proof of containment for arbitrary or detached descendants of a Codex subprocess. No XPC design or test is proposed here. |

## Shell-less research candidate

Plan 017's bounded research adapter does not require arbitrary model-generated shell work. A narrower mode could use Codex native web search, fixed local source context, and the CLI's `--output-last-message` output file while removing model-facing local process execution. This is compatible with the plan only if research and output still meet the plan's actual source/citation and approved-workspace requirements; it is an alternative to evaluate, not a scope decision or a substitute already proven.

Evidence on stable `codex-cli 0.157.1`:

- Its local `codex features list` reports `shell_tool` as `stable true` in the disposable empty home. With CLI overrides `--disable shell_tool --disable apps --disable hooks --disable plugins --disable multi_agent`, it reports each as `stable false`. The tested invocation was `env HOME=/private/tmp/openswap-codex-stable-home CODEX_HOME=/private/tmp/openswap-codex-stable-home /private/tmp/openswap-codex-stable-0.157.1/codex-aarch64-apple-darwin --disable shell_tool --disable apps --disable hooks --disable plugins --disable multi_agent -c 'web_search="live"' features list`; no model job was launched. The displayed feature list does not report whether `web_search="live"` is effective.
- Official [Codex configuration basics](https://learn.chatgpt.com/docs/config-file/config-basic) describes `shell_tool` as “Enable the default `shell` tool” and labels it stable. The stable CLI's `--disable` help states that it is equivalent to `features.<name>=false`. This is evidence for a documented disable control, not an observed model-facing tool inventory.
- The same local feature listing still reported `browser_use`, `computer_use`, and `code_mode_host` enabled after those disables. This shows the flags used above are not a complete tool allowlist. A prompt to avoid those tools is not enforcement. Native web search has a separate documented `web_search` setting; CLI help also describes `--search` as making the Responses `web_search` tool available. Do not enable it without deciding the allowed research surface and verifying the effective tools.
- The official [custom provider configuration](https://learn.chatgpt.com/docs/config-file/config-advanced) documents `model_providers.<id>.base_url` and says a custom provider's `requires_openai_auth` defaults to false. A bounded no-auth loopback mock-provider test was attempted; it captured five top-level advertised function names but the CLI exited 1 without a last-message file. See the [tool-surface spike result](./tool-surface-spike.md) for the exact sanitized invocation and limits. This does not establish a complete tool inventory or successful execution. The docs mark standalone web search as under development/off by default for custom providers, and this probe explicitly disabled `web_search`, so it does not test real hosted search or research behavior.

No stable CLI help/config evidence found here establishes a general per-run allowlist that admits only native web search while denying every local execution, plugin, MCP, browser, and app surface. The credential-free loopback capture linked above measured five advertised top-level functions in a request, but did not prove the complete tool inventory, behavior of the selected live account, or quality of actual web research.

## Phase-1 consequence

Until a supported mechanism is proven to stop detached descendants—or the owner explicitly accepts a verified shell-less research mode with the plan's source, citation, and output requirements intact—`interrupt` can report a stop request but must not claim execution stopped from process-group signals, leader exit, closed streams, `kqueue` snapshots, or worker restart alone. When complete execution status is unknowable, preserve `interrupted`/unknown and never auto-relaunch. The existing phase-1 cancellation gate remains blocked; this memo does not redesign or silently narrow Plan 017.

## Primary references

- Apple [`setsid(2)`](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/setsid.2.html)
- Apple [`kevent(2)` / `EVFILT_PROC`](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/kevent.2.html)
- Apple Foundation [`Process.interrupt()`](https://developer.apple.com/documentation/foundation/process/interrupt%28%29) and [`Process.terminate()`](https://developer.apple.com/documentation/foundation/process/terminate%28%29)
- Apple [Managing ongoing background processes in your Mac](https://developer.apple.com/documentation/appkit/managing-ongoing-background-processes-in-your-mac)
- Apple [Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html)
