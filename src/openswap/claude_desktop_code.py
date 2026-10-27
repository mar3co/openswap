"""Join Desktop onboarding to the existing Code roster, without switching it.

Desktop cookies establish identity only. Code authorization uses the shared CLI
login runner in a disposable config directory. Only a server-verified credential
is saved; no Desktop cookie is converted into, or stored as, a Code credential.
"""

from __future__ import annotations

import hashlib
import json
import queue
import tempfile
import threading
import time
from pathlib import Path

from openswap import oauth
from openswap.claude_desktop import ClaudeDesktopError, launch_environment
from openswap.claude_desktop_identity import observe_login, _read
from openswap.claude_login import ClaudeLoginRun
from openswap.exceptions import ClaudeSwitchError
from openswap.models import get_timestamp
from openswap.session import delete_macos_keychain_entry, read_config_dir_credentials
from openswap.worker.leases import AccountLeaseStore


def identity(profile):
    if profile.archived or not profile.auto_detect or not profile.detected_at:
        return None
    if not profile.account_uuid or not profile.organization_uuid or not profile.detected_login:
        return None
    return profile.account_uuid, profile.organization_uuid, profile.email.casefold()


def setup_key(profile, engine):
    return hashlib.sha256(json.dumps([identity(profile), str(engine.backup_dir.resolve())]).encode()).hexdigest()


def matching_slot(engine, profile):
    """Names and email alone cannot distinguish organizations or account owners."""
    wanted = identity(profile)
    if wanted is None:
        return None
    matches = [str(num) for num, row in ((engine.sequence_data() or {}).get("accounts") or {}).items()
               if row.get("kind", "oauth") == "oauth"
               and ((row.get("uuid") or "").lower(), (row.get("organizationUuid") or "").lower(),
                    (row.get("email") or "").casefold()) == wanted]
    if len(matches) > 1:
        raise ClaudeDesktopError("More than one saved Code account matches. Resolve the duplicate in Claude Code.")
    return matches[0] if matches else None


def completed_login_identity(directory):
    """Identity recorded by the successful CLI OAuth exchange in our scratch dir."""
    try:
        account = json.loads(_read(directory / ".claude.json", 2_000_000))["oauthAccount"]
        return ((account.get("accountUuid") or "").lower(), (account.get("organizationUuid") or "").lower(),
                (account.get("emailAddress") or "").casefold())
    except Exception:
        return None


def verify_credentials(credentials, profile, *, authorized_identity=None):
    wanted = identity(profile)
    if wanted is None:
        raise ProfileChanged("The Desktop account is no longer detected.")
    token = oauth.extract_access_token(credentials or "")
    if not token:
        raise ClaudeDesktopError("Claude Code did not save a login. Continue setup to try again.")
    found = oauth.fetch_oauth_profile(token)
    if not found:
        raise ClaudeDesktopError("Could not verify the Code login. Continue setup to try again.")
    actual = ((found.get("uuid") or "").lower(), (found.get("organizationUuid") or "").lower(),
              (found.get("email") or "").casefold())
    # Some personal-account responses omit organization/email. Only a fresh,
    # successful CLI OAuth exchange can supply those missing fields. A nonempty
    # server field always wins; config can never overrule a disagreement.
    if (actual[0] != wanted[0] or any(value and value != expected for value, expected in zip(actual, wanted))
            or (not all(actual) and authorized_identity != wanted)
            or (authorized_identity is not None and authorized_identity != wanted)):
        raise IdentityMismatch("Choose the same account and organization as this Desktop profile.")


class IdentityMismatch(ClaudeDesktopError):
    pass


class ProfileChanged(ClaudeDesktopError):
    pass


def save_verified_account(engine, profile, credentials):
    """Commit previously verified authorization through the shared store/lease guard.

    Caller holds the Desktop metadata lock and rechecks its login fingerprint here.
    No network calls, active credential writes, or account switching happen here.
    """
    if identity(profile) is None or not engine._validate_email(profile.email):
        raise ClaudeDesktopError("The detected account is not valid for Claude Code.")
    with AccountLeaseStore(engine.backup_dir, "claude").mutation_guard() as guard:
        guard.assert_available()
        engine._setup_directories()
        engine._init_sequence_file()
        data = engine.sequence_data()
        existing = matching_slot(engine, profile)
        if existing:
            return existing  # another add may have completed during authorization
        legacy = None
        for num, row in data["accounts"].items():
            if ((row.get("email") or "").casefold() == profile.email.casefold()
                    and (row.get("organizationUuid") or "").lower() == profile.organization_uuid):
                if row.get("uuid") or row.get("kind", "oauth") != "oauth" or legacy is not None:
                    raise ClaudeDesktopError("The saved Code account has a different identity. Check it in the Claude Code tab.")
                legacy = str(num)
        number = legacy or str(engine._get_next_account_number())
        email = data["accounts"][number]["email"] if legacy else profile.email
        config = json.dumps({"oauthAccount": {
            "emailAddress": email, "accountUuid": profile.account_uuid,
            "organizationUuid": profile.organization_uuid, "organizationName": profile.organization,
        }})
        try:
            engine._write_account_credentials(number, email, credentials)
            engine._write_account_config(number, email, config)
            data["accounts"][number] = {
                **data["accounts"].get(number, {}),
                "email": email, "uuid": profile.account_uuid,
                "organizationUuid": profile.organization_uuid, "organizationName": profile.organization,
                "added": data["accounts"].get(number, {}).get("added") or get_timestamp(),
            }
            if int(number) not in data["sequence"]:
                data["sequence"].append(int(number))
            data["sequence"].sort()
            data["lastUpdated"] = get_timestamp()
            engine._write_json(engine.sequence_file, data)
            engine._usage_store.clear_dead_token([number], {number: (email, profile.organization_uuid)})
        except Exception:
            # Only clean files when the roster did not commit this new slot.
            if not legacy and number not in ((engine.sequence_data() or {}).get("accounts") or {}):
                engine._delete_account_files(number, profile.email)
            raise
        return number


class DesktopCodeConnector:
    def __init__(self, store, engine, *, resolve_binary, notify=lambda *_: None,
                 login_factory=ClaudeLoginRun, observe=observe_login,
                 read_credentials=read_config_dir_credentials, cleanup=delete_macos_keychain_entry,
                 clock=time.monotonic):
        self.store, self.engine = store, engine
        self.resolve_binary, self.notify = resolve_binary, notify
        self.login_factory, self.observe = login_factory, observe
        self.read_credentials, self.cleanup, self.clock = read_credentials, cleanup, clock
        self._active = None
        self._login = None
        self._results = queue.SimpleQueue()
        self._next_scan = 0.0

    def suspend(self):
        if self._active:
            self._active[2].set()
        if self._login:
            self._login.cancel()

    def state(self, profile):
        if identity(profile) is None:
            return {}
        try:
            number = matching_slot(self.engine, profile)
            if number:
                return {"code_status": "In Claude Code", "code_slot": number}
            state = profile.code_setup_state if profile.code_setup_key == setup_key(profile, self.engine) else ""
            running = self._active and self._active[:2] == (profile.id, profile.code_setup_key)
            if state == "authorizing" and not running:
                state = "sign_in"  # interrupted setup resumes only on an explicit click
            messages = {
                "": "Adding to Claude Code…", "authorizing": "Finish sign-in in your browser",
                "sign_in": "Code setup paused", "connected": "Code account removed",
                "install": "Claude Code isn’t installed", "failed": "Code setup needs attention",
                "mismatch": "Choose the same Code account",
            }
            return {"code_status": messages[state],
                    **({"code_action": "cancel_code" if running else "connect_code"} if state else {})}
        except Exception:
            return {"code_status": "Code accounts unavailable", "code_action": "connect_code"}

    def retry(self, identifier):
        profile = self.store.get(identifier)
        if identity(profile) is None:
            raise ClaudeDesktopError("Detect this profile's account before continuing Code setup.")
        if self._active and self._active[0] == profile.id:
            return
        self.store.update(profile.id, expected=profile, code_setup_key="", code_setup_state="")
        self._next_scan = 0.0

    def cancel(self, identifier):
        if self._active and self._active[0] == identifier:
            self.suspend()

    def _record(self, profile, key, state):
        current = self.store.get(profile.id)
        if identity(current) != identity(profile) or identity(current) is None:
            return False
        self.store.update(profile.id, expected=current, code_setup_key=key if state else "", code_setup_state=state)
        return True

    def poll(self, *, enabled, idle=True):
        changed = False
        if not enabled:
            self.suspend()
        if self._active:
            try:
                current = self.store.get(self._active[0])
                if setup_key(current, self.engine) != self._active[1] or identity(current) is None:
                    self.suspend()
            except Exception:
                self.suspend()
        while True:
            try:
                profile, key, state = self._results.get_nowait()
            except queue.Empty:
                break
            self._active, self._login = None, None
            recorded = False
            try:
                recorded = self._record(profile, key, state)
                changed |= recorded
            except (ClaudeSwitchError, OSError):
                pass
            if recorded and state == "connected" and enabled:
                self.notify("Claude Code account added", f"{profile.email} · {profile.organization}")
        if not enabled or not idle or self._active or self.clock() < self._next_scan:
            return changed
        self._next_scan = self.clock() + 10
        try:
            for profile in self.store.list():
                if identity(profile) is None:
                    continue
                key = setup_key(profile, self.engine)
                if matching_slot(self.engine, profile):
                    if profile.code_setup_key != key or profile.code_setup_state != "connected":
                        changed |= self._record(profile, key, "connected")
                    continue
                if profile.code_setup_key == key:
                    continue  # no repeated browsers after denial, cancellation or restart
                binary = self.resolve_binary()
                if not binary:
                    changed |= self._record(profile, key, "install")
                    continue
                if not self._record(profile, key, "authorizing"):
                    continue
                cancel = threading.Event()
                self._active = (profile.id, key, cancel)
                try:
                    threading.Thread(target=self._connect, args=(profile, key, binary, cancel),
                                     daemon=True, name="claude-desktop-code").start()
                except Exception:
                    self._results.put((profile, key, "failed"))
                return True
        except (ClaudeSwitchError, OSError):
            pass
        return changed

    def _connect(self, profile, key, binary, cancel):
        state = "failed"
        try:
            fingerprint = self.observe(self.store, profile)
            self._check_current(profile, fingerprint, cancel)
            # An existing legacy slot can be linked after verifying its token.
            for number, row in ((self.engine.sequence_data() or {}).get("accounts") or {}).items():
                if ((row.get("email") or "").casefold() == profile.email.casefold()
                        and (row.get("organizationUuid") or "").lower() == profile.organization_uuid
                        and not row.get("uuid") and row.get("kind", "oauth") == "oauth"):
                    credentials, unreadable = self.engine.read_backup_credentials(str(number), row["email"])
                    if unreadable:
                        raise ClaudeDesktopError("Code credentials are unavailable.")
                    try:
                        verify_credentials(credentials, profile)
                    except ClaudeDesktopError:
                        break  # complete fresh authorization and repair the legacy slot
                    self._check_current(profile, fingerprint, cancel)
                    self.engine.backfill_account_uuid(str(number), profile.account_uuid,
                                                     expected_email=row["email"], expected_org=row["organizationUuid"],
                                                     expected_credentials=credentials)
                    if not matching_slot(self.engine, profile):
                        raise ClaudeDesktopError("The saved account changed during setup.")
                    state = "connected"
                    return
            with tempfile.TemporaryDirectory(prefix=".code-login-", dir=self.store.root) as raw:
                directory = Path(raw)
                try:
                    done, results = threading.Event(), []

                    def finished(result):
                        results.append(result)
                        done.set()

                    run = self.login_factory(binary, profile.email, env=launch_environment(directory), on_done=finished)
                    self._login = run
                    self._check_current(profile, fingerprint, cancel)
                    run.start()
                    while not done.wait(0.25):
                        if cancel.is_set():
                            run.cancel()
                    result = results[-1]
                    if result.canceled or result.timed_out or cancel.is_set():
                        state = "sign_in"
                        return
                    if result.returncode != 0:
                        return
                    credentials = self.read_credentials(raw, strict_keychain=True)
                    verify_credentials(credentials, profile, authorized_identity=completed_login_identity(directory))
                    # A rename is harmless; sign-out, org change, manual labels,
                    # archiving or disabling the feature invalidate this handoff.
                    with self.store._lock():
                        self._check_current(profile, fingerprint, cancel)
                        save_verified_account(self.engine, profile, credentials)
                    state = "connected"
                finally:
                    self.cleanup(directory)  # only this disposable login's Keychain entry
        except IdentityMismatch:
            state = "mismatch"
        except ProfileChanged:
            state = "sign_in" if cancel.is_set() else ""
        except Exception:
            state = "sign_in" if cancel.is_set() else "failed"
        finally:
            self._results.put((profile, key, state))

    def _check_current(self, profile, fingerprint, cancel):
        current = self.store.get(profile.id)
        if (cancel.is_set() or identity(current) != identity(profile) or not fingerprint
                or current.detected_login != fingerprint or profile.detected_login != fingerprint
                or self.observe(self.store, profile) != fingerprint):
            raise ProfileChanged("The Desktop account changed during Code setup.")
