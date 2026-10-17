# Architecture

```
        ┌─────────────┐                 ┌───────────────┐
        │ Claude Code │                 │   Codex CLI   │
        │  ~/.claude  │                 │ ~/.codex/     │
        └──────▲──────┘                 │  auth.json    │
               │                        └──────▲────────┘
               │ engine writes credentials     │
        ┌──────┼───────────────────────────────┼──────┐
        │      │                               │      │
   openswap CLI         AutoSwitchEngine    rumps extra
   (cli.py)          (autoswitch.py)     (menubar.py)
        │                  │                  │
        └────────┬─────────┴────────┬─────────┘
                 │                  │
            engine.Engine         settings.json           (shared policy)
            (engine/)             menubar_settings.json   (extra display)
                                  autoswitch_state.json   (cooldown / last switch)
                 │
            usage_store.py
                 │
                 └──── menubar extra writes widget-snapshot.json
                                    │
                                    ▼
                           OpenSwap.app (Swift)
                           WidgetKit extension
```

## Ownership

| Concern | Module | Notes |
| --- | --- | --- |
| Accounts, swap, credentials | `engine/` (`Engine` façade) | Extra, autoswitch, kickoff, and CLI `list`/`status`/`switch` call only the public façade. `switcher.py` is a one-release shim (`ClaudeAccountSwitcher` is `Engine`). |
| Consumer-facing engine surface | `engine/protocol.py` | `AccountEngine` Protocol both the Claude `Engine` and `CodexEngine` implement. |
| Codex accounts | `codex/` | Roster, slot homes, app-server usage, switch. Each slot dir is that account's `CODEX_HOME`. |
| Live Claude Code store | `engine/live.py` | Keychain `"Claude Code-credentials"` / `"Claude Code"`; degraded vs empty |
| Roster + backup Keychain | `engine/slots.py` | `sequence.json`; backups service `"openswap"`, account `account-{n}-{email}` |
| Identity | `engine/identity.py` | `(email, organizationUuid)` |
| Consume | `engine/consume.py` | One-time refresh CAS + unclaimed stash |
| Switch / capture | `engine/switch.py` | Classify outgoing live bytes before write |
| Isolated session profile | `session.py` | Idle-slot kickoff bootstrap (`setup_session`). No terminal launch (`openswap run` is gone). Must not POSIX-`exec` the extra |
| Idle-slot kickoff profile | `engine/session_profile.py` | Isolated `CLAUDE_CONFIG_DIR`; live kickoff is `claude -p` in place |
| Snapshot assembler | `engine/snapshot.py` | Store-only (`fetch=set()`) is roster-only: **does not read idle or active-slot backup credentials**. Unread idle is not `USAGE_NO_CREDENTIALS`. At most one live credential read per snapshot. |
| Auto-switch | `autoswitch.py` | UI-agnostic events; CLI and extra host it. Runtime: `autoswitch_state.json` |
| Shared policy knobs | `settings.py` | `autoswitch.*` and CLI `ui.theme` in `settings.json` (`openswap config`) |
| Extra display knobs | `menubar_display.MenuBarSettings` | `menubar_settings.json` only (popover Settings) |
| Popover UI | `menubar_panel.py` | AppKit, imported after rumps |
| 5h kickoff policy | `kickoff.py` | Pure; extra decides *when* |
| Widget JSON | `widget_snapshot.py` | Extra writes cards plus combined remaining; `updated_at` is last usage measurement, not extra paint time |
| Widget build | `widget_install.py` | `xcodebuild` + LaunchAgent |
| Claude Code status line | `statusline.py` | Opt-in wrap of `~/.claude/settings.json` `statusLine`. Paint reads live `.claude.json` + roster only (no Engine, no network). Wrap target lives in `settings.json` `statusline` (not a fourth file). `openswap statusline --codex` is paint-only (live `auth.json` + `codex/sequence.json`); Codex TUI has no command hook, so `config.toml` is never wrapped. |
| Live process SCAN | `process_detection.py` | Claude: `~/.claude/sessions/{pid}.json` + IDE locks. Codex: injected process table, TUI only (`codex exec` / `app-server` do not count). |
| ChatGPT desktop | `codex/desktop.py`, `codex/desktop_app.py` | Extra/CLI experimental quit → switch shared Codex `auth.json` → relaunch. Gated by `chatgpt_switching_enabled` (default off) and a worker capability probe. Routine signed updates stay available when the capability contract passes. Does not verify the profile ChatGPT loaded. |

The extra is a thin shell. It must not re-implement quota math, ranking, or credential writes. It must not call `_get_current_account`, `_account_kind`, or `_get_sequence_data`; kind and live `(email, orgUuid)` are on the snapshot / `Engine.live_identity`.

Usage fetch stays store-paced (`fetch=set()` while autoswitch is hosted). A leftover `claude-swap` backup Keychain item is copied once into `openswap` at Engine construct (see `migrations.migrate_claude_swap_backup_items`). Windows Credential Manager is not on the macOS hot path; `.enc` is locked-Keychain fallback only.

MIT / Cetinkol remains on `LICENSE` while inherited files (`oauth.py`, parts of `credentials.py`) remain.

## Data on disk (macOS)

| Path | Who |
| --- | --- |
| `~/Library/Application Support/OpenSwap/settings.json` | CLI + extra policy (`openswap config`). First run moves `~/.claude-swap-backup` here |
| `~/Library/Application Support/OpenSwap/menubar_settings.json` | Extra display + kickoff (popover Settings). Atomic 0600 write |
| `~/Library/Application Support/OpenSwap/autoswitch_state.json` | Engine cooldown, last switch, quarantine. Extra stamps `lastSwitchAt` after a hand switch |
| `~/Library/Application Support/OpenSwap/codex/` | Codex roster (`sequence.json`), slot homes (`slots/<n>/auth.json`), usage cache, Codex autoswitch state |
| `~/Library/Application Support/OpenSwap/widget-snapshot.json` | Extra → widget |
| `~/Library/LaunchAgents/com.opensoft.openswap.menubar.plist` | Extra service |
| `~/Library/LaunchAgents/com.opensoft.openswap.widget.plist` | Widget host |
| `~/Applications/OpenSwap.app` | Signed WidgetKit host |

Claude credentials on macOS are Keychain; Codex slots are 0600 `auth.json` files under `codex/slots/<n>/` because each slot doubles as a `CODEX_HOME`. Widget layout is per-widget in Edit Widget (App Intents), not these JSON files.

## Product surface

OpenSwap ships for macOS (extra, widget, kickoff, Keychain). The engine still has Windows/Linux branches from upstream; we do not promise those platforms. API-key slots (`openswap add-token`, extra → Add account) are first-class to switch to. They have no 5h/7d quota, so kickoff skips them. A key linked to a subscription account (`add-token --for`, roster field `creditFor` holding the parent's `(email, organizationUuid)`) is the auto-switch engine's last resort: on `at-limit` when no subscription has known room, or on `failover` when a subscription was measured and none has room (or none is in rotation); a proactive or consume-first tick never spends credit. An unlinked key needs `autoswitch.includeApiKeyAccounts`, and since plan 019 that flag no longer lets a proactive tick land on a key either. While a key is active the engine leaves it only for a subscription below the threshold (trigger `credit-return`, hold reason `on-api-credit`), including a key it placed the user on whose link has since gone. A free `count_tokens` probe (`openswap.api_key_health`, mar3co/openswap#97) checks each key on any pass that may fetch: every 30 minutes while it works, every 5 after a failure or an inconclusive answer, cached by key fingerprint in `cache/api_key_health.json`, which also records each slot's key fingerprint so a roster-only paint (the extra's store-only refresh while auto-switch runs) shows the verdict without reading the idle key. A 401 becomes the sentinel `api key revoked`; an error naming the credit balance becomes `api key out of credit` (count_tokens is not documented to check the balance, so this is best-effort); a spend limit reached, the documented 400 "You have reached your specified [workspace] API usage limits" or the tier cap's 429 `enforced_spend_limit_reached`, becomes `api key at spend limit` (also best-effort: count_tokens is not documented to enforce spend limits). OpenSwap keeps no spend cap of its own, since it cannot see a key's spend or stop a request already running; the README and token page point at a Console workspace spend limit instead. Such a key is never a target, and an engine-managed key that goes dead is left at once like an account at its limit (trigger `at-limit`, hold reason `api-key-dead` when nowhere has room); a dead key the user picked by hand is named in the hold line but not left. The monthly API credit included with Max and Team plans does not cover Claude Code, so a key whose organization holds only that credit passes the probe and still fails in Claude Code; the token page says so. The engine is shared, so with `autoswitch.includeApiKeyAccounts` on the same key rules apply to the Codex rotation.

### Two providers, two rotations

Claude Code and Codex CLI are independent rotations: one live Claude login and one live Codex login. They never pool. Codex slots are namespaced as `"codex:<n>"` in the extra and widget. Combined remaining counts Claude cards only. `autoswitch.codexEnabled` (default true) gates the Codex rotation. `openswap codex export|import` moves `auth.json` envelopes; `swap`/`move` rename slot dirs. Codex has no `unclaimed` stash.

The extra’s ChatGPT tab shows that same Codex roster. A card click there is a gated desktop restart (`codex/desktop.py`), not `Engine.switch_to`. Widget taps still call CLI `switch_to`.

## Constraints we keep

- Do not change the user’s default `~/.claude` login except via `switcher` (kickoff pings the live login **in place**). Status line install writes only `~/.claude/settings.json` `statusLine`, never `.claude.json`.
- Do not `os.exec*` the extra process (`kickoff` uses returning `subprocess.run`).
- Do not put WidgetKit inside the Python extra (impossible); snapshot + Darwin notification + host `.app` is the split.
- Keep the OpenSoft logo visible in the extra, including when account and usage text are off ([Menu bar](menubar.md)).
