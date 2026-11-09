# Reset events, phase S1, implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** OpenSwap fires per-account 5-hour and weekly window events (started,
ending soon, reset). It delivers them to a Mac notification and to signed
webhooks, each with its own filter, and has a `openswap events` CLI.

**Architecture:** A pure module (`reset_events.py`) owns the config, the
fired-state file, detection, filtering, payload and signing. A small delivery
module posts webhooks on background threads with bounded retries. The menu
bar calls one detection pass from its 1 s `on_sync_tick` using its existing
snapshot dict, and the CLI edits the same config file. No OpenTag or worker
code in S1 (that's S2).

**Tech Stack:** Python 3 stdlib (`urllib.request`, `hmac`, `hashlib`,
`threading`), rumps (menu bar), macOS Keychain via `openswap.macos_keychain`,
pytest (`uv run pytest`).

**Spec:** [plans/026-reset-events.md](026-reset-events.md). Read it first;
this plan argues from it.

## Global Constraints

- Event types exactly: `window.started`, `window.ending_soon`, `window.reset`.
  Windows exactly: `five_hour`, `seven_day`. `window.started` is 5-hour only.
- Payload keys exactly as the spec's Payload block, plus `"test": true` only
  on `openswap events test`.
- `id` = `"evt_" + sha256("provider|email|org_uuid|window|resets_at_iso|type")`
  hex, first 24 chars. Identical on every Mac for the same moment.
- Defaults: lead time 1800 s (5-hour) and 10800 s (weekly); minimum left 20%.
  The Mac notification destination exists and is off.
- Webhook: HTTPS, or `http` only when the host is `localhost`/`127.0.0.1`/`::1`.
  Header `OpenSwap-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`,
  `Content-Type: application/json`. Three attempts: 0 s, 30 s, 90 s. 10 s timeout
  each. Then drop and record `last_error` on the destination.
- Secrets live in the Keychain (service `OpenSwap reset events`, account =
  destination id), never in JSON.
- Events fire only from the menu bar process. A failed or hanging webhook never
  blocks the UI thread or another destination.
- Never send tokens, credential paths or session content.
- American English in all copy and identifiers. No unnecessary comments.
- Commits follow `feat(events): …` / `docs: …` with no AI attribution.

## Review Focus

1. **App restart mid-window:** a restart must not re-send any event already
   sent. The fired-state file is written before delivery starts.
2. **Mac asleep across a reset:** waking after `resets_at` sends `reset` once
   and never sends a late `ending_soon` for the window that already passed.
3. **First run with existing accounts:** seeding the state must not fire
   `started` for every account's current window.
4. **Account that falls back to last-known-good:** never produces `started`.
   It can produce `ending_soon` only from numbers belonging to the window that
   is ending (`resets_at > now`).
5. **Signed-out or API-key accounts** (no windows): produce no events and no
   errors.

Tests for each are listed in Task 2.

---

## File structure

- Create `src/openswap/reset_events.py`: types, config and state files,
  detection, filter, payload, signing. Pure apart from file I/O.
- Create `src/openswap/reset_events_delivery.py`: URL validation, Keychain
  secret, webhook POST with retries on a daemon thread.
- Modify `src/openswap/menubar.py`: wire detection into `on_sync_tick`, handle
  the new settings rows.
- Modify `src/openswap/menubar_display.py`: settings rows and notification copy.
- Modify `src/openswap/cli.py`: the `openswap events` command.
- Tests: `tests/test_reset_events.py`, `tests/test_reset_events_delivery.py`,
  `tests/test_reset_events_cli.py`, plus additions to the existing settings
  rows test file for the new rows.
- Docs: `README.md` command table row, `CHANGELOG.md` entry, and a webhook
  receiver section (payload, signature verification) in `docs/menubar.md`.

### Task 1: Config and fired-state files

**Files:**
- Create: `src/openswap/reset_events.py`
- Test: `tests/test_reset_events.py`

**Interfaces:**
- Produces:
  - `EVENT_TYPES = ("window.started", "window.ending_soon", "window.reset")`, `WINDOWS = ("five_hour", "seven_day")`
  - `@dataclass(frozen=True) class Filter: accounts: tuple[str, ...] = ()` (empty = all, values are `"provider:email:org_uuid"` keys), `windows: tuple[str, ...] = WINDOWS`, `events: tuple[str, ...] = EVENT_TYPES`, `lead_s: dict[str, int]` (default `{"five_hour": 1800, "seven_day": 10800}`), `min_left_pct: float = 20.0`
  - `@dataclass(frozen=True) class Destination: id: str; kind: str` (`"mac"` | `"webhook"`), `enabled: bool; url: str = ""; filter: Filter; last_error: str = ""`
  - `load_destinations(root: Path) -> list[Destination]`: reads `root / "reset_events.json"`. Missing file → `[Destination(id="mac", kind="mac", enabled=False, filter=Filter())]`. Corrupt file → same default. An entry with an unknown kind or bad value is dropped, others kept.
  - `save_destinations(root: Path, dests: list[Destination]) -> None` via `settings.atomic_write_json`, under a `FileLock` on `root / "reset_events.lock"` (same pattern as `settings._settings_write_lock`).
  - `@dataclass class FiredState: windows: dict[str, float]` (key `"<account_key>|<window>"` → last known `resets_at` ts), `sent: dict[str, float]` (key `"<dest_id>|<event_id>"` → sent-at ts)
  - `load_state(root) -> FiredState` / `save_state(root, state) -> None` on `root / "reset_events_state.json"`. `save_state` prunes `sent` entries older than 8 days.

- [ ] **Step 1: Write failing tests**
  - `test_missing_config_gives_disabled_mac_destination`: default list equals one `mac` destination, `enabled is False`, default `Filter()`.
  - `test_destinations_round_trip`: save two destinations (mac + webhook with a custom filter) and load them; the results are equal.
  - `test_unknown_kind_dropped_others_kept`
  - `test_corrupt_config_falls_back_to_default`
  - `test_state_round_trip_prunes_sent_older_than_8_days`: an entry at `now - 9*86400` is gone after save/load; one at `now - 86400` stays.
- [ ] **Step 2:** `uv run pytest tests/test_reset_events.py -q`. Expect: fail on import.
- [ ] **Step 3: Implement the types and the four functions.** JSON keys are camelCase (`leadS`, `minLeftPct`, `lastError`), like the other OpenSwap files. `save_state` also takes `now: float | None = None`, defaulting to `time.time()`, so tests can pin the clock.
- [ ] **Step 4:** Run the tests. Expect: pass.
- [ ] **Step 5:** Commit: `feat(events): reset event config and state files`.

### Task 2: Detection, filtering, payload, signature

**Files:**
- Modify: `src/openswap/reset_events.py`
- Test: `tests/test_reset_events.py`

**Interfaces:**
- Consumes: Task 1 types.
- Produces:
  - `@dataclass(frozen=True) class AccountReading: provider: str; email: str; org_uuid: str; label: str; org_name: str; is_active: bool; live: bool; usage: dict | None; fetched_at: float | None`, with property `key -> "provider:email:org_uuid"`.
  - `readings_from_snapshot(snapshot: dict) -> list[AccountReading]`: from the menu bar snapshot dict. Rows are `(num, email, is_active, display, last_good, alias, org_name, disabled, fetched_at)`. `identities[num]` is `(email, org_uuid)`. Provider is `"codex"` when `str(num).startswith("codex:")`, else `"claude"`. `live = isinstance(display, dict)`. `usage = display if live else last_good if isinstance(last_good, dict) else None`. `label = alias or email`. Skip rows with no usage dict.
  - `@dataclass(frozen=True) class ResetEvent: type: str; window: str; reading: AccountReading; resets_at: float; used_pct: float; scoped: tuple[dict, ...]`, with property `id`, per Global Constraints. `resets_at_iso` is `datetime.fromtimestamp(resets_at, timezone.utc).isoformat().replace("+00:00", "Z")`.
  - `detect(readings: list[AccountReading], dests: list[Destination], state: FiredState, now: float) -> list[tuple[Destination, ResetEvent]]`: mutates `state` (window tracking and `sent`), and returns what to deliver. Only `enabled` destinations are considered.
  - `matches(dest: Destination, event: ResetEvent, now: float) -> bool`
  - `payload(event: ResetEvent, *, mac: str, now: float, test: bool = False) -> dict`
  - `signature_header(secret: str, body: bytes, t: int) -> str`

**Detection rules** (the algorithm the tests pin; per reading, per window present in `usage` with numeric `pct` and a parseable `resets_at`):

```
k = f"{reading.key}|{window}";  ts = parsed resets_at;  known = state.windows.get(k)
# reset: from state alone, no fetch needed
if known is not None and known <= now:
    emit reset(resets_at=known, used_pct=0)
    state.windows[k] = next weekly boundary (known + n*7d > now) if window == "seven_day" else known
    known = state.windows[k]
# started / seed: only from live readings
if reading.live and ts > now and (known is None or ts > known):
    if known is not None and window == "five_hour" and pct < 100:
        emit started(resets_at=ts, used_pct=pct)
    state.windows[k] = ts            # known is None → seed silently
# ending soon: per destination, from the numbers for the window that is ending
if ts > now: candidate ending_soon(resets_at=ts, used_pct=pct)
```

`detect` returns `(dest, event)` for each enabled destination where
`matches` is true and `f"{dest.id}|{event.id}"` is not in `state.sent`, then
records it in `state.sent[...] = now`. `matches` checks the account filter
(empty = all), the window and the event type. For `ending_soon` it also needs
`0 < resets_at - now <= dest.filter.lead_s[window]` and
`100 - used_pct >= dest.filter.min_left_pct`. `scoped` carries
`usage.get("scoped")` entries as `{"name", "used_pct", "resets_at"}`.

`payload` follows the spec's Payload block field for field.
`seconds_until_reset` = `max(0, int(resets_at - now))`, `remaining_pct` =
`round(100 - used_pct, 1)`, `measured_at` comes from `fetched_at` (null when
missing), `observed_at` comes from `now`, and `account` =
`{"label", "email", "org"}` where `org` is `org_name`.

- [ ] **Step 1: Write failing tests.** Build readings with a helper `reading(window_ts=..., pct=..., live=True, fetched_at=...)`.
  - `test_first_observation_seeds_without_started` (Review Focus 3)
  - `test_later_resets_at_from_live_reading_fires_started_once`: a second `detect` with the same input returns nothing.
  - `test_started_not_fired_for_weekly`
  - `test_started_never_from_last_known_good` (Review Focus 4): `live=False` with a later `resets_at` → no `started`, and the state is not advanced.
  - `test_reset_fires_from_state_without_fetch`: state knows ts=T. At `now = T + 1` with readings still showing T, `reset` fires once with `resets_at == T`.
  - `test_weekly_state_rolls_to_next_boundary_after_reset`: after the reset, `state.windows[k] == T + 7*86400`.
  - `test_sleep_past_reset_gives_reset_not_ending_soon` (Review Focus 2): one `detect` at `now = T + 3600`, with a destination whose lead time covers T, returns only `reset`.
  - `test_ending_soon_respects_lead_and_min_left`: 5-hour, lead 1800. `now = T - 1900` → none. `T - 1700` at 85% used (15% left, min 20) → none. At 60% used → one event.
  - `test_ending_soon_per_destination_independent`: two destinations with different lead times each get exactly one event, at their own time.
  - `test_ending_soon_from_previous_window_numbers_skipped` (Review Focus 4): a last-good reading whose `resets_at <= now` produces no `ending_soon`.
  - `test_disabled_destination_gets_nothing`
  - `test_account_filter_limits_events`
  - `test_event_id_identical_across_states`: two fresh `FiredState`s give the same `id` for the same moment (the cross-Mac case).
  - `test_readings_skip_rows_without_usage` (Review Focus 5): sentinel display + no last_good → no reading.
  - `test_payload_shape`: the exact key set, `remaining_pct`, `seconds_until_reset`, the `Z` time format, and `scoped_limits` mapped.
  - `test_signature_header_matches_reference`: `hmac.new(b"s", b"1700000000." + body, sha256).hexdigest()` appears in `t=1700000000,v1=<hex>`.
- [ ] **Step 2:** Run. Expect: fail.
- [ ] **Step 3: Implement**, following the rules above.
- [ ] **Step 4:** Run. Expect: pass.
- [ ] **Step 5:** Commit: `feat(events): detect, filter and sign reset events`.

### Task 3: Webhook delivery

**Files:**
- Create: `src/openswap/reset_events_delivery.py`
- Test: `tests/test_reset_events_delivery.py`

**Interfaces:**
- Consumes: `payload`, `signature_header`, `Destination`.
- Produces:
  - `validate_webhook_url(url: str) -> str | None`: an error message, or `None` when valid (rules in Global Constraints; also rejects missing host and non-http(s) schemes).
  - `KEYCHAIN_SERVICE = "OpenSwap reset events"`. `new_secret(dest_id) -> str` (`secrets.token_urlsafe(32)`, stored with `macos_keychain.set_password`), `get_secret(dest_id) -> str | None`, `delete_secret(dest_id) -> None` (see Step 3 for the delete call).
  - `post_once(url: str, body: bytes, secret: str, *, now: float, opener=urllib.request.urlopen) -> None`: raises `DeliveryError(str)` on a non-2xx response, a network error or a timeout.
  - `deliver_async(dest: Destination, body: bytes, *, on_result: Callable[[str, str], None], sleep=time.sleep, opener=urllib.request.urlopen) -> threading.Thread`: a daemon thread doing up to three attempts at delays 0, 30, 90 s. Calls `on_result(dest.id, "")` on success or `on_result(dest.id, last_error)` after the final failure. A missing secret counts as an immediate failure, `"secret missing; re-add this webhook"`.

- [ ] **Step 1: Write failing tests** (fake `opener`, `sleep` recorder, Keychain monkeypatched):
  - `test_validate_rejects_http_non_localhost`, `test_validate_accepts_http_localhost_and_https`, `test_validate_rejects_ftp_and_hostless`
  - `test_post_once_sends_signature_and_json_headers`: the fake opener captures the `Request`. Check the method `POST`, `Content-Type`, and that `OpenSwap-Signature` verifies.
  - `test_post_once_raises_on_500`
  - `test_deliver_retries_then_reports_last_error`: the opener always fails. `sleep` is called with `[30, 60]` (cumulative 30, 90). `on_result` gets the error once.
  - `test_deliver_success_after_one_failure_reports_empty`
  - `test_missing_secret_reports_without_network`
- [ ] **Step 2:** Run. Expect: fail.
- [ ] **Step 3: Implement.** Check `macos_keychain` for a delete function first. If there isn't one, add `delete_password(service, account)`, mirroring `set_password` and using `security delete-generic-password`. A missing item is a no-op. Test it in this task.
- [ ] **Step 4:** Run. Expect: pass.
- [ ] **Step 5:** Commit: `feat(events): signed webhook delivery with bounded retries`.

### Task 4: Menu bar wiring and Mac notification

**Files:**
- Modify: `src/openswap/menubar.py` (`on_sync_tick` near line 683; the settings row handler near line 2470)
- Modify: `src/openswap/menubar_display.py` (Automation rows after the kickoff block near line 1209; a new `notification_copy_for_reset_event`)
- Test: `tests/test_reset_events.py` (copy), the existing settings rows test file (rows)

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `notification_copy_for_reset_event(event: ResetEvent, now: float) -> NotificationCopy`. Exact copy:
    - started: title `"{label}: 5-hour window started"`, body `"{remaining:.0f}% left · resets {clock}"`
    - ending_soon: title `"{label}: {window_name} ends in {countdown}"`, body `"{remaining:.0f}% left"`
    - reset: title `"{label}: {window_name} reset"`, body `"Full quota available"`

    `window_name` is `5-hour` / `weekly`. `countdown` reuses `_live_countdown`. `clock` is `oauth.reset_clock_string` (local 24-hour time, with the date when it isn't today).
  - Settings rows (section `SETTINGS_SECTION_AUTOMATION`):
    - group `"group_reset_events"`, labeled `"Reset events"`
    - toggle `"reset_events_mac"`, `"Notify on this Mac"`
    - hint `"reset_events_webhooks"`, which reads `"{n} webhook(s) · manage with openswap events"`, or `"Send events to your own automations with openswap events add-webhook"` when there are none. When any webhook has a `last_error`, append ` · last delivery failed`.

- [ ] **Step 1: Write failing tests:** the three copy strings, the rows present with the right values for 0 and 2 webhooks, and the failure suffix.
- [ ] **Step 2:** Run. Expect: fail.
- [ ] **Step 3: Implement:**
  - `_maybe_reset_events()`, called at the end of `on_sync_tick`. It runs at most once per 15 s (a monotonic timestamp gate). It reads destinations and state, calls `detect` with `readings_from_snapshot(self.snapshot)`, and calls `save_state` **before** delivering (Review Focus 1). Then it delivers:
    - `mac`: `self._notify(...)` on the main thread.
    - `webhook`: `deliver_async`, whose `on_result` takes a lock and updates `last_error` through `load_destinations`/`save_destinations`.
  - Skip the pass while `self._desktop_switching` or before the first snapshot (`self._snapshot_at == 0.0`).
  - `mac` is `socket.gethostname()`'s friendly form, via `scutil --get ComputerName`, cached once.
  - Toggle handler: flip `enabled` on the `mac` destination and save.
- [ ] **Step 4:** Run `uv run pytest -q`, teed to the scratchpad with only the summary shown. Expect: all pass.
- [ ] **Step 5:** Manual check: run the extra from the checkout (`docs/hacking.md`), turn on **Notify on this Mac**, and set a webhook to a local `nc -l 8765` receiver with a 5-hour lead time of 5 hours so `ending_soon` fires right away. Confirm one banner and one signed POST, then confirm a restart sends neither again. Screenshot the Automation section.
- [ ] **Step 6:** Commit: `feat(events): fire reset events from the menu bar`.

### Task 5: `openswap events` CLI and docs

**Files:**
- Modify: `src/openswap/cli.py` (add `"events"` to the command list at line 128, and dispatch in `main()` next to `config` at line 1716 through a new `_events_main(argv)` modeled on the `config` parser)
- Modify: `README.md`, `CHANGELOG.md`, `docs/menubar.md`
- Test: `tests/test_reset_events_cli.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces these commands (all accept `--json` where they print data):
  - `openswap events list`: id, kind, enabled, url, filter, last error.
  - `openswap events add-webhook URL [--accounts KEY,...] [--windows five_hour,seven_day] [--events started,ending_soon,reset] [--lead-5h MIN] [--lead-weekly MIN] [--min-left PCT]`. It validates the URL, creates id `wh_<8 hex>`, stores the secret, and prints the id and secret **once**, with the line `Save this secret now; it can't be shown again.`
  - `openswap events set ID [same filter flags] [--enable|--disable]` changes any destination's filter, including `mac`. Test: `test_set_updates_mac_filter`.
  - `openswap events remove ID` deletes the destination and its secret. `mac` can't be removed (error: `use the Mac toggle in Settings`).
  - `openswap events test ID`: synchronous delivery of a sample `window.ending_soon` with `"test": true` and the first account (or a placeholder when there are none). It prints `delivered` or the error and exits 1 on failure.

- [ ] **Step 1: Write failing tests** (Keychain and the opener faked):
  - `test_add_webhook_prints_secret_once_and_saves`
  - `test_add_webhook_rejects_http_remote`, which exits 2 with the validation message
  - `test_remove_deletes_secret`
  - `test_remove_mac_refused`
  - `test_list_json_shape`
  - `test_test_command_exit_code_on_failure`
- [ ] **Step 2:** Run. Expect: fail.
- [ ] **Step 3: Implement**, then the docs:
  - a README command table row;
  - a CHANGELOG entry;
  - a `docs/menubar.md` section with the payload, the event table, and a 10-line Python verification snippet for `OpenSwap-Signature` (reject `t` older than 5 minutes).
- [ ] **Step 4:** Run the full suite, teed. Expect: all pass.
- [ ] **Step 5:** Commit: `feat(events): openswap events command and receiver docs`.
