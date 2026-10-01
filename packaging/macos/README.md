# macOS spike build (plan 007)

One `OpenSwap.app`: PyInstaller-frozen extra + embedded WidgetKit appex +
in-bundle reload helper. Not a product install path (that is plan 008).

Local builds freeze unsigned. Signing and notarization run on GitHub Actions
(`.github/workflows/macos-app.yml`) once repository secrets are set. Never
put the identity string or notary credentials in a file.

## Build (unsigned, this Mac)

From the repo root:

```bash
./packaging/macos/build.sh
```

Needs Xcode, xcodegen, and uv. Output: `packaging/macos/dist/OpenSwap.app`
plus `OpenSwap-<version>.zip` and `.sha256` (all gitignored). Bundle size on
this spike Mac: 26M. The version comes from `pyproject.toml`;
`OPENSWAP_BUILD_NUMBER` (CI: the run number) sets `CFBundleVersion`.

Ship or copy the zip, not the `.app` folder. It is made with `ditto` after
stapling; copying the folder by other means (including
`actions/upload-artifact`) drops execute bits and symlinks and breaks the
signature. To try a CI build, download the `OpenSwap-app` artifact and unpack
the zip inside it with `ditto -x -k OpenSwap-*.zip .`.

For an isolated experimental build without replacing the ordinary dist app,
set absolute `OPENSWAP_DIST_DIR`, `OPENSWAP_BUILD_DIR`, and
`OPENSWAP_DERIVED_DIR` paths. See
[desktop testing](../../docs/chatgpt-desktop-testing.md) for the experimental
ChatGPT restart-and-switch action. Building does not install or launch the app.

To sign locally as well, export `OPENSWAP_SIGN_IDENTITY` (the quoted
codesigning identity) and `OPENSWAP_NOTARY_PROFILE=openswap-notary` after
`xcrun notarytool store-credentials openswap-notary`. CI does this from
secrets instead.

## GitHub Actions

Workflow `macOS app` freezes on `workflow_dispatch`, version tags (`v*`),
and pull requests that touch packaging or the widget project. Pull requests
always stay unsigned. Tag builds fail if the certificate secret is missing.

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

1. Bump `version` in `pyproject.toml`, commit, and push tag `v<version>`.
   The workflow fails if the tag and the pyproject version disagree.
2. `macOS app` builds, signs, notarizes, unpacks the zip and assesses that
   copy, then drafts a GitHub release with `OpenSwap-<version>.zip`, its
   `.sha256`, and `openswap.rb` rendered from
   `packaging/homebrew/openswap.rb.in`.
3. Check the draft and publish it. `Homebrew tap`
   (`.github/workflows/homebrew-tap.yml`) then copies `openswap.rb` into
   `mar3co/homebrew-openswap/Casks/`. It needs repository secret
   `HOMEBREW_TAP_TOKEN` (fine-grained token, Contents read/write on the tap
   repo only); without it the job warns and the cask is copied by hand. It
   can be re-run for any tag from the Actions tab.

Users then install with `brew install --cask mar3co/openswap/openswap`. The
app code still assumes a uv install until plan 008 lands; do not publish a
release before that.

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
