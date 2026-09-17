# Testing

```bash
uv run pytest
```

`pyproject.toml` sets `-n auto`. About 2250 tests. CI runs on macOS, and still on Ubuntu and Windows as a test farm (we do not ship those platforms).

The suite describes the product we ship: engine, extra, widget, kickoff, and advertised CLI. Cut verbs (`tui` / `watch` / `run` / `map` / `unmap`) have gone-stub tests only. Isolated-profile tests cover kickoff bootstrap, not a terminal session launch.

## Boundaries

| File | May import rumps / AppKit? |
| --- | --- |
| `tests/test_menubar.py` | No. Pure helpers only. |
| `tests/test_kickoff.py` | No. |
| `tests/test_widget_snapshot.py` | No. |
| `tests/test_widget_install.py` | No (paths, team parsing, plist). |
| `tests/test_autoswitch.py` | No. |
| `tests/test_codex_*.py` | No. |
| `tests/test_process_detection.py` | No. |
| `tests/test_statusline.py` | No. |

Live AppKit probes (status item padding, appearance) are one-off scripts, not CI.

## Conventions already in the suite

- Do not hit the real account store or Keychain. `conftest.py` isolates HOME / backup dirs.
- `no_keychain_fake` / `no_oauth_profile_fake` markers opt out of autouse stubs when a test mocks `subprocess` itself.
- Prefer asserting behavior (format strings, length formula, eligibility) over scraping source, except for wiring checks that cannot run AppKit (`fit_status_item` called from `rebuild_menu`).

## After UI changes

Restart the extra and exercise the popover (first click on a card, More without dismissing the box, leave-delay close, Dark/Light). Open Settings in the popover (not More) and change strategy, kickoff time from the hourly menu, and the asterisk; Back should return to the cards without expecting More to dismiss the box. Widget: `openswap widget --install`, then Edit Widgets. Right-click the widget and switch layout (all / combined / one account) and windows (5h / 7d / both). Browser tools do not apply.
