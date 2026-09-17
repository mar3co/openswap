# Plan 011: ChatGPT desktop switching feasibility

Status: IN PROGRESS. Static research and the experimental implementation are
complete; real desktop validation remains operator-owned and pending.
Baseline: PR #36, merge commit `8d33808`. Research date: 2026-09-17 UTC.

## Scope and decision

Reassess the earlier desktop exclusion. The installed unified ChatGPT app has
account-change handlers and a Codex app-server authentication path. A controlled
full-quit-and-relaunch switch is a plausible candidate, not supported behavior
yet. Do not add automatic desktop rotation or advertise verified desktop support based
on the synthetic backend experiment alone.

Read [the evidence and test matrix](../docs/chatgpt-desktop-feasibility.md)
before further work. The user subsequently authorized an experimental build
for manual validation; see [the testing guide](../docs/chatgpt-desktop-testing.md).
The initial research-only commit has now been extended with that explicit
action. Ordinary account clicks and automatic rotation do not restart the app.

## Completed research

- Inspect packaged application code without reading real credentials.
- Trace account notifications, identity invalidation, home selection, and
  backend/window lifetime.
- Compare the documented auth protocol with installed behavior.
- Add a disposable-home probe for file mutation versus backend restart and
  protocol login, using fabricated API keys and no model/quota calls.
- Identify gaps in current OpenSwap process detection, identity checks, and
  shutdown/recovery behavior before it could control a desktop session.

## Next gate: user-controlled two-account validation

This requires explicit authorization to affect a desktop session and two test
logins. Do not operate on the user's active app as part of ordinary research.

1. Verify effective credential store/home and backend ownership. Use a disposable
   profile, confirming both Electron userData and Codex home isolation.
2. Complete the A → B → A matrix in the evidence document, checking new Chat,
   Work, and Codex tasks, account-scoped state, refresh, and relaunch behavior.
3. Observe full app quit, backend exit, outgoing token preservation, and target
   identity after restart. Window closure alone is insufficient.
4. Record redacted outcomes and app/CLI versions. Do not commit credentials,
   cookies, personal history, or raw authenticated protocol logs.

Stop if isolation or backend ownership cannot be established, work remains
active, auth changes concurrently, identity cannot be verified, or rollback
cannot restore a known state. Do not patch private app bundles, scrape browser
cookies, or treat an unrelated app-server as the live desktop backend.

## Experimental implementation and promotion gate

The experimental action performs file-store/default-home preflight, graceful
bounded quit, refreshed outgoing backup, selected-slot validation, concurrency
checks, relaunch, and guarded rollback. CLI flags and the native menu dialog
require restart and idle acknowledgement. Other Codex clients are refused;
automatic switching must remain disabled. The result is explicitly
`awaiting_verification`: desktop UI identity and remote work activity cannot be
authoritatively checked by this implementation. The operator must verify them.

Custom homes and keyring/auto/ephemeral stores are unsupported in this first
test build. Passing the real two-account matrix above is still required before
promoting the feature out of experimental status.

Keep automatic rotation separate until its task-boundary and quota semantics
are established. Existing Codex TUI detection is informational and cannot be
reused as a desktop shutdown guard.
