# Plan 019: API credit fallback for a subscription account

## Status

- Priority: P2; effort: M; risk: MED; dependencies: none (touches the
  autoswitch ranking, the roster record, the extra's More menu).
- Planned at: `1a6bfb5`.
- Status: PROPOSED. Implementation plan: `plans/019-implementation.md`.

## Why this matters

A Claude subscription account can carry a Console API key for the same
organization, billed per token. When every subscription in the roster is out
of quota, work should continue on that key instead of stopping, and come back
to a subscription the moment one has room again.

Today OpenSwap can store a key (`openswap add-token`) but the key is a
stranger: it is not tied to any subscription account, it is ignored unless the
global `autoswitch.includeApiKeyAccounts` flag is on, and the engine's way
back off an active key is a defect (see below). Entering the key in the extra
is two bare prompts that ask for a label before the token and show the key in
plain text.

## Current state and conventions

- Roster records live in `sequence.json` (`engine/slots.py`); an API key
  slot is `{"kind": "api_key"}` with an auto-named `api-key-{slot}@token.local`
  label. `_reject_cross_kind_collision` refuses a key whose label equals an
  OAuth account's email, because identity is `(email, organizationUuid)` only.
  Keep that guard; the link below is a separate field.
- `AutoSwitchEngine.tick` (`autoswitch.py`): subscriptions with room always
  beat keys; keys are a last resort only when `include_api_key_accounts` is
  on, and only when the trigger is not a consume-first nudge.
- Defect to fix, not preserve: with the flag on and a key active, the key's
  usage reads as unknown, `_unhealthy_ticks` climbs for three ticks, and the
  engine fails over to any subscription with headroom above zero. Landing on
  a 95% account trips the proactive rule and bounces back to the key every
  cooldown. There is no test for this path; `test_active_api_key_idles_engine`
  covers the flag-off case only.
- `AccountSnapshot.kind` reaches the extra as `snapshot["kinds"]`
  (`menubar_display._adapt_snapshot`); `alias` shows how a per-record string
  travels to cards.
- Hold copy is `hold_line_from_event` in `menubar_display.py`; the extra does
  not re-rank.
- The popover has pages (`MAIN_PAGE`, `SETTINGS_PAGE`) in `menubar_panel.py`,
  a `_PopupButton`, and the ChatGPT sign-in page as the example of a page with
  a text field and action buttons. `BrandDialog` supports one masked field.
- Tests lift `MenuBarApp` methods with `tests/menubar_harness.py` and never
  import AppKit in `tests/test_menubar.py`. Pure copy and detection helpers go
  in `menubar_display.py` so they are testable.
- User-visible copy never echoes tokens, paths, or raw exceptions.

## Decisions

1. **Last resort, not same-account first.** A subscription with free quota
   always beats paid credit. A linked key is used only when no subscription
   candidate qualifies, exactly where keys sit today.
2. **Linking is consent.** A key linked to a subscription account is eligible
   without `autoswitch.includeApiKeyAccounts`. That flag keeps governing
   unlinked keys and is otherwise unchanged.
3. **Return only to a healthy subscription.** While an eligible key is active,
   the engine leaves it only for a subscription below the threshold. It never
   returns to an account that would re-trigger on the next tick.
4. **Link by identity, not slot.** The key record stores the parent's
   `(email, organizationUuid)`, so `swap`, `move`, and re-adding the parent
   keep the link. A link whose parent is gone resolves to nothing and the key
   behaves as unlinked until the parent returns.
5. **The link is the user's assertion.** OpenSwap cannot verify a key belongs
   to the named organization. Copy says "API credit for a@example.com", never
   "verified".

## Scope

Modify `src/openswap/engine/slots.py`, `src/openswap/engine/switch.py`,
`src/openswap/engine/protocol.py`, `src/openswap/engine/snapshot.py`,
`src/openswap/models.py`, `src/openswap/autoswitch.py`,
`src/openswap/cli.py`, `src/openswap/json_output.py`,
`src/openswap/menubar_display.py`, `src/openswap/menubar.py`,
`src/openswap/menubar_panel.py`, `tests/test_api_key_accounts.py`,
`tests/test_autoswitch.py`, `tests/test_cli.py`, `tests/test_menubar.py`,
`tests/test_menubar_tabs.py`, `README.md`, `docs/architecture.md`,
`docs/menubar.md`, this plan, and `plans/README.md`.

Codex and ChatGPT are out of scope: the Codex CLI has its own key handling
and its own rotation. Kickoff and the widget already skip keys and are
unchanged.

## Steps and done criteria

### 1. Roster: the link

- `add_account_from_token(token, email, slot, assume_yes, credit_for=None)`.
  `credit_for` is a slot number, email, or alias resolved through
  `resolve_account` (ambiguity is a hard error, as for kickoff). Reject with
  `ValidationError` when the token is a setup token (a login in its own
  right, not credit), or when the target is itself an API key slot.
- On success the record gains `"creditFor": {"email": ..., "organizationUuid": ...}`.
  Nothing else about the record changes; the label stays auto-named unless
  `--email` is given.
- `credit_parent_for(account_num) -> str | None` on the `AccountEngine`
  Protocol: the slot whose identity matches the stored link, or `None` when
  the key is unlinked or the parent is gone. `CodexEngine` returns `None`.
- `remove_account` of a parent prints, after the removal, one line per key
  that now has no parent: "Slot 3 (API credit for a@example.com) is now a
  standalone key." The key record is not rewritten.
- `AccountSnapshot` gains `credit_for: str = ""` (resolved parent slot).
  `Engine.accounts_snapshot` fills it from the roster only: no network, no
  credential read.

### 2. Engine: eligibility, ordering, return

- Eligible keys: linked keys always; unlinked keys only when
  `include_api_key_accounts` is on. One rule, one place, used for both the
  candidate census and the active-key branch.
- Among eligible keys, order: the key linked to the account being left,
  then other linked keys, then unlinked, each group in slot order. Everything
  else about last-resort selection stays: never for a consume-first nudge,
  and all-exhausted fires only when no eligible key remains.
- Active key branch, evaluated before any headroom or unhealthy logic:
  - Not eligible: the existing `active-api-key` no-switch, unchanged.
  - Eligible: trigger `credit-return`. Candidates are OAuth accounts with
    readable headroom and utilization strictly below `threshold`, ranked by
    most headroom. No all-above escape, no hysteresis against the key (the
    key has no utilization), cooldown applies as for a proactive switch.
    Nothing qualifies: `NoSwitchEvent(reason="on-api-credit")`,
    `TickOutcome.NO_ACTION`. Staying on credit is correct, not blocked, so
    the poll cadence is the normal one (a subscription window can roll over
    at any minute).
  - `_unhealthy_ticks` never increments for an active key.
- Hold copy: `on-api-credit` renders "Holding on API credit: no subscription
  account below {threshold}%." Leaving a subscription for a key uses the
  existing switch toast.

### 3. CLI

- `openswap add-token TOKEN --for ID` (hidden legacy spelling `--add-token`
  gets the same option). `--for` with a setup token is a usage error with a
  one-line reason.
- `openswap list` (`Engine.list_accounts` in `engine/snapshot.py`) shows a
  linked key as "API credit for a@example.com" in the place the sentinel
  note sits today, listed directly after its parent.
  JSON output (`json_output.py`) gains an additive `"creditFor": <parent
  slot>` on linked key rows only, like `alias` and `disabled`.

### 4. Extra: cards and hold line

- `_adapt_snapshot` adds `"credit_for": {num: parent_num}`.
- A linked key card is titled "API credit" with subtitle
  "for a@example.com", sorted immediately after its parent card. Unlinked
  keys keep today's card.
- The `openswap config` help for `autoswitch.includeApiKeyAccounts` says it
  applies to keys that are not linked to an account (the popover has no
  toggle for it).

### 5. Extra: Add from token page

- New `TOKEN_PAGE` in `menubar_panel.py`, same chrome as Settings with Back.
  "From API key or setup token…" in More opens it. Esc or Back returns to
  the cards without adding anything.
- Fields, top to bottom: masked token field (`NSSecureTextField`, paste
  works); a detection line under it; "Use as API credit for" popup; optional
  label field.
- `token_entry_state(text, accounts) -> dict` in `menubar_display.py` is the
  pure model: `kind` (`"api_key"`, `"setup_token"`, or `None`), the detection
  line ("API key", "Setup token", "Not a Claude token", empty when the field
  is empty), whether the popup shows (API key only), the popup choices (OAuth
  Claude accounts as "alias or org tag · email", with "None (standalone)"
  first), and whether Add is enabled (kind is not `None`). An API key is
  `credentials.looks_like_api_key`; a setup token is the documented
  `sk-ant-oat` prefix. The page is stricter than the engine, which treats
  any non-key text as a setup token; the CLI keeps that lenient path.
- Add calls `MenuBarApp._add_token(token, label, credit_for)`, which runs the
  engine call under `_guard`. Success: back to the cards and `refresh_async`.
  Failure: the page stays, the message renders inline, sanitized the way
  `login_panel_state` strips URLs and token-shaped substrings.
- The token string lives only in the field and the engine call: never in
  the panel's state dict, logs, or alerts.

### 6. Docs

- README: command table row for `add-token … --for`, one quick-start line.
- `docs/architecture.md` product-surface paragraph: linked keys are eligible
  without the flag; return rule in one sentence.
- `docs/menubar.md`: the token page in the More paragraph; the "API credit"
  card title.
- `plans/README.md`: row for 019.

## Tests (TDD, each written before its step)

- Roster (`test_api_key_accounts.py`): `--for` by slot, email, alias;
  ambiguity error; setup-token rejection; API-key target rejection; link
  survives `swap` and `move`; parent removal prints the notice and
  `credit_parent_for` returns `None`; re-adding the parent resolves again;
  snapshot `credit_for` is filled from the roster alone.
- Engine (`test_autoswitch.py`): linked key eligible with the flag off;
  unlinked key still excluded with the flag off; subscription with room beats
  a linked key; key ordering (left account's key first, then linked, then
  unlinked); credit-return goes to the best below-threshold subscription;
  credit-return holds with `on-api-credit` when every subscription is at or
  above threshold and never bounces (sweep ticks, assert no switch); cooldown
  gates credit-return; `_unhealthy_ticks` stays zero on an active key;
  consume-first never targets a linked key.
- CLI (`test_cli.py`): `--for` plumbing; the usage error; list row text; JSON
  `creditFor`.
- Extra (`test_menubar.py`, `test_menubar_tabs.py`): `token_entry_state`
  table (empty, key, setup token, garbage, popup visibility, Add enabled,
  choices and their order); card title and sort for a linked key; hold line
  for `on-api-credit`; `_add_token` success and sanitized failure through the
  harness. No AppKit import in tests.

## Acceptance

1. Two subscriptions and one key linked to account 1, flag off: account 1
   at 95%, account 2 at 10% switches to 2; both at 100% switches to the key;
   account 2 back at 20% on a later tick returns to 2; account 2 at 92% holds
   on the key with the credit hold line.
2. `openswap list` and the popover both show the key as API credit for
   account 1, directly under it.
3. Adding a key from the popover: the token is never visible, the popup
   appears only for `sk-ant-api…` input, a wrong paste disables Add with a
   plain reason, and an engine rejection shows inline without the token.
4. `uv run pytest` green; no AppKit import from tests.

## Known limitations and follow-ups

- OpenSwap cannot see a revoked key or an exhausted credit balance. Claude
  Code reports that error itself; the engine stays on the key until a
  subscription recovers. Follow-up to file on acceptance of this plan: a
  cheap validity probe through the token-count endpoint, surfaced as a
  sentinel like `token expired` (filed as mar3co/openswap#97).
- The link cannot be verified against the key's organization.
- Extra-usage overflow on the same login (Anthropic's `/extra-usage`) is a
  different mechanism and is not addressed here.
