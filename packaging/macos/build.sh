#!/usr/bin/env bash
# Freeze and assemble OpenSwap.app. Sign, notarize, and staple when an
# identity is in the environment (GitHub Actions or a local keychain).
#
# Identity is env-only: OPENSWAP_SIGN_IDENTITY.
# Notary: OPENSWAP_NOTARY_PROFILE (keychain) or OPENSWAP_NOTARY_KEY_PATH +
# OPENSWAP_NOTARY_KEY_ID + OPENSWAP_NOTARY_ISSUER.
# OPENSWAP_BUILD_NUMBER (optional, default 1) becomes CFBundleVersion.
# OPENSWAP_ARCH (optional, default `uname -m`) is arm64 or x86_64. The freeze
# is single-architecture because uv's Python is, so a release builds once per
# arch on a matching runner and the cask picks the zip for the user's CPU.
#
# Every run ends with $DIST/OpenSwap-<version>-<arch>.zip and its .sha256. The zip is
# made with ditto after stapling, so it is the file to ship: a plain copy of
# the .app folder (actions/upload-artifact, cp without -R) drops the execute
# bits and symlinks and breaks the signature. Beside it goes
# OpenSwap-<version>-<arch>.dmg and its .sha256: the same app next to a link
# to /Applications, for installing without Homebrew. A signed build signs the
# DMG; a notarized one also notarizes and staples it. The cask uses the zip.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
WIDGET="$ROOT/macos/OpenSwapWidget"
DERIVED="${OPENSWAP_DERIVED_DIR:-${HOME}/Library/Caches/openswap-spike}"
DIST="${OPENSWAP_DIST_DIR:-$HERE/dist}"
WORK="${OPENSWAP_BUILD_DIR:-$HERE/build}"
APP="$DIST/OpenSwap.app"
IDENTITY="${OPENSWAP_SIGN_IDENTITY:-}"
VERSION="$(awk -F'"' '/^version = /{print $2; exit}' "$ROOT/pyproject.toml")"
BUILD_NUMBER="${OPENSWAP_BUILD_NUMBER:-1}"
export OPENSWAP_BUILD_NUMBER="$BUILD_NUMBER"
if [[ -z "$VERSION" ]]; then
  echo "could not read version from $ROOT/pyproject.toml" >&2
  exit 1
fi
ARCH="${OPENSWAP_ARCH:-$(uname -m)}"
case "$ARCH" in
  arm64 | x86_64) ;;
  *)
    echo "OPENSWAP_ARCH must be arm64 or x86_64, not '$ARCH'" >&2
    exit 1
    ;;
esac
# openswap.spec hands this to PyInstaller as target_arch, which refuses an
# arch the running Python cannot produce instead of freezing the wrong one.
export OPENSWAP_ARCH="$ARCH"
RELEASE_ZIP="$DIST/OpenSwap-$VERSION-$ARCH.zip"
RELEASE_DMG="$DIST/OpenSwap-$VERSION-$ARCH.dmg"

# Every Mach-O in the bundle must run on $ARCH: the cask serves this zip to
# that CPU only, so one wheel or helper built for the other arch would ship a
# broken app. The appex and helper may be universal; that is fine.
check_arch() {
  local bad=0 f archs
  while IFS= read -r -d '' f; do
    file -b "$f" | grep -q '^Mach-O' || continue
    archs="$(lipo -archs "$f" 2>/dev/null || true)"
    if ! grep -qw -- "$ARCH" <<<"$archs"; then
      echo "not $ARCH: ${f#"$APP"/} (${archs:-unknown})" >&2
      bad=1
    fi
  done < <(find "$APP/Contents" -type f -print0)
  if [[ "$bad" != 0 ]]; then
    echo "$APP has Mach-O files that do not run on $ARCH" >&2
    exit 1
  fi
  echo "every Mach-O in $APP runs on $ARCH"
}

checksum() {
  (cd "$DIST" && shasum -a 256 "$(basename "$1")" > "$(basename "$1").sha256")
  echo "packaged $1 ($(cut -d' ' -f1 < "$1.sha256"))"
}

# Submit one file to Apple and wait. notarytool can exit 0 on an Invalid
# result, which would surface later as a bare stapler error, so check the
# status here and print Apple's log instead. Needs NOTARY_AUTH.
notarize() {
  local file="$1" json status id
  json="$WORK/notary-$(basename "$file").json"
  xcrun notarytool submit "$file" --wait --output-format json "${NOTARY_AUTH[@]}" \
    > "$json" || true
  cat "$json"
  status="$(plutil -extract status raw -o - "$json" 2>/dev/null || true)"
  if [[ "$status" != "Accepted" ]]; then
    id="$(plutil -extract id raw -o - "$json" 2>/dev/null || true)"
    echo "notarization status for $(basename "$file"): ${status:-unknown}" >&2
    if [[ -n "$id" ]]; then
      xcrun notarytool log "$id" "${NOTARY_AUTH[@]}" >&2 || true
    fi
    exit 1
  fi
}

# The disk image a person installs from: the finished app next to a link to
# /Applications, so the window says "drag here". ditto keeps the signature
# and the stapled ticket. hdiutil on CI runners sometimes fails with
# "Resource busy", so it gets three tries.
make_dmg() {
  local root="$WORK/dmg-root" attempt
  rm -rf "$root" "$RELEASE_DMG" "$RELEASE_DMG.sha256"
  mkdir -p "$root"
  ditto "$APP" "$root/OpenSwap.app"
  ln -s /Applications "$root/Applications"
  for attempt in 1 2 3; do
    if hdiutil create -volname OpenSwap -srcfolder "$root" -fs HFS+ \
      -format UDZO -ov "$RELEASE_DMG"; then
      break
    fi
    if [[ "$attempt" == 3 ]]; then
      echo "hdiutil could not create $RELEASE_DMG" >&2
      exit 1
    fi
    sleep 5
  done
  rm -rf "$root"
}

# Zip the finished app and build its DMG for shipping. Called on every exit
# path, so an unsigned PR build uploads the same files a release does. With
# an identity the DMG is signed; `package notarize` also notarizes and
# staples it, which changes the file, so its checksum is written last.
package() {
  rm -f "$RELEASE_ZIP" "$RELEASE_ZIP.sha256"
  ditto -c -k --keepParent "$APP" "$RELEASE_ZIP"
  checksum "$RELEASE_ZIP"
  make_dmg
  if [[ -n "$IDENTITY" ]]; then
    codesign --force --timestamp --sign "$IDENTITY" "$RELEASE_DMG"
  fi
  if [[ "${1:-}" == "notarize" ]]; then
    notarize "$RELEASE_DMG"
    xcrun stapler staple "$RELEASE_DMG"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$RELEASE_DMG"
  fi
  checksum "$RELEASE_DMG"
}

cd "$WIDGET"
xcodegen generate

xcodebuild -project OpenSwapWidget.xcodeproj -scheme OpenSwapWidget \
  -configuration Release -derivedDataPath "$DERIVED" \
  -destination generic/platform=macOS \
  MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
  CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=NO build
xcodebuild -project OpenSwapWidget.xcodeproj -scheme OpenSwapWidgetReload \
  -configuration Release -derivedDataPath "$DERIVED" \
  MARKETING_VERSION="$VERSION" CURRENT_PROJECT_VERSION="$BUILD_NUMBER" \
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
check_arch

if [[ -z "$IDENTITY" ]]; then
  echo "OPENSWAP_SIGN_IDENTITY is unset; leaving $APP unsigned." >&2
  echo "GitHub Actions (.github/workflows/macos-app.yml) signs and notarizes when secrets are present." >&2
  package
  exit 0
fi

# 1. every Mach-O the freezer produced (dylibs, .so, the Python library).
# Match on file type, not name or mode: PyInstaller's libpython has no
# extension, and an executable script is not a Mach-O and needs no signature.
find "$APP/Contents/Frameworks" "$APP/Contents/Resources" -type f -print0 |
  while IFS= read -r -d '' f; do
    if file -b "$f" | grep -q '^Mach-O'; then
      printf '%s\0' "$f"
    fi
  done |
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
  package
  exit 0
fi

# Upload copy for notarytool only; the shipped zip is rebuilt after stapling.
ZIP="$WORK/OpenSwap-notarize.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

if [[ -n "${OPENSWAP_NOTARY_KEY_PATH:-}" ]]; then
  : "${OPENSWAP_NOTARY_KEY_ID:?OPENSWAP_NOTARY_KEY_ID is required with OPENSWAP_NOTARY_KEY_PATH}"
  : "${OPENSWAP_NOTARY_ISSUER:?OPENSWAP_NOTARY_ISSUER is required with OPENSWAP_NOTARY_KEY_PATH}"
  NOTARY_AUTH=(--key "$OPENSWAP_NOTARY_KEY_PATH"
    --key-id "$OPENSWAP_NOTARY_KEY_ID"
    --issuer "$OPENSWAP_NOTARY_ISSUER")
elif [[ -n "${OPENSWAP_NOTARY_PROFILE:-}" ]]; then
  NOTARY_AUTH=(--keychain-profile "$OPENSWAP_NOTARY_PROFILE")
else
  echo "signed $APP; notarization skipped (set OPENSWAP_NOTARY_PROFILE or OPENSWAP_NOTARY_KEY_PATH)."
  package
  exit 0
fi

notarize "$ZIP"

xcrun stapler staple "$APP"
spctl --assess --type execute --verbose=2 "$APP"
package notarize
