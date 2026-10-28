"""Join Desktop onboarding to the existing Code roster, without switching it.

Desktop cookies establish identity only. Code authorization uses the shared CLI
login runner in a disposable config directory. Only a server-verified credential
is saved; no Desktop cookie is converted into, or stored as, a Code credential.
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import shutil
import stat
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path

from openswap import oauth
from openswap.claude_desktop import ClaudeDesktopError, launch_environment
from openswap.claude_desktop_identity import observe_login, read_limited
from openswap.claude_login import ClaudeLoginRun
from openswap.exceptions import ClaudeSwitchError
from openswap.models import get_timestamp
from openswap.session import delete_macos_keychain_entry, read_config_dir_credentials
from openswap.worker.leases import AccountLeaseStore

_logger = logging.getLogger("openswap")
MISMATCH = "Choose the same account and organization as this Desktop profile."
OPEN_CLAUDE_CODE = "Open Claude Code once (sign in if it asks), then continue setup."


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
    """Identity recorded by the successful CLI OAuth exchange in our scratch dir.

    None means the config could not be read, which is never a mismatch.
    """
    try:
        account = json.loads(read_limited(directory / ".claude.json", 2_000_000))["oauthAccount"]
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
    # A nonempty server field always wins; config can never overrule a disagreement.
    if not actual[0] or any(value and value != expected for value, expected in zip(actual, wanted)):
        raise IdentityMismatch(MISMATCH)
    # Some personal-account responses omit organization/email. Only a fresh,
    # successful CLI OAuth exchange can supply those missing fields.
    if authorized_identity is None:
        if not all(actual):
            raise ClaudeDesktopError("Could not verify the Code login's organization. Continue setup to try again.")
    elif authorized_identity != wanted:
        raise IdentityMismatch(MISMATCH)


REFRESH_NEEDS_LOGIN = frozenset({"invalid_grant", "invalid_client", "no_refresh_token", "store-unmirrored"})


def answer(credentials):
    """The profile endpoint's ``(status, identity)`` for a saved token.

    No request is sent for a credential without an access token; it is
    incomplete, so only a browser login can repair its slot.
    """
    token = oauth.extract_access_token(credentials or "")
    if not token:
        return "incomplete", dict.fromkeys(("uuid", "organizationUuid", "email"))
    return oauth.classify_oauth_profile(token)


def _contradicts(found, expected):
    actual = ((found["uuid"] or "").lower(), (found["organizationUuid"] or "").lower(),
              (found["email"] or "").casefold())
    return any(value and expected_value and value != expected_value
               for value, expected_value in zip(actual, expected))


def verdict(status, found, profile, *, live=None):
    """A token's answer for this profile: match, mismatch, incomplete, rejected or transient.

    ``live`` is the live config's ``(email, organization, account uuid)``
    when the token is Claude Code's own. An answer that contradicts it is
    transient: Claude Code is switching or signing in, so a retry can help.
    """
    if status not in ("verified", "incomplete"):
        return status
    if live is not None and _contradicts(found, (live[2].lower(), live[1].lower(), live[0].casefold())):
        return "transient"
    if _contradicts(found, identity(profile)):
        return "mismatch"
    return "match" if status == "verified" else "incomplete"


def saved_login_mismatch(email, found):
    other = found.get("email") or ""
    named = f" ({other})" if other and other.casefold() != email.casefold() else ""
    return (f"The saved Claude Code login for {email} answers as a different account{named}. "
            f"To use this profile's account, choose Add account > Sign in to Claude Code… and sign in as {email}. "
            "This replaces that saved login.")


def refresh_step(outcome):
    """After the consume gate: verify the new token, log in by browser, or fail for a retry."""
    if outcome.error in REFRESH_NEEDS_LOGIN:
        return "login"
    if outcome.error or not outcome.credentials:
        return "failed"
    return "verify"


def delete_login_keychain_item(directory):
    """A scratch login's item must be confirmed gone, or absent, before its folder goes."""
    delete_macos_keychain_entry(directory, strict=True)


class IdentityMismatch(ClaudeDesktopError):
    def __init__(self, message=MISMATCH, *, note=""):
        super().__init__(message)
        self.note = note


class ProfileChanged(ClaudeDesktopError):
    pass


class ActionNeeded(ClaudeDesktopError):
    """Setup failed for a reason the user must fix; its message replaces the general one."""


def save_verified_account(engine, profile, credentials, *, guard=nullcontext):
    """Add a verified credential to the Code roster, or repair its legacy slot.

    Runs under the engine's mutation guard and writes only backups and the
    roster: the active login is never touched and no account is switched.
    ``guard`` is entered inside the engine lock twice, never around a slot
    write: once before the slot's files are written, so a caller can abandon
    a changed row while the slot is still untouched, and once around the
    roster commit. Abandoning at the commit removes a new slot's files
    before the engine lock is released. A legacy slot keeps the new
    credential, which is for its own email and organization: the write also
    invalidated the slot's session profile, so the spent original could
    leave the account signed out. Only a config this save created is removed.
    """
    if identity(profile) is None or not engine._validate_email(profile.email):
        raise ClaudeDesktopError("The detected account is not valid for Claude Code.")
    with AccountLeaseStore(engine.backup_dir, "claude").mutation_guard() as lease:
        lease.assert_available()
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
        original_config = ""
        if legacy:
            _, unreadable = engine.read_backup_credentials(number, email)
            if unreadable:
                raise ClaudeDesktopError("Code credentials are unavailable.")
            original_config = engine.read_account_config(number, email)
        with guard():
            pass
        written = created_config = committed = False
        try:
            engine.write_account_credentials(number, email, credentials)
            written = True
            # A legacy slot keeps the full config captured when it was added.
            if not original_config:
                engine._write_account_config(number, email, json.dumps({"oauthAccount": {
                    "emailAddress": email, "accountUuid": profile.account_uuid,
                    "organizationUuid": profile.organization_uuid, "organizationName": profile.organization,
                }}))
                created_config = True
            with guard():
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
                committed = True
        except Exception as exc:
            if legacy:
                if not written:
                    # A failed or timed-out store write may or may not have
                    # replaced the original; either credential is kept.
                    _logger.warning("could not write a Code account backup during setup: %s", type(exc).__name__)
                elif created_config and not committed:
                    try:
                        (engine.configs_dir / f".claude-config-{number}-{email}.json").unlink(missing_ok=True)
                    except OSError as cleanup:
                        _logger.warning("could not remove a Code account config after abandoned setup: %s",
                                        type(cleanup).__name__)
            else:
                # Only clean files when the roster did not commit this new slot.
                with suppress(Exception):
                    if number not in ((engine.sequence_data() or {}).get("accounts") or {}):
                        engine._delete_account_files(number, profile.email)
            raise
        try:
            engine._usage_store.clear_dead_token([number], {number: (email, profile.organization_uuid)})
        except Exception as exc:
            # The roster has committed; a stale usage quarantine is not a failed add.
            _logger.warning("could not clear the usage quarantine after adding a Code account: %s", type(exc).__name__)
        return number


class DesktopCodeConnector:
    def __init__(self, store, engine, *, resolve_binary, notify=lambda *_: None,
                 login_factory=ClaudeLoginRun, observe=observe_login,
                 read_credentials=read_config_dir_credentials, cleanup=delete_login_keychain_item,
                 clock=time.monotonic):
        self.store, self.engine = store, engine
        self.resolve_binary, self.notify = resolve_binary, notify
        self.login_factory, self.observe = login_factory, observe
        self.read_credentials, self.cleanup, self.clock = read_credentials, cleanup, clock
        self._active = None
        self._login = None
        self._results = queue.SimpleQueue()
        self._next_scan = 0.0
        self._started = time.time()
        self._sweeper = None
        self._notes = {}

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
            note = self._notes.get(profile.id, ("", ""))
            if state in ("failed", "mismatch") and note[0] == profile.code_setup_key and note[1]:
                messages[state] = note[1]  # held in memory; a restart shows the general message
            return {"code_status": messages[state],
                    **({"code_action": "cancel_code" if running else "connect_code"} if state else {})}
        except ClaudeDesktopError as exc:
            return {"code_status": str(exc)}  # duplicate roster rows; resolved in Claude Code, not by retrying
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
        elif self._sweeper is None:
            # Keychain deletes block; the UI thread never waits on them.
            self._sweeper = threading.Thread(target=self._sweep_orphaned_logins, daemon=True, name="claude-desktop-code-sweep")
            try:
                self._sweeper.start()
            except Exception:
                self._sweeper = None
        if self._active:
            try:
                current = self.store.get(self._active[0])
                if setup_key(current, self.engine) != self._active[1] or identity(current) is None:
                    self.suspend()
            except Exception:
                self.suspend()
        while True:
            try:
                profile, key, state, note = self._results.get_nowait()
            except queue.Empty:
                break
            self._active, self._login = None, None
            self._notes[profile.id] = (key, note)
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
                try:
                    slot = matching_slot(self.engine, profile)
                except ClaudeDesktopError:
                    continue  # duplicate roster rows for this profile only; state() shows the message
                if slot:
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
                    self._results.put((profile, key, "failed", ""))
                return True
        except (ClaudeSwitchError, OSError):
            pass
        return changed

    def _sweep_orphaned_logins(self):
        """Discard scratch logins a quit or crash left behind.

        Best-effort. A folder written since this process started may belong to
        a live login, so it is left alone.
        """
        try:
            folders = list(self.store.root.glob(".code-login-*"))
        except OSError:
            return
        for folder in folders:
            try:
                info = folder.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_mtime >= self._started:
                    continue
            except OSError:
                continue
            self._discard_login(folder)

    def _discard_login(self, directory):
        """Empty a scratch login, then remove it once its Keychain item is confirmed gone.

        A Keychain that cannot confirm (locked, denied, timed out) keeps the
        emptied folder: its path is the item's only pointer, and the next
        sweep tries again.
        """
        try:
            for child in directory.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            self.cleanup(directory)
            directory.rmdir()
        except Exception as exc:
            _logger.warning("could not discard a Code setup login: %s", type(exc).__name__)

    def _connect(self, profile, key, binary, cancel):
        state, launched, note = "failed", False, ""
        try:
            fingerprint = self.observe(self.store, profile)
            self._check_current(profile, fingerprint, cancel)
            guard = lambda: self._commit_guard(profile, fingerprint, cancel)
            # An existing legacy slot can be linked after verifying its token.
            for number, row in ((self.engine.sequence_data() or {}).get("accounts") or {}).items():
                if ((row.get("email") or "").casefold() == profile.email.casefold()
                        and (row.get("organizationUuid") or "").lower() == profile.organization_uuid
                        and not row.get("uuid") and row.get("kind", "oauth") == "oauth"):
                    credentials, unreadable = self.engine.read_backup_credentials(str(number), row["email"])
                    if unreadable:
                        raise ClaudeDesktopError("Code credentials are unavailable.")
                    verified = self._verify_legacy(str(number), row, credentials, profile)
                    if verified is None:
                        break  # complete fresh authorization and repair the legacy slot
                    self._check_current(profile, fingerprint, cancel)
                    self.engine.backfill_account_uuid(str(number), profile.account_uuid,
                                                     expected_email=row["email"], expected_org=row["organizationUuid"],
                                                     expected_credentials=verified, guard=guard)
                    if not matching_slot(self.engine, profile):
                        raise ClaudeDesktopError("The saved account changed during setup.")
                    state = "connected"
                    return
            raw = tempfile.mkdtemp(prefix=".code-login-", dir=self.store.root)
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
                launched = True
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
                self._check_current(profile, fingerprint, cancel)
                save_verified_account(self.engine, profile, credentials, guard=guard)
                state = "connected"
            finally:
                self._discard_login(directory)  # only this disposable login's files and Keychain entry
        except IdentityMismatch as exc:
            state, note = "mismatch", exc.note
        except ProfileChanged:
            # Once the browser opened, only an explicit click may open it again.
            state = "sign_in" if cancel.is_set() or launched else ""
        except Exception as exc:
            state = "sign_in" if cancel.is_set() else "failed"
            if isinstance(exc, ActionNeeded):
                note = str(exc)
        finally:
            self._results.put((profile, key, state, note))

    def _verify_legacy(self, number, row, credentials, profile):
        """The slot credential to pin a silent link to, or None to repair the slot by browser login.

        Claude Code owns the active account's credentials, so the live one
        is only read and verified. Any other slot's rejected token is
        refreshed only through the engine's consume gate, which serializes
        single-use refresh tokens and writes the successor itself.
        """
        email = row["email"]
        live = self.engine._live_identity_for(email, row["organizationUuid"])
        if live:
            if not oauth.extract_access_token(credentials or ""):
                return None  # the link would leave the slot without a Code credential
            active = self.engine._read_active_credentials()
            if active.degraded or active.value is None:
                status, found = "transient", {}
            else:
                status, found = answer(active.value)
            result = verdict(status, found, profile, live=live)
            if result == "rejected" and oauth.is_oauth_token_expired(
                    (oauth.extract_oauth_data(active.value) or {}).get("expiresAt")):
                raise ActionNeeded(OPEN_CLAUDE_CODE)
        else:
            status, found = answer(credentials)
            result = verdict(status, found, profile)
            if result == "rejected":
                if self.engine.live_session_pids_for(number, email):
                    raise ActionNeeded("Close this account’s running Claude Code session, then continue setup.")
                outcome = self.engine.consume_backup_grant(number, email, credentials, refuse_active=True)
                step = refresh_step(outcome)
                if step == "login":
                    return None
                if step == "failed":
                    raise ClaudeDesktopError("Could not refresh the saved Code login. Continue setup to try again.")
                credentials = outcome.credentials
                status, found = answer(credentials)
                result = verdict(status, found, profile)
        if result == "mismatch":
            raise IdentityMismatch(note=saved_login_mismatch(email, found))
        if result == "transient":
            raise ClaudeDesktopError("Could not verify the Code login. Continue setup to try again.")
        return credentials if result == "match" else None

    def _check_row(self, profile, fingerprint, cancel):
        current = self.store.get(profile.id)
        if (cancel.is_set() or identity(current) != identity(profile) or not fingerprint
                or current.detected_login != fingerprint or profile.detected_login != fingerprint):
            raise ProfileChanged("The Desktop account changed during Code setup.")

    def _check_current(self, profile, fingerprint, cancel):
        """Row check plus a cookie re-read; runs with no lock held."""
        self._check_row(profile, fingerprint, cancel)
        if self.observe(self.store, profile) != fingerprint:
            raise ProfileChanged("The Desktop account changed during Code setup.")

    @contextmanager
    def _commit_guard(self, profile, fingerprint, cancel):
        """Registry lock and row check for the fresh-row check and the roster commit, never a slot write.

        Entered only inside the engine lock.
        """
        with self.store.lock():
            self._check_row(profile, fingerprint, cancel)
            yield
