# Widget companion

Python cannot host WidgetKit. Split:

1. Extra writes `~/Library/Application Support/OpenSwap/widget-snapshot.json` (`widget_snapshot.py`). Payload is `accounts` plus `combined.five_hour` / `combined.seven_day` (remaining slots across healthy accounts; signed-out and disabled omitted). The widget computes the same from `accounts` if `combined` is missing.
2. Extra posts Darwin notification `com.opensoft.openswap.widget.reload`.
3. `OpenSwap.app` (LSUIElement) listens and calls `WidgetCenter.shared.reloadAllTimelines()`.
4. The appex reads the JSON (sandbox: home-relative read-write exception on `Library/Application Support/OpenSwap/` only + `getpwuid` for the real home; container `NSHomeDirectory()` is wrong).
5. A tap writes `~/Library/Application Support/OpenSwap/widget-command.json` (`{"op":"switch","num":…}`). The extra consumes it on the 1s sync tick and calls CLI `switch_to` (Claude signed-out repair still applies). Widget taps do not take the ChatGPT popover’s gated desktop-restart path. Combined remaining taps the slot with the most remaining on the primary window. The extra must be running; the widget cannot switch on its own. Disabled cards are not tappable. Sentinel notes render even when last-good bars are present.

The widget is `AppIntentConfiguration` (`OpenSwapWidgetIntent`). Right-click → Edit Widget sets layout (all accounts, combined remaining, one account), which windows (5h, 7d, both), and the account picker for one-account. Those choices live on the widget instance, not in extra Settings.

Sources: `macos/OpenSwapWidget/`. Install: `openswap widget --install` (`widget_install.py`).

## Build

`xcodebuild` scheme `OpenSwapWidget`, Release, `DEVELOPMENT_TEAM=…`, copy to `~/Applications/OpenSwap.app`, bootstrap LaunchAgent `com.opensoft.openswap.widget`.

`project_dir()` walks from `widget_install.py` until it finds `macos/OpenSwapWidget/OpenSwapWidget.xcodeproj`. That only works from this git checkout (or if sources are vendored next to the package as `macos_widget`). A PyPI wheel does not include `macos/`.

Regenerate the Xcode project with xcodegen from `macos/OpenSwapWidget/project.yml` if you change that file or add a `.swift` file under `Widget/`. `openswap widget --install` runs `xcodebuild` only.

## Signing

`detect_development_team()` order:

1. `DEVELOPMENT_TEAM` env
2. TeamIdentifier on the already-installed app (so rebuilds do not flip bundle ids)
3. Xcode last-selected team, if it has a local Apple Development cert
4. Any local cert OU

Last Xcode team on a machine with more than one team can flip identifiers. Prefer the installed app’s team so `com.opensoft.openswap.widget` does not change and already-placed widgets do not go blank.

No App Group: that needs the Developer Portal. The home-relative sandbox exception is enough (read-write on that directory only, so the appex can write the command file).

## Families

`systemSmall` (up to 2, most remaining on the primary window, not locked to active), `systemMedium` (up to 3, slot order), `systemLarge` (up to 6), `systemExtraLarge` (up to 10). Combined remaining is a hero `remaining / total` for 5h and/or 7d (each healthy account is one slot; percentages are not averaged). Timeline: 30 one-minute entries, then `.after(30m)`. Countdown uses `resets_at_ts` from the snapshot, not a frozen string. `updated_at` is the last usage measurement (live slots, else last-good), not extra paint time. Older than 10 minutes shows an "Updated … ago" footer. Signed-out cards keep last-good bars but omit a live reset clock.

`accessoryCircular` / `accessoryRectangular` are iOS Lock Screen and watchOS complications only (`@available(macOS, unavailable)`). This companion is a macOS widget, so those families are not declared. There are no chrome action buttons; the account block (or combined hero) is the tap target.

Colors match `theme.py` (`SEV_OK` / `WARN` / `CRIT`, 70 / 90). Use `NSColor` dynamic providers, not a one-shot `@Environment(\.colorScheme)`.

## Caches

xcodebuild derived data: `~/Library/Caches/openswap-widget`. Safe to delete.
