# Plan 010: Codex parity follow-ups (badge, knob, transfer, swap, sessions)

> Executor: TDD. Follow Claude patterns; do not invent a third provider
> shape. Never Keychain for Codex. Tests inject `home=` / `backup_dir=` /
> `popen=` / `ps=`; never the developer's `~/.codex` or rumps/AppKit in
> `test_menubar.py` / `test_widget_snapshot.py` / `test_codex_*.py` /
> `test_process_detection.py` / `test_statusline.py`. Combined remaining
> stays Claude-only. No `wham/usage`. No ChatGPT desktop. No
> `codex unclaimed` (no consume gate). Commits: `feat(codex|engine|cli|menubar):`
> / `docs:` / `fix(…):`, imperative, no AI trailers. Branch
> `claude/010-codex-parity` from `origin/main`. Never push `upstream`.

## Why

Plan 009 shipped independent Claude/Codex rotations. Named follow-ups and
leftover copy/docs from 009:

- Extra quarantine toast still says "Claude Code" for Codex events
- Architecture ASCII is Claude-only; `plans/README.md` still lists 009 TODO
- Swift widget has no Codex badge (Python already prefixes `Codex ·` and
  emits `provider`)
- No `autoswitch.codexEnabled` knob (both engines always start)
- No `openswap codex export/import` or `swap`/`move`
- No Codex running-sessions line (Claude has plan 003)
- "Codex status line": Codex TUI has **no command hook** (`tui.status_line`
  is built-in item IDs). Do **not** wrap `config.toml`. Paint-only
  `openswap statusline --codex` is the honest CLI analog.

## Out of scope

- ChatGPT desktop, `wham/usage`, Codex Keychain, pooling combined remaining
- `openswap unclaimed` for Codex
- Homebrew / 007 / 008
- Nested `autoswitch.codex.enabled` JSON object (use camelCase
  `autoswitch.codexEnabled` like `includeApiKeyAccounts`)

## Tasks

### Task 1 — Quarantine copy + docs nits

**Quarantine toast** (`menubar_display.notification_copy_for_event`):
`event.provider == "codex"` → body
`"Sign in with this account in Codex, then click it in the extra."`
else keep the Claude Code sentence.

**CLI** (`QuarantineEvent._human`): if `self.provider == "codex"`, recovery
is `openswap codex switch N` after signing in again, so the refreshed live
credentials are captured into the existing slot.

**Docs**: architecture ASCII shows Codex CLI (`~/.codex/auth.json`) beside
Claude Code, both feeding CLI / AutoSwitchEngine / extra. Mark 009 DONE
in `plans/README.md` and add 010 IN PROGRESS. Drop leftover plan-narration
comments in `tests/test_codex_cli.py` if present.

TDD tests first:

- `test_codex_quarantine_toast_says_sign_in_with_codex` in `test_menubar.py`
- `test_codex_quarantine_human_says_codex_switch` in `test_autoswitch.py`

### Task 2 — Swift Codex badge

`AccountCard` already has `provider` in Python JSON; Swift ignores it.

- `Snapshot.swift`: `var provider: String?` on `AccountCard`; sample cards
  may omit it (optional).
- `OpenSwapWidgetView.swift` `AccountBlock`: if
  `account.provider == "codex" || account.num.hasPrefix("codex:")`, show
  trailing 10pt medium muted `Text("Codex")` next to active/disabled
  (same chrome as `"disabled"`). Keep Python `Codex ·` title prefix
  (shared with extra cards).

No pytest for Swift. Do not change Python payload.

### Task 3 — `autoswitch.codexEnabled`

`AutoSwitchSettings.codex_enabled: bool = True`.
`SETTING_SPECS`: json_key `codexEnabled`, dotted `autoswitch.codexEnabled`.
Default true = current behavior. `test_registry_covers_every_dataclass_field`
must stay green.

Gate **construction** only (`AutoSwitchEngine` stays unaware):

- CLI `_auto_command`: start Codex engine iff
  `settings.codex_enabled and codex.switchable_account_numbers()`.
- Extra `_ensure_codex_engine`: same check via `load_settings`.
  Toggling off stops only `_codex_engine`, not Claude.

Extra Settings: when master auto is on **and** `has_codex`, a toggle row
`id="codex_enabled"`, label `"Auto-switch Codex accounts"`. `_on_setting`
writes `set_setting(..., "autoswitch.codexEnabled", ...)`.

Tests: `test_settings.py` round-trip; `test_menubar.py` settings rows;
CLI/source gate that `codexEnabled` false does not construct the Codex
`AutoSwitchEngine`.

### Task 4 — `openswap codex export/import`

New `src/openswap/codex/transfer.py`. Do **not** reuse Claude
`FORMAT_VERSION = 1` (missing `config.oauthAccount` would break Claude
import). Envelope:

```json
{
  "version": 1,
  "provider": "codex",
  "exportedAt": "<iso>",
  "exportedFrom": "macos|…",
  "swapVersion": "<openswap.__version__>",
  "encrypted": false,
  "activeAccountNumber": "1" | null,
  "accounts": [
    {
      "number": 1,
      "email": "a@x.com",
      "accountId": "acc-1",
      "planType": "plus",
      "kind": "oauth",
      "added": "<iso>",
      "alias": "work",
      "disabled": false,
      "auth": { }
    }
  ]
}
```

- Reject `encrypted: true`, wrong provider, missing auth.
- Export active slot prefers **live** `auth.json` bytes (newer generation).
- Slim: `auth.json` object only (not slot-home sessions/config.toml).
- Import: validate all, then under `codex/.lock` write `slots/<n>/auth.json`
  0600 via existing `_write_auth_file` / `_write_slot`. Skip existing
  healthy slots unless `--force`. Do not call `add_account` (that requires
  live login). Do not switch live. Reuse exported number if free else
  `_next_free_number`. Alias collision with another local slot: drop alias
  + warning.
- PATH `-` = stdout/stdin. File 0600. Windows: skip 0600 assert like
  existing Codex tests.
- CLI: `openswap codex export PATH [--account NUM|EMAIL|ALIAS]`,
  `openswap codex import PATH [--force]`.

Tests in `tests/test_codex_transfer.py` + CLI dispatch in `test_codex_cli.py`.
Mirror `test_transfer.py` cases that apply (round-trip, force, skip, `-`,
0600 on POSIX, reject encrypted, reject claude envelope).

### Task 5 — `openswap codex swap` / `move`

`CodexEngine.swap_accounts` / `move_account` under `FileLock`. Exchange
`sequence.json` records + rename `slots/<n>/` dirs (temp name if needed)
+ rewrite `activeAccountNumber` + sort sequence. No Keychain, no session
profiles, no `_ensure_no_live_session`. Cap move target
`max(99, highest existing)` like Claude. Missing auth.json is an empty
slot, not an abort.

CLI nested verbs like add/list. Tests in `test_codex_engine.py` + CLI.
Skip `unclaimed`.

### Task 6 — Codex running sessions (plan 003 analog)

Codex has no `sessions/{pid}.json`. SCAN process table; inject `ps=`.

`process_detection.py`: `CodexProcess`, `is_codex_comm`, `classify_codex_argv`,
`parse_process_table`, `get_running_codex_instances` (kind `tui` only).
Kinds: bare/`resume`/`fork` → `tui`; `exec`/`e` → `exec`; `app-server` →
`app-server`; `app` → `app`; else `other`. Drop pid ≤ 1.

`menubar_display`: `format_codex_running_line`, `switch_codex_restart_hint(running)`.
`codex_restart_hint()` remains the `running=True` string.
Toasts: Codex restart sentence only when `running` is true (engine + manual).
On SCAN error, keep `codex_running=True` (do not drop the hint).

Extra snapshot: `codex_running`, `codex_running_line`. Popover: second muted
line above footer; add `RUNNING_LINE_H` if present.

Tests as listed in the 003-analog exploration (classify, tui-only, injected
ps, toast gating). No rumps/AppKit.

### Task 7 — Paint-only Codex status line

No `--install` for Codex. `openswap statusline --codex` paints the live
Codex account label from `auth.json` + `codex/sequence.json` (store-only,
no Engine, no network). Reuse `account_label` (alias → planType/`org_name`
→ personal / email local-part). Always exit 0. Help must not claim wrapping
`config.toml`.

Tests in `test_statusline.py` with `temp_home`.

### Task 8 — Step 11 extra/widget pass (evidence)

If rumps extra can launch with two Claude + two Codex accounts, run the
009 Step 11 checklist. If not, capture the launcher/account gap; unit
tests remain the UI gate. Do not fabricate screenshots or touch real
Keychain/`~/.codex` from tests.

## Done when

- [ ] Quarantine Codex copy in extra + CLI human
- [ ] Architecture ASCII + plans/README 009 DONE, 010 this branch
- [ ] Swift Codex badge
- [ ] `autoswitch.codexEnabled` default true; extra + CLI honor it
- [ ] `openswap codex export|import|swap|move` round-trip in tests
- [ ] Codex TUI running line + gated restart toast
- [ ] `openswap statusline --codex` paint-only
- [ ] `uv run pytest` green
- [ ] No pooling, no unclaimed, no config.toml wrap, no Keychain for Codex
