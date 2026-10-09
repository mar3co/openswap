"""OpenSwap account engine: Isolated CLAUDE_CONFIG_DIR profile for idle-slot kickoff; adopt rotated creds back."""

from __future__ import annotations

from openswap.engine.notes import *  # noqa: F403
from openswap.worker.leases import AccountLeaseStore

class SessionProfileMixin:
    """Isolated CLAUDE_CONFIG_DIR profile for idle-slot kickoff; adopt rotated creds back."""

    def _session_dir(self, account_num: str, email: str) -> Path:
        from openswap.session import session_dir_for

        return session_dir_for(self.backup_dir, account_num, email)

    def _token_status_lines(
        self, account_info: tuple[int, str, str, str, bool, str, str]
    ) -> list[str]:
        """Source-labelled token-status lines for one account's display row."""
        num, email, _org_name, org_uuid, is_active, creds, _alias = account_info
        if looks_like_api_key(creds):
            return []
        if is_active:
            line = _label_token_status("active profile", creds)
            return [line] if line is not None else []

        from openswap.session import (
            read_session_credentials,
            session_identity_drifted,
        )

        lines: list[str] = []
        session_dir = self._session_dir(str(num), email)
        session_creds = read_session_credentials(session_dir)
        if session_creds:
            if session_identity_drifted(session_dir, email, org_uuid):
                lines.append("session profile: ignored (different account)")
            else:
                line = _label_token_status("session profile", session_creds)
                if line is not None:
                    lines.append(line)
        backup_line = _label_token_status("stored backup", creds)
        if backup_line is not None:
            lines.append(backup_line)
        return lines

    def _live_session_pids(self, account_num: str, email: str) -> list[int]:
        """PIDs of Claude instances running against an account's session profile.

        Scan-shaped: an unreadable record contributes no PID. Fine for the
        usage heuristics that read this; a destructive guard must use
        ``_ensure_no_live_session``, which asks the readability question too.
        """
        from openswap.session import scan_live_sessions

        sessions, _ = scan_live_sessions(self._session_dir(account_num, email))
        return [s.pid for s in sessions]

    def _ensure_no_live_session(self, account_num: str, email: str, action: str) -> None:
        """Refuse a destructive operation while a session-mode claude is live.

        "We could not read the records" refuses too, and says so in its own
        words. This gates ``_bootstrap`` (Keychain entry deleted,
        ``.credentials.json`` overwritten) and slot removal, so treating an
        unreadable record as an absent one runs them under a live instance.
        """
        from openswap.session import scan_live_sessions

        pids = self._live_session_pids(account_num, email)
        if pids:
            raise SessionError(
                f"Account-{account_num} ({email}) has a live session-mode Claude "
                f"instance (PID {', '.join(map(str, pids))}). "
                f"Exit it first, then retry {action}."
            )
        session_dir = self._session_dir(account_num, email)
        _, unreadable = scan_live_sessions(session_dir)
        if unreadable:
            raise SessionError(
                f"Account-{account_num} ({email}) has {unreadable} session "
                f"record(s) that could not be read, so whether a Claude "
                f"instance is live cannot be determined. Inspect "
                f"{session_dir / 'sessions'} and remove or repair them, then "
                f"retry {action}."
            )

    def _invalidate_session_credentials(self, account_num: str, email: str) -> None:
        """Drop a session profile's credential material, keeping its history.

        The next setup_session fails the reuse check and re-bootstraps from
        backup; the bootstrap merges .claude.json, so the profile's own
        projects/history survive. Used when backup credentials change under
        an existing profile (e.g. --import --force).
        """
        from openswap.session import (
            clear_session_stale,
            delete_macos_keychain_entry,
        )

        session_dir = self._session_dir(account_num, email)
        if not session_dir.exists():
            return
        delete_macos_keychain_entry(session_dir)
        (session_dir / ".credentials.json").unlink(missing_ok=True)
        clear_session_stale(session_dir)
        self._logger.info(
            f"Invalidated session credentials for account {account_num}"
        )

    def invalidate_session_credentials(self, account_num: str, email: str) -> None:
        """Public wrapper for session bootstrap."""
        self._invalidate_session_credentials(account_num, email)

    def _session_profile_ahead(
        self, account_num: str, email: str, org_uuid: str
    ) -> str | None:
        """The session profile's credential when it is a newer generation of
        this slot's family than the stored backup, else None.

        Claude rotates the token family inside a session profile and the
        backup never follows, so after any session that refreshed, the backup
        holds a consumed generation: activated, its first refresh gets
        invalid_grant. Generations are told apart by fingerprint and ordered
        by access-token issue time (``expiresAt``: every refresh, and every
        login, issues a token that expires later than the last), which also
        keeps a fresh re-login in the backup from reading as drift -- there
        the backup is the later one.

        Three shapes answer None outright: a profile that an in-session
        /login re-pointed at another account (a different family, not a
        newer generation of this one); a profile flagged stale (the backup
        moved under it while it was live, and openswap has already decided it
        re-bootstraps); and anything unreadable, because a read error is not
        evidence of drift.
        """
        from openswap.session import (
            is_session_stale,
            read_session_credentials,
            session_identity_drifted,
        )

        session_dir = self._session_dir(account_num, email)
        if is_session_stale(session_dir):
            return None
        profile = read_session_credentials(session_dir)
        if not profile or session_identity_drifted(session_dir, email, org_uuid):
            return None
        backup, unreadable = self._read_account_credentials_ex(account_num, email)
        if unreadable:
            return None
        if oauth.credential_fingerprint(profile) == oauth.credential_fingerprint(
            backup
        ):
            return None
        issued = oauth.extract_oauth_data(profile) or {}
        stored = oauth.extract_oauth_data(backup) or {}
        try:
            newer = float(issued.get("expiresAt") or 0) > float(
                stored.get("expiresAt") or 0
            )
        except (TypeError, ValueError):
            return None
        return profile if newer else None

    def _adopt_session_credential(
        self, account_num: str, email: str, org_uuid: str
    ) -> bool:
        """Capture a quiescent session profile's credential into the slot backup.

        The complement of ``_post_backup_write``: that direction invalidates
        a profile when the backup moves, this one advances the backup when
        the profile did. Without it a drifted backup is a landmine -- a
        switch activates a consumed generation whose first refresh gets
        invalid_grant, and the collector's backup path POSTs that dead grant
        and strikes a healthy slot -- and nothing ever defuses it, because
        the profile keeps passing the local reuse check and is never
        re-bootstrapped.

        Only while the profile is quiescent: a live claude is rotating that
        family and owns it. Decided and written under openswap's own lock, since
        ``_bootstrap`` and the consume gate's persist move the same two
        copies. The store write is deliberately the pure one:
        ``_post_backup_write`` would invalidate the very profile just
        captured, and the two now hold the same generation. Returns whether
        the backup was advanced.
        """
        from openswap.session import profile_is_quiescent

        session_dir = self._session_dir(account_num, email)
        with AccountLeaseStore(self.backup_dir, "claude").mutation_guard() as lease_guard:
            lease_guard.assert_unleased()
            if not profile_is_quiescent(session_dir):
                return False
            profile = self._session_profile_ahead(account_num, email, org_uuid)
            if profile is None:
                return False
            self._store._write_account_credentials(account_num, email, profile)
        self._logger.info(
            f"Adopted account {account_num}'s session profile credential "
            "into its backup"
        )
        return True

    def _delete_session_profile(self, account_num: str, email: str) -> None:
        """Remove an account's session profile dir and its keychain entry.

        Keychain first: the hashed service name is derived from the dir path
        and can't be recomputed once the dir is gone.

        The stale marker is a SIBLING of the dir, so ``rmtree`` does not take
        it: clear it explicitly, or the next profile created for this same
        slot+email inherits a re-bootstrap flag nothing set for it.
        """
        from openswap.session import (
            clear_session_stale,
            delete_macos_keychain_entry,
        )

        session_dir = self._session_dir(account_num, email)
        if session_dir.exists():
            delete_macos_keychain_entry(session_dir)
            shutil.rmtree(session_dir, ignore_errors=True)
        # NOT under that `if`. The marker lives OUTSIDE the dir, so it
        # outlives it: `purge` removes profile dirs (`iterdir()` + `is_dir()`)
        # and leaves the dot-file beside them by design. Early-returning on
        # the missing dir left that marker for the next profile in this slot,
        # which then re-bootstraps on a flag nothing set for it.
        cleared = clear_session_stale(session_dir)
        if session_dir.exists() or not cleared:
            # Both removals tolerate a denied dir, which is right -- the
            # caller has already deleted the credentials and must reach the
            # roster write. But it arrives there with the profile still on
            # disk, so an INFO saying it was removed is the only record, and
            # it is wrong. The surviving stale marker also makes the next
            # run's stale arm re-fire on this slot forever.
            self._logger.warning(
                "Could not fully remove account %s's session profile at %s; "
                "credentials are gone but the profile dir and/or its stale "
                "marker survive (check permissions on it and its parent).",
                account_num, session_dir,
            )
            return
        self._logger.info(
            f"Removed session profile for account {account_num} at {session_dir}"
        )
