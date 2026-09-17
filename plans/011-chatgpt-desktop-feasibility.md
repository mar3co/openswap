# Plan 011: ChatGPT desktop switching feasibility

Status: IN PROGRESS. Static and isolated-backend research complete;
real desktop validation pending.
Baseline: PR #36, merge commit `8d33808`. Research date: 2026-09-17 UTC.

## Scope and decision

Reassess the earlier desktop exclusion. The installed unified ChatGPT app has
account-change handlers and a Codex app-server authentication path. A controlled
full-quit-and-relaunch switch is a plausible candidate, not supported behavior
yet. Do not add automatic desktop rotation or advertise desktop support based
on the synthetic backend experiment alone.

Read [the evidence and test matrix](../docs/chatgpt-desktop-feasibility.md)
before further work. The research PR changes no production switching behavior.

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

## Implementation only after that gate

Write a separate implementation plan for an explicit desktop switch action:
verified storage/lifecycle preflight, graceful bounded quit, refreshed outgoing
backup, selected-slot validation, concurrency checks, relaunch, authoritative
identity verification, and rollback. Include custom homes, file/keyring modes,
shared CLI sessions, failure recovery, and active-work refusal tests.

Keep automatic rotation separate until its task-boundary and quota semantics
are established. Existing Codex TUI detection is informational and cannot be
reused as a desktop shutdown guard.
