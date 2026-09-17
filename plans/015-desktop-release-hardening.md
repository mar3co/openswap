# Plan 015: Harden desktop recovery and completion feedback

## Status

- Priority: P1; effort: M; risk: MED; dependencies: none.
- Planned at: `30bcb1e`, 2026-09-17, including the existing dirty UI changes.
- Status: DONE for recovery/notification scope. Profile detection remains blocked
  as recorded below. User authorized implementation, not a push or app restart.

## Review record

Implementation reviewed in `/private/tmp/openswap-desktop-release.6uwaaE`
against its seeded working-tree baseline. Independent safety review found no
additional introduced P1/P2 issues in auth/roster reconciliation. Main reran
the full suite: 2,730 passed, 3 skipped, 3 existing pytest warnings. The diff
whitespace check passed. Integration of reviewed hunks into the current
checkout was approved and completed; no commit, push, or app restart performed.
The final current-checkout suite passed with macOS UI access for concurrent
native-dialog tests: 2,735 passed, 3 skipped, 3 existing pytest warnings. An
earlier run caught transient tests from that separate in-progress refactor;
a sandboxed retry could not initialize WindowServer. Neither required changes
to that other task's source by this task.

## Why this matters

A directory-fsync failure can occur after auth replacement but before the
transaction records its write. The current exception then incorrectly says
credentials were unchanged. Successful switches also show a long modal that
should be a quiet notification, with a persistent inline fallback.

## Current state and conventions

- `src/openswap/codex/desktop.py`: `_atomic_text` calls `os.replace` before
  directory fsync. `switch` sets `wrote = target` only after `_atomic_text`
  returns; rollback uses content equality to preserve concurrent writers.
- `src/openswap/menubar.py`: `_drain_desktop_result` sets `_desktop_status`,
  records the manual-switch cooldown, shows a success `_alert`, then refreshes.
- `src/openswap/menubar_display.py`: `NotificationCopy` contains title,
  subtitle, body and `rumps_args()`. `_notify` currently calls
  `rumps.notification(*copy.rumps_args())` without failure isolation.
- Tests use `FakeApp` and temporary synthetic credential fixtures in
  `tests/test_codex_desktop.py`; callback tests extract MenuBarApp methods in
  `tests/test_desktop_menubar.py` to avoid importing AppKit. Extend these.
- User confirmed one profile change; Chat, Work, Codex tasks and A→B→A
  have NOT been confirmed. Selected credentials do not prove loaded profile.

## Scope

Only modify `src/openswap/codex/desktop.py`, `src/openswap/menubar.py`,
`src/openswap/menubar_display.py`, `tests/test_codex_desktop.py`,
`tests/test_desktop_menubar.py`, and relevant paragraphs in
`docs/testing.md`, `docs/menubar.md`, `docs/chatgpt-desktop-feasibility.md`.
The relevant completion/validation paragraphs in
`docs/chatgpt-desktop-testing.md` are also in scope.
Do not alter the panel branding, onboarding, version/config gates, consent
semantics, automatic rotation policy, or CLI result schema. Preserve all
existing dirty and staged edits. No live credentials, app restarts or switches.

## Git workflow and verification

Use a disposable `codex/desktop-release-hardening` git worktree based on HEAD,
seeded with the current working files so prior UI work is retained. Keep a
baseline of those seeded files. Implement with apply_patch. Do not push or
commit the user's branch. Return the delta for review before integration.

Drift check: `git diff --stat 30bcb1e..HEAD -- src/openswap/codex/desktop.py
src/openswap/menubar.py src/openswap/menubar_display.py`; also compare seeded
files against the current working tree before integration.

Tests: `env UV_CACHE_DIR=/tmp/openswap-auth-probe-uv-cache uv run pytest -q
tests/test_codex_desktop.py tests/test_desktop_menubar.py
tests/test_chatgpt_auto_menubar.py`. Expected: all pass.
Full gate: same command with only `pytest -q`. Expected: all pass.

## Steps and done criteria

1. Reproduce post-replace fsync failure using synthetic fixtures. Cover auth
   and roster replacement outcomes, plus failure before auth replacement.
   Track possible writes before durability failure; reconcile exact live
   contents before rollback or any claim that credentials were unchanged.
   Never overwrite concurrent auth/roster changes. Retain recovery journal
   and actionable guidance when safe rollback cannot be established.
   Verify focused desktop tests pass, including fault-injection regression.
2. Replace only the completed-success modal with a quiet notification:
   title `ChatGPT reopened`, body `Check the selected account in ChatGPT.`
   Keep an inline `ChatGPT reopened · Check the profile` status. Do not expose
   email or token data in notifications. Explicitly set sound=False. Isolate
   notification exceptions so refresh and completed-switch status survive.
   Preserve error dialogs, cooldown and pre-switch consent. Test notification,
   no success modal, notification failure, status and refresh.
3. Remove test-oriented phrasing from completion/error feedback; retain one
   honest experimental disclosure in switch consent while qualification is
   incomplete. Do not promote selected to verified. Update only related docs
   with the precise observed test scope and remaining release gates.
4. Run focused then full suite; review the whole delta, with no source changes
   outside scope. Report test counts and any skipped checks accurately.

## STOP conditions and maintenance

Stop if source drift overlaps the change, safe rollback needs a broader
transaction redesign, or a test fails twice without an understood cause.
No synthetic desktop verifier: profile detection is a separate research task
and may only claim verification with independent evidence from the running app.
Future notification backends must retain silent delivery and inline fallback.

## Profile-detection research result

Static inspection of ChatGPT 26.908.70816/build 9275 found
`accessInputs.readAccountInfo()` deriving identity from the app's existing
connection, exposed to internal renderer services. Official App Server docs
describe `account/read` but do not establish a supported way for OpenSwap to
attach to the desktop's private stdio connection. No runtime private IPC was
queried. The environment also refuses computer-use inspection of this app;
that restriction must not be bypassed through another UI or data channel.
Automatic running-profile checking is therefore BLOCKED on an accessible,
independent identity source, not established as impossible. Do not claim this
plan implements it or add a file-based substitute labeled verified.
