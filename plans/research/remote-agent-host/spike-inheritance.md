# Phase 1 spike: Codex configuration and permission inheritance

Checked 2026-09-27. This report distinguishes official documentation, local help, a credential-free local sandbox probe, and behavior still unknown. It does not inspect the user's real Codex configuration, credentials, Keychain, or run a provider job. Plan 017 is canonical; this document does not approve a phase-2 implementation.

## Result

**The filesystem boundary is promising but not phase-gate proof.** On the installed pre-release CLI, a named macOS Seatbelt permission profile allowed reads and writes under one synthetic workspace while denying reads of a separate synthetic `CODEX_HOME` (including fake `auth.json` and `config.toml`) and writes outside the workspace. This was a direct `codex sandbox` probe, not an authenticated `codex exec` run or a model/tool integration test.

Codex has several independent inheritance surfaces. A clean `CODEX_HOME` removes user-home config/MCP/plugin state from that Codex home, but project/system configuration, hooks, skills, model search, and system-level state need their own controls. Workspace sandboxing does not restrict MCP, web search, apps, browser, or computer-use surfaces. No evidence currently proves that the chosen headless run has only the intended research tools or that its model cannot read real auth/config secrets.

## Evidence collected

### Installed binary and CLI help (observed)

Binary: `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex`.

With `CODEX_HOME=/private/tmp/openswap-codex-inheritance-home` (a disposable home), `codex --version` printed:

```text
codex-cli 0.158.0-alpha.2.1
```

This is a pre-release build and is not an approved supported target. All CLI observations below are version-specific until repeated against the pinned supported release.

`codex exec --help` lists `--sandbox read-only|workspace-write|danger-full-access`, `-C`, `--add-dir`, `--skip-git-repo-check`, `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, `--json`, `--output-schema`, `--search`, `--dangerously-bypass-approvals-and-sandbox`, and `--dangerously-bypass-hook-trust`. The help text says `--ignore-user-config` skips `$CODEX_HOME/config.toml` but **auth still uses `CODEX_HOME`**. `--add-dir` explicitly adds further writable directories. `--skip-git-repo-check` permits operation outside a Git repository; it does not say it disables project config discovery.

`codex sandbox --help` exposes `--permission-profile`, `--cd`, and `--log-denials`. `codex app-server generate-json-schema --help` offers experimental protocol schema generation. No app-server schema or model process was launched for this inheritance spike.

### Official documentation (documented, not local tests)

- [Advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced) says Codex loads trusted project `.codex/config.toml` layers from the project root down to the working directory. Project hooks load from trusted project layers; user-level hooks remain separate. Project root discovery also finds `AGENTS.md` and `.codex` configuration by walking upward. Setting `project_root_markers = []` is documented to stop parent-directory searching, but was not tested here.
- [MCP documentation](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) says MCP configuration is shared by the ChatGPT desktop app, CLI, and IDE, normally lives in `CODEX_HOME/config.toml`, and may also be project-scoped for trusted projects. Server definitions can include executable commands, environment variables, HTTP headers, OAuth, and enabled-tool lists.
- [Configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) documents command environment inheritance as `all | core | none`; `all` is the default. Its `ignore_default_excludes` default keeps environment variable names containing `KEY`, `SECRET`, or `TOKEN` before filters run. Explicit `filters` can form an allowlist for command subprocesses, but the environment policy is not itself a filesystem boundary and this was not validated with `codex exec`.
- [Hooks documentation](https://learn.chatgpt.com/docs/config-file/config-advanced#hooks) documents command and MCP-tool lifecycle hooks in user and project config layers. Hooks can run executable commands; instructions to omit hooks are not enforcement. The CLI exposes a dangerous hook-trust bypass, not a verified all-hooks-off switch.
- [Skills documentation](https://learn.chatgpt.com/docs/build-skills) says Codex discovers skills from repository, user, admin, and system locations; repository discovery scans `.agents/skills` up the current directory to the repository root. Skills include instructions and may bundle scripts/references. The config reference has `skills.config` enablement entries, but this spike did not establish an invocation flag that disables every inherited skill source.
- [Plugin architecture](https://developers.openai.com/plugins/concepts/plugins) documents plugins that can bundle skills, MCP servers, and lifecycle hooks. A fresh disposable Codex home reported `No marketplace plugins found.`; this says nothing about plugin/app state loaded by a real home or other runtime surfaces.
- [Permission profiles](https://learn.chatgpt.com/docs/permissions) describes local command filesystem boundaries separately from MCP, connectors, browser, computer use, web search, approved escalations, and Codex service traffic. Network-domain rules constrain command traffic only when the network proxy is active; they do not restrict web search, apps, or MCP. The page marks permission profiles beta and subject to change.
- [Authentication](https://learn.chatgpt.com/docs/auth) documents multiple credential-storage methods, including file-backed and OS-backed storage, and provider-managed refresh. It does not establish which storage mode or writes a given enrolled identity requires. `CODEX_HOME` must remain provider-managed state for the selected local identity; do not give it to model/tool commands as a writable root.
- The sandbox profile's `:workspace` basis includes temp-directory access by default. The [permission reference](https://learn.chatgpt.com/docs/permissions) explicitly documents denying `:tmpdir` and `:slash_tmp` when a task requires a narrower root. The older `workspace-write` configuration also has additive `writable_roots` and temp-exclusion options in the [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

### Disposable macOS Seatbelt probe (tested locally)

All files were synthetic and under `/private/tmp`. No model, `codex exec`, network, user configuration, provider credential, or Keychain entry was accessed. The outer execution sandbox blocked Seatbelt setup on the first attempt (`sandbox-exec: sandbox_apply: Operation not permitted`); a narrowly escalated rerun let the CLI's Seatbelt wrapper execute. Treat this as a limitation of the outer test harness plus a local CLI sandbox test, not as a provider capability failure.

Disposable `CODEX_HOME=/private/tmp/openswap-codex-inheritance-home/config.toml`:

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

The test root was `/private/tmp/openswap-codex-boundary/workspace`, with `inside.txt` containing `workspace-sentinel`. A sibling `outside/outside.txt` contained `outside-sentinel`. The disposable `CODEX_HOME` also held the profile above and a fake `auth.json` containing `synthetic-auth-sentinel`.

Command for workspace/outside read and write probes:

```sh
CODEX_HOME=/private/tmp/openswap-codex-inheritance-home \
  /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex \
  sandbox --permission-profile research-test --log-denials \
  --cd /private/tmp/openswap-codex-boundary/workspace \
  /bin/sh -c 'cat /private/tmp/openswap-codex-boundary/workspace/inside.txt; cat /private/tmp/openswap-codex-boundary/outside/outside.txt; printf inside-write > /private/tmp/openswap-codex-boundary/workspace/write-test.txt; printf outside-write > /private/tmp/openswap-codex-boundary/outside/write-test.txt'
```

Observed stdout:

```text
workspace-sentinel
cat: /private/tmp/openswap-codex-boundary/outside/outside.txt: Operation not permitted
/bin/sh: /private/tmp/openswap-codex-boundary/outside/write-test.txt: Operation not permitted

=== Sandbox denials ===
None found.
```

Exit status was `1`. The in-workspace write completed before the outside write failed. The `None found` text is the wrapper's denial-report output; the shell's explicit `Operation not permitted` results establish the tested read/write behavior.

Separate exact-`CODEX_HOME` read probe:

```sh
CODEX_HOME=/private/tmp/openswap-codex-inheritance-home \
  /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex \
  sandbox --permission-profile research-test --log-denials \
  --cd /private/tmp/openswap-codex-boundary/workspace \
  /bin/sh -c 'cat "$CODEX_HOME/auth.json"; cat "$CODEX_HOME/config.toml"'
```

Both reads returned `Operation not permitted`, exit status `1`; the denial log included `file-read-data` entries for the synthetic auth and config files. This proves a local Seatbelt property for the CLI wrapper with the above profile on this prerelease build. It does **not** prove `codex exec` selects and maintains that profile, nor that the model cannot reach inherited MCP/web/app tools.

A fresh-home `codex mcp list` printed `No MCP servers configured yet.` A fresh-home `codex plugin list` printed `No marketplace plugins found.` These are observations about the empty disposable home only.

### Cross-message findings from `spike-codex`

The Codex lifecycle spike reports the same binary/version and independently confirms its help-only probes used temporary `HOME`/`CODEX_HOME`; those probes do not establish auth selection. It reports that no `codex exec` job or real auth/config/Keychain access has occurred, and that its harness currently refuses authenticated runs until the owner selects a profile and establishes exclusivity. Actual research/search, `codex exec` writable-root behavior, auth refresh writes/races, and process cancellation/recovery remain unverified in that spike as of this report. These are collaborator-reported findings, not independent observations by this inheritance spike.

## Inheritance inventory and enforceability

| Surface | What can be inherited | What this spike can claim |
|---|---|---|
| Config and policy | User `CODEX_HOME/config.toml`, selected profile files, system/admin config and requirements, trusted project `.codex/config.toml` layers | Help supports `--ignore-user-config`, `--profile`, and strict parsing. We inspected no real layers. Project/system inheritance remains an explicit audit and test requirement. |
| MCP | User/project server definitions, env/header/OAuth state, plugin-bundled MCP servers | A clean disposable home had no configured MCPs. No test proved absence of user/system/project/plugin MCPs during an authenticated run. |
| Hooks | User config or hooks files; project hooks when project trust allows; plugin hook bundles | Can execute command or MCP hooks. No real hook state inspected, and no all-layers-off behavior verified. The dangerous trust bypass is not an acceptable restriction. |
| Plugins and skills | Plugin marketplaces/caches and plugin-bundled tools; skills from repository, user, admin, and system roots | Empty-home list commands were empty. Skills are workflow instructions, not OS access controls. No all-source disable proof. |
| Environment | Parent process variables; Codex's child-process inheritance/filter/set policy; MCP-specific env forwarding | Docs allow filtering and allowlisting child process variables; documented default is broad (`all`). No model-facing env dump or authenticated child-process test was run. Treat any auth, token, proxy, or transport secret in the child environment as exposed until tested. |
| Filesystem | Research workspace plus implicit temp roots under broad workspace profile; any explicit `--add-dir`/configured writable root | Custom profile wrapper denied synthetic outside and CODEX_HOME reads/writes as above. `--add-dir` must never be derived from a remote field. This remains unverified through `codex exec`. |
| Network and research tools | Shell network, native search, MCP HTTP/stdio, browser, app/plugin tools | Permission profiles do not cover all these surfaces. Native `--search` exposes live web search; sandbox command network rules do not govern it. Actual effective tool list is unknown. |
| Provider auth/state | CLI-owned credentials, token refresh, session/runtime state under its selected profile and possible OS keychain | Provider process may need to mutate its own auth/runtime state. That provider-owned state is outside the model/tool writable-root contract and is not evidence to grant model shell writes to `CODEX_HOME`. Storage mode, paths, refresh behavior and races remain untested here. |

## Gate findings and remaining unknowns

1. **Version gate remains open.** Pin a supported stable CLI version and repeat the help/schema and boundary checks against that binary. The present `0.158.0-alpha.2.1` is prerelease and cannot substantiate a shipped compatibility claim.
2. **Model/tool secret isolation is not proven end-to-end.** The Seatbelt wrapper denies fake `CODEX_HOME` files, but an authenticated `codex exec` with an adversarial read probe has not run. No claim is made that the model cannot read real auth or config files.
3. **Inherited connectors are not proven absent.** A clean profile yields empty MCP/plugin inventory, but the effective project, admin/system, plugin and desktop tool sets were not observed during an agent session. Require an allowlisted effective tool inventory and tests before phase exit.
4. **Project discovery may conflict with the current research directory.** Official docs say a working directory inside a trusted Git project inherits parent `.codex` config/hooks and project guidance. `--skip-git-repo-check` only addresses the Git guard according to its help. Whether phase 1's chosen research directory is inside such a project, and which resources it discovers, remains unverified. Plan 017 remains canonical; this report does not change its cwd rule.
5. **Provider-managed state is a distinct root.** Codex may need provider-owned writes for credential refresh or CLI session/runtime bookkeeping under its selected local profile. The model/tool sandbox must keep that path unreadable and unwritable. Exact refresh writes and races are the separate `spike-codex` evidence; no credential copying or snapshots are justified here.
6. **Tool restrictions are distinct from shell sandboxing.** `--search`, MCP, plugins, browser/apps and other built-in surfaces have independent capability controls. If unsupported surfaces cannot be positively disabled for this research profile, the plan's research restriction gate fails; prompt instructions and `workspace-write` alone do not satisfy it.

## Commands observed

The version and help probes ran with an empty temporary `CODEX_HOME`, for example:

```sh
CODEX_HOME=/private/tmp/openswap-codex-inheritance-home \
  /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex --version

CODEX_HOME=/private/tmp/openswap-codex-inheritance-home \
  /Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex exec --help
```

The helper command `node /Users/yohan/.codex/skills/.system/openai-docs/scripts/fetch-codex-manual.mjs` failed because DNS resolution for `developers.openai.com` was unavailable to shell `curl`; official pages were fetched with the approved web documentation capability instead.
