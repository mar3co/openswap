# Test the experimental ChatGPT desktop switch

This build includes a real **quit → switch credentials → relaunch** action,
not just a diagnostic probe. It is experimental: OpenSwap verifies the local
transaction and app process lifecycle, not the authenticated UI identity.
You must verify the resulting account in Chat, Work, and Codex yourself.

## Supported first test

- macOS with the unified `/Applications/ChatGPT.app` (`com.openai.codex`) and
  its bundled Codex CLI. This first build checks the OpenAI signature and
  supports app version `26.908.70816`, build `9275`; an updated version needs
  another compatibility check. Older native ChatGPT apps are not supported.
- Default `~/.codex` home and file-backed OAuth credentials. Keyring, auto,
  ephemeral storage, custom homes, and custom authentication/backend setups
  are refused. Ordinary model preferences do not select a different login.
- At least two OAuth logins already captured with `openswap codex add`.
  The currently active login must also be managed. API-key slots are refused.
- Codex automatic switching off. Stop other Codex CLI/IDE clients. OpenSwap
  refuses other detected Codex processes rather than killing them.

## Before clicking

Save and finish **all local and remote work**, including this conversation if
you are viewing it in ChatGPT. The action quits the whole app, not just a
window. OpenSwap cannot determine whether a cloud task is idle; confirming the
dialog or passing `--confirm-idle` is your acknowledgement, not an automated
idle check. There is no force-quit fallback.

In **More… → Switch ChatGPT app (experimental)**, choose **Pause Codex
auto-switching for testing…**, or turn off **Auto-switch Codex accounts** in
Settings (shown when general auto-switching is on). The equivalent CLI is:

```sh
openswap config set autoswitch.codexEnabled false
```

Keep it off throughout this experiment. A running auto-switch worker rechecks
this setting under the credential lock before committing a switch.

## Menu-bar test

Quit the existing **OpenSwap** menu-bar instance yourself, then open the test
build. Do not run two OpenSwap menu-bar instances with the same account store.
Building does not install, launch, or replace your normal app.

Open **More… → Switch ChatGPT app (experimental) → your target account**.
Read the confirmation and choose **Restart ChatGPT** only after work is idle.
The menu shows a busy state while the transaction runs. The ordinary account
cards and widget retain their existing CLI-only switch behavior.

When ChatGPT reopens, check its profile menu and start a new Chat conversation,
a new Work task, and a new Codex task to confirm the selected identity. Check
account-scoped history and projects too. Switching files and launching a
process alone are not proof that every surface authenticated correctly.

Repeat A → B → A and record app version, selected slot, visible identity, and
result for each surface. Do not record or share tokens or auth-file contents.
Only use expendable test work until these checks pass.

## CLI test

With this checkout, use `uv run openswap` instead of `openswap` below; the
installed CLI may still be an older version. The frozen app's
`Contents/MacOS/OpenSwap` executable accepts these same arguments.

```sh
openswap codex list
openswap codex desktop status 2
openswap codex desktop switch 2 --confirm-restart --confirm-idle
```

`status` is a preflight, not an authenticated desktop identity check. `--json`
is available on both desktop commands. Successful switching returns
`status: awaiting_verification`; it does not claim the desktop is authenticated.

## Refusals and recovery

If preflight refuses the configuration, do not delete settings or credentials
to bypass it. Use a supported test setup or report the sanitized error. If
ChatGPT refuses to quit, finish the work or quit it yourself and retry.

OpenSwap preserves refreshed outgoing credentials and writes a private recovery
record before replacing the live file. On failure it restores only when it can
stop the app and prove it is not overwriting newer credentials or roster data.
An unresolved `desktop-recovery.json` in OpenSwap's Codex state directory blocks
another desktop transaction. This file contains credentials: **never paste,
upload, or commit it**. Check safe recovery status and use the guarded restore:

```sh
openswap codex desktop recovery-status
openswap codex desktop recover --confirm-restart --confirm-idle
```

The status output omits credentials. Recovery may leave ChatGPT stopped and
refuses to overwrite newer or unrecognized state. If it refuses, preserve the
record and use normal ChatGPT sign-in to regain access before seeking help
with the sanitized error. Do not open/share the journal or delete it to bypass
the safeguard. Keep all work idle during recovery.

To return to the prior login after a completed switch, repeat the explicit
desktop action for the prior managed slot, with work idle. If login fails,
normal ChatGPT sign-in remains available. Verify the visible account before
resuming work; keep automatic switching off until this experiment is complete.

## Native interface changes

The experimental controls reuse the existing native menu and modal-dialog
patterns; the usual card and widget interaction is unchanged.

| Before | After |
|--------|-------|
| No desktop switch action | Separate experimental account submenu under More… |
| Codex rotation control hidden when general auto is off | Separately confirmed pause action in the experimental submenu |
| No desktop restart consent | Mandatory dialog explaining whole-app restart, idle acknowledgement, and shared auth |
| No desktop operation state | Background worker with a busy menu and duplicate-action guard |
| No desktop completion state | Explicit manual-verification message or sanitized failure/recovery guidance |

## Validation recorded for this test build

On 2026-09-17 UTC: 2,619 tests passed, 3 skipped. The macOS app and widget host
built successfully; the frozen executable exposes the desktop commands. A
read-only check of the installed ChatGPT app passed the signature/version gate
and detected its running process. Its ordinary file-store configuration also
passed the configuration check. No real login was switched and ChatGPT was not
quit or relaunched during development.

The operator's existing editable menu-bar installation was restarted on the new
code and its separate widget host replaced, preserving the existing two-service
layout. The separate frozen test app is also available as a build artifact.
These are local development builds, not notarized releases. Real A → B → A
desktop authentication validation remains pending.
