#!/usr/bin/env bash
# Freeze and assemble OpenSwap.app. Sign, notarize, and staple when an
# identity is in the environment (GitHub Actions or a local keychain).
#
# Identity is env-only: OPENSWAP_SIGN_IDENTITY.
# Notary: OPENSWAP_NOTARY_PROFILE (keychain) or OPENSWAP_NOTARY_KEY_PATH +
# OPENSWAP_NOTARY_KEY_ID + OPENSWAP_NOTARY_ISSUER.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WIDGET="$ROOT/macos/OpenSwapWidget"
DERIVED="${OPENSWAP_DERIVED_DIR:-${HOME}/Library/Caches/openswap-spike}"
DIST="${OPENSWAP_DIST_DIR:-$HERE/dist}"
WORK="${OPENSWAP_BUILD_DIR:-$HERE/build}"
APP="$DIST/OpenSwap.app"
IDENTITY="${OPENSWAP_SIGN_IDENTITY:-}"

cd "$WIDGET"
xcodegen generate

xcodebuild -project OpenSwapWidget.xcodeproj -scheme OpenSwapWidget \
  -configuration Release -derivedDataPath "$DERIVED" \
  -destination generic/platform=macOS \
  CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=NO build
xcodebuild -project OpenSwapWidget.xcodeproj -scheme OpenSwapWidgetReload \
  -configuration Release -derivedDataPath "$DERIVED" \
  CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=NO build

cd "$HERE"
# Local extra from the repo root (.[menubar]). The plan's '../..[menubar]'
# string is missing the slash after the path.
uv run --directory "$ROOT" --with pyinstaller --with '.[menubar]' \
  pyinstaller --noconfirm --clean --distpath "$DIST" --workpath "$WORK" \
  "$HERE/openswap.spec"

RELEASE="$DERIVED/Build/Products/Release"
APPEX="$RELEASE/OpenSwap.app/Contents/PlugIns/OpenSwapWidgetExtension.appex"
HELPER="$RELEASE/openswap-widget-reload"
if [[ ! -d "$APPEX" ]]; then
  echo "missing appex at $APPEX" >&2
  exit 1
fi
if [[ ! -f "$HELPER" ]]; then
  echo "missing reload helper at $HELPER" >&2
  exit 1
fi

mkdir -p "$APP/Contents/PlugIns" "$APP/Contents/MacOS" "$APP/Contents/Resources"
rm -rf "$APP/Contents/PlugIns/OpenSwapWidgetExtension.appex"
cp -R "$APPEX" "$APP/Contents/PlugIns/OpenSwapWidgetExtension.appex"
cp "$HELPER" "$APP/Contents/MacOS/openswap-widget-reload"
chmod +x "$APP/Contents/MacOS/openswap-widget-reload"

ICNS="$RELEASE/OpenSwap.app/Contents/Resources/AppIcon.icns"
if [[ -f "$ICNS" ]]; then
  cp "$ICNS" "$APP/Contents/Resources/AppIcon.icns"
else
  ICONSET="$WORK/AppIcon.iconset"
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  SRC="$WIDGET/Host/Assets.xcassets/AppIcon.appiconset"
  cp "$SRC/icon_16.png" "$ICONSET/icon_16x16.png"
  cp "$SRC/icon_32.png" "$ICONSET/icon_16x16@2x.png"
  cp "$SRC/icon_32.png" "$ICONSET/icon_32x32.png"
  cp "$SRC/icon_64.png" "$ICONSET/icon_32x32@2x.png"
  cp "$SRC/icon_128.png" "$ICONSET/icon_128x128.png"
  cp "$SRC/icon_256.png" "$ICONSET/icon_128x128@2x.png"
  cp "$SRC/icon_256.png" "$ICONSET/icon_256x256.png"
  cp "$SRC/icon_512.png" "$ICONSET/icon_256x256@2x.png"
  cp "$SRC/icon_512.png" "$ICONSET/icon_512x512.png"
  cp "$SRC/icon_1024.png" "$ICONSET/icon_512x512@2x.png"
  iconutil -c icns -o "$APP/Contents/Resources/AppIcon.icns" "$ICONSET"
fi
/usr/libexec/PlistBuddy -c "Set :CFBundleIconFile AppIcon" "$APP/Contents/Info.plist" \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleIconFile string AppIcon" "$APP/Contents/Info.plist"

plutil -lint "$APP/Contents/Info.plist"

if [[ -z "$IDENTITY" ]]; then
  echo "OPENSWAP_SIGN_IDENTITY is unset; leaving $APP unsigned." >&2
  echo "GitHub Actions (.github/workflows/macos-app.yml) signs and notarizes when secrets are present." >&2
  exit 0
fi

# 1. every Mach-O the freezer produced (dylibs, .so, Python framework)
find "$APP/Contents/Frameworks" "$APP/Contents/Resources" -type f \
  \( -name '*.dylib' -o -name '*.so' -o -perm -u+x \) -print0 |
  xargs -0 codesign --force --options runtime --timestamp --sign "$IDENTITY"
if [[ -d "$APP/Contents/Frameworks/Python.framework" ]]; then
  codesign --force --options runtime --timestamp --sign "$IDENTITY" \
    "$APP/Contents/Frameworks/Python.framework"
fi
# 2. helper tool
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  "$APP/Contents/MacOS/openswap-widget-reload"
# 3. the appex with its sandbox entitlements
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  --entitlements "$WIDGET/Widget/Widget.entitlements" \
  "$APP/Contents/PlugIns/OpenSwapWidgetExtension.appex"
# 4. the outer app last, with the hardened-runtime entitlements
codesign --force --options runtime --timestamp --sign "$IDENTITY" \
  --entitlements "$HERE/entitlements.plist" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

if [[ "${OPENSWAP_SKIP_NOTARY:-}" == "1" ]]; then
  echo "OPENSWAP_SKIP_NOTARY=1; signed $APP but skipped notarization."
  exit 0
fi

ZIP="$DIST/OpenSwap.zip"
ditto -c -k --keepParent "$APP" "$ZIP"

if [[ -n "${OPENSWAP_NOTARY_KEY_PATH:-}" ]]; then
  : "${OPENSWAP_NOTARY_KEY_ID:?OPENSWAP_NOTARY_KEY_ID is required with OPENSWAP_NOTARY_KEY_PATH}"
  : "${OPENSWAP_NOTARY_ISSUER:?OPENSWAP_NOTARY_ISSUER is required with OPENSWAP_NOTARY_KEY_PATH}"
  xcrun notarytool submit "$ZIP" --wait \
    --key "$OPENSWAP_NOTARY_KEY_PATH" \
    --key-id "$OPENSWAP_NOTARY_KEY_ID" \
    --issuer "$OPENSWAP_NOTARY_ISSUER"
elif [[ -n "${OPENSWAP_NOTARY_PROFILE:-}" ]]; then
  xcrun notarytool submit "$ZIP" --wait \
    --keychain-profile "$OPENSWAP_NOTARY_PROFILE"
else
  echo "signed $APP; notarization skipped (set OPENSWAP_NOTARY_PROFILE or OPENSWAP_NOTARY_KEY_PATH)."
  exit 0
fi

xcrun stapler staple "$APP"
spctl --assess --type execute --verbose=2 "$APP"
