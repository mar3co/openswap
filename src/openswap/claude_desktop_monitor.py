"""Background account labels for registered Desktop profiles.

Only one worker runs at a time. Main-thread polling applies conditional metadata
updates; launching/focusing profiles never waits for this worker. File changes
trigger cookie inspection, and only changed logins or periodic refreshes make
account requests. Disabled, archived and manually managed profiles are skipped.
"""

from __future__ import annotations

import queue
import threading
import time

from openswap.claude_desktop_identity import AccountDetector, KeyAccessError, disk_stamp, observe_login

SCAN_INTERVAL = 10.0
REFRESH_INTERVAL = 600.0
RETRY_INTERVAL = 60.0
CLEAR_DETAILS = {"email": "", "organization": "", "detected_at": ""}


def _labels(profile):
    return profile.email, profile.organization, profile.detected_at, profile.account_uuid, profile.organization_uuid, profile.detected_login


class ProfileIdentityMonitor:
    def __init__(self, store, *, detector=None, clock=None, observe=None, stamp=None):
        self.store = store
        self.detector = detector or AccountDetector(store)
        self.clock = clock or time.monotonic
        self.observe = observe or observe_login
        self.stamp = stamp or disk_stamp
        self.statuses = {}
        self._records = {}
        self._results = queue.SimpleQueue()
        self._active = False
        self._generation = 0
        self._cancel = threading.Event()
        self._next_scan = 0.0
        self._enabled = True

    def suspend(self):
        self._generation += 1
        self._cancel.set()
        self._records.clear()
        self.statuses.clear()
        self._enabled = False
        self.detector.clear()

    def refresh(self, identifier=None):
        if identifier is None:
            self._records.clear()
        else:
            self._records.pop(identifier, None)
        self._next_scan = 0.0

    def poll(self, *, enabled, idle=True):
        changed = False
        if not enabled and self._enabled:
            self.suspend()
        if enabled and not self._enabled:
            self._enabled = True
            self._next_scan = 0.0
        # Modal edits hold results until they close. Their conditional writes
        # invalidate any result taken before the user's edit/archive/restore.
        if idle or not enabled:
            while True:
                try:
                    generation, outcome = self._results.get_nowait()
                except queue.Empty:
                    break
                if outcome is None:
                    self._active = False
                    continue
                if generation != self._generation or not enabled:
                    continue
                profile, record, details = outcome
                try:
                    current = self.store.get(profile.id)
                    if (current != profile or not current.auto_detect
                            or (record["stamp"] is not None and self.stamp(self.store, current) != record["stamp"])):
                        self.refresh(profile.id)
                        continue
                    if details is not None and _labels(current) != tuple(details.get(k, "") for k in ("email", "organization", "detected_at", "account_uuid", "organization_uuid", "detected_login")):
                        current = self.store.update(profile.id, expected=profile, **details)
                        changed = True
                    record["labels"] = _labels(current)
                    self._records[profile.id] = record
                    status = record["status"]
                    changed |= self.statuses.get(profile.id, "") != status
                    self.statuses[profile.id] = status
                except Exception:
                    self.refresh(profile.id)  # stale or unreadable metadata is never overwritten
        now = self.clock()
        if not enabled or not idle or self._active or now < self._next_scan:
            return changed
        self._next_scan = now + SCAN_INTERVAL
        try:
            profiles = [p for p in self.store.list() if p.auto_detect]
        except Exception:
            return changed
        ids = {p.id for p in profiles}
        self._records = {key: value for key, value in self._records.items() if key in ids}
        for key in list(self.statuses):
            if key not in ids:
                changed |= bool(self.statuses.pop(key))
        if not profiles:
            return changed
        generation = self._generation
        self._cancel = cancel = threading.Event()
        previous = dict(self._records)
        self._active = True

        def work():
            try:
                for profile in profiles:
                    if cancel.is_set():
                        break
                    outcome = self._check(profile, previous.get(profile.id, {}), cancel)
                    if outcome is not None:
                        self._results.put((generation, outcome))
            finally:
                self._results.put((generation, None))

        try:
            threading.Thread(target=work, daemon=True, name="claude-profile-accounts").start()
        except Exception:
            self._active = False
        return changed

    def _check(self, profile, previous, cancel):
        now = self.clock()
        details = None
        record = {"stamp": None, "fingerprint": None, "status": "Account details unavailable",
                  "next_check": now + RETRY_INTERVAL, "labels": _labels(profile)}
        try:
            stamp = self.stamp(self.store, profile)
            record["stamp"] = stamp
            if (stamp == previous.get("stamp") and _labels(profile) == previous.get("labels")
                    and now < previous.get("next_check", 0)):
                return None
            fingerprint = self.observe(self.store, profile)
            record["fingerprint"] = fingerprint
            if cancel.is_set():
                return None
            if not fingerprint:
                record["status"] = "Waiting for sign-in"
                if profile.detected_at:
                    details = CLEAR_DETAILS
            elif (fingerprint == previous.get("fingerprint") and _labels(profile) == previous.get("labels")
                  and now < previous.get("next_check", 0)):
                record = {**previous, "stamp": stamp}
            elif self.detector.denied:
                record["status"] = "Account detection paused"
            else:
                details = self.detector(self.store, profile.id, expected_fingerprint=fingerprint)
                if cancel.is_set():
                    return None
                # Capture a stable handoff to the UI thread. A sign-in change
                # after the request must not be bound to the new disk stamp.
                stamp = self.stamp(self.store, profile)
                if self.observe(self.store, profile) != fingerprint or self.stamp(self.store, profile) != stamp:
                    return None
                record.update(stamp=stamp, status="", next_check=self.clock() + REFRESH_INTERVAL)
        except KeyAccessError:
            details = None
            record["status"] = "Account detection paused"
        except Exception:
            details = None
        if (details is None and profile.detected_at and record["fingerprint"] is not None
                and record["fingerprint"] != profile.detected_login):
            # A known new login must not keep the previous detected identity;
            # the persisted login, not this process's memory, says what is known.
            details = CLEAR_DETAILS
        return profile, record, details
