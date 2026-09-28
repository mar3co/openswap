# Stable Codex CLI candidate: 0.157.1

Checked 2026-09-27. This is a credential-free comparison of the latest stable public ARM macOS release with the phase-1 synthetic filesystem probe. It does not inspect the installed app's configuration, credentials, or Keychain and does not start `codex exec` or a provider job. Plan 017 remains canonical; this report is evidence, not compatibility approval.

## Release identity and reproducibility

The official [OpenAI Codex GitHub release `rust-v0.157.1`](https://github.com/openai/codex/releases/tag/rust-v0.157.1) identifies version 0.157.1 as the latest stable release, published 2026-09-26, from commit `3665039`. Its [expanded release assets](https://github.com/openai/codex/releases/expanded_assets/rust-v0.157.1) list the ARM macOS archive as 90.3 MB with SHA-256 `3c45b162b7a76f51325015b1d0a8112c73219b7a9b59cd5762c37c9ba55894fa`.

The archive was fetched from `https://github.com/openai/codex/releases/download/rust-v0.157.1/codex-aarch64-apple-darwin.tar.gz` to `/private/tmp/openswap-codex-0.157.1.tar.gz`. `shasum -a 256` returned exactly the published digest above. Its only archive member was `codex-aarch64-apple-darwin`; it was extracted to `/private/tmp/openswap-codex-stable-0.157.1/`, not installed or copied over the app/global CLI. With `HOME` and `CODEX_HOME` both set to the empty disposable `/private/tmp/openswap-codex-stable-home`, the binary printed:

```text
codex-cli 0.157.1
```

The default shell network sandbox could not resolve `github.com`; the download then succeeded with a narrowly escalated public-asset fetch. This is a test-environment network limitation, not a release or CLI failure.

## Help and profile compatibility (observed locally)

On this stable binary, `exec --help` lists `--sandbox read-only|workspace-write|danger-full-access`, `--ignore-user-config`, `--ignore-rules`, `--profile`, `--add-dir`, and `--json`. `sandbox --help` accepts `--permission-profile`, `--cd`, and `--log-denials`. Thus the named permission-profile interface used in the prior pre-release probe exists on this stable candidate as well. Help output alone does not establish how `codex exec` applies that profile to model/tool subprocesses.

The disposable profile was identical to the one in [the inheritance spike](./spike-inheritance.md):

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

Synthetic files only were created beneath `/private/tmp/openswap-codex-stable-boundary/`: `workspace/inside.txt` contained `workspace-sentinel`, and `outside/outside.txt` contained `outside-sentinel`. The disposable `CODEX_HOME` contained a fake `auth.json` and this test profile's `config.toml`; neither contained real configuration or credentials. With `HOME` and `CODEX_HOME` set to that disposable directory, the tested command was:

```sh
codex sandbox --permission-profile research-test --log-denials \
  --cd /private/tmp/openswap-codex-stable-boundary/workspace \
  /bin/sh -c 'printf "workspace read: "; cat inside.txt; if printf "workspace-write-ok\n" > write-test-2.txt; then echo "workspace write: allowed"; fi; if cat ../outside/outside.txt >/dev/null; then echo "outside read: allowed"; else echo "outside read: denied"; fi; if printf "outside-write\n" > ../outside/write-test-2.txt; then echo "outside write: allowed"; else echo "outside write: denied"; fi; if cat "$CODEX_HOME/auth.json" >/dev/null; then echo "synthetic auth read: allowed"; else echo "synthetic auth read: denied"; fi; if cat "$CODEX_HOME/config.toml" >/dev/null; then echo "synthetic config read: allowed"; else echo "synthetic config read: denied"; fi'
```

Observed stdout/stderr summary (the wrapper returned exit 0 because each denied operation was handled by the shell's `if`):

```text
workspace read: workspace-sentinel
workspace write: allowed
cat: ../outside/outside.txt: Operation not permitted
outside read: denied
outside write: denied
/bin/sh: ../outside/write-test-2.txt: Operation not permitted
cat: /private/tmp/openswap-codex-stable-home/auth.json: Operation not permitted
synthetic auth read: denied
cat: /private/tmp/openswap-codex-stable-home/config.toml: Operation not permitted
synthetic config read: denied
```

The denial log separately recorded `file-read-data` for the outside sentinel and both fake `CODEX_HOME` files, plus `file-write-create` for the outside write. The initial attempt without narrow escalation could not apply Seatbelt (`sandbox_apply: Operation not permitted`); the successful rerun executed the CLI's macOS sandbox against only these synthetic files. This is direct wrapper/profile evidence on the stable binary, not evidence that `codex exec` inherits it correctly.

## Gate interpretation

This removes one narrow uncertainty: the tested named-profile syntax and wrapper-level workspace/outside/auth-config boundary are not unique to the installed `0.158.0-alpha.2.1` prerelease; they also work on the hash-verified stable `0.157.1` public release. The stable release is a reproducible candidate for further compatibility tests, not an approved or pinned production target.

The phase gate remains open. No authenticated `codex exec` was run; no model/tool adversarial read/write was tested; no evidence shows model/tool code cannot reach inherited MCP, hooks, plugins, skills, browser/apps, native search, or environment secrets. Provider-managed auth/token/session writes, refresh behavior and concurrency remain unknown. The report does not establish account selection or stable identity. See [the provisional adapter contract](./adapter-contract.md) and [the inheritance inventory](./spike-inheritance.md) for the remaining gates and documented-versus-tested distinction.
