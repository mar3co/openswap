# Decision memo: Claude authentication for remote-triggered runs

**Status: DECIDED by the owner (2026-10-07).** The options and evidence below
are kept as they were written before the decision.

## Owner's decision

The owner chose **"Unmodified Claude Code with the owner's native login"**,
limited to **the owner's own paired Macs** and **tasks the owner starts
themselves**. In the owner's words, using their own login "is the whole point
of remote agents". This is the owner's product decision; it is not provider or
legal clearance, and the evidence section below still applies as recorded risk.

What the implementation does under this decision (plan 017, phase 1):

- Runs the unmodified `claude` binary the owner installed (`claude -p`,
  stream-json). OpenSwap pins its SHA-256 on first use
  (`openswap worker claude pin`) and refuses to launch if the file changes;
  there is no published digest to verify against, so the pin is
  trust-on-first-use. Auto-update is disabled for jobs; after an owner-side
  update the owner re-pins and re-runs the live check.
- Selects the account through its OpenSwap-managed Claude profile (plan 003
  session profiles: `CLAUDE_CONFIG_DIR=<backup>/sessions/<n>-<slug>`),
  signed in by the owner with `openswap worker claude prepare`, which runs
  the binary's own `claude auth login` into that profile (OpenSwap seeds no
  credential; Claude Code keeps the sign-in in the profile's own Keychain
  item). The owner's default Claude login (`~/.claude`, `~/.claude.json` and its Keychain item)
  is never changed; the live check records its metadata before and after
  and fails if it moved.
- The CLI alone signs in, stores and refreshes tokens in that profile. The worker never reads, uploads, proxies or logs a
  credential, and the job environment is an allowlist without API keys.
- Restricts each job to research tools (`Read`, `Grep`, `Glob`, `WebSearch`,
  `WebFetch`; `--restricted`, `dontAsk`, no MCP, no slash commands, no
  session persistence) inside a Seatbelt profile that only allows writes to
  the approved folder, the profile, the run's temporary folder and this
  user's cache folders, and hides the owner's default login, the other
  accounts and OpenSwap's own state.
- Runs live only after `openswap worker live-check --provider claude` passes
  on that Mac and the owner opts in; the opt-in is separate from Codex's and
  bound to the pinned binary.

## Decision needed

Which authentication path, if any, may OpenSwap support when a remote request
starts a Claude Code run on the owner's Mac? The selected path must preserve
the user's direct account relationship and billing, keep credentials on that
Mac, and be permitted for this exact OpenSwap/OpenTag flow.

## Current evidence

Anthropic's current [Claude Code legal and compliance guidance](https://code.claude.com/docs/en/legal-and-compliance)
says that preinstalling or running the unmodified binary in a product is
subject to its Commercial Terms and conditions. It says the user must
authenticate with their own credentials and be billed directly. It also says
OAuth is intended for ordinary use of Claude Code and native Anthropic
applications; developers building products that interact with Claude's
capabilities should use API-key or supported cloud-provider auth; third-party
developers may not offer Claude.ai login or route Free, Pro, or Max requests
for users; and the unmodified binary's native sign-in remains available to
end users ([same official guidance](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)).
The [Agent SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart)
similarly directs product developers to API authentication unless prior
approval has been obtained. These statements establish the technical and
published policy boundaries, but do not determine whether a remote-triggered
run against the owner's locally signed-in binary is covered. No legal or
provider clearance has been obtained for that specific flow.

OpenSwap's existing Claude session profiles are managed credential copies,
and profile isolation alone does not resolve product permission. The worker
must not read, copy, seed, proxy or upload Claude credentials. Any permitted
native-login route would need the provider binary to own login, storage,
refresh, and billing.

The phase-one evidence is tracked in [017 progress](../../017-progress.md).
Its synthetic Codex sandbox probe does not establish anything about Claude's
permitted authentication path. No provider slot or exclusive live-auth
ownership has been authorized for credentialed work.

## Options

| Option | Consequence | Status |
| --- | --- | --- |
| Defer Claude; ship Codex only | Avoids making an unsupported assumption. Revisit only after provider clarification or an explicitly approved API path. | **Recommended interim position** |
| Unmodified Claude Code with the owner's native login | Keeps sign-in and refresh inside the provider binary and the user's local profile. Remote-triggered orchestration may still be treated as routing subscription requests; the docs do not settle this product-specific distinction. Requires written confirmation for this exact design before implementation. | Pending provider and owner clearance |
| Anthropic API key or supported cloud-provider credentials | Uses the documented programmatic integration path, with billing under the selected key/provider agreement. Requires a separate product/billing choice and owner-supplied authorized credentials; no silent conversion from subscription auth. | Possible later expansion; not selected |
| Exclude Claude from Remote Agent Host | Explicitly disables Claude dispatch, regardless of a local Claude install. | Available owner decision |

## Recommendation and gate (before the decision)

Keep Claude disabled and implement Codex only. Before adding Claude code, record
one of: (1) provider confirmation that the exact user-local, unmodified-binary,
remote-trigger flow is permitted, plus the owner-selected auth and account
ownership behavior; (2) an API/cloud-provider path with explicit billing and
credential ownership; or (3) a decision to exclude Claude. This memo's
recommendation is not the owner's answer. Until an answer is recorded, no
Claude auth files or Keychain entries may be inspected, copied, modified, or
used by a spike.

The feasibility evidence and provider documentation are not legal advice and
do not constitute permission. Documentation checked 2026-09-28.
