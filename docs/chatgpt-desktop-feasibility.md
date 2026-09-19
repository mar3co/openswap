# ChatGPT desktop account switching: feasibility study

Research date: 2026-09-17 UTC. Baseline: PR #36 merged at `8d33808`.

## Decision

Reopen desktop support as a candidate. The earlier blanket statement that
ChatGPT desktop cannot be a rotation target is not established by the evidence.
The installed unified ChatGPT app has account-change handling and uses Codex
app-server authentication. This is evidence for feasibility, not a verified
OpenSwap desktop switch implementation.

The initial research commit did not add a switch command. The follow-up now
includes an explicitly experimental, user-confirmed restart-and-switch action;
see [the testing guide](chatgpt-desktop-testing.md). This is not verified
production desktop support. In particular, neither
replacing a file nor restarting an Electron window proves that Chat, Work, and
Codex now use the same intended account. A real two-account desktop test is
still required before advertising support.

## Official protocol evidence

[Authentication](https://learn.chatgpt.com/docs/auth) documents browser sign-in
for the desktop app and Codex clients. Codex supports file, OS credential-store,
automatic, and ephemeral credential storage. `CODEX_HOME` selects the home
directory; `cli_auth_credentials_store="file"` selects file storage. The
documentation explicitly describes shared cached login for
CLI and IDE; it does not by itself establish every desktop storage boundary.

[App-server authentication](https://learn.chatgpt.com/docs/app-server) exposes
account reads, login, logout, and account-change notifications. Its experimental
external-token mode delegates token refresh to the host application. That mode
is not a drop-in replacement for a managed stored OAuth login, nor does starting
a separate app-server establish control of the desktop's existing connection.

## Installed application evidence

Inspected application binaries and packaged JavaScript, not user credentials:

- App: `/Applications/ChatGPT.app`, version `26.908.70816`, build `9275`.
- Bundle ID: `com.openai.codex`.
- Bundled CLI: `0.154.0-alpha.6.2`.
- Archive: `Contents/Resources/app.asar`.

These findings apply to this version of the unified app. They do not establish
support for older native ChatGPT apps, other platforms, or future builds.

In the archive's `.vite/build/main-DaMR-wdT.js`:

- The notification handler dispatches `account/updated` to auth-status
  listeners. Account information caches are invalidated on account updates
  and authenticated-principal changes.
- Account-bound ChatGPT managers are retired when the account/user identity
  changes. Thread state also responds to identity changes.
- Principal changes trigger site-authentication cleanup. Logout and changes
  away from ChatGPT authentication also clear associated browser/site state.
- Electron's `userData` and the connection's Codex home are separate storage
  namespaces. Isolated app-launch code configures both; changing only one is
  not evidence of a fully isolated desktop profile.

The archive's
`webview/assets/chatgpt-desktop-auth-url-d6611578ee33.js` builds the desktop
browser sign-in bridge. Workspace selection includes current/allowed workspace
information and refreshes after login. A workspace change within one login is
not proof of switching between separate user accounts.

The app therefore already has mechanisms to react to authentication changes.
The open question is how an external OpenSwap action can reliably cause the
right backend and frontend to observe the same change. A permanently stale
token cache is not an established finding.

Additional lifecycle findings from the archive:

- `.vite/build/worker.js` resolves `CODEX_HOME`, otherwise `$HOME/.codex`.
  The effective home still needs verification for each running connection.
- `.vite/build/src-CCXHtyvY.js` implements an owned stdio child and kills it on
  connection close. The main bundle's daemon branch requires `CODEX_CLI_PATH`,
  no custom CLI command, no bundled CLI, and a supported daemon version.
  This suggests the stdio path for a normal bundled launch, but static
  inspection does not verify the current session's transport, exclude all
  persistent helpers, or establish actual shutdown behavior.
- `.vite/build/window-all-closed-BxbCP6YG.js` keeps the packaged macOS app
  alive when its last window closes. Closing a window is not quitting the app.
- Remote-control/socket plumbing exists, but this investigation did not
  establish a supported external endpoint for changing the live desktop
  backend's login. A newly launched app-server is a different process.

## Isolated backend experiment

### Follow-up: automatic identity verification (2026-09-17)

Read-only inspection of the running installed app confirmed that its main
process owns a bundled `codex app-server` child. That child's standard input
and output are connected directly to the desktop process. It had no named
Unix listener or TCP listening socket for an independent `account/read`
client. This is a live observation for this run, not a claim about every
desktop version or configuration.

The current official App Server documentation describes `account/read` with
`refreshToken: false`, and stdio, Unix-socket, and WebSocket transports. Merely
starting another listener would inspect a different backend and would not
verify the app's existing session. We did not start a new backend, intercept
the existing stdio connection, or enable debugging/remote access.

The app process also has private IPC sockets. Their existence is not evidence
of a supported external identity-verification API; they were not queried.
The computer-use tool explicitly blocked inspection of `com.openai.codex` for
safety reasons, so the Accessibility/profile-menu proposal could not be
validated in this environment. No alternate UI-inspection mechanism was used.

Automatic desktop-profile verification remains unimplemented and unproven.
Any user-confirmed fallback must be labeled **manually checked**, not
automatically verified, and must expire on app restart, account change, or
loss of the evidence it was tied to. A fresh backend account read alone would
still not prove that Chat, Work, and Codex all adopted the same session.

Run the committed probe without touching a real login:

```sh
python3 tools/probe_codex_auth_reload.py --codex /Applications/ChatGPT.app/Contents/Resources/codex
```

The probe uses disposable home/config directories, explicitly selects file
storage, and uses fabricated API keys. It performs no model or quota calls and
prints only summarized protocol fields. It does not launch or control the UI.
Local observation on 2026-09-17 UTC: the bundled CLI above and PATH CLI
`0.153.4` produced the same results:

| Operation | Account read | Account-update notification |
|-----------|--------------|-----------------------------|
| Start without auth | none | — |
| Add auth file while backend runs | still none | none observed |
| Restart backend | API key | — |
| Delete auth file while backend runs | still API key | none observed |
| Restart backend again | none | — |
| Log in through that backend's protocol | API key | `apikey` |

Each live-file change was observed for 0.5 seconds, followed by an immediate
`account/read` with `refreshToken: false`. This establishes that file replacement
alone did not immediately update these instances; it does **not** prove that
auth can never reload later or through another operation. Synthetic API-key
results do not establish real OAuth refresh or full desktop behavior.

A separate exploratory synthetic external-token test reported
`chatgptAuthTokens`, did not create an auth file, and returned to no account on
restart. That test is not included in the committed probe. The mode's host-owned
refresh lifecycle makes it a separate integration, not evidence that rewriting
managed OAuth credentials will hot-switch the desktop.

## Evidence needed before a desktop switch command

Use a disposable desktop profile and two test logins. Record identity labels,
versions, and outcomes only; never publish tokens, cookies, or auth files.

1. Identify the desktop connection's actual home, credential-store mode, and
   backend lifetime. Confirm whether quitting the app also stops its backend.
2. Establish account A across the profile menu, a new Chat conversation, a new
   Work task, and a new Codex task. Check remote/cloud work separately.
3. Apply a supported login change or a controlled file-store change while no
   task is running. Observe backend account reads and frontend identity-change
   notifications. Do not infer success solely from a rewritten auth file.
4. Repeat A to B and B to A, including relaunch, token refresh, custom home,
   missing credentials, and failure recovery. Confirm the refreshed outgoing
   credentials are preserved and the target identity matches the selected slot.
5. Check account-scoped history, projects, plugin/browser auth, workspace
   restrictions, and active-task behavior. Define a switch boundary that does
   not move running work silently to a different account.

The first candidate to validate is **full quit → preserve outgoing credentials
→ switch the verified file store → relaunch → verify identity**. Full app quit
is expected to retire its owned backend, but that must be observed with a real
desktop test. Do not assume that `open -a` forwards shell `CODEX_HOME` settings
or that changing Electron's `userData` also changes its Codex home.

An implementation must refuse unknown storage/lifecycle arrangements and
active work; detect concurrent credential changes; validate the selected slot
against its stored identity; preserve tokens refreshed during shutdown; and
provide bounded shutdown, rollback, and authoritative postlaunch checks. A
decoded JWT or OpenSwap's active-slot bookkeeping is not proof that the desktop
has authenticated as the intended user. Keyring-backed logins need separate
support rather than silently falling back to a file.

The experimental action exists solely to gather that evidence with an operator;
do not promote it to supported desktop behavior until this matrix passes.
Keep automatic quota rotation separate: it needs its own active-task policy,
usage semantics, and recovery guarantees. A process-table scan currently used
for display is not a sufficient shutdown or mutation guard.

Static inspection found an internal renderer `readAccountInfo` path that derives
account and user IDs, email, and plan from the desktop connection's cached auth
token. No supported external attachment to that reader has been established;
private IPC was not queried. UI automation restrictions also prevented an
independent profile-menu inspection in this pass, which is not evidence that a
separately permissioned integration is impossible. Automatic running-profile
verification remains blocked on an accessible, independent identity source.
