# Plan 007: Prove a single notarized OpenSwap.app (Python extra + WidgetKit) can ship via a Homebrew cask

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 92722b1..HEAD -- src/openswap/menubar_display.py src/openswap/launch_agent.py src/openswap/widget_snapshot.py src/openswap/widget_install.py src/openswap/cli.py macos/OpenSwapWidget/`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M (a spike: 1 to 3 working days, most of it waiting on notarization and re-signing)
- **Risk**: MED (new build tooling; nothing user-facing changes until plan 008)
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `92722b1`, 2026-09-09
- **Executor note (2026-09-09)**: operator overrode Step 0 STOP. Local Mac
  freezes unsigned (`build.sh` exits 0 without `OPENSWAP_SIGN_IDENTITY`).
  Sign/notarize/staple moved to GitHub Actions
  (`.github/workflows/macos-app.yml` + `packaging/macos/ci-import-signing-keychain.sh`).
  Do not treat a missing local identity as a halt.

## Why this matters

Today OpenSwap installs from a git checkout with `uv tool install --editable`,
and the widget installs by running `xcodebuild` on each user's Mac with that
user's own Apple Development certificate. Every user therefore needs Python,
uv, Xcode, and a signing identity. That is the real adoption blocker, not the
lack of a Homebrew formula.

The destination is one `OpenSwap.app`: the Python menu bar extra as the main
executable, the WidgetKit extension embedded in the same bundle, the CLI
exposed from inside the bundle, Developer ID signed and notarized, installed
with `brew install --cask`. Nothing needs a Swift rewrite; the ~27k lines of
Python and 2261 tests stay as they are.

This plan is the **spike** that proves the four unknowns before anyone
rewires the product (plan 008 does the rewiring):

1. PyInstaller can freeze `openswap menubar` (rumps + PyObjC) on the Python
   this repo uses, and the frozen app runs the popover and reads Keychain.
2. The frozen app plus the embedded WidgetKit appex passes Apple notarization
   with the hardened runtime.
3. The widget can be reloaded from inside the same bundle after the Python
   extra writes a snapshot (there is no separate Swift host any more).
4. The same executable serves as the CLI when run from a terminal with
   arguments.

Output of the spike is a go/no-go note plus a working build script under
`packaging/macos/`, not a product change.

## Current state

Files and their roles:

- `src/openswap/cli.py` — CLI entry (`openswap.cli:main`). `openswap menubar`
  dispatches to `openswap.menubar.run`; `openswap widget` to `_widget_command`
  (line 913). No-args prints help (line ~948: `if not argv: argv = ["--help"]`).
- `src/openswap/menubar.py:33-58` — `run(switcher)`: calls
  `ensure_notification_identity()`, imports `rumps` and `AppKit` lazily, sets
  accessory activation policy.
- `src/openswap/menubar_display.py:95-130` — `ensure_notification_identity`
  writes an `Info.plist` **next to `sys.executable`** so rumps can post
  notifications. In a signed bundle `sys.executable` is
  `OpenSwap.app/Contents/MacOS/OpenSwap`; writing there breaks the code
  signature. The bundle already has a real `Info.plist`, so this must be a
  no-op when frozen.
- `src/openswap/launch_agent.py:95-108` — `resolve_program()` prefers
  `sys.argv[0]` when its basename is `openswap`, else `which`, else
  `[sys.executable, "-m", "openswap"]`. The plist runs `[*program, "menubar"]`
  (line 140). In a bundle the executable basename is `OpenSwap`.
- `src/openswap/widget_snapshot.py:94-97` — `widget_app_path()` is hard-coded
  to `~/Applications/OpenSwap.app`. `notify_widget_reload()` (line 246) posts
  Darwin notification `com.opensoft.openswap.widget.reload`; `wake_widget_host`
  (line 261) `open -ga`s that app so its Swift host can relay the reload.
- `src/openswap/widget_install.py:42-60` — `project_dir()` finds
  `macos/OpenSwapWidget/OpenSwapWidget.xcodeproj` by walking up from the
  package; `install()` (line 245) runs `xcodebuild` with
  `CODE_SIGN_STYLE=Automatic` and `DEVELOPMENT_TEAM=<detected>`.
- `macos/OpenSwapWidget/project.yml` — xcodegen spec. Two targets:
  `OpenSwapWidget` (application, bundle id `com.opensoft.openswap.widget`,
  `Host/main.swift` is a 30-line accessory app that observes the reload
  notification and calls `WidgetCenter.shared.reloadAllTimelines()`) and
  `OpenSwapWidgetExtension` (app-extension, bundle id
  `com.opensoft.openswap.widget.extension`). `ENABLE_HARDENED_RUNTIME: NO`,
  `CODE_SIGN_STYLE: Automatic`.
- `macos/OpenSwapWidget/Host/Host.entitlements` — host is **sandboxed**
  (`com.apple.security.app-sandbox`). The Python extra cannot be sandboxed
  (it runs `security`, `launchctl`, `claude`, and reads `~/.claude`). The
  spike bundle's outer app is therefore not sandboxed; only the appex keeps
  its sandbox. Developer ID distribution allows that (the App Store would not).
- `macos/OpenSwapWidget/Widget/Widget.entitlements` — appex sandbox with a
  home-relative read-write exception on `Library/Application Support/OpenSwap/`.
  Keep exactly as is.
- `pyproject.toml` — `requires-python = ">=3.12"`, `menubar` extra is
  `rumps>=0.4.0` (pulls `pyobjc-core`, `pyobjc-framework-cocoa`). `.python-version`
  is `3.14`; `uv run python --version` prints 3.14.7. PyInstaller 6.22 supports
  Python 3.8 through 3.15 (checked 2026-09-09 at https://pyinstaller.org/en/stable/).
  py2app 0.28.10 (Feb 2026) also runs on 3.14 but wants a framework build of
  Python, which uv's interpreters are not. **Use PyInstaller.**

Conventions to match:

- No secrets in any file. The Developer ID certificate lives in the login
  Keychain; notarization credentials live in a `notarytool` keychain profile.
  The plan only ever references them by name.
- No AI attribution trailers in commits.
- Commit style from `git log`: `improve: …`, `fix: …`, `docs: …`.
- Bundle ids in use: `com.opensoft.openswap.menubar` (LaunchAgent label and
  rumps notification id), `com.opensoft.openswap.widget` (Swift host),
  `com.opensoft.openswap.widget.extension` (appex).

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `uv run pytest` | `2261 passed` (3 skipped) at `92722b1` |
| Dev deps for the spike | `uv run --with pyinstaller --with '.[menubar]' python -c "import PyInstaller, rumps; print(PyInstaller.__version__)"` | prints `6.x` |
| Regenerate Xcode project | `cd macos/OpenSwapWidget && xcodegen generate` | writes `OpenSwapWidget.xcodeproj` |
| Build appex only | see Step 3 | `.appex` under derived data |
| Verify signature | `codesign --verify --deep --strict --verbose=2 dist/OpenSwap.app` | `valid on disk`, `satisfies its Designated Requirement` |
| Gatekeeper check | `spctl --assess --type execute --verbose=2 dist/OpenSwap.app` | `accepted`, `source=Notarized Developer ID` |
| Notarize | `xcrun notarytool submit dist/OpenSwap.zip --keychain-profile openswap-notary --wait` | `status: Accepted` |
| Staple | `xcrun stapler staple dist/OpenSwap.app` | `The staple and validate action worked!` |

## Scope

**In scope** (the only files you should create or modify):

- `packaging/macos/openswap.spec` (create) — PyInstaller spec
- `packaging/macos/entitlements.plist` (create) — hardened-runtime entitlements for the outer app
- `packaging/macos/build.sh` (create) — build appex, freeze, assemble, sign, notarize, staple
- `packaging/macos/README.md` (create) — how to run it and the spike verdict
- `macos/OpenSwapWidget/Host/WidgetReload/main.swift` (create) and the matching
  target in `macos/OpenSwapWidget/project.yml` — a tiny command-line helper
  that calls `WidgetCenter.shared.reloadAllTimelines()` (Step 5)
- `src/openswap/menubar_display.py` — one guard in `ensure_notification_identity`
- `src/openswap/cli.py` — one helper and one frozen-default in `main`
- `tests/test_menubar.py` — one test for the guard
- `tests/test_cli.py` — two tests for the frozen default
- `.gitignore` — add `packaging/macos/dist/` and `packaging/macos/build/`
- `plans/README.md` — status row

**Out of scope** (do NOT touch):

- `src/openswap/widget_install.py`, `src/openswap/launch_agent.py`,
  `src/openswap/update_check.py`, `src/openswap/widget_snapshot.py` — the
  product rewiring (install paths, upgrade via brew, LaunchAgent program) is
  plan 008. The spike must not change how current users install.
- The Homebrew tap and cask — plan 008.
- `macos/OpenSwapWidget/Widget/**` — the appex is proven; do not change it.
- The engine (`src/openswap/engine/**`), credentials, autoswitch.
- CI (`.github/workflows/ci.yml`) — release automation is plan 008.

## Git workflow

- Branch: `claude/007-single-app-spike` from `main`.
- One commit per step where a step touches tracked files. Message style:
  `improve: pyinstaller spec for a single OpenSwap.app` / `fix: skip notification plist when frozen`.
- Do not push or open a PR unless the operator instructed it.

## Steps

### Step 0: Confirm prerequisites on this Mac (no repo changes)

The operator said the Apple Developer account is the SecureSuite team. You need:

1. A **Developer ID Application** certificate in the login Keychain:
   `security find-identity -v -p codesigning | grep "Developer ID Application"`
   → at least one line. Record the full identity string (the quoted
   `Developer ID Application: <Org> (<TEAMID>)`) and the 10-character TEAMID.
   If there is none, STOP: the operator must create it in
   developer.apple.com → Certificates (Developer ID Application) and install
   it. Do not attempt to create it yourself.
2. A notarytool keychain profile named `openswap-notary`:
   `xcrun notarytool history --keychain-profile openswap-notary` → exits 0
   (an empty history is fine). If it fails, STOP and ask the operator to run
   `xcrun notarytool store-credentials openswap-notary` with an App Store
   Connect API key or app-specific password. Never put those values in a file.
3. Xcode with the macOS 14+ SDK: `xcodebuild -version` → `Xcode 15` or newer.
4. xcodegen: `which xcodegen` → a path. If missing, `brew install xcodegen`.

**Verify**: all four commands above succeed.

### Step 1: Make `ensure_notification_identity` a no-op when frozen

In `src/openswap/menubar_display.py`, `ensure_notification_identity`
currently starts:

```python
    if platform != "darwin":
        return None
    path = (executable or Path(sys.executable)).parent / "Info.plist"
```

Add, directly after the platform guard:

```python
    if getattr(sys, "frozen", False):
        # Inside a real .app bundle Contents/Info.plist already carries the
        # bundle id, and writing next to the executable would break the
        # code signature.
        return None
```

Add a test in `tests/test_menubar.py` next to the existing
`ensure_notification_identity` tests (search the file for that name and model
the new test on the neighbouring one): monkeypatch `sys.frozen = True`, call
`ensure_notification_identity(tmp_path / "OpenSwap")`, assert it returns
`None` and `tmp_path / "Info.plist"` does not exist.

**Verify**: `uv run pytest tests/test_menubar.py -q` → all pass, count is one
higher than before.

### Step 2: Default to `menubar` when the frozen app is launched without a terminal

Finder, `open -a`, and LaunchServices start the bundle's main executable with
no arguments and no controlling terminal (older macOS may add a `-psn_…`
argument). Plan 008's LaunchAgent passes `menubar` explicitly. A person who
types `openswap` in a terminal (the cask will symlink this same executable
onto `PATH`) must still get help, not a menu bar extra taking over their
shell. The deciding signal is therefore "is there a terminal", not "is the
app frozen".

In `src/openswap/cli.py`, add a module-level helper next to `_prog_name`
(search for `def _prog_name`):

```python
def _frozen_without_terminal() -> bool:
    """True when the frozen .app was started by Finder / LaunchServices."""
    if not getattr(sys, "frozen", False):
        return False
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError, OSError):
        return True  # no usable stdin at all: not a terminal
```

Then in `main`, replace

```python
    # Bare `openswap` prints help (used to open the terminal dashboard).
    if not argv:
        argv = ["--help"]
```

with

```python
    # Bare `openswap` prints help (used to open the terminal dashboard).
    # The frozen bundle launched by Finder has no terminal: run the extra.
    if _frozen_without_terminal() and not [a for a in argv if not a.startswith("-psn")]:
        argv = ["menubar"]
    if not argv:
        argv = ["--help"]
```

This sits after the `auto` / `widget` / `statusline` / `config` dispatch
block on purpose: an empty argv never matches those verbs, so nothing before
this point needs to know about the frozen case. The existing `menubar`
handling then takes over unchanged.

Add two tests in `tests/test_cli.py` next to the existing test that sets
`sys.argv` to `["openswap", "menubar"]` and patches `openswap.menubar.run`
(search for that test and copy its structure). Both set
`sys.frozen = True` (monkeypatch with `raising=False`) and
`sys.argv = ["openswap"]`:

1. No terminal: `monkeypatch.setattr(sys, "stdin", io.StringIO())` →
   `main` calls the patched `openswap.menubar.run`.
2. Terminal: `monkeypatch.setattr(sys.stdin, "isatty", lambda: True)` →
   `main` raises `SystemExit` with code 0 (argparse `--help`), captured
   stdout contains `Commands:`, and the patched `openswap.menubar.run` was
   never called.

**Verify**: `uv run pytest tests/test_cli.py -q` → all pass, count is two
higher than before.

### Step 3: Build the appex with manual Developer ID signing disabled (sign later)

Add to `macos/OpenSwapWidget/project.yml` a third target, and regenerate the
project:

```yaml
  OpenSwapWidgetReload:
    type: tool
    platform: macOS
    sources:
      - path: Host/WidgetReload
    settings:
      base:
        PRODUCT_NAME: openswap-widget-reload
        PRODUCT_BUNDLE_IDENTIFIER: com.opensoft.openswap.widget-reload
        MACOSX_DEPLOYMENT_TARGET: "14.0"
        SKIP_INSTALL: YES
```

Create `macos/OpenSwapWidget/Host/WidgetReload/main.swift`:

```swift
import WidgetKit

// Command-line helper that lives in OpenSwap.app/Contents/MacOS. `Bundle.main`
// for an executable inside Contents/MacOS resolves to the enclosing .app, so
// WidgetKit reloads that app's widgets.
WidgetCenter.shared.reloadAllTimelines()
```

Then, from `macos/OpenSwapWidget`, `xcodegen generate` and build the appex and
the helper **unsigned** (signing happens once, inside-out, in Step 6):

```bash
xcodebuild -project OpenSwapWidget.xcodeproj -scheme OpenSwapWidget \
  -configuration Release -derivedDataPath "$HOME/Library/Caches/openswap-spike" \
  -destination generic/platform=macOS \
  CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=NO build
xcodebuild -project OpenSwapWidget.xcodeproj -target OpenSwapWidgetReload \
  -configuration Release -derivedDataPath "$HOME/Library/Caches/openswap-spike" \
  CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=NO build
```

**Verify**: `ls "$HOME/Library/Caches/openswap-spike/Build/Products/Release/"`
→ contains `OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex` and
`openswap-widget-reload`.

### Step 4: Write the PyInstaller spec and freeze the extra

Create `packaging/macos/entitlements.plist` (the outer app is NOT sandboxed;
these two keys are what PyInstaller's own notarization guidance requires for
a frozen Python):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
  <true/>
  <key>com.apple.security.cs.disable-library-validation</key>
  <true/>
</dict>
</plist>
```

Create `packaging/macos/openswap.spec`:

```python
# PyInstaller spec: one executable that is the menu bar extra when launched
# with no arguments and the CLI otherwise (see openswap.cli.main).
from PyInstaller.building.api import BUNDLE, COLLECT, EXE, PYZ
from PyInstaller.building.build_main import Analysis

a = Analysis(
    ["entry.py"],
    pathex=["../../src"],
    hiddenimports=["rumps", "AppKit", "Foundation", "objc", "truststore"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="OpenSwap",
    console=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="OpenSwap")
app = BUNDLE(
    coll,
    name="OpenSwap.app",
    bundle_identifier="com.opensoft.openswap",
    info_plist={
        "CFBundleDisplayName": "OpenSwap",
        "CFBundleShortVersionString": "0.1.0",
        "LSUIElement": True,
        "LSMinimumSystemVersion": "14.0",
        "NSHumanReadableCopyright": "",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    },
)
```

Create `packaging/macos/entry.py`:

```python
from openswap.cli import main

main()
```

Freeze from the repo root:

```bash
cd packaging/macos && uv run --with pyinstaller --with '../..[menubar]' \
  pyinstaller --noconfirm --clean openswap.spec
```

If PyInstaller reports a missing module at runtime in Step 7, add it to
`hiddenimports` and rebuild; do not restructure `openswap` to work around it.

**Verify**: `ls packaging/macos/dist/OpenSwap.app/Contents/MacOS/OpenSwap` →
exists. `packaging/macos/dist/OpenSwap.app/Contents/MacOS/OpenSwap list` from
a terminal → prints the account table (same output as `openswap list`). On
macOS a `console=False` PyInstaller binary keeps the terminal's stdin/stdout
when run from one, so this is expected to work as is. If it prints nothing,
STOP and report; do not switch to `console=True`, which makes PyInstaller
set `LSBackgroundOnly`, a key a status-item app must not carry.

### Step 5: Assemble the bundle

Create `packaging/macos/build.sh` that does, in order (Steps 3 and 4 inlined,
then this):

1. `cp -R` the built appex into
   `dist/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex`.
2. `cp` `openswap-widget-reload` into `dist/OpenSwap.app/Contents/MacOS/`.
3. Copy the app icon: `dist/OpenSwap.app/Contents/Resources/AppIcon.icns`
   built from `macos/OpenSwapWidget/Host/Assets.xcassets/AppIcon.appiconset/`
   via `iconutil` (or reuse the `.icns` xcodebuild produced inside the Swift
   host's Resources), and set `CFBundleIconFile` = `AppIcon` in the bundle's
   `Info.plist` with `/usr/libexec/PlistBuddy`.

**Verify**: `plutil -lint dist/OpenSwap.app/Contents/Info.plist` → `OK`;
`ls dist/OpenSwap.app/Contents/PlugIns/` → the appex.

### Step 6: Sign inside-out, notarize, staple

Append to `build.sh`, with `IDENTITY` read from the environment
(`OPENSWAP_SIGN_IDENTITY`, the string recorded in Step 0) and never hard-coded:

```bash
APP=dist/OpenSwap.app
# 1. every Mach-O the freezer produced (dylibs, .so, Python framework)
find "$APP/Contents/Frameworks" "$APP/Contents/Resources" -type f \
  \( -name '*.dylib' -o -name '*.so' -o -perm -u+x \) -print0 |
  xargs -0 codesign --force --options runtime --timestamp --sign "$IDENTITY"
# 2. helper tool
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  "$APP/Contents/MacOS/openswap-widget-reload"
# 3. the appex with its sandbox entitlements
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  --entitlements ../../macos/OpenSwapWidget/Widget/Widget.entitlements \
  "$APP/Contents/PlugIns/OpenSwapWidgetExtension.appex"
# 4. the outer app last, with the hardened-runtime entitlements
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  --entitlements entitlements.plist "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
ditto -c -k --keepParent "$APP" dist/OpenSwap.zip
xcrun notarytool submit dist/OpenSwap.zip --keychain-profile openswap-notary --wait
xcrun stapler staple "$APP"
spctl --assess --type execute --verbose=2 "$APP"
```

If the Python framework inside `Contents/Frameworks` is a directory bundle
(`Python.framework`), sign it as a bundle (`codesign … Python.framework`)
after its inner binaries; PyInstaller 6 normally ships a flat `Python`
dylib instead.

**Verify**: notarytool prints `status: Accepted`; `spctl` prints `accepted`
and `source=Notarized Developer ID`. If notarization is `Invalid`, run
`xcrun notarytool log <id> --keychain-profile openswap-notary` and fix only
signing/entitlement issues it names; anything else is a STOP.

### Step 7: Run the bundle as a user would and record the verdict

This Mac already runs OpenSwap: `~/Applications/OpenSwap.app` is the current
widget host (bundle id `com.opensoft.openswap.widget`) and its appex is the
one WidgetKit knows about (`pluginkit -m -i com.opensoft.openswap.widget.extension -v`
prints that path). The spike bundle carries an appex with the **same** id, so
the old app must be moved aside, not just stopped, or WidgetKit sees two
parents for one extension.

**Never switch accounts during this step.** The extra and the CLI act on the
operator's live Claude login. `list` (a Keychain read) is the proof the
frozen build needs; the switch checks belong to the operator.

Prepare (run each line, in order):

```bash
launchctl bootout gui/$(id -u)/com.opensoft.openswap.menubar
launchctl bootout gui/$(id -u)/com.opensoft.openswap.widget
pluginkit -r ~/Applications/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex
mv ~/Applications/OpenSwap.app ~/Applications/OpenSwap.app.pre-spike
cp -R packaging/macos/dist/OpenSwap.app /Applications/
pluginkit -a /Applications/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex
pluginkit -m -i com.opensoft.openswap.widget.extension -v
```

The last command must list exactly one path, under `/Applications`. If it
still lists the `~/Applications` one, STOP.

Checks:

1. `open -a /Applications/OpenSwap.app`. Expected: the status item appears
   within a few seconds, the popover opens on click, cards show usage bars.
   Do not click a card.
2. `log show --last 5m --predicate 'process == "OpenSwap"'` (or Console.app):
   no `ModuleNotFoundError`, no signature or library-validation errors.
3. `/Applications/OpenSwap.app/Contents/MacOS/OpenSwap list` from a terminal.
   Expected: the same table as `openswap list` (the frozen build reads the
   Keychain). Also `/Applications/OpenSwap.app/Contents/MacOS/OpenSwap` with
   no arguments prints help rather than starting a second extra.
4. Add the OpenSwap widget from Edit Widgets. Expected: it renders cards from
   `~/Library/Application Support/OpenSwap/widget-snapshot.json`. Also note
   whether a widget that was already on the desktop before this step kept
   rendering or went blank; plan 008 needs that fact.
5. In one terminal run
   `log stream --predicate 'subsystem == "com.apple.widgetkit"'`; in another
   run `/Applications/OpenSwap.app/Contents/MacOS/openswap-widget-reload`.
   Expected: the stream shows a reload request naming
   `com.opensoft.openswap.widget.extension` within a couple of seconds. This
   is the in-bundle reload proof (plan 008 has the extra call this helper
   instead of posting the Darwin notification). No account switch is needed.
6. **Operator only**: a card click switches accounts; a widget tap switches
   (the extra must be running); `/Applications/OpenSwap.app/Contents/MacOS/OpenSwap switch <n>`
   from a terminal matches `openswap switch <n>`. Record these three as
   `OPERATOR` in the verdict and leave them; the operator fills in PASS or
   FAIL, or drops them.

Restore (run each line, in order):

```bash
pkill -x OpenSwap
pluginkit -r /Applications/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex
rm -rf /Applications/OpenSwap.app
mv ~/Applications/OpenSwap.app.pre-spike ~/Applications/OpenSwap.app
pluginkit -a ~/Applications/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.opensoft.openswap.widget.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.opensoft.openswap.menubar.plist
```

Write `packaging/macos/README.md` with: how to run `build.sh`, the env vars it
needs (`OPENSWAP_SIGN_IDENTITY`, the `openswap-notary` profile name), the
bundle size from `du -sh dist/OpenSwap.app`, and a **Spike verdict** section
listing each of the six checks above as PASS, FAIL, or OPERATOR with one line
of evidence. That verdict is what plan 008 is gated on.

**Verify**: `test -f packaging/macos/README.md && grep -c "PASS\|FAIL\|OPERATOR" packaging/macos/README.md` → at least 6.

## Test plan

- `tests/test_menubar.py`: frozen guard in `ensure_notification_identity`
  (Step 1).
- `tests/test_cli.py`: frozen no-args without a terminal launches the menu
  bar; with a terminal it prints help (Step 2).
- Pattern: neighbouring tests in the same files.
- Manual: the six checks in Step 7, recorded in `packaging/macos/README.md`;
  check 6 is the operator's.
- Verification: `uv run pytest` → all pass, three new tests.

## Done criteria

- [ ] `uv run pytest` exits 0 with three more tests than at `92722b1` (2264 passed)
- [ ] `packaging/macos/build.sh` exists and, with the two env inputs, produces a stapled `dist/OpenSwap.app`
- [ ] `spctl --assess --type execute --verbose=2 packaging/macos/dist/OpenSwap.app` → `accepted`, `source=Notarized Developer ID`
- [ ] `packaging/macos/README.md` has a Spike verdict with all six checks marked
- [ ] `~/Applications/OpenSwap.app` is back in place and both LaunchAgents are loaded again (`launchctl list | grep opensoft` shows both labels)
- [ ] `grep -rn "Developer ID Application:" packaging/ macos/` returns no matches (identity is env-only)
- [ ] `git status` shows no files outside the in-scope list modified; `packaging/macos/dist/` and `build/` are git-ignored
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- Step 0 finds no Developer ID Application certificate or no notarytool
  profile. Only the operator can create those.
- The code at the locations in "Current state" does not match the excerpts.
- PyInstaller cannot import `rumps` or `AppKit` in the frozen app after two
  `hiddenimports` attempts. Report the exact traceback; the fallback is
  py2app with a framework Python, which is a different plan.
- Notarization returns `Invalid` for a reason other than a signing or
  entitlement problem you can name from the notarytool log.
- The reload helper (Step 7 check 5) produces no reload request in
  `log stream`. Report it. The fallback for plan 008 is the existing 30-line
  observer in `macos/OpenSwapWidget/Host/main.swift`, embedded in this same
  bundle as a helper app, not a new design; the operator still approves it.
- You are about to switch accounts, click a card, or tap the widget. Those
  are the operator's checks (Step 7 check 6).
- You find yourself wanting to change `widget_install.py`, `launch_agent.py`,
  or `update_check.py`. That is plan 008.

## Maintenance notes

- After this spike, plan 008 rewires the product: `launch_agent.resolve_program`
  must accept the bundle executable, `widget_app_path` must accept
  `/Applications` as well as `~/Applications`, `notify_widget_reload` should
  run the helper when frozen, `openswap upgrade` must print
  `brew upgrade --cask openswap` for a frozen install, the tap and cask
  (with `app` and `binary` stanzas and a `zap` stanza for the LaunchAgents
  and `Application Support/OpenSwap`) get written, and CI builds the release
  on a macOS runner with the certificate in secrets.
- Existing users have LaunchAgents pointing at the uv console script. Plan
  008 needs a one-time migration that rewrites those plists.
- The outer bundle id changes from `com.opensoft.openswap.widget` (Swift
  host) to `com.opensoft.openswap`. Widgets already placed on a desktop may
  go blank and need re-adding when 008 ships; Step 7 check 4 records what
  actually happens, and 008's release note must say so.
- Reviewer focus: the signing order in `build.sh` (inside-out, outer app
  last), the two entitlements on the outer app (no more than needed), and
  that no identity or credential string landed in any file.
- Deferred: `SMAppService` login item instead of a LaunchAgent. The existing
  LaunchAgent code works and is tested; do not switch mechanisms in the spike.
