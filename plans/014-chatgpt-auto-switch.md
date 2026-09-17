# Plan 014: ChatGPT auto-switch with confirmed restart

## Scope

Expose an Auto-switch control on the ChatGPT tab, matching Claude's placement.
Keep provider preferences independent. ChatGPT automatic account selection must
not be confused with the existing live Codex credential-file rotation.

## Safety contract

- Default off; enabling explicitly explains that restart confirmation is required.
- Use the existing quota/strategy engine in dry-run mode to propose a candidate.
  It must never commit a credential switch from a background decision.
- There is no authoritative idle signal covering Chat, Work and Codex. Defer
  every desktop transition until the user reviews it and confirms idle/restart
  through the existing guarded desktop transaction. Do not infer idle from a
  closed window, absence of local processes, or a quiet usage poll.
- Disable legacy live Codex rotation with consent when enabling this mode, and
  prevent conflicting modes. Do not change Claude's auto-switch preference.
- Persist the monitor preference, not pending credentials or approvals. Invalidate
  stale candidates on account/login/policy changes, off, or transaction completion.
- Pause monitoring around onboarding and desktop transactions; reject stale
  worker events. Never automatically open a modal or restart ChatGPT.

## UI

Auto-switch toggle, short confirmation-required caption, and a 'Switch ready'
status with Review switch action. Reuse the existing desktop restart dialog.
Saving an account remains enabled by default, without immediately switching.

## Ownership and validation

- GPT-5.6 implementation subagent: app/settings/panel lifecycle and focused tests.
- Primary agent: safety review, documentation, full suite and synthetic native QA.
- Test independent toggles, startup/off/restart, stale events, candidate removal,
  cancellation, onboarding/transaction guards, and zero background auth commits.
- Do not exercise real OAuth, change account settings, or restart ChatGPT in QA.

Status: implemented locally and reviewed; unattended restarts remain intentionally unsupported.

## Validation — 2026-09-17

- GPT-5.6 subagent implemented the provider control, monitor lifecycle and tests;
  primary-agent review added restart-dialog revalidation, separate safe failure
  notices, bounded retry timing and provider-specific Settings controls.
- Full suite: 2,699 passed, 3 skipped; 3 pre-existing pytest deprecation warnings.
- Fifteen focused lifecycle tests cover stale events, identity/eligibility changes,
  consent, independent preferences, conflicts, pause/resume and persistence.
- A real Codex-engine fixture confirms selection-only ticks do not invoke auth
  switching or explicit token freshening. Ordinary usage polling is unchanged.
- Fourteen synthetic AppKit previews passed across light/dark appearances:
  off/on, pending review, verification warning, empty list, Settings and Claude.
- The monitor reads the existing cooldown state without writing it; confirmed
  switches continue to record their cooldown. Failure retries are delayed.
- No real login, account switch, automatic-mode enablement, or ChatGPT restart
  was performed. Existing account preferences were not migrated or enabled.
