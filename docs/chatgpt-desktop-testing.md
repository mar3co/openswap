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
- At least two OAuth logins saved through the menu bar or `openswap codex add`.
  The currently active login must also be managed. API-key slots are refused.
- Codex automatic switching off. Stop other Codex CLI/IDE clients. OpenSwap
  refuses other detected Codex processes rather than killing them.

## Before clicking

Save and finish **all local and remote work**, including this conversation if
you are viewing it in ChatGPT. The action quits the whole app, not just a
window. OpenSwap cannot determine whether a cloud task is idle; confirming the
dialog or passing `--confirm-idle` is your acknowledgement, not an automated
idle check. There is no force-quit fallback.

The ChatGPT account-switch confirmation also asks permission to pause Codex
automatic rotation when enabled. Canceling leaves settings and credentials
unchanged. You can also turn off **Auto-switch Codex CLI accounts** in
Settings → Automation (shown when Claude auto-switching is on). The equivalent
CLI is:

```sh
openswap config set autoswitch.codexEnabled false
```

Keep it off throughout this experiment. A running auto-switch worker rechecks
this setting under the credential lock before committing a switch.

## Add another account without logging out

Choose **ChatGPT → Add account**, or **Sign in with ChatGPT** on an empty tab.
OpenSwap launches the official Codex browser sign-in in a private temporary
home, without a Terminal window. Finish sign-in, review the email and workspace,
optionally enter a nickname, then choose **Save account**. Saving does not
change the active account, live credential file, or running ChatGPT app.

If your browser chooses the wrong account, use **Copy link** in another browser
profile. **Open browser** reopens the sign-in page. **Use a code** starts the
official device flow; availability depends on account or workspace policy.
Cancel and timeout stop the owned login process and clean up its temporary
credentials after it exits. Duplicate accounts are rejected, not overwritten.

New accounts are **enabled by default** when saved, with no second confirmation.
They are eligible for automatic Codex rotation when rotation is on; saving does
not itself switch accounts. Keep rotation off for desktop testing.
**Save current login** still captures an existing live login; it is not a logout
or a way to create a new session. No terminal commands are needed for this flow.

This uses the official [Codex authentication flow](https://learn.chatgpt.com/docs/auth).
Saving credentials is separate from the experimental desktop switch below;
desktop authentication still requires manual verification after a switch.

## ChatGPT Auto-switch

The ChatGPT tab has its own **Auto-switch** preference, independent of Claude.
It automatically selects a replacement using Codex quota and the configured
strategy. It does **not** use ChatGPT message limits or silently restart the app.

When a replacement is ready, choose **Review switch**. Finish your work, then
confirm the existing restart dialog. OpenSwap cannot reliably determine whether
all Chat, Work and Codex tasks are idle, so every restart requires confirmation.
Canceling leaves the active account unchanged.

Enabling this mode explains the confirmation requirement and pauses legacy
Codex credential-only rotation with your consent. The modes cannot run together.
Turn it off to discard the pending suggestion; onboarding and a desktop switch
pause monitoring. New accounts remain enabled by default, but saving does not
approve a restart. This is automatic selection with a confirmed switch, not
unattended desktop rotation.

## Menu-bar test

Quit the existing **OpenSwap** menu-bar instance yourself, then open the test
build. Do not run two OpenSwap menu-bar instances with the same account store.
Building does not install, launch, or replace your normal app.

Open the menu-bar popover, select **ChatGPT**, then click your target account.
Changing tabs only changes the view, never credentials. The selected tab is
remembered until OpenSwap restarts, including visits to Settings.
Read the confirmation and choose **Restart ChatGPT** only after work is idle.
This opens ChatGPT even if it was previously closed. The menu shows a busy
state while the transaction runs. The Claude tab, widget, and ordinary CLI
commands retain their existing behavior; only ChatGPT popover account clicks
use the desktop flow. If the native popover cannot attach, the experimental
submenu remains available in the fallback menu.

ChatGPT and Codex share this account list. The usage bars show **Codex usage**,
not ChatGPT message limits. “Selected” means the shared credential file matches
that account, not that desktop authentication has been verified. API-key slots
are shown as CLI-only and cannot be selected here. Rotate/Best stay on the
Claude tab; the overflow menu labels its Claude actions explicitly.

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

The experimental controls reuse native tabs, account cards, and modal dialogs.
Widget behavior is unchanged.

| Before | After |
|--------|-------|
| Mixed Claude/Codex list | Remembered Claude and ChatGPT tabs with provider-specific accounts and status |
| Separate desktop submenu | One ChatGPT card action for the shared ChatGPT/Codex login; submenu only as a fallback |
| Codex rotation control hidden when general auto is off | Explicit rotation-pause consent integrated into the switch confirmation |
| Ambiguous usage and active labels | Codex usage caption, shared-file “selected” badge, and CLI-only API-key cards |
| Claude strategy actions beside Codex cards | Rotate/Best shown only on Claude; overflow actions explicitly label Claude |
| No desktop restart consent | Mandatory dialog explaining whole-app restart, idle acknowledgement, and shared auth |
| No desktop operation state | Background worker with a busy menu and duplicate-action guard |
| No desktop completion state | Manual-verification dialog and persistent in-process status, or sanitized failure/recovery guidance |
| Bare empty-list label | Provider-specific guidance: browser sign-in for ChatGPT, current-login capture for Claude |
| Loading/read failures look like no accounts | Separate loading, unavailable, and retryable error states |
| Empty tabs show irrelevant controls | Auto-switch, Rotate/Best, usage captions, and running/restart status hidden until accounts are present |
| Capturing the current login is the only add-account path | Isolated browser sign-in, with copy-link and device-code alternatives |
| No sign-in progress or identity review | Inline progress, cancel/retry, account review and optional nickname |
| Browser-added accounts require Save, then Enable | Save enables the account immediately; the success screen offers only Done |
| Sign-in competes with normal account captions | Focused sign-in view, aligned actions, wrapped errors, and nickname drafts preserved across navigation |
| Auto-switch is visible only on Claude | Independent ChatGPT Auto-switch control in the same header position |
| No desktop-safe automatic selection | Selection-only monitor with a concise confirmation-required caption |
| Replacement choice requires manual inspection | Switch ready status and Review switch action; the existing restart confirmation still applies |
| Policy controls depend only on Claude auto-switch | Threshold and strategy remain available when ChatGPT automatic selection is on |
| Ambiguous shared Settings toggle | General and Automation sections with distinct Claude, ChatGPT, and Codex CLI labels plus one shared policy |
| Crowded account footer | Wider Add account and Review switch buttons without truncated labels |

## Validation recorded for this test build

ChatGPT Auto-switch follow-up on 2026-09-17: 2,699 tests passed, 3 skipped.
Fifteen dedicated monitor lifecycle tests and a selection-only Codex-engine test
cover stale events/identities, independent settings, consent, conflicts and
pause/resume. Fourteen synthetic native previews passed in light/dark themes.
No real account switch or ChatGPT restart was performed; the new preference
defaults off. Automatic selection still requires the operator to confirm each
desktop restart and verify the resulting authenticated account.

Browser-onboarding follow-up on 2026-09-17: 2,681 tests passed, 3 skipped.
Twenty synthetic native previews passed across light/dark themes, including
browser/device handoff, wrapped errors and nickname persistence across navigation.
Isolated login success/cancel/timeout, malformed stores, duplicate accounts and
enabled insertion are covered without real OAuth. Real browser sign-in acceptance
is still pending; no live login was replaced or revoked during development.

Tabbed-UI follow-up on 2026-09-17: 2,636 tests passed, 3 skipped. Synthetic
native AppKit previews passed in light and dark appearance, including tab
navigation, usage bars, API-key restrictions, and verification status. The
editable menu-bar installation was reloaded for testing. This UI pass did not
switch credentials or restart ChatGPT; the separate widget host was unchanged.

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
