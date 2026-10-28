# macOS spike build (plan 007)

One `OpenSwap.app`: PyInstaller-frozen extra + embedded WidgetKit appex +
in-bundle reload helper. Users install it with the Homebrew cask
(`packaging/homebrew/openswap.rb.in`); the app code that runs it from the
bundle is described in [widget](../../docs/widget.md#cask-app-bundle) and
[menu bar](../../docs/menubar.md#process).

Local builds freeze unsigned. Signing and notarization run on GitHub Actions
(`.github/workflows/macos-app.yml`) once repository secrets are set. Never
put the identity string or notary credentials in a file.

## Build (unsigned, this Mac)

From the repo root:

```bash
./packaging/macos/build.sh
```

Needs Xcode, xcodegen, and uv. Output: `packaging/macos/dist/OpenSwap.app`
plus `OpenSwap-<version>-<arch>.zip` and `.sha256` (all gitignored). Bundle
size on this spike Mac: 26M. The version comes from `pyproject.toml`;
`OPENSWAP_BUILD_NUMBER` (CI: the run number) sets `CFBundleVersion`.

The app is single-architecture: uv's Python builds are per CPU, so PyInstaller
freezes for the Mac it runs on. `OPENSWAP_ARCH` (`arm64` or `x86_64`, default
`uname -m`) names the zip and is passed to PyInstaller as `target_arch`, which
refuses an arch the running Python cannot produce. Before zipping, `build.sh`
fails if any Mach-O in the bundle does not include that arch (a universal
widget appex or reload helper passes).

Ship or copy the zip, not the `.app` folder. It is made with `ditto` after
stapling; copying the folder by other means (including
`actions/upload-artifact`) drops execute bits and symlinks and breaks the
signature. To try a CI build, download the `OpenSwap-app-arm64` or
`OpenSwap-app-x86_64` artifact and unpack the zip inside it with
`ditto -x -k OpenSwap-*.zip .`.

For an isolated experimental build without replacing the ordinary dist app,
set absolute `OPENSWAP_DIST_DIR`, `OPENSWAP_BUILD_DIR`, and
`OPENSWAP_DERIVED_DIR` paths. See
[desktop testing](../../docs/chatgpt-desktop-testing.md) for the experimental
ChatGPT restart-and-switch action. Building does not install or launch the app.

To sign locally as well, export `OPENSWAP_SIGN_IDENTITY` (the quoted
codesigning identity) and `OPENSWAP_NOTARY_PROFILE=openswap-notary` after
`xcrun notarytool store-credentials openswap-notary`. CI does this from
secrets instead.

## Native appearance in DEV and release

Use `openswap.spec` for all frozen builds, including lightweight UI previews.
`appkit_compat.py` pins the main executable's linked SDK to 26.2 after
PyInstaller creates it and checks the finished bundle. This keeps AppKit's
native tab/button styling consistent on the same macOS version, even when
local and CI Python distributions were built with different SDKs. It preserves
the executable's minimum macOS version and repairs its ad-hoc signature before
bundling; release signing still runs last. CI also checks the shipped zip.
The SDK baseline is a deliberate UI choice, not an environment override.

A local preview runner can use the same spec without building the widget:

```bash
OPENSWAP_DEV_ENTRY=/absolute/path/to/local-preview.py \
  uv run --with pyinstaller --with '.[menubar]' pyinstaller \
  --noconfirm --clean --distpath /tmp/openswap-dev-dist \
  --workpath /tmp/openswap-dev-build packaging/macos/openswap.spec
```

The optional runner uses the DEV bundle identifier; the appearance policy and
Info.plist settings remain shared. The runner controls any DEV label or startup
behavior. `build.sh` always uses the release entry point, even if
`OPENSWAP_DEV_ENTRY` is set. Building does not launch or install either app.
Running the source directly under Python cannot guarantee the same appearance:
macOS uses that Python executable's linked SDK. Test visual changes in the
frozen preview instead of maintaining a separate spec or copying SDK values
from an installed app. macOS versions and system appearance settings can still
change how native controls look.

## GitHub Actions

Workflow `macOS app` freezes on `workflow_dispatch`, version tags (`v*`),
and pull requests that touch packaging or the widget project. It runs twice,
natively: `arm64` on `macos-26` and `x86_64` on `macos-26-intel`, and each leg
unpacks its zip and runs `OpenSwap --version` from it. Pull requests always
stay unsigned. Tag builds fail if the certificate secret is missing.

Repository secrets (Settings → Secrets and variables → Actions):

| Secret | What it is |
|---|---|
| `SIGNING_APPLICATION_P12_BASE64` | base64 of the application signing .p12 |
| `SIGNING_APPLICATION_P12_PASSWORD` | password used when exporting that .p12 |
| `SIGNING_IDENTITY` | optional pin of the quoted identity name |
| `NOTARY_KEY_ID` | App Store Connect API key id |
| `NOTARY_ISSUER_ID` | App Store Connect issuer UUID |
| `NOTARY_KEY_BASE64` | base64 of the `.p8` API key |

`SIGNING_P12_PASSWORD` is an accepted alias for the .p12 password. Encode
with `base64 -i cert.p12 | pbcopy` (no line wraps).

If notarization is rejected, `build.sh` prints Apple's `notarytool log` for
the submission before failing, so the reason is in the job log.

## Releasing

1. In one PR, bump `version` in `pyproject.toml`, run `uv lock` (the lock
   carries the project's own version and CI syncs with `--locked`), and
   rename `## Unreleased` in `CHANGELOG.md` to `## <version> (YYYY-MM-DD)`.
   `test_the_current_version_has_notes` fails a bump without that section.
2. Once it merges, tag the merge commit and push the tag (pushing it is what
   starts the build):

   ```bash
   git fetch origin
   git tag -a v<version> <merge-sha> -m "OpenSwap <version>"   # or origin/main if nothing merged since
   git push origin refs/tags/v<version>
   ```

   The workflow fails if the tag and the pyproject version disagree, or if
   `CHANGELOG.md` has no section for the version.
3. `macOS app` builds, signs, notarizes, unpacks the zip and assesses that
   copy on each architecture. Once both legs pass, it drafts a GitHub release
   with `OpenSwap-<version>-arm64.zip`, `OpenSwap-<version>-x86_64.zip`,
   both DMGs, their `.sha256` files, and `openswap.rb` rendered from
   `packaging/homebrew/openswap.rb.in`. The cask's `on_arm` and `on_intel`
   blocks carry each zip's URL and checksum, so Homebrew downloads the one
   built for the user's CPU. The release notes are the version's
   `CHANGELOG.md` section (`packaging/release_notes.py`) plus a link to
   every commit since the previous tag.
4. Check the draft and publish it. `Homebrew tap`
   (`.github/workflows/homebrew-tap.yml`) then copies `openswap.rb` into
   `mar3co/homebrew-openswap/Casks/`. It needs repository secret
   `HOMEBREW_TAP_TOKEN` (fine-grained token, Contents read/write on the tap
   repo only). If the secret is missing, empty or cannot push to the tap,
   the job fails with an error naming it, and the cask is not pushed (write
   access is checked even when the tap already has the cask); fix the
   secret and re-run the workflow for that tag from the Actions tab
   (`gh workflow run homebrew-tap.yml -f tag=v<version>`), or copy
   `openswap.rb` into the tap by hand.

Users then install with `brew install --cask mar3co/openswap/openswap` and
run `openswap setup`, which also moves a Mac off the uv install (see
[menu bar](../../docs/menubar.md#moving-from-a-source-install)). Before
publishing, check the draft on a clean account and on a Mac with the uv
install (plan 008 Step 7).

## Spike verdict

Recorded 2026-09-09 against `claude/007-single-app-spike`. This Mac has
Apple Development identities only, so local sign/notarize/staple did not
run. Freeze/assemble did. Step 7 did not replace `~/Applications/OpenSwap.app`
or boot out the live widget host (same appex id). The live extra was
stopped only long enough to launch the frozen extra, then restored.

1. **PASS** (unsigned, process) — after bootout of
   `com.opensoft.openswap.menubar`,
   `packaging/macos/dist/OpenSwap.app/Contents/MacOS/OpenSwap menubar`
   stayed up (pid 8348, empty stderr, no `ModuleNotFoundError`) and wrote
   usage lines to `openswap.log`. Popover click and `open -a` were not
   used (unsigned Gatekeeper). Live extra (`~/.local/bin/openswap menubar`)
   and widget host (`~/Applications/OpenSwap.app`) were running again
   afterwards; pluginkit still lists the live appex only.
2. **FAIL** — unsigned `spctl --assess` on the dist app:
   `invalid Info.plist (plist or signature have been modified)`
   (PlistBuddy sets `CFBundleIconFile` after PyInstaller's ad-hoc sign;
   inside-out signing in CI is what repairs that).
3. **PASS** — `packaging/macos/dist/OpenSwap.app/Contents/MacOS/OpenSwap list`
   exit 0, table identical to live `openswap list` (same Keychain accounts,
   787 bytes on this run). Same binary with no args on a TTY prints help
   (`Commands:`) and does not leave a second extra running.
4. **FAIL** — Edit Widgets not exercised; live widget host was left in place
   so WidgetKit would not see two parents for one appex id. Needs a
   notarized CI artifact before repeating Step 7 against `/Applications`.
5. **PASS** (helper runs) — in-bundle `openswap-widget-reload` exits 0.
   WidgetKit log proof against a registered spike appex is still deferred
   (same reason as 4).
6. **OPERATOR** — card click / widget tap / frozen `switch <n>` left for the
   operator; executor must not switch the live Claude login.

Plan 008 is gated on a notarized PASS of 1–5 from the GitHub artifact
(`source=Notarized Developer ID` on `spctl --assess`). Re-run the
workflow after the secrets above exist.
