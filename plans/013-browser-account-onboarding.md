# Plan 013: Browser-led ChatGPT account onboarding

Status: implemented locally; live OAuth acceptance remains for the operator.

## Contract

- Primary action: Sign in with ChatGPT. Secondary: Save current login.
- Use the official Codex login flow, launched without a Terminal window in a
  new private CODEX_HOME with explicitly file-backed credentials. Never run
  logout, write the normal auth file, restart ChatGPT, or copy user config to
  bypass managed authentication requirements.
- Background state: starting → waiting → ready → saved; errors are retryable.
  Cancel/timeout stop only the owned child process and remove its temporary
  credentials. No credentials or login URLs in logs or public state.
- Review returned identity before Save account; optional nickname. Commit
  under the roster lock; reject duplicate identities without overwriting their
  tokens. Do not change the active-account marker or live credential file.
- Save new accounts enabled by default, as requested in the follow-up. Saving
  does not switch accounts; normal automatic-rotation eligibility applies.
- UI text stays short: one headline, one explanatory line where possible,
  compact actions and inline errors. Keep existing Claude capture behavior.
- Offer a copy-link handoff to a different browser profile. Device-code login
  is a fallback where supported; terminal guidance is troubleshooting only.

## Ownership

1. Backend agent: isolated login lifecycle, atomic account insertion,
   synthetic process/credential tests. Own codex onboarding module and engine.
2. UI agent: native onboarding state panel and concise empty-state copy;
   own panel/display and focused UI tests.
3. Primary agent: native app callbacks, menu integration, account enable action,
   cross-layer tests, review, docs, native renders, and reload of OpenSwap only.

## Validation

Test success, cancellation at each boundary, failed/late process completion,
timeouts, duplicates, malformed credentials, unsupported configs, missing CLI,
and new-account exclusion from auto-rotation. Verify the live auth bytes and
active marker remain unchanged. Render synthetic states in light and dark
mode. Do not perform real sign-in, logout, or account switching in development.

## Results — 2026-09-17

- Backend and native UI implemented by separate GPT-5.6 agents; primary-agent
  integration and safety review completed.
- Full suite: 2,681 passed, 3 skipped (3 existing pytest deprecation warnings).
- Twenty synthetic AppKit previews rendered across light/dark themes, including
  long errors, browser/device handoff, identity review and saved-account status.
  Nickname persistence across Settings and provider navigation passed.
- Real synthetic child-process regression confirms short flushed login output
  is visible before child exit. Cancellation, timeout, environment isolation,
  strict roster validation, duplicate refusal and enabled insertion are covered.
- No real OAuth sign-in, logout, live credential switch or ChatGPT restart was
  performed. Changes are local and not yet committed or pushed.
