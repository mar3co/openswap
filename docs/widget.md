# Widget companion

Python cannot host WidgetKit. Split:

1. Extra writes `~/Library/Application Support/OpenSwap/widget-snapshot.json` (`widget_snapshot.py`). Payload is `accounts` plus `combined.five_hour` / `combined.seven_day` (remaining slots across healthy accounts; signed-out and disabled omitted). The widget computes the same from `accounts` if `combined` is missing.
2. Extra asks for a timeline reload. In the cask's app it runs the bundled `Contents/MacOS/openswap-widget-reload` (no wait), which calls `WidgetCenter.shared.reloadAllTimelines()` for its enclosing app. A source install posts Darwin notification `com.opensoft.openswap.widget.reload` instead.
3. Source install only: the separately built host `~/Applications/OpenSwap.app` (LSUIElement, `com.opensoft.openswap.widget`) listens and calls `reloadAllTimelines()`.
4. The appex reads the JSON (sandbox: home-relative read-write exception on `Library/Application Support/OpenSwap/` only + `getpwuid` for the real home; container `NSHomeDirectory()` is wrong).
5. A tap writes `~/Library/Application Support/OpenSwap/widget-command.json` (`{"op":"switch","num":…}`). The extra consumes it on the 1s sync tick and calls CLI `switch_to` (Claude signed-out repair still applies). Widget taps do not take the ChatGPT popover’s gated desktop-restart path. Combined remaining taps the slot with the most remaining on the primary window. The extra must be running; the widget cannot switch on its own. Disabled cards are not tappable. Sentinel notes render even when last-good bars are present.

The widget is `AppIntentConfiguration` (`OpenSwapWidgetIntent`). Right-click → Edit Widget sets layout (all accounts, combined remaining, one account), which windows (5h, 7d, both), and the account picker for one-account. Those choices live on the widget instance, not in extra Settings.

Sources: `macos/OpenSwapWidget/`. Install: `openswap widget --install` (`widget_install.py`).

## Cask app (bundle)

The cask's `OpenSwap.app` (`com.opensoft.openswap`) is one notarized bundle, laid out by `packaging/macos/build.sh`:

- `Contents/MacOS/OpenSwap`: the frozen extra and CLI (Homebrew links it as `openswap`)
- `Contents/MacOS/openswap-widget-reload`: the reload helper (target `OpenSwapWidgetReload`)
- `Contents/PlugIns/OpenSwapWidgetExtension.appex`: the widget (`com.opensoft.openswap.widget.extension`)

`openswap.bundle` finds it from the resolved `sys.executable`. In the app nothing is built: `openswap widget --install` runs `pluginkit -a` on the bundled appex, retires an old source-install host (below), and runs the helper once. `--uninstall` is `pluginkit -r` only and never deletes the app (that is `brew uninstall`). `--status` lists each registered copy from `pluginkit -m -D -v -i com.opensoft.openswap.widget.extension`; more than one means two providers (without `-D` pluginkit reports only the last instance of the highest version, so a duplicate would not show). There is no widget LaunchAgent and nothing to wake.

### Moving from a source install

`openswap setup`, the frozen extra's start-up, and `openswap widget --install` in the app retire the old host when `~/Applications/OpenSwap.app` has bundle id `com.opensoft.openswap.widget`, or when the source build's derived data `~/Library/Caches/openswap-widget` is still there (its `Build/Products/Release/OpenSwap.app` carries the same appex id):

1. `pluginkit -a` the bundled appex first. If that fails nothing is removed, so the next run tries again.
2. `pluginkit -r` the old host's appex, then boot out LaunchAgent `com.opensoft.openswap.widget`, remove its plist, and move the app to the Trash (`NSFileManager trashItemAtURL`, recoverable). If `-r` exits nonzero and the copy is still listed by `pluginkit -m -D -v -i`, nothing is removed and the step fails (a copy that is not listed was not registered, which is fine).
3. `pluginkit -r` the build product's appex (same check) and delete `~/Library/Caches/openswap-widget` (that directory only; it is OpenSwap's own build cache).

An app with any other bundle id (the cask's own, installed with `--appdir ~/Applications`) is left alone.

With the cask's appdir set to `~/Applications` (`HOMEBREW_CASK_OPTS=--appdir=~/Applications`), the old host sits where the cask installs the app and `brew install --cask` refuses to overwrite it. `install.sh` therefore retires it before a fresh `brew install` when that is the appdir: same bundle-id check, `launchctl bootout` (a not-loaded agent is fine), remove its plist, `pluginkit -r` its appex, and `mv` it into `~/.Trash` under a name that does not overwrite an earlier copy (`OpenSwap 2.app`, ...). It never deletes the app.

The appex id is unchanged but its parent app's bundle id changes from `com.opensoft.openswap.widget` to `com.opensoft.openswap`, so desktop and Notification Center widgets placed with the old host may go blank and need adding again (plan 007 check 4 records what actually happens).

## Build (source install)

`xcodebuild` scheme `OpenSwapWidget`, Release, `DEVELOPMENT_TEAM=…`, copy to `~/Applications/OpenSwap.app`, bootstrap LaunchAgent `com.opensoft.openswap.widget`. The frozen app never takes this path.

`project_dir()` walks from `widget_install.py` until it finds `macos/OpenSwapWidget/OpenSwapWidget.xcodeproj`. That only works from this git checkout (or if sources are vendored next to the package as `macos_widget`). A PyPI wheel does not include `macos/`.

Regenerate the Xcode project with xcodegen from `macos/OpenSwapWidget/project.yml` if you change that file or add a `.swift` file under `Widget/`. `openswap widget --install` runs `xcodebuild` only.

## Signing (source install)

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
