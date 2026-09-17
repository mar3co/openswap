# Plan 012: Unified menu-bar account tabs

Status: IMPLEMENTED locally. Automated and synthetic native UI validation
passed; real desktop account switching remains an explicit operator test.

User-approved scope: present Claude and ChatGPT as peer tabs, with one shared
ChatGPT/Codex account list and no CLI/desktop target selector.

## Implementation plan

1. Use native tab controls with clear selection and at least 40-point hit
   targets. Remember the provider within the running process across refresh,
   popover close, and Settings navigation. Tabs never mutate credentials.
2. Filter account cards and running/hold messages by provider. Clearly label
   Codex usage, shared-file selection, experimental restart behavior, and
   non-selectable CLI-only API keys. Keep existing numeric typography.
3. Route ChatGPT card clicks through the existing guarded desktop transaction.
   Include explicit consent to persistently pause Codex rotation when needed;
   do not change Claude rotation. Retain all restart/idle/recovery safeguards.
4. Surface busy, failure, and verification-needed states. Do not equate a
   successful file transaction with an authenticated desktop session.
5. Keep Claude Rotate/Best controls on Claude, label overflow provider actions,
   and remove the duplicate desktop submenu except when the panel is absent.
6. Verify helper behavior, actual callback wiring, cancellation, mid-dialog
   races, native layout, and the regression suite. Restart only OpenSwap for
   testing; do not switch credentials or restart ChatGPT during development.

Delegation: a GPT-5.6 Luna agent implements the panel/display layer and tests;
the primary agent handles switch integration, review, documentation, and QA.

Non-goals: automatic desktop rotation, ChatGPT message-limit measurement,
changing widget/CLI switching semantics, or claiming real A → B → A validation.

Empty-state follow-up: both tabs include provider-specific sign-in guidance
and an Add current login action using the existing capture flow. Initial
loading, failed reads with retry, and unavailable provider storage are distinct
from a verified empty roster. Empty tabs hide irrelevant switching controls;
snapshot transitions refresh the panel through the existing mouse-up-safe queue.
Validation: 2,648 tests passed, 3 skipped; synthetic ready/loading/error panels
rendered in light and dark appearance. The editable menu bar was reloaded;
no real accounts were added or switched during testing.

## Validation

- Full regression suite: 2,636 passed, 3 skipped, 3 pre-existing deprecation warnings.
- Native AppKit panels rendered successfully in light/dark appearance using
  synthetic accounts and usage bars; no live account files were used.
- Navigation checks confirmed tab changes never invoke switching and the
  selected tab survives close and Settings navigation.
- The existing editable menu-bar installation was reloaded to use these
  changes. ChatGPT was not quit, relaunched, or switched during development.
