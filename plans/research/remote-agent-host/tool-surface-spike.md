# Codex tool-surface probe: request captured, CLI completion unresolved

Checked 2026-09-28 with hash-verified stable Codex CLI 0.157.1. The first credential-free attempt did not capture a provider request; a bounded follow-up captured five advertised top-level functions but the CLI exited 1 without a last-message file. This is not a successful provider compatibility result, research run, or approval to change Plan 017.

## Preflight and isolation

Existence-only checks found `/etc/codex/config.toml`, `/etc/codex/requirements.toml`, `/etc/codex/managed_config.toml`, `/etc/codex/hooks.json`, and `/Library/Managed Preferences/com.openai.codex.plist` absent. The `com.openai.codex` preference domain existed; `defaults read-type` reported the documented `config_toml_base64` and `requirements_toml_base64` keys absent. No preference or configuration contents were read. No credentials, Keychain, real Codex home, or user environment values were accessed.

The CLI was configured with a fresh temporary `HOME` and `CODEX_HOME`; environment variables were supplied through `env -i` (PATH, HOME, CODEX_HOME, TMPDIR, LANG only). Its custom provider used `wire_api = "responses"`, `base_url = "http://127.0.0.1:<ephemeral-port>/v1"`, `requires_openai_auth = false`, `supports_websockets = false`, and `cli_auth_credentials_store = "file"`. User config and rules were ignored, project-root parent discovery was disabled with `project_root_markers = []`, and the empty temporary working directory had no project configuration. The invocation disabled shell, app, hook, plugin, multi-agent, browser, computer-use, code-mode, unified-exec, skill-search/dependency, remote-plugin, and workspace-dependency features; it also set `web_search = "disabled"`, `agents.enabled = false`, analytics off, update checks off, telemetry exporter none, `--no-daemon`, `--ephemeral`, and read-only shell sandbox mode. The mock listener bound only to loopback on an OS-assigned port and would return one canned final response. The mock extracted only top-level tool `type` and `name`; it did not retain request prompts, headers, or other fields.

These were explicit command-local settings, not evidence that the effective model request honored them. The [official custom-provider docs](https://learn.chatgpt.com/docs/config-file/config-advanced) document custom `base_url` and `requires_openai_auth`, which defaults to false. The [official sample configuration](https://learn.chatgpt.com/docs/config-file/config-sample) documents `cli_auth_credentials_store = "file"`. The docs say managed preferences override local config/CLI overrides, which is why the MDM payload-key existence check was a precondition. This run supplied no credential and configured no external model-provider URL. Outbound traffic was not independently traced, so this report does not claim a general network-denial proof.

## Observed result

In the first attempt, the mock server announced `LISTENING 127.0.0.1:53439`, then closed after its 28-second no-request timeout. It received no POST and did not create `tool-names.json`. The CLI did not finish within the bounded attempt and was stopped; shell exit was 130 from the explicit interrupt. The only filtered startup diagnostic was:

```text
WARNING: proceeding, even though we could not create PATH aliases: Refusing to create helper binaries under temporary dir "/private/tmp/openswap-tool-surface" (codex_home: AbsolutePathBuf("/private/tmp/openswap-tool-surface/codex-home"))
```

This warning is observed, but there is no evidence it caused the absence of a request. In that first attempt the provider request never arrived, so no tool names were measured. The later one-shot follow-up is recorded below.

## Follow-up combined attempt

On 2026-09-28 a single bounded follow-up used the hash-verified stable CLI at `/private/tmp/openswap-codex-stable-0.157.1/codex-aarch64-apple-darwin` (`codex-cli 0.157.1`). All run files were under `/private/tmp/openswap-tool-surface-attempt3`; the wrapper was `/private/tmp/openswap-tool-surface-attempt.py`. It created fresh empty `home`, `codex-home`, and `workspace` directories. It supplied no credentials, did not copy an existing home, and did not read a real Codex home or Keychain. The existing same-day managed-preference-key existence-only preflight above was used; no configuration contents were read.

The first try to bind the loopback server under the default sandbox was denied before Codex started. The combined retry used the explicitly authorized narrow elevated bind and created an `HTTPServer` on `127.0.0.1` with an OS-assigned ephemeral port. The server stayed alive until the CLI exited. It accepted the first POST, parsed the request in memory, persisted only top-level `tools[]` type/name pairs to `tool-types.json`, and returned a canned final response with no tool calls. It did not persist prompts, request bodies, headers, tokens, or other request fields. The ephemeral port was not retained in the result output; the URL below records its exact shape.

The environment was supplied explicitly through `env -i`:

```text
PATH=/usr/bin:/bin
HOME=/private/tmp/openswap-tool-surface-attempt3/home
CODEX_HOME=/private/tmp/openswap-tool-surface-attempt3/codex-home
TMPDIR=/private/tmp/openswap-tool-surface-attempt3
LANG=en_US.UTF-8
```

The CLI argument vector used the following executable and arguments. `PORT` denotes the OS-assigned loopback port. No fixture config file was created; user config and rules were ignored, and run-specific settings were passed as command-line overrides. System/managed policy effects were not measured by this invocation:

```text
/private/tmp/openswap-codex-stable-0.157.1/codex-aarch64-apple-darwin
--no-daemon --strict-config
--disable shell_tool --disable apps --disable hooks --disable plugins
--disable multi_agent --disable browser_use --disable browser_use_external
--disable browser_use_full_cdp_access --disable computer_use --disable code_mode_host
--disable code_mode --disable unified_exec --disable unified_exec_tty
--disable skill_search --disable skill_mcp_dependency_install --disable remote_plugin
--disable workspace_dependencies
exec --json --ephemeral --skip-git-repo-check --ignore-user-config --ignore-rules
--sandbox read-only --cd /private/tmp/openswap-tool-surface-attempt3/workspace
--output-last-message /private/tmp/openswap-tool-surface-attempt3/last-message.txt
--model mock-model
-c 'model_provider="mock"'
-c 'model_providers.mock.name="Loopback mock"'
-c 'model_providers.mock.base_url="http://127.0.0.1:PORT/v1"'
-c 'model_providers.mock.wire_api="responses"'
-c 'model_providers.mock.requires_openai_auth=false'
-c 'model_providers.mock.supports_websockets=false'
-c 'cli_auth_credentials_store="file"'
-c 'project_root_markers=[]'
-c 'web_search="disabled"'
-c 'analytics.enabled=false'
-c 'check_for_update_on_startup=false'
-c 'otel.exporter="none"'
-c 'agents.enabled=false'
Return exactly MOCK_FINAL_ONLY.
```

The mock received the request and captured five advertised top-level functions: `request_user_input`, `view_image`, `get_goal`, `create_goal`, and `update_goal`. The CLI then exited with status 1; `last-message.txt` was not created. The wrapper retained only whether stdout/stderr were nonempty, not their contents, so the cause of the nonzero exit is unknown. Its bounded cleanup finished for the CLI leader and mock server (`cleanup_uncertain=false`); it did not prove that detached descendants were absent. No retry was made as part of this fixture.

This is evidence that this no-auth loopback configuration reached the custom Responses endpoint and exposed those five top-level tool entries in that request. It does not establish successful CLI completion, provider compatibility beyond request delivery, or absence of internal/unadvertised handlers. `web_search` was explicitly disabled, so research capability, web sourcing, and citation behavior were not tested. No authenticated provider run, account isolation, auth refresh, cancellation semantics, or general outbound-network confinement was tested.

## Gate result

The initial mock route attempt did not reach the server; the single follow-up did reach it, but the CLI failed to complete. Do not infer that Codex 0.157.1 rejects `requires_openai_auth = false`, or that these five advertised entries are a complete inventory or prove a shell-less/internal-handler-free runtime. The no-auth custom-provider request path is observed; successful completion and the research tool surface remain unproven. There is no live provider fallback.

This result changes no plan requirement. The phase-1 tool-restriction gate remains open: no claim is made that arbitrary process execution is absent, that only web research remains available, or that local source/citation behavior is preserved. The exit-1 cause is unknown.
