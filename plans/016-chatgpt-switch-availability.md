# Plan 016: App-aware ChatGPT switching

## Status

- Priority: P1; effort: M; risk: MED; dependencies: 014, 015, provider-aware OAuth (PR #49).
- Planned at: `862ae85` (updated `main` after PR #49).
- Status: DONE. Implemented on `feat/chatgpt-switch-availability` from
  `862ae85`.

## Why this matters

ChatGPT account rows currently always offer a credential-changing desktop
restart, and ChatGPT suggestions start from the child Auto-switch preference
alone. The extra must not mutate ChatGPT credentials unless the operator has
explicitly enabled switching, the installed app is a typed capable target, and
a fresh process observation is in hand. Installation, process state, selected
credentials, and relaunch still do not verify the profile ChatGPT actually
loaded.

## Current state and conventions

- `MenuBarSettings` in `menubar_display.py` persists `chatgpt_auto_enabled`
  (suggestions only). There is no parent switching preference.
- ChatGPT card clicks go through `_on_panel_account_click` →
  `_make_desktop_switch` → `DesktopSwitcher(self.codex).switch(...)`, which
  constructs a fresh `DesktopApp` and always uses “Restart ChatGPT” copy.
- `DesktopApp` already validates the allowlisted bundle, signature, and
  process snapshot. `DesktopSwitcher.__init__` already accepts a shared
  `app` instance. Do not change the CLI desktop command, credential schema,
  OAuth onboarding, account numbering, recovery schema, threshold policy,
  app allowlist, signature policy, or Codex process guard unless a failing
  acceptance test proves it necessary.
- Tests extract `MenuBarApp` methods with `tests/menubar_harness.py` and
  never import AppKit from `tests/test_menubar.py`. Extend those suites.
- User-visible copy must never expose paths, command lines, tokens, stderr,
  signing details, or arbitrary exceptions.

## Scope

Modify `src/openswap/menubar_display.py`, `src/openswap/menubar.py`,
`src/openswap/menubar_panel.py`, `src/openswap/codex/desktop_app.py`,
`src/openswap/codex/desktop.py` (shared `DesktopApp` wiring only),
`tests/test_menubar.py`, `tests/test_desktop_menubar.py`,
`tests/test_chatgpt_auto_menubar.py`, `tests/test_codex_desktop_app.py`,
`tests/test_menubar_tabs.py`, `docs/menubar.md`,
`docs/chatgpt-desktop-feasibility.md`, this plan, and `plans/README.md`.

Do not switch a real account, modify real credentials, or quit/launch the
user’s real ChatGPT app.

## Steps and done criteria

### 1. Parent preference and migration

- Add `chatgpt_switching_enabled: bool = False` to `MenuBarSettings`.
- Settings → Automation shows **Enable ChatGPT switching** above **Suggest
  ChatGPT account switches**. Disable the child toggle while the parent is
  off. Controller checks are authoritative; a disabled NSSwitch is not enough.
- If an old file has `chatgpt_auto_enabled=true` and lacks the new key,
  migrate the parent to true. Materialize the key only on the next successful
  save. A present `false` always wins, including when suggestions are on.
- Persist first. UI and monitor state may change only after save succeeds.
  On failure, restore the prior in-memory preference and show sanitized
  feedback. Do not change Claude auto-switch or live Codex rotation settings.

### 2. Typed asynchronous capability detection

- Model `checking`, `unsupported`, `missing`, `invalid`, `stopped`, and
  `running` with stable reason codes on `DesktopAppError` / a frozen
  observation type. Apply the same Codex-home, backend, credential-store, and
  managed-configuration policy preflight as the transaction. Never classify
  failures by matching exception text.
- Probe lazily on a worker when the ChatGPT view becomes active. Never run
  codesign or process inspection on the AppKit thread. Generation guards
  drop stale worker results. A `running`/`stopped` observation and a retryable
  operational failure expire after five seconds of monotonic time; stable
  terminal installation and policy states do not use that TTL.
- Serialize all access to the shared `DesktopApp` with one controller-owned
  lock. Pause or invalidate probes during desktop transactions.
- Pass that same `DesktopApp` instance into `DesktopSwitcher` so validation
  caching is shared. Transaction revalidation inside `switch()` stays
  authoritative.

### 3. Gate manual switching

- Keep ChatGPT accounts and account-management controls visible when
  switching is disabled. Disable only credential-changing account-row
  activation.
- Reject activation through mouse, keyboard, accessibility, stale callbacks,
  and direct callback invocation. Controller checks are authoritative.
- Parent-off offers **Enable in Settings**.
- Fresh stopped uses **Switch and open ChatGPT**; fresh running uses
  **Restart ChatGPT**.
- Recheck the persisted preference and capability generation before showing
  consent, and recheck both generation and freshness after consent. Recheck
  persisted preference and generation again at worker entry.
- Preserve `DesktopSwitcher` preflight, locking, credential-generation
  checks, recovery journal, and process-race handling.

### 4. Reconcile ChatGPT suggestions

- Start or restart the dry-run suggestion monitor only when the effective
  parent preference is on (`chatgpt_switching_enabled` and
  `chatgpt_auto_enabled`).
- Turning the parent off must stop the monitor, invalidate its generation,
  clear pending suggestions, and refresh the UI. Ignore stale monitor events
  after disablement.
- Manual switching and suggestions remain independent stored toggles.
- Cover failed persistence (no monitor/UI mutation) and pending-suggestion
  invalidation.

### 5. Documentation and validation

- Update `docs/menubar.md` and `docs/chatgpt-desktop-feasibility.md`. Do not
  claim that installation, process state, selected credentials, or relaunch
  verifies the profile ChatGPT actually loaded.
- Capture synthetic light/dark native screenshots for preference-off,
  missing-app, stopped, running, and Automation-settings. Record paths below.
  If the native renderer cannot run, record the honest launcher failure.
- Run the gates in order. Record exact pass/skip/warning counts.

## Git workflow and verification

Branch: `feat/chatgpt-switch-availability` from `origin/main` at `862ae85`.

```
uv run pytest -q tests/test_menubar.py tests/test_desktop_menubar.py tests/test_chatgpt_auto_menubar.py tests/test_codex_desktop_app.py tests/test_menubar_tabs.py
uv run pytest -q tests/test_codex_desktop.py tests/test_chatgpt_auto_safety.py
uv run pytest -q
git diff --check
```

## STOP conditions

Stop if overlapping uncommitted work cannot be preserved, if a failing
acceptance test appears to require changing the CLI desktop command,
credential schema, OAuth onboarding, account numbering, recovery schema,
threshold policy, app allowlist, signature policy, or Codex process guard,
or if a test fails twice without an understood cause.

## Validation record

- Focused gate 1: 372 passed, 0 skipped, 0 warnings (`tests/test_menubar.py`
  `tests/test_desktop_menubar.py` `tests/test_chatgpt_auto_menubar.py`
  `tests/test_codex_desktop_app.py` `tests/test_menubar_tabs.py`).
- Safety gate 2: 49 passed, 0 skipped, 0 warnings
  (`tests/test_codex_desktop.py` `tests/test_chatgpt_auto_safety.py`).
- Combined focused+safety: 421 passed.
- Full suite: 2833 passed, 4 skipped, 3 existing pytest warnings.
- `git diff --check`: passed (no whitespace errors).
- Screenshots: synthetic AppKit PNGs were generated during implementation
  in a local scratch directory and are **not** in this checkout or the
  git tree. They are not a merge gate. A real two-account desktop proof
  that ChatGPT loaded the intended profile remains out of scope.
- `src/openswap/codex/desktop.py` was not modified: `DesktopSwitcher` already
  accepts a shared `DesktopApp` instance; the extra now passes the
  controller-owned instance. Transactions call
  `DesktopApp.invalidate_validation_cache()` before `switch()` so a
  secondary bundle change after a probe is re-signed.
