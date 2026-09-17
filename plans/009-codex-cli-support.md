# Plan 009: Codex CLI accounts (second provider beside Claude)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, do **not** update `plans/README.md`
> (the reviewer maintains the index).
>
> **Drift check (run first)**: `git diff --stat b00dce8..HEAD -- src/openswap/autoswitch.py src/openswap/engine/ src/openswap/models.py src/openswap/cli.py src/openswap/menubar.py src/openswap/menubar_display.py src/openswap/menubar_panel.py src/openswap/widget_snapshot.py src/openswap/kickoff.py tests/test_autoswitch.py tests/test_engine_abi.py tests/test_menubar.py tests/test_widget_snapshot.py tests/test_kickoff.py`
> Expect only 008 (app cask) diffs, if any. If `AutoSwitchEngine._freshen_target`,
> `_adapt_snapshot`, `panel_accounts`, or `invoke_kickoff` no longer exist as
> quoted under "Current state", STOP.
>
> **Phase gates**: this is one plan with three phases (A engine + CLI, B
> autoswitch + kickoff, C extra + widget). Each phase ends with a full
> `uv run pytest` and a commit. Do not start the next phase with a red suite.

## Status

- **Priority**: P2
- **Effort**: L
- **Risk**: MEDIUM (touches the auto-switch engine's credential path; adds a
  second live login the extra can rewrite)
- **Depends on**: none (independent of 007/008; do not touch `packaging/`)
- **Category**: direction
- **Planned at**: commit `b00dce8`, 2026-09-10
- **Spike**: chat findings 2026-09-10 (Codex CLI feasible; ChatGPT desktop app
  is not a rotation target and is out of scope here)

2026-09-17 clarification: desktop exclusion above describes this plan's scope,
not a demonstrated technical impossibility. See
[plan 011](011-chatgpt-desktop-feasibility.md) for the new investigation.

## Why this matters

OpenSwap rotates Claude Code accounts. Codex CLI users have the same problem
(a 5-hour and a weekly window per ChatGPT account, one login at a time) and
the same shape of solution: back up a login, write another one into the live
store, read usage for the bars. After this plan a user can `openswap codex
add` two ChatGPT logins, see their 5h/7d bars next to the Claude ones in
`openswap list`, the extra, and the widget, switch between them by hand,
have `openswap auto` and the extra rotate them near a limit, and have the
morning kickoff open a Codex 5-hour window too.

Claude and Codex are **two independent rotations** (one live Claude login,
one live Codex login). They never pool: a Codex account cannot be switched
into Claude Code. The engine gets a provider seam *beside* the Claude
`Engine`, not beneath it: the 12k-line Claude engine stays untouched except
for one method move (`freshen_backup`), and a small `CodexEngine` implements
the same consumer-facing surface.

## Codex facts this plan relies on (verified 2026-09-10)

- Live store: `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`), mode
  0600, JSON: `{"auth_mode": "chatgpt"|"apiKey"|"chatgptAuthTokens",
  "OPENAI_API_KEY": str|null, "tokens": {"id_token", "access_token",
  "refresh_token", "account_id"}, "last_refresh": iso}`. Codex refreshes the
  bundle in place when `last_refresh` is ~8 days old; OpenAI's CI docs say one
  `auth.json` per serialized stream. One machine rotating one live file is
  inside that rule.
- `CODEX_HOME` relocates Codex's whole state dir (config, auth, sessions).
  It is the analogue of `CLAUDE_CONFIG_DIR`.
- Identity: `id_token` is a JWT; claims `email` and
  `"https://api.openai.com/auth": {"chatgpt_account_id", "chatgpt_user_id",
  "chatgpt_plan_type"}`. `tokens.account_id` equals `chatgpt_account_id`.
- Usage without spending quota: `codex app-server` speaks newline-delimited
  JSON-RPC on stdio. Client sends `{"id":1,"method":"initialize","params":
  {"clientInfo":{"name":"openswap","title":"OpenSwap","version":"0.1.0"}}}`,
  waits for the `id:1` response, sends the notification
  `{"method":"initialized"}`, then `{"id":2,"method":"account/rateLimits/read",
  "params":{}}` and reads `{"id":2,"result":{"rateLimits":{"primary":
  {"usedPercent":42,"windowDurationMins":300,"resetsAt":1788265323},
  "secondary":{"usedPercent":61,"windowDurationMins":10080,"resetsAt":...}}}}`.
  Either window may be null. `resetsAt` is unix seconds. Some clients need a
  short wait after the handshake; reading until the matching `id` handles it.
  The server may refresh a near-expiry token and rewrite `auth.json` in its
  `CODEX_HOME` while answering.
- `GET chatgpt.com/backend-api/wham/usage` exists but is undocumented. Not
  used. Do not add it.
- Headless ping: `codex exec "ok"` (non-interactive; final message on stdout).
  Known upstream issue: many sequential `codex exec` runs on ChatGPT auth can
  end in `token_invalidated`. Kickoff runs once per slot per day; keep it so.
- Refresh-token rotation on refresh is undocumented. Treat every Codex
  process that touched a home as having possibly rewritten `auth.json`:
  the slot directory **is** the home, so nothing has to be copied back.

## Current state

- `src/openswap/engine/engine.py` — `class Engine(LiveMixin, SlotsMixin,
  IdentityMixin, ConsumeMixin, SwitchMixin, SessionProfileMixin,
  SnapshotMixin)`. `__init__(debug=False)` sets `backup_dir =
  get_backup_root()`, `sequence_file`, `_logger = setup_logging(backup_dir)`,
  `_usage_store = UsageStore(backup_dir / "cache")`.
- `src/openswap/engine/__init__.py` re-exports `Engine` plus `USAGE_*`
  constants from `json_output`.
- `src/openswap/models.py:124` `AccountSnapshot(number, email, org_name,
  org_uuid, is_active, kind, switchable, usage: UsageEntry, alias="",
  disabled=False)`; `display_tag` = org_name or "personal".
  `AccountsSnapshot(active_number, accounts, taken_at)`.
- Methods consumers actually call on the engine (grep of `cli.py`,
  `autoswitch.py`, `menubar.py`, `session.py`):
  - autoswitch: `set_poll_policy_inputs(threshold, models)`,
    `usage_entries_by_account(fetch=None, *, scheduled=False)`,
    `current_account_number()`, `has_live_login()`, `account_email(num)`,
    `account_kind_for(num)`, `switchable_account_numbers()`,
    `read_account_credentials(num, email)` (only to fingerprint via
    `oauth.credential_fingerprint`, which hashes full content when there is
    no Claude refresh token), `switch_to(num, json_output=True)`, and inside
    `_freshen_target` only: `live_session_pids_for`, `consume_backup_grant`,
    `account_identity`, `backfill_account_uuid`.
  - extra: `live_identity()`, `slot_identity(num)`, `current_account_number()`,
    `switch(strategy)`, `switch_to(num, json_output=True)`,
    `add_account`, `add_account_from_token`, `remove_account`,
    `set_account_disabled`, `accounts_snapshot(fetch)` (via `SnapshotSource`),
    `_get_claude_config_path()`.
- `src/openswap/autoswitch.py:798` `_freshen_target(self, number, email) ->
  str` returns `"ok" | "invalid_grant" | "identity-conflict" | "transient" |
  "skip-live-session" | <systemic status>`. It calls
  `oauth.extract_oauth_data(creds)` on the slot's backup bytes and returns
  `"invalid_grant"` when that yields nothing. **A Codex `auth.json` would be
  quarantined by this.** `_note_token_identity` (866) is used only by it.
  `_perform` (2138) calls `switch_to(number, json_output=True)` and reads
  `result["switched"]`, `result.get("from")`, `.get("to")`,
  `.get("warnings", [])`, `.get("reason")`.
- `src/openswap/autoswitch.py:670` `AutoSwitchEngine(switcher, settings,
  on_event, *, dry_run=False, state_path=None, clock=time.time)`;
  `state_path` defaults to `switcher.backup_dir / STATE_FILENAME`.
- `src/openswap/engine/switch.py:996` switch result dict: `{"switched": bool,
  "from": ref|None, "to": ref, "reason": str, "warnings": list}` where a ref
  is `{"number": str|int, "email": str}` (`autoswitch._ref`).
- `src/openswap/usage_store.py` `UsageStore(cache_dir, clock)`;
  `Identity = tuple[str, str]`; `entries(identities, models=())`,
  `reserve(nums, identities, *, respect_plans, repair_overslept=False) ->
  {num: claim_id}`, `record(outcomes: {num: FetchRecord}, identities, claims,
  plans=None) -> set[str]`. `FetchRecord(usage=None, error=None,
  retry_after_s=None, sentinel=None, struck_fp=None)`. Usage dict shape:
  `{"five_hour": {"pct": float, "resets_at": iso, "countdown": str,
  "clock": str}, "seven_day": {...}}`; `oauth.format_reset(iso) ->
  (countdown, clock)`; `oauth.relevant_windows` / `account_headroom` read
  only `pct` and `resets_at`.
- `src/openswap/engine/snapshot.py:1034` `_collect_usage_entries` is the
  reference for driving the store: `reserve(requested, identities,
  respect_plans=True, repair_overslept=True)` when `fetch is None`, else
  `respect_plans=False`; then `record(records, identities, claims, plans)`;
  then `entries(...)` again; sentinels overlaid with `with_sentinel`.
- `src/openswap/macos_keychain.py` — not used by this plan (see "Storage
  decision" below).
- `src/openswap/cli.py:918` `main()` pre-dispatches `auto`, `widget`,
  `statusline`, `config`, `unclaimed`, `alias`, `swap`, `move` by
  `argv[0]` before the flag parser. `cli.py:346` `_auto_command` builds
  `AutoSwitchEngine(Engine(), merged_with_cli(load_settings(...), args),
  emit, dry_run=...)`. `cli.py:1319` is the single `switcher.list_accounts(`
  call.
- `src/openswap/menubar.py:32` `run(switcher)`; `MenuBarApp.__init__` builds
  `SnapshotSource(switcher)`, `_worker` does `raw = self._snapshot_source.take(
  full=..., store_only=...)`, `snap = _adapt_snapshot(raw)`, then
  `publish_widget_snapshot(snap, ...)`. `_start_engine` (240) hosts one
  `AutoSwitchEngine`. `_on_account_click(num, *, close_panel)` (700) calls
  `self.switcher.switch_to(str(num), json_output=True)`. `_run_kickoff`
  (1019) iterates `self.snapshot["accounts"]` 9-tuples and calls
  `invoke_kickoff()` (active) or `invoke_kickoff(session_dir)` (idle).
  `_detect_active_change` stats `self._config_path` (`~/.claude.json`).
  `MenuBarPanel(on_switch=lambda num: self._on_account_click(num, ...))`;
  `_CardView.mouseUp_` calls `self.on_switch(self.card["num"])`.
- `src/openswap/menubar_display.py:1362` `_adapt_snapshot(snap) -> dict` with
  `accounts` = list of 9-tuples `(num, email, is_active, display, last_good,
  alias, org_name, disabled, fetched_at)`, plus `active_*`, `identities`,
  `kinds`. `panel_accounts(snapshot, now)` (996) turns rows into cards
  `{num, title, subtitle, active, disabled, note, needs_relogin, fetched_at,
  windows}` via `account_card_names(email, alias, org_name)`.
- `src/openswap/widget_snapshot.py:203` `build_widget_payload` →
  `{"schema", "updated_at", "accounts": cards, "combined":
  build_combined(accounts, now)}`; `combined_window` skips cards with
  `disabled` or `needs_relogin`. `parse_switch_command(raw) -> num|None`.
  Swift `AccountCard` (`macos/OpenSwapWidget/Widget/Snapshot.swift:39`) is
  `Codable` with `num: String`; `SwitchAccountIntent` writes
  `{"op":"switch","num": num}`. Swift decodes with
  `convertFromSnakeCase` and ignores unknown keys.
- `src/openswap/kickoff.py:194` `build_kickoff_argv(claude_bin)`,
  `build_kickoff_env(session_dir, environ)`, `invoke_kickoff(session_dir=None,
  *, which=None, run=None, timeout=90, environ=None)`; module constants
  `KICKOFF_PROMPT = "ok"`, `KICKOFF_TIMEOUT_S`. `kickoff_account_eligible(*,
  is_api_key, usage, now)` reads only `usage["five_hour"]["resets_at"]`, so
  it works for Codex usage dicts unchanged.
- `tests/conftest.py` — autouse `_isolate_real_home` (fake `$HOME`),
  `block_real_keychain`, `block_real_oauth_profile_fetch`; `temp_home`
  fixture. `tests/test_engine_abi.py` freezes the extra-facing ABI.
  `tests/test_autoswitch.py::EngineHarness` drives a real `Engine`.
- `docs/testing.md`: `tests/test_menubar.py`, `test_kickoff.py`,
  `test_widget_snapshot.py`, `test_autoswitch.py` must not import rumps or
  AppKit.

## Storage decision (read before Step 5)

Claude backups live in the Keychain because Claude Code itself keeps its
credential there. Codex keeps `auth.json` as a 0600 plaintext file, and every
Codex process this plan runs (`app-server` for usage, `exec` for kickoff)
needs that file inside a `CODEX_HOME` directory anyway. So each Codex slot is a
directory, `<backup_dir>/codex/slots/<n>/` (0700) holding `auth.json` (0600),
and that directory **is** the slot's `CODEX_HOME`. Codex refreshing a token
during a usage read rewrites the slot in place; there is no copy-back and no
second generation of the refresh token. This is the same posture as
`~/.codex` itself. Do not add a Keychain copy of the same bytes.

`docs/architecture.md` says "Credentials on macOS are Keychain, not files in
that directory." Step 12 amends that sentence to name the Codex exception.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Full suite | `uv run pytest` | all pass (~2250 + new) |
| One file | `uv run pytest tests/test_codex_engine.py -n 0 -v` | all pass |
| ABI | `uv run pytest tests/test_engine_abi.py tests/test_autoswitch.py` | all pass |
| Real usage read (manual, macOS, Codex logged in) | `uv run python -c "from openswap.codex.usage import read_rate_limits; from openswap.codex.auth import codex_home; import shutil; print(read_rate_limits(codex_home(), codex_bin=shutil.which('codex')))"` | prints `{"primary": {...}, "secondary": ...}` |
| CLI smoke | `uv run openswap codex list` | table or "No Codex accounts" |

## Suggested executor toolkit

- TDD per step: write the failing test, run it red, implement, run green,
  commit. Pure helpers first (`codex/auth.py`, `codex/usage.py`), then the
  engine, then wiring.
- Never read the developer's real `~/.codex`: every test passes an explicit
  `home=` / `backup_dir=` under `tmp_path` or `temp_home`.
- Fake `codex` binaries in tests are Python callables injected as `popen=` /
  `run=`; never `shutil.which("codex")` in a test.

## Scope

**In scope**:
- New package `src/openswap/codex/` (`__init__.py`, `auth.py`, `usage.py`,
  `engine.py`, `kickoff.py`)
- New `src/openswap/engine/protocol.py`; `engine/__init__.py` export
- `src/openswap/autoswitch.py` (move freshen into the engine; call
  `switcher.freshen_backup`)
- `src/openswap/engine/switch.py` or a new `engine/freshen.py` (home of the
  moved `_freshen_target` body)
- `src/openswap/models.py` (`AccountSnapshot.provider`)
- `src/openswap/cli.py` (`codex` verbs, Codex section in `list`, second loop
  in `auto`, pass the Codex engine to the extra)
- `src/openswap/kickoff.py` (generalize the argv/env builders)
- `src/openswap/menubar.py`, `menubar_display.py`, `menubar_panel.py`,
  `widget_snapshot.py`
- Tests: new `tests/test_codex_auth.py`, `tests/test_codex_usage.py`,
  `tests/test_codex_engine.py`, `tests/test_codex_cli.py`; additions to
  `tests/test_engine_abi.py`, `tests/test_autoswitch.py`,
  `tests/test_kickoff.py`, `tests/test_menubar.py`,
  `tests/test_widget_snapshot.py`
- `docs/architecture.md`, `README.md` command table

**Out of scope**:
- Any change under `macos/` (the widget decodes the new cards as-is; a
  "Codex" badge in Swift is a follow-up)
- The ChatGPT desktop app
- `wham/usage` HTTP fallback
- `openswap statusline` (Claude only), `session.py`, `process_detection.py`
  (no Codex "running" line)
- `openswap codex export/import`, `swap`, `move`, `unclaimed`
- Per-provider autoswitch settings (both providers read `autoswitch.*`)
- Pooling Claude and Codex in the widget's combined view
- Homebrew / packaging (007, 008)

## Git workflow

- Branch: `claude/009-codex-plan` (the open PR's branch; this plan file is
  its first commit). Work on it directly.
- Commits (one per phase at minimum; per step is fine):
  - `feat(engine): account-engine protocol; freshen_backup moves into Engine`
  - `feat(codex): auth.json identity, app-server usage, CodexEngine`
  - `feat(cli): openswap codex add/list/switch/remove; Codex rows in list and auto`
  - `feat(kickoff): codex exec ping for Codex slots`
  - `feat(menubar): Codex cards, switch, rotation, kickoff in the extra and widget`
  - `docs: Codex provider in architecture and README`
- Push to `origin` after each phase gate so the PR shows progress. Never
  push to `upstream`, never force-push, never merge. No AI attribution
  trailers in commits or the PR.

## Design

### Provider seam

`src/openswap/engine/protocol.py`:

```python
"""Consumer-facing engine surface shared by the Claude Engine and CodexEngine.

Autoswitch, the extra, kickoff, and the CLI program against this, never
against provider internals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from openswap.models import AccountsSnapshot
from openswap.usage_store import UsageEntry


@runtime_checkable
class AccountEngine(Protocol):
    provider: str          # "claude" | "codex"
    backup_dir: Path

    def accounts_snapshot(self, fetch: set[str] | None = None) -> AccountsSnapshot: ...
    def usage_entries_by_account(
        self, fetch: set[str] | None = None, *, scheduled: bool = False
    ) -> dict[str, UsageEntry]: ...
    def set_poll_policy_inputs(self, threshold: float, models: tuple[str, ...]) -> None: ...
    def current_account_number(self) -> str | None: ...
    def has_live_login(self) -> bool: ...
    def live_identity(self) -> tuple[str, str] | None: ...
    def slot_identity(self, num: str | int) -> tuple[str, str] | None: ...
    def account_email(self, account_num: str) -> str: ...
    def account_kind_for(self, account_num: str) -> str: ...
    def switchable_account_numbers(self) -> list[str]: ...
    def read_account_credentials(self, account_num: str, email: str) -> str: ...
    def freshen_backup(self, number: str, email: str) -> str: ...
    def switch(self, strategy: str | None = None, json_output: bool = False) -> dict | None: ...
    def switch_to(self, identifier: str, json_output: bool = False, force: bool = False) -> dict | None: ...
    def set_account_disabled(self, identifier: str, disabled: bool) -> None: ...
    def remove_account(self, identifier: str, assume_yes: bool = False) -> None: ...
```

`Engine` gains `provider = "claude"` (class attribute) and
`freshen_backup(number, email) -> str` (the moved `_freshen_target` body plus
`_note_token_identity`; `FRESHEN_BUFFER_MS` and `_SYSTEMIC_STATUSES` move
with it, `autoswitch.py` re-imports them from their new home so its tests
keep their names). `AutoSwitchEngine._freshen_target` becomes a one-liner:
`return self.switcher.freshen_backup(number, email)`.

`CodexEngine.freshen_backup` returns `"ok"` for OAuth slots (Codex refreshes
on its own inside the slot home) and `"ok"` for API-key slots.

`AccountSnapshot` gains `provider: str = "claude"` (last field; every
existing constructor call stays valid). For Codex rows: `org_name` carries
the plan type (`"plus"`, `"pro"`, `"team"`, `""`), `org_uuid` carries the
ChatGPT `account_id`, `display_tag` therefore reads `pro` / `plus` /
`personal`.

### Slot identifiers across providers

Consumers that show both providers in one list (extra, widget) namespace
Codex slot numbers as the string `"codex:<n>"`. Claude nums are unchanged.
The Swift widget already treats `num` as an opaque string and round-trips it
through `widget-command.json`, so it needs no change. A helper in
`codex/__init__.py`:

```python
CODEX_NUM_PREFIX = "codex:"

def split_provider_num(num: str | int) -> tuple[str, str]:
    """``"codex:2"`` → ``("codex", "2")``; ``2`` / ``"2"`` → ``("claude", "2")``."""
    text = str(num)
    if text.startswith(CODEX_NUM_PREFIX):
        return "codex", text[len(CODEX_NUM_PREFIX):]
    return "claude", text
```

### Data on disk

```
<backup_dir>/codex/
  sequence.json          {"schemaVersion":1,"activeAccountNumber":null,"lastUpdated":iso,
                          "sequence":[1,2],"accounts":{"1":{"email":..,"accountId":..,
                          "planType":..,"kind":"oauth"|"api_key","added":iso,
                          "alias"?:str,"disabled"?:true}}}
  .lock                  FileLock for every roster/live mutation
  slots/<n>/auth.json    the slot's credential; slots/<n> is its CODEX_HOME
  cache/usage.json       UsageStore for Codex rows (identity = (email, accountId))
  autoswitch_state.json  second AutoSwitchEngine's state (cooldown/quarantine)
```

`<backup_dir>` is `paths.get_backup_root()` (macOS:
`~/Library/Application Support/OpenSwap`).

### Switch semantics (CodexEngine)

Under `codex/.lock`:

1. Read the live `auth.json` (`codex_home()/auth.json`). Parse identity.
2. If it parses and matches a managed slot (`accountId` + `email`, or
   `auth_fingerprint` equality with that slot's stored bytes): copy the live
   bytes into that slot's `auth.json` (the live file is the newest
   generation). If it parses and matches **no** slot: raise
   `CodexSwitchError("The live Codex login (<email>) is not managed. Run
   'openswap codex add' first, or pass --force to overwrite it.")` unless
   `force`. Absent or unparseable live file: proceed.
3. If the target is the current slot: return `{"switched": False, "from":
   ref, "to": ref, "reason": "already-active", "warnings": []}`.
4. Write the target slot's bytes to the live path atomically (temp file in
   the same dir, `os.chmod(0o600)`, `os.replace`), create `~/.codex` (0700)
   if missing.
5. Set `activeAccountNumber`, `lastUpdated`. Return `{"switched": True,
   "from": ref|None, "to": ref, "reason": "switched", "warnings": []}`.

`switch(strategy)`: `None` → next slot in `sequence` after the active one,
skipping disabled slots (wraps); `"best"` → highest `oauth.account_headroom`
over store entries (`fetch=None`), ties to the lowest slot; `"next-available"`
→ rotation order, first slot whose headroom is `None` or `> 0`. Anything else
→ `ValueError`.

`add_account(alias=None) -> str`: read live `auth.json`; `None` identity →
`CodexAuthError("No Codex login found at <path>. Run 'codex login' first.")`;
already managed → `CodexAuthError("<email> is already account <n>")`; else
next free number, `slots/<n>/auth.json` written 0600, roster record,
`activeAccountNumber = n`. Returns the number.

### Usage read

`accounts_snapshot(fetch)` mirrors `_collect_usage_entries`: identities
`{n: (email, accountId)}`; static sentinels `USAGE_API_KEY` for API-key
slots and `USAGE_NO_CREDENTIALS` for a slot whose `auth.json` is missing or
unparseable; `reserve(requested, identities, respect_plans=(fetch is None))`;
for each claimed slot call `read_rate_limits(home, codex_bin=...)` where
`home` is the live `codex_home()` when the slot is active and
`slots/<n>` otherwise; success → `FetchRecord(usage=rate_limits_to_usage(...))`,
failure → `FetchRecord(error="app-server")`; `record(records, identities,
claims, None)`; then `entries(identities)`. After a read against the live
home, copy the live bytes into the active slot (Codex may have refreshed).
`codex` missing from PATH → every requested slot records
`FetchRecord(error="codex-not-installed")` and the CLI prints one hint.

## Steps

### Phase A — seam, Codex engine, CLI

### Step 1: Drift check and branch

Run the drift check. Create `advisor/009-codex-cli-support` from `main`.

**Verify**: `uv run pytest -q` is green before any change.

### Step 2: Protocol + move freshen into the Claude engine

Files: create `src/openswap/engine/protocol.py` (as in Design), create
`src/openswap/engine/freshen.py`, modify `engine/engine.py`,
`engine/__init__.py`, `autoswitch.py`, `models.py`; tests in
`tests/test_engine_abi.py`, `tests/test_autoswitch.py`.

Failing tests first (`tests/test_engine_abi.py`):

```python
from openswap.engine.protocol import AccountEngine

def test_engine_satisfies_account_engine_protocol():
    s = _linux_engine()
    assert isinstance(s, AccountEngine)
    assert s.provider == "claude"

def test_account_snapshot_defaults_to_claude_provider():
    entry = UsageEntry()
    snap = AccountSnapshot("1", "a@x.com", "", "", True, "oauth", True, entry)
    assert snap.provider == "claude"
```

And in `tests/test_autoswitch.py`, next to the existing freshen tests
(search `_freshen_target`): keep them, they must stay green, and add

```python
def test_freshen_delegates_to_engine(harness, monkeypatch):
    calls = []
    monkeypatch.setattr(
        harness.engine.switcher, "freshen_backup",
        lambda number, email: calls.append((number, email)) or "ok",
    )
    assert harness.engine._freshen_target("2", "b@x.com") == "ok"
    assert calls == [("2", "b@x.com")]
```

(`harness.engine` is the `AutoSwitchEngine`; if the harness names it
differently, use its attribute.)

Implementation:

- `engine/freshen.py`: `class FreshenMixin` with `freshen_backup(self,
  number, email) -> str` = the current `AutoSwitchEngine._freshen_target`
  body with `self.switcher.` → `self.` and `self.clock()` → `time.time()`,
  plus `_note_token_identity` moved verbatim. Move `FRESHEN_BUFFER_MS` and
  `_SYSTEMIC_STATUSES` here; `autoswitch.py` imports them from
  `openswap.engine.freshen` (keeps `autoswitch.FRESHEN_BUFFER_MS` readable
  by tests).
- `Engine` adds `FreshenMixin` to its bases and `provider = "claude"`.
- `AutoSwitchEngine._freshen_target` → `return
  self.switcher.freshen_backup(number, email)`; delete
  `_note_token_identity` from autoswitch.
- `models.AccountSnapshot`: add `provider: str = "claude"` as the last field.
- `engine/__init__.py`: export `AccountEngine`.

**Verify**: `uv run pytest tests/test_engine_abi.py tests/test_autoswitch.py
tests/test_switcher.py -q` green. Commit.

### Step 3: `codex/auth.py` (pure)

Create `src/openswap/codex/__init__.py` (with `CODEX_NUM_PREFIX`,
`split_provider_num`) and `src/openswap/codex/auth.py`.

Failing tests (`tests/test_codex_auth.py`):

```python
import base64, json
from pathlib import Path
from openswap.codex import split_provider_num
from openswap.codex.auth import (
    CodexIdentity, auth_fingerprint, auth_path, codex_home, decode_jwt_claims,
    parse_auth,
)

def _jwt(claims: dict) -> str:
    def b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"{b64({'alg': 'none'})}.{b64(claims)}.sig"

def _auth(email="a@x.com", account_id="acc-1", plan="plus", refresh="rt-1") -> str:
    claims = {"email": email, "https://api.openai.com/auth": {
        "chatgpt_account_id": account_id, "chatgpt_plan_type": plan}}
    return json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"id_token": _jwt(claims), "access_token": "at",
                   "refresh_token": refresh, "account_id": account_id},
        "last_refresh": "2026-09-10T00:00:00Z",
    })

def test_codex_home_env_and_default(monkeypatch, tmp_path):
    assert codex_home({"CODEX_HOME": str(tmp_path)}) == tmp_path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert codex_home({}) == tmp_path / ".codex"
    assert auth_path(tmp_path) == tmp_path / "auth.json"

def test_decode_jwt_claims_tolerates_garbage():
    assert decode_jwt_claims("not.a.jwt") == {}
    assert decode_jwt_claims("") == {}
    assert decode_jwt_claims(_jwt({"email": "e"}))["email"] == "e"

def test_parse_auth_oauth_identity():
    ident = parse_auth(_auth())
    assert ident == CodexIdentity(email="a@x.com", account_id="acc-1",
                                  plan_type="plus", kind="oauth")

def test_parse_auth_api_key_identity():
    text = json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk-x", "tokens": None})
    ident = parse_auth(text)
    assert ident.kind == "api_key" and ident.email == "" and ident.account_id == ""

def test_parse_auth_returns_none_for_junk():
    assert parse_auth("") is None
    assert parse_auth("{}") is None
    assert parse_auth("{not json") is None

def test_fingerprint_tracks_refresh_token():
    assert auth_fingerprint(_auth(refresh="a")) == auth_fingerprint(_auth(refresh="a", plan="pro"))
    assert auth_fingerprint(_auth(refresh="a")) != auth_fingerprint(_auth(refresh="b"))
    assert auth_fingerprint("") is None

def test_split_provider_num():
    assert split_provider_num("codex:2") == ("codex", "2")
    assert split_provider_num(3) == ("claude", "3")
    assert split_provider_num("3") == ("claude", "3")
```

Implementation (`codex/auth.py`):

```python
"""Codex CLI ``auth.json``: location, identity, fingerprint. Pure; no I/O beyond paths."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

CODEX_HOME_ENV = "CODEX_HOME"
AUTH_FILENAME = "auth.json"
OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


@dataclass(frozen=True)
class CodexIdentity:
    email: str
    account_id: str
    plan_type: str
    kind: str  # "oauth" | "api_key"


def codex_home(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    value = env.get(CODEX_HOME_ENV)
    return Path(value) if value else Path.home() / ".codex"


def auth_path(home: Path) -> Path:
    return home / AUTH_FILENAME


def decode_jwt_claims(token: str) -> dict:
    """Payload of a JWT without verifying it; ``{}`` on any malformation."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load(text: str) -> dict | None:
    try:
        data = json.loads(text) if text else None
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def parse_auth(text: str) -> CodexIdentity | None:
    data = _load(text)
    if not data:
        return None
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    id_token = tokens.get("id_token") if tokens else None
    if isinstance(id_token, str) and id_token:
        claims = decode_jwt_claims(id_token)
        auth = claims.get(OPENAI_AUTH_CLAIM) or {}
        account_id = tokens.get("account_id") or auth.get("chatgpt_account_id") or ""
        return CodexIdentity(
            email=str(claims.get("email") or ""),
            account_id=str(account_id),
            plan_type=str(auth.get("chatgpt_plan_type") or ""),
            kind="oauth",
        )
    if data.get("auth_mode") == "apiKey" or data.get("OPENAI_API_KEY"):
        return CodexIdentity(email="", account_id="", plan_type="", kind="api_key")
    return None


def auth_fingerprint(text: str) -> str | None:
    """Refresh-token hash when present, full-content hash otherwise, None for empty."""
    if not text:
        return None
    data = _load(text) or {}
    tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    refresh = tokens.get("refresh_token") if tokens else None
    if isinstance(refresh, str) and refresh:
        return "sha256:" + hashlib.sha256(refresh.encode()).hexdigest()
    return "sha256-full:" + hashlib.sha256(text.encode()).hexdigest()
```

**Verify**: `uv run pytest tests/test_codex_auth.py -q` green. Commit.

### Step 4: `codex/usage.py` (app-server driver + mapping)

Failing tests (`tests/test_codex_usage.py`):

```python
import io, json
from pathlib import Path
import pytest
from openswap.codex.usage import CodexUsageError, rate_limits_to_usage, read_rate_limits

_NOW = 1_788_000_000.0

def test_rate_limits_to_usage_maps_windows_by_duration():
    payload = {
        "primary": {"usedPercent": 42, "windowDurationMins": 300, "resetsAt": 1788265323},
        "secondary": {"usedPercent": 61, "windowDurationMins": 10080, "resetsAt": 1788765541},
    }
    usage = rate_limits_to_usage(payload, _NOW)
    assert usage["five_hour"]["pct"] == 42.0
    assert usage["five_hour"]["resets_at"] == "2026-09-01T12:22:03+00:00"
    assert "countdown" in usage["five_hour"] and "clock" in usage["five_hour"]
    assert usage["seven_day"]["pct"] == 61.0

def test_rate_limits_to_usage_tolerates_missing_secondary():
    usage = rate_limits_to_usage({"primary": {"usedPercent": 5, "windowDurationMins": 300}, "secondary": None}, _NOW)
    assert usage == {"five_hour": {"pct": 5.0}}

def test_rate_limits_to_usage_none_when_empty():
    assert rate_limits_to_usage({}, _NOW) is None
    assert rate_limits_to_usage({"primary": None, "secondary": None}, _NOW) is None


class FakeProc:
    """Scripted app-server: records stdin lines, replays stdout lines."""
    def __init__(self, replies: list[str]):
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(line + "\n" for line in replies))
        self.terminated = False
        self.returncode = None
    def terminate(self): self.terminated = True
    def wait(self, timeout=None): self.returncode = 0; return 0
    def kill(self): self.terminated = True

def test_read_rate_limits_handshake_then_request(tmp_path: Path):
    limits = {"primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 1}, "secondary": None}
    proc = FakeProc([
        json.dumps({"method": "serverNotice", "params": {}}),          # noise before the reply
        json.dumps({"id": 1, "result": {"userAgent": "codex"}}),
        json.dumps({"id": 2, "result": {"rateLimits": limits}}),
    ])
    captured = {}
    def popen(argv, **kwargs):
        captured["argv"] = argv; captured["env"] = kwargs["env"]; return proc
    result = read_rate_limits(tmp_path, codex_bin="/opt/codex", popen=popen, environ={"PATH": "/usr/bin"})
    assert result == limits
    assert captured["argv"] == ["/opt/codex", "app-server"]
    assert captured["env"]["CODEX_HOME"] == str(tmp_path)
    sent = [json.loads(l) for l in proc.stdin.getvalue().splitlines()]
    assert sent[0]["method"] == "initialize" and sent[0]["id"] == 1
    assert sent[1] == {"method": "initialized"}
    assert sent[2]["method"] == "account/rateLimits/read" and sent[2]["id"] == 2
    assert proc.terminated

def test_read_rate_limits_error_reply_raises(tmp_path: Path):
    proc = FakeProc([json.dumps({"id": 1, "result": {}}), json.dumps({"id": 2, "error": {"message": "unauthorized"}})])
    with pytest.raises(CodexUsageError, match="unauthorized"):
        read_rate_limits(tmp_path, codex_bin="/opt/codex", popen=lambda *a, **k: proc)

def test_read_rate_limits_eof_raises(tmp_path: Path):
    proc = FakeProc([])
    with pytest.raises(CodexUsageError):
        read_rate_limits(tmp_path, codex_bin="/opt/codex", popen=lambda *a, **k: proc)
```

Implementation (`codex/usage.py`):

```python
"""Codex rate limits via ``codex app-server`` (JSON-RPC over stdio), no quota spent."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from openswap import oauth
from openswap.codex.auth import CODEX_HOME_ENV

APP_SERVER_TIMEOUT_S = 20.0
CLIENT_INFO = {"name": "openswap", "title": "OpenSwap", "version": "0.1.0"}
FIVE_HOUR_MAX_MINS = 600  # a window this short or shorter is the 5h bucket


class CodexUsageError(Exception):
    """app-server did not answer ``account/rateLimits/read``."""


def _window(entry: object) -> dict | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("usedPercent"), (int, float)):
        return None
    out = {"pct": float(entry["usedPercent"])}
    ts = entry.get("resetsAt")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        iso = datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
        out["resets_at"] = iso
        out["countdown"], out["clock"] = oauth.format_reset(iso)
    return out


def rate_limits_to_usage(payload: dict, now: float) -> dict | None:
    """``rateLimits`` → OpenSwap usage dict (``five_hour`` / ``seven_day``)."""
    usage: dict = {}
    for key in ("primary", "secondary"):
        entry = payload.get(key) if isinstance(payload, dict) else None
        window = _window(entry)
        if window is None:
            continue
        mins = entry.get("windowDurationMins")
        label = "five_hour" if isinstance(mins, (int, float)) and mins <= FIVE_HOUR_MAX_MINS else "seven_day"
        usage.setdefault(label, window)
    return usage or None


def _send(proc, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _await(proc, wanted_id: int) -> dict:
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(msg, dict) and msg.get("id") == wanted_id:
            if "error" in msg:
                err = msg["error"]
                text = err.get("message") if isinstance(err, dict) else str(err)
                raise CodexUsageError(f"app-server error: {text}")
            return msg.get("result") or {}
    raise CodexUsageError("app-server closed before answering")


def read_rate_limits(
    home: Path,
    *,
    codex_bin: str,
    popen: Callable[..., object] = subprocess.Popen,
    environ: Mapping[str, str] | None = None,
    timeout: float = APP_SERVER_TIMEOUT_S,
) -> dict:
    """``rateLimits`` for the login in ``home`` (its ``CODEX_HOME``)."""
    env = dict(os.environ if environ is None else environ)
    env[CODEX_HOME_ENV] = str(home)
    proc = popen(
        [codex_bin, "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, env=env, cwd=str(home),
    )
    try:
        _send(proc, {"id": 1, "method": "initialize", "params": {"clientInfo": CLIENT_INFO}})
        _await(proc, 1)
        _send(proc, {"method": "initialized"})
        _send(proc, {"id": 2, "method": "account/rateLimits/read", "params": {}})
        result = _await(proc, 2)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    limits = result.get("rateLimits")
    if not isinstance(limits, dict):
        raise CodexUsageError("app-server reply had no rateLimits")
    return limits
```

`timeout` is enforced by the caller running the read inside the existing
`ThreadPoolExecutor` pattern of the Claude engine, or, simpler and
sufficient here: wrap the whole body in `threading.Timer(timeout,
proc.kill)` started before `_send` and cancelled in `finally`. Add that
timer; a test with a `FakeProc` whose `stdout` blocks is not required.

**Manual verify (macOS, Codex logged in)**: run the "Real usage read" command
from the table. Expected: a dict with `primary`. If the server answers
`id:1` but rejects the `initialized` notification with an error line (no
`id`, so `_await` ignores it) that is fine; if `id:2` errors with a method
name, STOP and report the exact reply.

**Verify**: `uv run pytest tests/test_codex_usage.py -q` green. Commit.

### Step 5: `CodexEngine`

Create `src/openswap/codex/engine.py`. Constructor:

```python
class CodexEngine:
    provider = "codex"

    def __init__(
        self,
        *,
        backup_dir: Path | None = None,        # default paths.get_backup_root()
        home: Path | None = None,              # default codex_home()
        codex_bin: Callable[[], str | None] | None = None,  # default shutil.which("codex")
        read_limits: Callable[..., dict] | None = None,     # default usage.read_rate_limits
        clock: Callable[[], float] = time.time,
        debug: bool = False,
    ): ...
```

Attributes: `backup_dir` (the Claude backup root, so autoswitch's default
`state_path` would collide — every caller passes `state_path=self.state_dir /
"autoswitch_state.json"` explicitly, see Step 7), `state_dir = backup_dir /
"codex"`, `sequence_file`, `lock_file = state_dir / ".lock"`, `slots_dir`,
`_usage_store = UsageStore(state_dir / "cache", clock)`, `_logger =
setup_logging(backup_dir, debug=debug)`.

Failing tests (`tests/test_codex_engine.py`). Reuse `_auth()` and `_jwt()`
from `test_codex_auth.py` (import them).

```python
import json
from pathlib import Path
import pytest
from openswap.codex.engine import CodexEngine, CodexAuthError, CodexSwitchError
from openswap.engine.protocol import AccountEngine
from openswap.json_output import USAGE_API_KEY, USAGE_NO_CREDENTIALS
from tests.test_codex_auth import _auth

def _engine(tmp_path, limits=None, codex_bin="/opt/codex"):
    calls = []
    def read_limits(home, *, codex_bin, **kw):
        calls.append(Path(home))
        if isinstance(limits, Exception):
            raise limits
        return limits or {"primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 1788265323},
                          "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 1788765541}}
    home = tmp_path / "codex-home"
    eng = CodexEngine(backup_dir=tmp_path / "backup", home=home,
                      codex_bin=lambda: codex_bin, read_limits=read_limits, clock=lambda: 1_788_000_000.0)
    eng._test_calls = calls
    return eng, home

def _login(home: Path, **kw) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(_auth(**kw))

def test_satisfies_protocol(tmp_path):
    eng, _ = _engine(tmp_path)
    assert isinstance(eng, AccountEngine) and eng.provider == "codex"

def test_add_captures_live_login_into_slot_dir(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a")
    num = eng.add_account()
    assert num == "1"
    slot = eng.slots_dir / "1" / "auth.json"
    assert slot.read_text() == (home / "auth.json").read_text()
    assert oct(slot.stat().st_mode & 0o777) == "0o600"
    assert eng.current_account_number() == "1"
    assert eng.live_identity() == ("a@x.com", "acc-a")
    assert eng.slot_identity("1") == ("a@x.com", "acc-a")

def test_add_refuses_missing_and_duplicate(tmp_path):
    eng, home = _engine(tmp_path)
    with pytest.raises(CodexAuthError, match="codex login"):
        eng.add_account()
    _login(home)
    eng.add_account()
    with pytest.raises(CodexAuthError, match="already"):
        eng.add_account()

def test_switch_to_writes_live_and_captures_outgoing(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a", refresh="rt-a1")
    eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b")
    eng.add_account()
    # Codex refreshed the live (b) token since we captured it:
    _login(home, email="b@x.com", account_id="acc-b", refresh="rt-b2")
    result = eng.switch_to("1", json_output=True)
    assert result["switched"] is True
    assert result["to"] == {"number": "1", "email": "a@x.com"}
    assert result["from"] == {"number": "2", "email": "b@x.com"}
    assert "rt-a1" in (home / "auth.json").read_text()
    assert "rt-b2" in (eng.slots_dir / "2" / "auth.json").read_text()   # newest generation kept
    assert eng.current_account_number() == "1"

def test_switch_to_same_slot_is_already_active(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    assert eng.switch_to("1", json_output=True)["reason"] == "already-active"

def test_switch_refuses_unmanaged_live_login_unless_forced(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a"); eng.add_account()
    _login(home, email="stranger@x.com", account_id="acc-s")
    with pytest.raises(CodexSwitchError, match="not managed"):
        eng.switch_to("1", json_output=True)
    assert eng.switch_to("1", json_output=True, force=True)["switched"] is True

def test_rotate_skips_disabled_and_wraps(tmp_path):
    eng, home = _engine(tmp_path)
    for e in ("a", "b", "c"):
        _login(home, email=f"{e}@x.com", account_id=f"acc-{e}"); eng.add_account()
    eng.switch_to("1", json_output=True)
    eng.set_account_disabled("2", True)
    assert eng.switch(json_output=True)["to"]["number"] == "3"
    assert eng.switch(json_output=True)["to"]["number"] == "1"
    assert eng.switchable_account_numbers() == ["1", "3"]

def test_snapshot_reads_idle_slot_from_its_own_home(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com", account_id="acc-a"); eng.add_account()
    _login(home, email="b@x.com", account_id="acc-b"); eng.add_account()
    snap = eng.accounts_snapshot(fetch=None)
    assert [a.number for a in snap.accounts] == ["1", "2"]
    assert snap.active_number == "2"
    assert all(a.provider == "codex" for a in snap.accounts)
    assert snap.accounts[0].usage.last_good["five_hour"]["pct"] == 10.0
    assert snap.accounts[0].org_name == "plus" and snap.accounts[0].org_uuid == "acc-a"
    assert set(eng._test_calls) == {eng.slots_dir / "1", home}

def test_snapshot_store_only_never_calls_codex(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    eng.accounts_snapshot(fetch=set())
    assert eng._test_calls == []

def test_api_key_slot_is_switchable_but_has_no_bars(tmp_path):
    eng, home = _engine(tmp_path)
    home.mkdir()
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apiKey", "OPENAI_API_KEY": "sk"}))
    eng.add_account()
    snap = eng.accounts_snapshot()
    assert eng.account_kind_for("1") == "api_key"
    assert snap.accounts[0].usage.sentinel == USAGE_API_KEY
    assert snap.accounts[0].switchable is True
    assert eng._test_calls == []          # never asks app-server for an API key

def test_missing_slot_file_is_no_credentials(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    (eng.slots_dir / "1" / "auth.json").unlink()
    snap = eng.accounts_snapshot()
    assert snap.accounts[0].usage.sentinel == USAGE_NO_CREDENTIALS
    assert snap.accounts[0].switchable is False
    assert eng.switchable_account_numbers() == []

def test_snapshot_records_failure_and_keeps_last_good(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    good = eng.accounts_snapshot().accounts[0].usage.last_good
    eng._read_limits = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    eng._usage_store.clock = lambda: 1_788_001_000.0   # past SERVE_TTL_S
    snap = eng.accounts_snapshot()
    assert snap.accounts[0].usage.last_good == good
    assert snap.accounts[0].usage.last_error

def test_codex_not_installed_is_reported_not_raised(tmp_path):
    eng, home = _engine(tmp_path, codex_bin=None)
    _login(home); eng.add_account()
    snap = eng.accounts_snapshot()
    assert snap.accounts[0].usage.last_error == "codex-not-installed"

def test_freshen_backup_is_ok(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home); eng.add_account()
    assert eng.freshen_backup("1", "a@x.com") == "ok"

def test_alias_and_remove(tmp_path):
    eng, home = _engine(tmp_path)
    _login(home, email="a@x.com"); eng.add_account()
    eng.set_alias("1", "work")
    assert eng.list_aliases() == [("1", "work", "a@x.com")]
    assert eng.resolve_account("work")[0] == "1"
    eng.remove_account("work", assume_yes=True)
    assert eng.accounts_snapshot(fetch=set()).accounts == ()
    assert not (eng.slots_dir / "1").exists()
```

Implementation notes for `codex/engine.py` (write it; the tests above are
the spec):

- Exceptions `CodexAuthError`, `CodexSwitchError` subclass
  `openswap.exceptions.ClaudeSwitchError` so the CLI's existing error
  rendering applies unchanged.
- Roster helpers: `_read_roster() -> dict` (init file if missing, same shape
  as the Data-on-disk block), `_write_roster(data)` via
  `settings.atomic_write_json`, all mutations under `FileLock(self.lock_file)`
  (`openswap.locking.FileLock`, same usage as `autoswitch._state_lock`).
- `_slot_auth_path(num) = slots_dir / num / "auth.json"`;
  `_write_slot(num, text)`: mkdir 0700, temp file + `os.chmod(0o600)` +
  `os.replace`. `_write_live(text)`: same into `auth_path(self.home)`.
- `_live_text() -> str` ("" when missing), `live_identity()` →
  `(email, account_id)` or `None`; `_find_slot(identity, text) -> str | None`
  matches on `(email, accountId)` first, then `auth_fingerprint` equality
  against each slot's bytes.
- `resolve_account(identifier)`: number → alias → email, ambiguity raises
  `ConfigError` like `IdentityMixin._resolve_account_identifier`.
- `accounts_snapshot`, `usage_entries_by_account`, `set_poll_policy_inputs`
  (store the tuple; unused otherwise), `read_account_credentials(num,
  email)` → slot bytes or "", `account_email`, `account_kind_for`,
  `slot_identity`, `has_live_login`, `current_account_number`,
  `switchable_account_numbers` (roster order, slot has `auth.json`, not
  disabled), `set_account_disabled`, `set_alias`/`unset_alias`/`list_aliases`
  (use `models.normalize_alias`), `remove_account` (rmtree the slot dir,
  clear `activeAccountNumber` if it was active), `list_accounts(
  show_token_status=False, json_output=False, fetch=None)` printing a table
  with the same columns as the Claude one (`#`, email, plan tag, 5h, 7d,
  active marker), returning `{"schemaVersion": 1, "provider": "codex",
  "accounts": [json_output.account_row(...)]}` in JSON mode with
  `organizationName` = plan type.
- Store the injected callables as `self._codex_bin` and `self._read_limits`
  (tests override `_read_limits` directly).
- `kind` sentinels: `USAGE_API_KEY` for `kind == "api_key"`,
  `USAGE_NO_CREDENTIALS` when the slot file is missing/unparseable; a slot
  in either state is never passed to `reserve`. `switchable` is `True` only
  when the slot file exists and parses (API-key slots are switchable, like
  Claude's `add-token` slots). Everything else goes through `reserve` →
  `_read_limits` → `record`.
- After any `read_limits(home=self.home, ...)` (active slot), call
  `_capture_live(num)` which copies the live bytes into the slot when the
  fingerprint or bytes differ.

**Verify**: `uv run pytest tests/test_codex_engine.py -q` green; `uv run
pytest tests/test_real_store_guard.py -q` green (the guard must not see a
write outside `tmp_path`). Commit.

### Step 6: CLI verbs and the Codex section in `list`

Modify `src/openswap/cli.py`. Add `_codex_command(argv)` pre-dispatched
right after the `auto` branch in `main()`:

```
openswap codex add [--alias NAME]
openswap codex list [--json]
openswap codex switch [<num|email|alias>] [--strategy best|next-available] [--force]
openswap codex remove <num|email|alias> [-y]
openswap codex disable|enable <num|email|alias>
openswap codex alias <num|email|alias> <name> | --unset
```

argparse with `sub = parser.add_subparsers(dest="verb")`, one function per
verb calling the matching `CodexEngine` method; `--json` on `list` and
`switch` prints `json.dumps(payload)`; errors are rendered through the same
`except ClaudeSwitchError` path the main parser uses (copy the two lines).
Exit codes: 0 success, 1 error, 2 "no action" for `switch` that returned
`switched=False`.

`openswap list` (`cli.py:1319` region): after the Claude listing, when not
`--json` and `CodexEngine().accounts_snapshot(fetch=set()).accounts` is
non-empty, print a blank line, an accent header `Codex`, and
`CodexEngine().list_accounts()`. `--json` output of `openswap list` is
unchanged (schema stability); `openswap codex list --json` is the JSON path.

Add `codex` lines to the `main()` help text under `auto`, and to the
README command table (Step 12 does docs; the help text is code, do it here).

Failing tests (`tests/test_codex_cli.py`), following `tests/test_cli.py`'s
pattern of `monkeypatch.setattr(sys, "argv", [...])` + `main()` +
`SystemExit`:

```python
import json
import sys
import pytest
from openswap.cli import main
from tests.test_codex_auth import _auth

def run_cli(args: list[str]) -> int:
    """Drive ``main()`` like a shell would; return the exit code (0 when it returns)."""
    sys.argv = ["openswap", *args]
    try:
        main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0

def test_codex_add_list_switch_roundtrip(temp_home, capsys, monkeypatch):
    home = temp_home / ".codex"; home.mkdir()
    (home / "auth.json").write_text(_auth(email="a@x.com", account_id="acc-a"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("openswap.codex.engine.read_rate_limits", lambda *a, **k: {"primary": {"usedPercent": 3, "windowDurationMins": 300}})
    monkeypatch.setattr("shutil.which", lambda name: "/opt/codex" if name == "codex" else None)
    run_cli(["codex", "add"])                       # helper: sets argv, calls main, returns exit code
    (home / "auth.json").write_text(_auth(email="b@x.com", account_id="acc-b"))
    run_cli(["codex", "add", "--alias", "b"])
    assert run_cli(["codex", "switch", "1"]) == 0
    assert "a@x.com" in (home / "auth.json").read_text()
    out = capsys.readouterr().out
    assert "a@x.com" in out and "b@x.com" in out
    assert run_cli(["codex", "switch", "1"]) == 2   # already active

def _one_codex_account(temp_home, monkeypatch):
    home = temp_home / ".codex"; home.mkdir()
    (home / "auth.json").write_text(_auth(email="c@x.com", account_id="acc-c"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("openswap.codex.engine.read_rate_limits", lambda *a, **k: {"primary": {"usedPercent": 3, "windowDurationMins": 300}})
    monkeypatch.setattr("shutil.which", lambda name: "/opt/codex" if name == "codex" else None)
    assert run_cli(["codex", "add"]) == 0

def test_list_shows_codex_section_only_when_present(temp_home, capsys, monkeypatch):
    run_cli(["list"])
    assert "Codex" not in capsys.readouterr().out
    _one_codex_account(temp_home, monkeypatch)
    capsys.readouterr()
    run_cli(["list"])
    out = capsys.readouterr().out
    assert "Codex" in out and "c@x.com" in out

def test_list_json_schema_unchanged(temp_home, capsys, monkeypatch):
    _one_codex_account(temp_home, monkeypatch)
    capsys.readouterr()
    run_cli(["list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "codex" not in payload and "provider" not in payload
    assert all("c@x.com" != row.get("email") for row in payload.get("accounts", []))

def test_codex_list_json_has_provider(temp_home, capsys, monkeypatch):
    _one_codex_account(temp_home, monkeypatch)
    capsys.readouterr()
    run_cli(["codex", "list", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "codex"
    assert payload["accounts"][0]["email"] == "c@x.com"
    assert payload["accounts"][0]["organizationName"] == "plus"
```

`temp_home` must isolate `~/.codex`: extend `conftest._isolate_real_home` to
also `monkeypatch.delenv("CODEX_HOME", raising=False)` (its fake `$HOME`
already relocates the default `~/.codex`).

**Verify**: `uv run pytest tests/test_codex_cli.py tests/test_cli.py -q`
green; `uv run pytest -q` green. Commit. **Phase A gate.**

### Phase B — autoswitch and kickoff

### Step 7: `openswap auto` hosts a second engine

In `_auto_command`, after the Claude `AutoSwitchEngine` is built:

```python
codex = CodexEngine(debug=args.debug)
codex_engine = None
if codex.switchable_account_numbers():
    codex_engine = AutoSwitchEngine(
        codex, settings, _prefixed(emit, "codex"), dry_run=args.dry_run,
        state_path=codex.state_dir / "autoswitch_state.json",
    )
```

where `_prefixed(emit, provider)` wraps every event: set
`event.provider = provider` (add `provider: str = "claude"` to
`AutoSwitchEvent`, included in `to_json()` and prefixed in `human()` as
`[codex] `). Loop mode: run `codex_engine.run_loop()` in a daemon thread,
`stop()` both on SIGINT/SIGTERM. `--once`: `codex_engine.tick()` after the
Claude tick; the exit code stays the Claude outcome (document in the
epilog: "Codex rotation runs alongside; its outcome is logged, not
returned").

Failing tests (`tests/test_autoswitch.py`, new class
`TestCodexRotation` using a `CodexEngine` seeded like Step 5's helper, with
`_entry_for` usage injected through `monkeypatch.setattr(codex,
"usage_entries_by_account", ...)` the way `EngineHarness.tick_with_entries`
does):

```python
from openswap.codex.engine import CodexEngine
from openswap.settings import AutoSwitchSettings
from tests.test_codex_auth import _auth

def codex_harness(tmp_path, clock):
    """Two OAuth Codex slots, slot 1 live; app-server never called (usage is injected)."""
    home = tmp_path / "codex-home"
    eng = CodexEngine(backup_dir=tmp_path / "backup", home=home,
                      codex_bin=lambda: "/opt/codex",
                      read_limits=lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")),
                      clock=clock)
    home.mkdir()
    (home / "auth.json").write_text(_auth(email="a@x.com", account_id="acc-a", refresh="rt-a"))
    eng.add_account()
    (home / "auth.json").write_text(_auth(email="b@x.com", account_id="acc-b", refresh="rt-b"))
    eng.add_account()
    eng.switch_to("1", json_output=True)
    return eng, home

def _codex_auto(eng, clock, events):
    return AutoSwitchEngine(eng, AutoSwitchSettings(), events.append, dry_run=False,
                            state_path=eng.state_dir / "autoswitch_state.json", clock=clock)

def test_codex_engine_switches_when_over_threshold(tmp_path, monkeypatch):
    clock = FakeClock(); events = []
    eng, home = codex_harness(tmp_path, clock)
    now = clock()
    entries = {"1": _entry_for(_usage(95.0), now), "2": _entry_for(_usage(10.0), now)}
    monkeypatch.setattr(eng, "usage_entries_by_account", lambda fetch=None, *, scheduled=False: entries)
    outcome = _codex_auto(eng, clock, events).tick()
    assert outcome is TickOutcome.SWITCHED
    assert eng.current_account_number() == "2"
    assert "rt-b" in (home / "auth.json").read_text()
    assert (eng.state_dir / "autoswitch_state.json").exists()
    assert not (tmp_path / "backup" / "autoswitch_state.json").exists()   # never the Claude file
    assert [e.kind for e in events if e.kind == "switch"] and all(e.provider == "codex" for e in events)

def test_codex_slot_is_never_quarantined_for_missing_claude_oauth(tmp_path, monkeypatch):
    clock = FakeClock(); events = []
    eng, home = codex_harness(tmp_path, clock)
    now = clock()
    entries = {"1": _entry_for(_usage(95.0), now), "2": _entry_for(_usage(10.0), now)}
    monkeypatch.setattr(eng, "usage_entries_by_account", lambda fetch=None, *, scheduled=False: entries)
    _codex_auto(eng, clock, events).tick()
    assert not [e for e in events if e.kind == "quarantine"]
```

(`FakeClock`, `_usage`, `_entry_for`, `TickOutcome` are the module's existing
helpers; `AutoSwitchSettings()` defaults: threshold 90, hysteresis 10, so
95 → 10 is a proactive switch.)

**Verify**: `uv run pytest tests/test_autoswitch.py -q` green. Commit.

### Step 8: Kickoff for Codex slots

Modify `src/openswap/kickoff.py`: generalize the builders without changing
the Claude call sites.

```python
def build_kickoff_argv(claude_bin: str) -> list[str]:            # unchanged
def build_codex_kickoff_argv(codex_bin: str) -> list[str]:
    return [codex_bin, "exec", KICKOFF_PROMPT]

def build_codex_kickoff_env(home, environ=None) -> dict[str, str]:
    """``CODEX_HOME`` pinned to the slot home; ``None`` pings the live login."""
    src = os.environ if environ is None else environ
    env = {k: v for k, v in src.items() if k != "OPENAI_API_KEY"}
    if home is not None:
        env["CODEX_HOME"] = str(home)
    else:
        env.pop("CODEX_HOME", None)
    return env

def invoke_codex_kickoff(home=None, *, which=None, run=None, timeout=KICKOFF_TIMEOUT_S, environ=None):
    # mirrors invoke_kickoff: shutil.which("codex") → SessionError("'codex' was not found on PATH...")
    # argv = build_codex_kickoff_argv(bin); env = build_codex_kickoff_env(home, environ)
    # cwd = str(home) if home else None; returning subprocess.run(..., capture_output=True, text=True, timeout=timeout, check=False)
```

Tests (`tests/test_kickoff.py`), mirroring
`test_invoke_kickoff_print_argv_session_dir_and_returning_subprocess`:

```python
def test_invoke_codex_kickoff_exec_argv_and_home(tmp_path):
    captured = {}
    def fake_which(name): return "/opt/fake/codex" if name == "codex" else None
    def fake_run(argv, **kw):
        captured["argv"] = list(argv); captured["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    invoke_codex_kickoff(tmp_path, which=fake_which, run=fake_run, environ={"PATH": "/usr/bin", "OPENAI_API_KEY": "sk"})
    assert captured["argv"] == ["/opt/fake/codex", "exec", KICKOFF_PROMPT]
    assert captured["kw"]["env"]["CODEX_HOME"] == str(tmp_path)
    assert "OPENAI_API_KEY" not in captured["kw"]["env"]

def test_invoke_codex_kickoff_live_login_has_no_codex_home():
    captured = {}
    def fake_run(argv, **kw):
        captured["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    invoke_codex_kickoff(None, which=lambda n: "/opt/fake/codex", run=fake_run,
                         environ={"PATH": "/usr/bin", "CODEX_HOME": "/elsewhere"})
    assert "CODEX_HOME" not in captured["kw"]["env"]
    assert captured["kw"]["cwd"] is None

def test_invoke_codex_kickoff_missing_binary_raises():
    with pytest.raises(SessionError, match="codex"):
        invoke_codex_kickoff(None, which=lambda n: None, run=lambda *a, **k: None)

def test_invoke_codex_kickoff_source_uses_subprocess_not_exec():
    src = inspect.getsource(invoke_codex_kickoff); assert "os.exec" not in src
```

**Verify**: `uv run pytest tests/test_kickoff.py -q` green; full suite
green. Commit. **Phase B gate.**

### Phase C — extra and widget

### Step 9: Snapshot adapter, cards, widget payload (pure, no AppKit)

Modify `menubar_display.py`, `widget_snapshot.py`; tests in
`tests/test_menubar.py`, `tests/test_widget_snapshot.py` (both must stay
free of rumps/AppKit imports).

- `_adapt_snapshot(snap, codex_snap=None)`: Claude rows unchanged; when
  `codex_snap` is given, append one 9-tuple per Codex account with
  `num = f"codex:{acc.number}"`, `org_name = acc.org_name` (plan type),
  and register `identities["codex:n"]`, `kinds["codex:n"]`. `active_*`
  keys keep describing the Claude login only. Add `"codex_active_num":
  str | None`.
- `panel_accounts`: for rows whose num is `codex:`-prefixed, title/subtitle
  come from `account_card_names` as today, then `title = f"Codex · {title}"`
  and the card gets `"provider": "codex"`; Claude cards get
  `"provider": "claude"`.
- `widget_snapshot.build_combined(accounts, now)`: only Claude cards feed
  `combined_window` (filter `card.get("provider") != "codex"`).
- `widget_snapshot.parse_switch_command` is unchanged (`"codex:2"` is a
  valid non-empty string).

Tests:

```python
# tests/test_menubar.py
def test_adapt_snapshot_appends_codex_rows_namespaced():
    claude = AccountsSnapshot(active_number="2", taken_at=0.0, accounts=(
        AccountSnapshot("1", "a@x.com", "", "", False, "oauth", True, UsageEntry()),
        AccountSnapshot("2", "b@x.com", "Ads Online", "org-b", True, "oauth", True, UsageEntry()),))
    codex = AccountsSnapshot(active_number="1", taken_at=0.0, accounts=(
        AccountSnapshot("1", "c@x.com", "plus", "acc-c", True, "oauth", True, UsageEntry(), provider="codex"),))
    out = _adapt_snapshot(claude, codex)
    assert [row[0] for row in out["accounts"]] == ["1", "2", "codex:1"]
    assert out["accounts"][2][6] == "plus"                 # org_name slot carries the plan
    assert out["kinds"]["codex:1"] == "oauth"
    assert out["identities"]["codex:1"] == ("c@x.com", "acc-c")
    assert out["codex_active_num"] == "1"
    assert out["active_num"] == "2" and out["active_email"] == "b@x.com"   # Claude only

def test_adapt_snapshot_without_codex_is_unchanged():
    claude = AccountsSnapshot(active_number="1", taken_at=0.0, accounts=(
        AccountSnapshot("1", "a@x.com", "", "", True, "oauth", True, UsageEntry()),))
    out = _adapt_snapshot(claude)
    assert [row[0] for row in out["accounts"]] == ["1"]
    assert out["codex_active_num"] is None

def test_panel_accounts_prefixes_codex_title_and_sets_provider():
    snap = {"accounts": [
        (1, "a@x.com", True, _USAGE, _USAGE, "", "", False, None),
        ("codex:1", "c@x.com", True, _USAGE, _USAGE, "", "plus", False, None)]}
    cards = panel_accounts(snap, now=_NOW)
    assert cards[0]["provider"] == "claude"
    assert cards[1]["provider"] == "codex" and cards[1]["title"] == "Codex · plus"
    assert cards[1]["num"] == "codex:1"

# tests/test_widget_snapshot.py
def test_combined_excludes_codex_cards():
    payload = ws.build_widget_payload(snap_with_one_claude_and_one_codex, now=_NOW)
    assert payload["combined"]["five_hour"]["total"] == 1
    assert [c["num"] for c in payload["accounts"]] == ["1", "codex:1"]
```

**Verify**: `uv run pytest tests/test_menubar.py tests/test_widget_snapshot.py -q`
green. Commit.

### Step 10: The extra hosts Codex (switch, rotation, kickoff, menus)

Modify `menubar.py` (and the `cli.py` call that launches it: `run(switcher,
codex=CodexEngine())`; `run(switcher, codex=None)` keeps every existing test
valid).

- `__init__`: `self.codex = codex`; `self._codex_source = SnapshotSource(codex)
  if codex else None`; `self._codex_auth_path = auth_path(codex.home)` and
  `self._codex_auth_mtime = 0.0`; `self._codex_engine = None`.
- `_worker`: `codex_raw = self._codex_source.take(full=full,
  store_only=self._codex_engine is not None)` inside its own try/except
  (a Codex failure must not blank Claude cards; log at debug and pass
  `None`). `snap = _adapt_snapshot(raw, codex_raw)`.
- `_detect_active_change`: also stat `self._codex_auth_path`; a changed
  mtime triggers `refresh_async()` when `codex_live_slot_changed(snapshot,
  codex.current_account_number())` (a pure sibling of `live_slot_changed`
  reading `codex_active_num`; add it to `menubar_display.py` with a unit
  test).
- `_on_account_click(num, *, close_panel)`: `provider, n =
  split_provider_num(num)`; for `"codex"` skip the relogin repair and run
  `self._run_switch(lambda: self.codex.switch_to(n, json_output=True))`,
  then `_finish_manual_switch(result, self._name_for_num(num), ...)`.
  `_finish_manual_switch` and the toast copy must not say "Restart Claude
  Code" for a Codex switch: pass `running=False` for the Codex branch
  (`notification_copy_for_manual_switch(dest_name, running=False)`), and
  append the sentence "Restart Codex to apply." (add
  `codex_restart_hint()` to `menubar_display.py` returning that string,
  with a test).
- `_start_engine` / `_stop_engine` / `_restart_engine`: also build/stop
  `AutoSwitchEngine(self.codex, load_settings(...), <prefixed
  on_event>, dry_run=False, state_path=self.codex.state_dir /
  "autoswitch_state.json")` when `self.codex` is set and
  `self.codex.switchable_account_numbers()` is non-empty; run in its own
  daemon thread. Engine events with `event.provider == "codex"` go through
  the same `_on_engine_event` notify path (the `[codex] ` prefix from Step
  7 is in `human()`), but must not update `_hold_event` (Claude hold line
  only): guard `hold_cache_after_event` with `if getattr(event,
  "provider", "claude") == "claude"`.
- `_run_kickoff`: for rows whose num is `codex:`-prefixed, use
  `kickoff_account_eligible(is_api_key=(kinds.get(num) == "api_key"),
  usage=last_good)` as today, then `invoke_codex_kickoff()` when the slot is
  the Codex active one, else `invoke_codex_kickoff(self.codex.slots_dir / n)`.
  No `SessionManager` for Codex.
- `_add_menu`: add "From current Codex login" → `self.codex.add_account()`
  wrapped like `on_add_login` (notify on `ClaudeSwitchError`), only when
  `self.codex` is set. `_remove_menu` / `_disable_menu`: iterate the
  combined `snapshot["accounts"]`, dispatch by `split_provider_num`.
- `rebuild_menu` title: unchanged (Claude only).

Pure helpers get tests in `tests/test_menubar.py`
(`codex_live_slot_changed`, `codex_restart_hint`, the hold guard via
`hold_cache_after_event` with a codex event). The AppKit wiring is covered
by the manual pass below.

### Step 11: Popover section header

`menubar_panel.py`: when building card views, insert a muted label row
`"Codex"` (`font_small`, `SECTION_H = 18.0`) before the first card whose
`provider == "codex"`, and add `SECTION_H` to the panel height once when any
Codex card exists. Keep the constant next to `FOOTER_H`. No tests (AppKit);
manual pass.

**Manual pass (docs/testing.md "After UI changes")**: restart the extra with
two Claude and two Codex accounts. Popover shows a "Codex" header, Codex
cards titled `Codex · plus`/`pro` with 5h/7d bars; clicking a Codex card
switches (check `cat ~/.codex/auth.json | jq .tokens.account_id` changes),
toast says "Restart Codex to apply." and not "Restart Claude Code". Enable
auto-switch in Settings: `~/Library/Application Support/OpenSwap/codex/
autoswitch_state.json` appears within a minute. Widget "All accounts"
lists the Codex cards; "Combined remaining" total excludes them; tapping a
Codex card switches it. Dark/Light both fine.

**Verify**: full suite green. Commit. **Phase C gate.**

### Step 12: Docs

- `docs/architecture.md`: add the `codex/` package to the ownership table
  ("Codex accounts: roster, slot homes, app-server usage, switch"); add
  `engine/protocol.py` ("consumer-facing surface both engines implement");
  amend "Credentials on macOS are Keychain" to "Claude credentials on macOS
  are Keychain; Codex slots are 0600 `auth.json` files under
  `codex/slots/<n>/` because each slot doubles as a `CODEX_HOME`"; add the
  `codex/` paths to "Data on disk"; add a "Two providers, two rotations"
  paragraph under "Product surface".
- `README.md`: `openswap codex add|list|switch|remove` rows in the command
  table; one sentence in the intro ("and Codex CLI").
- `docs/testing.md`: add `tests/test_codex_*.py` to the "must not import
  rumps/AppKit" table (they never should).

Commit `docs: Codex provider in architecture and README`.

## Test plan

- `test_codex_auth.py`: home resolution, JWT decoding, identity for
  oauth/api-key/junk, fingerprint, `split_provider_num`.
- `test_codex_usage.py`: window mapping (duration → label, null windows,
  ISO reset), app-server handshake order, error reply, EOF, env/argv.
- `test_codex_engine.py`: protocol conformance, add/duplicate/missing,
  switch writes live + captures outgoing newest generation, already-active,
  unmanaged live refusal + force, rotate/disable/wrap, idle slot read from
  its own home, store-only never runs codex, sentinels, failure keeps
  last-good, codex-not-installed, aliases, remove.
- `test_codex_cli.py`: add/list/switch round trip, `list` section
  presence, `list --json` unchanged.
- `test_engine_abi.py`: `Engine` satisfies `AccountEngine`; snapshot
  default provider.
- `test_autoswitch.py`: freshen delegation; Codex rotation switches and
  uses its own state file; no `invalid_grant` quarantine for Codex.
- `test_kickoff.py`: codex exec argv/env/missing binary/no exec.
- `test_menubar.py` / `test_widget_snapshot.py`: namespaced rows, card
  provider + title, combined excludes Codex, `codex_live_slot_changed`,
  `codex_restart_hint`, hold guard.
- Manual: real `read_rate_limits` against `~/.codex`; the extra pass in
  Step 11.

## Done criteria

- [ ] `uv run pytest` exits 0 on macOS
- [ ] `isinstance(Engine(), AccountEngine)` and `isinstance(CodexEngine(), AccountEngine)`
- [ ] `openswap codex add` twice, `openswap codex switch 1`, `openswap list` shows a Codex section with 5h/7d bars for the idle slot
- [ ] `openswap auto` with two Codex slots writes `codex/autoswitch_state.json`, never the Claude one
- [ ] Extra: Codex cards, click-to-switch, "Restart Codex" toast, kickoff pings `codex exec` once per slot per day
- [ ] Widget: Codex cards listed, combined excludes them, tap switches
- [ ] No file under `macos/` changed; no `wham/usage` request anywhere (`grep -r wham src/` is empty)
- [ ] No Keychain item for Codex (`security find-generic-password -s openswap-codex` fails)
- [ ] No files outside scope

## STOP conditions

- Drift check shows `_freshen_target`, `_adapt_snapshot`, `panel_accounts`,
  or `invoke_kickoff` changed shape since `b00dce8`.
- The real `codex app-server` rejects `initialize` or answers
  `account/rateLimits/read` with a "method not found" error (report the
  exact reply; the method name may have moved).
- You find yourself editing `engine/switch.py`, `engine/consume.py`,
  `engine/slots.py`, or `credentials.py` for anything other than the
  `freshen_backup` move.
- You want to write a Codex credential to the Keychain, or copy
  `auth.json` anywhere other than `codex/slots/<n>/` and the live path.
- You want to run `codex exec` more than once per slot per day, or to
  refresh Codex tokens with your own HTTP call.
- A test needs `@pytest.mark.no_keychain_fake`, a real `codex` binary, or
  the developer's `~/.codex`.
- Any Swift change looks necessary.

## Maintenance notes

- Reviewer: the `freshen_backup` move is the one edit to the Claude engine.
  Check it is a pure move (diff `autoswitch.py` deletions against
  `engine/freshen.py` additions) and that the existing freshen tests in
  `test_autoswitch.py` did not need edits beyond attribute names.
- `AccountSnapshot.org_name` carrying the plan type is deliberate reuse so
  every display path (`display_tag`, `account_card_names`, widget subtitle)
  works unchanged. If a later plan wants a distinct field, add it then.
- Follow-ups not in this plan: Swift "Codex" badge on widget cards; a
  per-provider `autoswitch.codex.enabled` knob; `openswap codex
  export/import`; a Codex status line.
- Upstream Codex issue "sequential `codex exec` → token_invalidated":
  kickoff is once per slot per day; if users report Codex re-login after
  kickoff, disable Codex kickoff by default before investigating.
