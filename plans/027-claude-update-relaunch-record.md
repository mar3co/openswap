# Profiles closed by Claude's own update: record

## Problem

Claude Desktop installs a downloaded update by itself once a window has been
idle ("stealth update"). Before quitting it writes `stealth-relaunch` into that
instance's user-data folder, and Claude deletes the file the next time that
folder starts. ShipIt then relaunches `/Applications/Claude.app` without
`--user-data-dir`, so only the Default profile comes back. Every other profile
stays closed until the user notices. Seen 2026-11-12: VS Personal quit at
16:55:30 for 5.11492.0 and stayed closed until reopened by hand at 18:13.

ShipIt waits for every instance of the shared app to exit, so a profile that
quits while another profile is in use can't be updated yet.

## Rulings

- 2026-11-12, user: while the update waits on other open profiles, the closed
  profile stays closed and OpenSwap notifies once that it closed for the update
  and that Update Claude installs it now. Once the update has installed,
  OpenSwap reopens it. Rejected: reopening at once on the old version, since
  profiles would take turns closing and the update might never install.
- 2026-11-12, controller: Default is never reopened by this path; ShipIt
  relaunches the ordinary app, which is Default. It still gets the waiting
  notice.
- 2026-11-12, controller: one reopen attempt per marker, and markers older
  than 24 hours ignored. Both superseded by the review ledger below.

## Outcomes

| Profile state | Downloaded update | Other profile running | Outcome |
|---|---|---|---|
| no marker, archived, marker over 7 days old, running, or state unknown | any | any | nothing |
| closed with marker | unknown (status unreadable) | any | nothing |
| closed with marker | newer than installed | yes | one notice per marker |
| closed with marker | newer than installed | no | nothing (installing now) |
| managed, closed with marker | none newer | any | reopen (up to 3 tries), attach Dock icon, notify; notify once if all tries fail |
| Default, closed with marker | none newer | any | nothing |

- 2026-11-12, user: one more fix round after the final review, because its first finding meant the feature never fired. This is an exception to the one-dispatch budget, not a change to it.
- 2026-11-12, user: markers older than 7 days are ignored, so a profile left closed long ago stays closed. This supersedes the removal of the age limit after cold seat 2. The trade-off: if another profile stays open more than 7 days, the closed one isn't reopened, though the waiting notice already pointed to Update Claude.

## Review ledger

Cold seat 1 (Opus, diff only), 2026-11-12:
- Important: a failed or "already_opening" reopen was marked done and never retried. Fixed: up to 3 tries per marker; "launched" and "already_running" end it. Supersedes "one reopen attempt per marker" above.
- Minor: an unexpected error from attaching the Dock icon dropped the reopened list. Fixed: that catch is broad, because the icon is optional.
- Minor: the reopen notice said "after Claude's update" even when the download was discarded rather than installed. Fixed: the copy now says Claude had closed the profile to install an update.
- Minor: missing tests for failed reopens. Added. Declined: separate tests for the generation and enabled skips inside the loop. They are the same guards `test_passive_label_refresh_waits` already pins before the thread starts.

Cold seat 2 (Opus, diff only), 2026-11-12:
- Important: the waiting notice promises a reopen, but the 24-hour cutoff dropped the marker when the other profile stayed open for over a day. Reachable in normal use. Fixed: the age cutoff is removed. A marker exists only when Claude closed the profile for an update and nothing has opened it since, so reopening it is what Claude intended. Archived profiles stay excluded.
- Important: after 3 failed tries the profile stayed closed with no message. Fixed: a one-time "didn't reopen" notice.
- Minor: an unexpected exception type from opening dropped the whole pass. Fixed: opening now catches any exception and counts it as a failed try.
- Minor: overlapping label threads could make the try count imprecise. Declined: the per-profile launch lock prevents a double launch, and the count only bounds retries.
- Minor: test gaps. Added a poll-level test that Default is left to Claude. Declined: a test for the in-loop generation/enabled guard (see seat 1).

Cold seat 3 (Opus, diff only, final), 2026-11-12:
- Important, verified on the user's Mac: Squirrel keeps ShipItState.plist after installing, pointing at the update copy it has already moved into place, so `downloaded_update` returned None (unknown), and nothing ever reopened. It also blanked the existing "No update downloaded" label after every install. Fixed: a state file whose update copy no longer exists reads as "" (nothing waiting).
- Important: old markers from before this feature could reopen long-closed profiles. Fixed per the user's 7-day ruling.
- Minor: `_relaunch_tries` is written on the label thread without a lock. Declined: threads are serialized by the 60 s gate, and the per-profile launch lock prevents a double launch.
- Minor: the reopened and didn't-reopen notices ignore a stale generation. Declined: the launch already happened, so reporting it is correct.

Cold seat 4 (Opus, diff only, on the extra round), 2026-11-12. Under the budget, every non-Critical finding is filed, not fixed. None was Critical.
- Important: the marker's contents are never read. Claude also writes it for a user quit with a saved page (`userQuit`) and for a navigation-restore relaunch (`navOnly`), and a check can land in Claude's own relaunch gap. Filed as mar3co/openswap#190.
- Important: a marker whose tries are used up still gets the waiting notice on a later update. Filed as mar3co/openswap#191.
- Minor: Claude treats its own marker as stale after 5 minutes, while OpenSwap honors it for 7 days. This is deliberate, per the user's 7-day ruling: OpenSwap acts later, after the install, which Claude's own relaunch never waits for.
