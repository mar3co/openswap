"""OpenSwap account engine: Roster (sequence.json), backup Keychain, aliases, disabled flags, slot numbers."""

from __future__ import annotations

from openswap.engine.notes import *  # noqa: F403
from openswap.worker.leases import AccountLeaseStore

class SlotsMixin:
    """Roster (sequence.json), backup Keychain, aliases, disabled flags, slot numbers."""

    def _setup_directories(self) -> None:
        """Create backup directories with proper permissions."""
        for directory in [self.backup_dir, self.configs_dir, self.credentials_dir]:
            directory.mkdir(parents=True, exist_ok=True)
            if sys.platform != "win32":
                os.chmod(directory, 0o700)

    def _uses_file_backup_backend(self) -> bool:
        return self._store._uses_file_backup_backend()

    def _backup_enc_path(self, account_num: str, email: str) -> Path:
        return self._store._backup_enc_path(account_num, email)

    def _write_backup_enc(self, account_num: str, email: str, credentials: str) -> None:
        self._store._write_backup_enc(account_num, email, credentials)

    def _kc_read_backup(self, account_num: str, email: str) -> str:
        return self._store._kc_read_backup(account_num, email)

    def _kc_write_backup(self, account_num: str, email: str, credentials: str) -> None:
        self._store._kc_write_backup(account_num, email, credentials)

    def _delete_backup_keychain_quiet(self, account_num: str, email: str) -> None:
        self._store._delete_backup_keychain_quiet(account_num, email)

    def _post_backup_write(self, account_num: str, email: str) -> None:
        """Invalidate the slot's session profile after backup credentials change.

        Backup credentials changed (re-login via --add-account, --add-token,
        import, switch backing up, or a usage-refresh rotation): a session profile
        seeded from the old credentials may now hold a stale or rotated-out token
        that still passes the local reuse check. Drop the profile's credential
        material so the next setup_session / kickoff re-bootstraps from this fresh backup
        (history is preserved). A LIVE session keeps its own copy untouched — claude
        manages it; pulling credentials out from under a running process would be
        worse than the drift caveat — but gets a stale marker so setup_session
        re-bootstraps it once it is no longer live.
        """
        if self._live_session_pids(account_num, email):
            from openswap.session import mark_session_stale

            if not mark_session_stale(self._session_dir(account_num, email)):
                self._logger.error(
                    "Account %s's backup credentials changed but its live "
                    "session profile could not be marked stale; it may keep "
                    "serving the superseded generation once it exits.",
                    account_num,
                )
        else:
            self._invalidate_session_credentials(account_num, email)

    def _read_account_credentials(self, account_num: str, email: str) -> str:
        return self._store._read_account_credentials(account_num, email)

    def _write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Write account credentials to backup, then invalidate the slot's session.

        The store performs the pure write and raises on failure *before* returning,
        so ``_post_backup_write`` (the session-invalidation chokepoint) runs exactly
        once and only after a successful write.

        PAST THE STORE WRITE, NOTHING MAY RAISE. The write ADVANCES the slot,
        and every caller reads an exception from this method as "the persist
        failed" — so a raise here reports a failure for a slot that holds the
        new credential. At the post-POST call site that is worse than losing
        the invalidation: the grant is already spent, the handler stashes a
        "successor" byte-identical to what the store now holds, and a
        successful refresh is demoted to ``transient`` (measured: the tick then
        emits "could not freshen any candidate (network?)" forever over a
        healthy slot, and setup_session prints "Could not refresh the token").

        So the invalidation is contained, and its failure LEAVES THE MARKER
        instead. That is not a downgrade: a profile whose access token is still
        unexpired passes the local reuse check, so simply skipping the
        invalidation would let it keep serving a superseded generation until it
        expires. ``STALE_MARKER`` is what forces the re-bootstrap regardless,
        and it is the same mechanism the live-session branch already relies on.

        ``OSError``, not ``Exception``. EACCES on the session dir and a
        read-only mount is the whole fault list above, and both are
        ``OSError``; the suite's own real-store guard is deliberately NOT an
        ``OSError`` subclass (``tests/conftest.py``) so that no containment in
        this codebase can hide a write into the REAL store. Widening this to
        ``Exception`` disarmed exactly that guard for every write routing
        through here.
        """
        self._store._write_account_credentials(account_num, email, credentials)
        try:
            self._post_backup_write(account_num, email)
        except OSError:
            from openswap.session import mark_session_stale

            if mark_session_stale(self._session_dir(account_num, email)):
                self._logger.warning(
                    "Stored account %s's credential but could not invalidate "
                    "its session profile; marked it stale so the next run "
                    "re-bootstraps.", account_num, exc_info=True,
                )
            else:
                # Nothing recorded the superseded profile: the marker is what
                # forces the re-bootstrap, and the local reuse check cannot
                # see a revoked-but-unexpired token. Say so at ERROR rather
                # than let a silent warning imply the fallback worked.
                self._logger.error(
                    "Stored account %s's credential but could NOT invalidate "
                    "its session profile OR mark it stale; the profile may "
                    "keep serving the superseded generation until its token "
                    "expires.", account_num, exc_info=True,
                )

    def _delete_account_credentials(self, account_num: str, email: str) -> None:
        self._store._delete_account_credentials(account_num, email)

    def _delete_account_credentials_strict(self, account_num: str, email: str) -> None:
        """Pre-commit clear that raises when the key still reads non-empty."""
        self._store.delete_account_credentials_strict(account_num, email)

    def _delete_account_files(self, account_num: str, email: str) -> None:
        """Delete all backup files for an account (credentials + config).

        Single chokepoint for every path that removes or displaces a slot
        (remove_account, add_account/add_token slot overwrite & migration):
        refuses while a session-mode claude is live against the slot, and
        removes the slot's session profile alongside the backups so a stale
        profile can never outlive its account.

        Raises:
            SessionError: a live session-mode instance is using this account.
        """
        self._ensure_no_live_session(account_num, email, "the operation")
        self._delete_account_credentials(account_num, email)
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            config_file.unlink()
        self._delete_session_profile(account_num, email)

    def _prune_mappings(self, email: str, org_uuid: str) -> None:
        """Drop directory mappings for an identity that no longer has a slot.

        Called wherever an identity leaves the account table for good
        (remove_account, add_account/add_token slot overwrite). Slot
        *migration* and --import --force keep the (email, org) identity that
        mappings are keyed by, so they need no pruning.
        """
        from openswap.mappings import MappingStore

        pruned = MappingStore(self.backup_dir).prune_account(email, org_uuid or "")
        if pruned:
            print(dimmed(f"Removed {pruned} directory mapping(s) for this account"))

    def _read_account_config(self, account_num: str, email: str) -> str:
        """Read account config from backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        if config_file.exists():
            return config_file.read_text(encoding="utf-8")
        return ""

    def _account_is_switchable(self, account_num: str) -> bool:
        """Whether a slot has both stored credentials and config backups.

        Used by switch() and switch_to() to decide whether a target slot can
        be activated without re-adding the account. Tolerates stale sequence
        entries that reference a removed account record.
        """
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(str(account_num))
        if not record:
            return False
        email = record.get("email", "")
        if not self._read_account_credentials(str(account_num), email):
            return False
        if not self._read_account_config(str(account_num), email):
            return False
        return True

    def _roster_has_config(self, account_num: str) -> bool:
        """Whether a slot has a config backup, without reading Keychain creds.

        Store-only snapshot uses this for ``switchable`` so paint does not
        ``security``-read idle backups.
        """
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(str(account_num))
        if not record:
            return False
        email = record.get("email", "")
        return bool(self._read_account_config(str(account_num), email))

    def _write_account_config(
        self, account_num: str, email: str, config: str
    ) -> None:
        """Write account config to backup."""
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.write_text(config, encoding="utf-8")
        if sys.platform != "win32":
            os.chmod(config_file, 0o600)

    def set_alias(self, identifier: str, alias: str) -> tuple[str, str]:
        """Set (or rename) the alias for the account matching identifier.

        ``identifier`` is a slot number, email, or existing alias (so a
        typo'd alias can be corrected with ``openswap alias <old> <new>`` as
        well as by number/email). Returns ``(account_num, normalized_alias)``.

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
            ValidationError: alias format is invalid.
            ConfigError: the normalized alias is already used by another account.
        """
        self._refuse_session_shell()
        try:
            normalized = normalize_alias(alias)
        except ValueError as e:
            raise ValidationError(str(e)) from e

        with FileLock(self.lock_file):
            self._get_sequence_data_migrated()
            account_num = self._resolve_account_identifier(identifier)
            if not account_num:
                raise AccountNotFoundError(
                    f"No account found with identifier: {identifier}"
                )
            data = self._get_sequence_data() or {}
            record = data.get("accounts", {}).get(account_num)
            if not record:
                raise AccountNotFoundError(f"Account-{account_num} does not exist")

            conflict = self._alias_in_use(normalized, exclude_num=account_num)
            if conflict is not None:
                raise ConfigError(
                    f"Alias '{normalized}' is already used by account {conflict}"
                )

            record["alias"] = normalized
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
            return account_num, normalized

    def unset_alias(self, identifier: str) -> str:
        """Clear the alias for the account matching identifier.

        Returns the account number. Idempotent: clearing an already-unset
        alias succeeds silently (no error), matching ``openswap config unset``'s
        posture of "the end state is what you asked for".

        Raises:
            AccountNotFoundError: identifier doesn't match any account.
        """
        self._refuse_session_shell()
        with FileLock(self.lock_file):
            self._get_sequence_data_migrated()
            account_num = self._resolve_account_identifier(identifier)
            if not account_num:
                raise AccountNotFoundError(
                    f"No account found with identifier: {identifier}"
                )
            data = self._get_sequence_data() or {}
            record = data.get("accounts", {}).get(account_num)
            if not record:
                raise AccountNotFoundError(f"Account-{account_num} does not exist")

            if "alias" in record:
                del record["alias"]
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)
            return account_num

    def list_aliases(self) -> list[tuple[str, str, str]]:
        """Every set alias as ``(account_num, alias, email)``, slot-number order."""
        data = self._get_sequence_data_migrated()
        accounts = (data or {}).get("accounts", {})
        rows = [
            (num, acc.get("alias"), acc.get("email", ""))
            for num, acc in accounts.items()
            if acc.get("alias")
        ]
        return sorted(rows, key=lambda r: int(r[0]))

    def swap_accounts(self, first: str, second: str) -> tuple[str, str]:
        """Exchange two accounts' slot numbers (list order / numeric targets).

        Everything keyed by the slot number moves with the swap: the
        sequence records (including aliases, which belong to the account),
        the per-slot credential and config backups, membership in
        ``sequence`` (kept sorted, so rotation and ``openswap list`` order
        follow the new numbers), ``activeAccountNumber``, and each slot's
        session profile directory (history preserved). Directory mappings key on
        (email, org) and are unaffected. Usage-cache rows key on the slot
        number but carry the account identity, so a swapped row fails the
        identity check and self-heals on the next poll. Auto-switch
        quarantine entries also key on the slot number and are not moved,
        but self-heal on the next pass: the stale entry fails its
        email/fingerprint check and is released, and a dead account under
        its new number is re-caught by freshen-before-activate.

        The whole resolve-validate-mutate span runs under the account lock
        (like switch and the usage-refresh persist). The ``sequence.json``
        write is the commit point: a failure before it rolls both slots back
        (via durable staged copies when the backup keys overlap), and after
        it only best-effort cleanup of stale keys remains.

        Returns the two resolved slot numbers ``(first_num, second_num)``.
        """
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Local I/O only from here on, so the account lock can span the whole
        # resolve-validate-mutate sequence — a concurrent switch or usage-
        # refresh persist (which take the same lock) can never interleave
        # with the relocation.
        self._refuse_session_shell()
        with AccountLeaseStore(self.backup_dir, "claude").mutation_guard() as lease_guard:
            lease_guard.assert_available()
            return self._swap_accounts_locked(first, second)

    def _read_backup_or_abort(self, account_num: str, email: str) -> str:
        """Backup read for swap/move's pre-mutation snapshot; raises on an
        unreadable (not absent) backup.

        Nothing has moved yet at the call sites, so an unreadable verdict
        aborts here rather than committing a swap/move that silently drops
        the slot's live refresh token in favor of an empty destination.
        """
        creds, unreadable = self._read_account_credentials_ex(account_num, email)
        if unreadable:
            raise ConfigError(
                f"Account-{account_num}'s stored credential could not be "
                "read (keychain unavailable?); nothing was changed. Retry "
                "once it is readable again."
            )
        return creds

    def _swap_accounts_locked(self, first: str, second: str) -> tuple[str, str]:
        """Body of :meth:`swap_accounts`; the caller holds ``self.lock_file``.

        Split out so ``move_account`` can resolve identifiers and dispatch
        inside one lock acquisition (FileLock is non-reentrant): a slot
        number resolved outside the lock could be renumbered by a concurrent
        swap/move and target the wrong account.
        """
        self._get_sequence_data_migrated()

        num_a = self._resolve_account_identifier(first)
        if not num_a:
            raise AccountNotFoundError(f"No account found with identifier: {first}")
        num_b = self._resolve_account_identifier(second)
        if not num_b:
            raise AccountNotFoundError(f"No account found with identifier: {second}")
        if num_a == num_b:
            raise ValidationError("Cannot swap an account with itself")

        data = self._get_sequence_data() or {}
        record_a = data.get("accounts", {}).get(num_a)
        record_b = data.get("accounts", {}).get(num_b)
        if not record_a:
            raise AccountNotFoundError(f"Account-{num_a} does not exist")
        if not record_b:
            raise AccountNotFoundError(f"Account-{num_b} does not exist")

        email_a = record_a.get("email", "")
        email_b = record_b.get("email", "")

        # Backups and session profiles are keyed by (slot, email); relocating
        # them under a live session-mode claude would pull state out from
        # under a running process.
        self._ensure_no_live_session(num_a, email_a, "--swap-accounts")
        self._ensure_no_live_session(num_b, email_b, "--swap-accounts")

        # Read both slots' backup material up front so a read failure aborts
        # before anything has been moved. Missing material reads as "" (an
        # api-key or never-backed-up slot) and stays missing after the swap
        # — but the plain reader answers that same "" for a backup that
        # EXISTS and simply could not be read right now (locked Keychain,
        # a permission glitch). See _read_backup_or_abort.
        creds_a = self._read_backup_or_abort(num_a, email_a)
        creds_b = self._read_backup_or_abort(num_b, email_b)
        config_a = self._read_account_config(num_a, email_a)
        config_b = self._read_account_config(num_b, email_b)

        staging: dict[str, Path] = {}
        try:
            if email_a == email_b:
                # Same email: the two slots' backup keys fully overlap, so
                # every write below overwrites the other account's material.
                # Park durable copies first — a failure mid-write can then
                # never leave a credential existing only in this process's
                # memory. (Staging fails -> abort before anything changed.)
                staging = self._stage_overlap_material(
                    {num_a: (creds_a, config_a), num_b: (creds_b, config_b)}
                )

            # Move each session profile to its owner's new slot key. When both
            # accounts share an email the two paths swap directly, so stage the
            # first through a temporary name.
            self._swap_session_dirs(num_a, email_a, num_b, email_b)

            self._write_or_clear_slot_backup(num_b, email_a, creds_a, config_a)
            self._write_or_clear_slot_backup(num_a, email_b, creds_b, config_b)

            data["accounts"][num_a], data["accounts"][num_b] = record_b, record_a
            int_a, int_b = int(num_a), int(num_b)
            # Renumber, then sort: sequence is kept sorted everywhere (add
            # sorts on insert), so rotation and list order follow the new
            # slot numbers instead of preserving the old visual positions.
            data["sequence"] = [
                int_b if n == int_a else int_a if n == int_b else n
                for n in data.get("sequence", [])
            ]
            data["sequence"].sort()
            active = data.get("activeAccountNumber")
            if active == int_a:
                data["activeAccountNumber"] = int_b
            elif active == int_b:
                data["activeAccountNumber"] = int_a
            data["lastUpdated"] = get_timestamp()
            # The commit point: _write_json's rename publishes the swap.
            self._write_json(self.sequence_file, data)
        except BaseException:
            self._rollback_swap(
                num_a, email_a, creds_a, config_a,
                num_b, email_b, creds_b, config_b,
                staging,
            )
            raise

        # Post-commit cleanup, all best-effort: the records already reference
        # the new keys only. A failure here leaks a stale file, never a wrong
        # read — logged loudly because a stale key under a freed slot would
        # poison a future same-email account landing on that number.
        if email_a != email_b:
            for num, email in ((num_a, email_a), (num_b, email_b)):
                try:
                    self._delete_account_files(num, email)
                except Exception as e:
                    self._logger.error(
                        f"Stale backup left under old key {num} ({email}): {e}"
                    )
        # The .prev generations retained while writing the destination keys
        # hold the displaced material — another account's credential (or a
        # stale one) that recovery must never resurrect onto the key's new
        # owner. Cleared destinations already dropped theirs.
        if creds_a:
            self._store.delete_previous_backup(num_b, email_a)
        if creds_b:
            self._store.delete_previous_backup(num_a, email_b)
        self._discard_staging(staging)

        self._logger.info(
            f"Swapped slots: {num_a} ({email_a}) <-> {num_b} ({email_b})"
        )
        return num_a, num_b

    def _write_or_clear_slot_backup(
        self, account_num: str, email: str, creds: str, config: str
    ) -> None:
        """Set one slot key to this account's exact backup, or empty it.

        An empty source must not leave leftover material under the destination
        key (same-email overlap, or a stale file from an earlier crash).
        """
        if creds:
            self._write_account_credentials(account_num, email, creds)
        else:
            self._delete_account_credentials_strict(account_num, email)
        if config:
            self._write_account_config(account_num, email, config)
        else:
            self._delete_config_backup(account_num, email)

    def _delete_config_backup(self, account_num: str, email: str) -> None:
        """Delete one slot key's config backup file, if present.

        Unconditional unlink: ``exists()`` returns False on an inaccessible
        directory, which would fail open in the required-clear paths.
        Missing is fine (``missing_ok``); permission/I/O errors propagate —
        every caller either needs the abort (write-or-clear) or already
        wraps and counts the failure (rollback, stray cleanup).
        """
        config_file = self.configs_dir / f".claude-config-{account_num}-{email}.json"
        config_file.unlink(missing_ok=True)

    def _discard_staging(self, staging: dict[str, Path]) -> None:
        """Remove staged pre-swap copies, telling the user about survivors.

        A staging file that cannot be removed holds plaintext credentials, so
        a silent leak is not acceptable — and a leftover also blocks the next
        same-email swap (staging refuses to overwrite existing files).
        """
        for path in staging.values():
            try:
                path.unlink()
            except OSError as e:
                self._logger.error(f"Could not remove swap staging copy: {e}")
                warning(
                    f"Could not remove swap staging file {path} — it holds "
                    f"pre-swap credentials; please delete it manually."
                )

    def _stage_overlap_material(
        self, material: dict[str, tuple[str, str]]
    ) -> dict[str, Path]:
        """Park slots' backup material in temp files before overlapping writes.

        Used by same-email swaps, where each slot's write destroys the other
        slot's stored material. File-based on every platform — durability
        across a process death is the point, so the files (0600 from
        creation, in the credentials directory, normally alive for
        milliseconds) are created with ``O_EXCL`` and never overwrite an
        existing staging file: a leftover from an interrupted swap may be
        the only surviving copy of a credential, so the swap refuses and
        points at it instead of retrying over it. A failure *here* aborts
        the swap before anything has been overwritten.

        Deliberately NOT built: a manifest-based auto-recovery (a leftover
        cannot cheaply be told apart from post-commit cleanup residue, and
        restoring credentials on a wrong guess is worse than stopping), and
        Keychain-backed staging on macOS (the Keychain is the very backend
        whose mid-write failures this protects against).
        """
        staged: dict[str, Path] = {}
        try:
            for num, (creds, config) in material.items():
                for kind, content in (("creds", creds), ("config", config)):
                    if not content:
                        continue
                    path = self.credentials_dir / f".swap-staging-{kind}-{num}.json"
                    if path.exists():
                        raise ConfigError(
                            f"Found leftover staging from an interrupted swap: "
                            f"{path}. It holds that slot's pre-swap credentials "
                            f"and may be the only surviving copy. Verify both "
                            f"accounts still work (`openswap list`), then delete "
                            f"the file and retry."
                        )
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    staged[f"{kind}-{num}"] = path
        except ConfigError:
            # Leftover found: remove only what THIS call created.
            self._discard_staging(staged)
            raise
        except OSError as e:
            self._discard_staging(staged)
            raise ConfigError(
                f"Could not stage swap material, nothing was changed: {e}"
            )
        return staged

    def _swap_session_dirs(
        self, num_a: str, email_a: str, num_b: str, email_b: str
    ) -> None:
        """Exchange two slots' session profile directories, best effort.

        A profile that cannot be moved is not rescued: the caller prunes the
        old slot keys afterwards (``_delete_account_files``, which removes
        session profiles too), and setup_session re-bootstraps a missing
        profile from the relocated backups, so a skipped move costs at most
        that slot's session history.
        """
        dir_a = self._session_dir(num_a, email_a)
        dir_b = self._session_dir(num_b, email_b)
        new_a = self._session_dir(num_b, email_a)  # account A's new home
        new_b = self._session_dir(num_a, email_b)  # account B's new home

        staging = None
        try:
            if dir_a.exists():
                staging = dir_a.with_name(dir_a.name + ".swapping")
                os.replace(dir_a, staging)
            if dir_b.exists() and not new_b.exists():
                os.replace(dir_b, new_b)
            if staging is not None and not new_a.exists():
                os.replace(staging, new_a)
                staging = None
        except OSError as e:
            self._logger.warning(f"Session profile move skipped during swap: {e}")
        finally:
            if staging is not None:
                # Never strand a profile under the staging name.
                try:
                    if not dir_a.exists():
                        os.replace(staging, dir_a)
                except OSError:
                    pass

    def _rollback_swap(
        self,
        num_a: str,
        email_a: str,
        creds_a: str,
        config_a: str,
        num_b: str,
        email_b: str,
        creds_b: str,
        config_b: str,
        staging: dict[str, "Path"],
    ) -> None:
        """Best-effort restore of both slots after a failed swap mutation.

        Runs only before the metadata commit, so restoring means putting the
        *old* keys back. Matters most when the two accounts share an email:
        their backup keys fully overlap, so a half-written swap has already
        overwritten one account's material — and a key whose original was
        empty must go back to empty rather than keep the other account's
        credential. Every step is attempted independently; if any fails, the
        staged pre-swap copies are kept on disk for manual recovery instead
        of being deleted.
        """
        self._logger.error(
            f"Swap {num_a} <-> {num_b} failed mid-write; restoring both slots"
        )
        failures = 0
        # Undo the session-profile exchange (same staging trick, reversed).
        self._swap_session_dirs(num_b, email_a, num_a, email_b)
        overlap = email_a == email_b
        for kind, num, email, original in (
            ("creds", num_a, email_a, creds_a),
            ("config", num_a, email_a, config_a),
            ("creds", num_b, email_b, creds_b),
            ("config", num_b, email_b, config_b),
        ):
            try:
                if original:
                    if kind == "creds":
                        self._write_account_credentials(num, email, original)
                    else:
                        self._write_account_config(num, email, original)
                elif overlap:
                    # The overlapping key may now hold the *other* account's
                    # material — an originally-empty slot must read empty
                    # again, not serve someone else's credential. Strict: a
                    # suppressed failure here must count as a failure, so
                    # the staged copies are kept and reported.
                    if kind == "creds":
                        self._delete_account_credentials_strict(num, email)
                    else:
                        self._delete_config_backup(num, email)
            except Exception as e:
                failures += 1
                self._logger.error(
                    f"Rollback {kind} restore failed for slot {num}: {e}"
                )
        if email_a != email_b:
            # Drop half-written copies under the new keys; the records still
            # point at the old slots. (When the emails match, the "new" keys
            # are the keys just restored — nothing stale exists.)
            for num, email in ((num_b, email_a), (num_a, email_b)):
                try:
                    self._delete_account_credentials(num, email)
                    self._delete_config_backup(num, email)
                except Exception as e:
                    failures += 1
                    self._logger.error(f"Rollback cleanup failed for slot {num}: {e}")
        if not failures:
            # The restore writes above pushed the half-written material into
            # the keys' retained .prev generations; both keys now hold their
            # exact originals, so those generations are pure contamination.
            # (On a partial rollback everything is left in place — maximum
            # material preserved for manual recovery.)
            for num, email, original in (
                (num_a, email_a, creds_a),
                (num_b, email_b, creds_b),
            ):
                if original:
                    self._store.delete_previous_backup(num, email)
        if staging:
            if failures:
                kept = ", ".join(str(p) for p in staging.values())
                self._logger.error(
                    f"Rollback incomplete — staged pre-swap copies kept for "
                    f"manual recovery: {kept}"
                )
                warning(
                    f"Swap rollback was incomplete; your pre-swap credentials "
                    f"are preserved in: {kept}"
                )
            else:
                self._discard_staging(staging)

    def move_account(self, account: str, target: str) -> tuple[str, str, bool]:
        """Assign ``account`` to slot number ``target`` (the general form of swap).

        ``account`` is any ``NUM|EMAIL|ALIAS``; ``target`` is the destination
        slot number. Three cases:

        - target is the account's current slot -> no-op.
        - target slot is empty -> the account is relocated there and its old
          slot is freed. ``swap`` cannot express this (it needs two accounts).
        - target slot is occupied -> the two accounts trade places, exactly
          like ``swap account <occupant>``; the displaced account takes the
          vacated slot, so nothing is ever lost.

        Slot numbers may be sparse (``remove`` leaves gaps, ``add`` grows from
        the max), so any positive number up to 99 — or the current highest
        slot, if a table already grew past that — is a legal target. The cap
        exists because ``add`` numbers from the max: a stray huge target would
        inflate every future account number.

        Returns ``(source_num, target_num, swapped)`` where ``swapped`` is True
        when an occupant was displaced.
        """
        self._refuse_session_shell()
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        target = target.strip()
        if not target.isdigit() or int(target) < 1:
            raise ValidationError(
                f"Target slot must be a positive slot number, got: {target!r} "
                f"(use `swap` to trade two accounts by identifier)"
            )
        target = str(int(target))  # normalize "01" -> "1"

        # Resolution and dispatch happen inside the same lock acquisition as
        # the mutation (via the *_locked helpers — FileLock is non-reentrant):
        # a slot number resolved outside the lock could be renumbered by a
        # concurrent swap/move and end up moving the wrong account.
        with AccountLeaseStore(self.backup_dir, "claude").mutation_guard() as lease_guard:
            lease_guard.assert_available()
            self._get_sequence_data_migrated()

            num_src = self._resolve_account_identifier(account)
            if not num_src:
                raise AccountNotFoundError(
                    f"No account found with identifier: {account}"
                )

            data = self._get_sequence_data() or {}
            if not data.get("accounts", {}).get(num_src):
                raise AccountNotFoundError(f"Account-{num_src} does not exist")

            # `add` numbers new accounts from the highest slot, so a stray huge
            # target would inflate every future account number.
            max_slot = max(
                (int(n) for n in data.get("accounts", {}) if n.isdigit()), default=0
            )
            cap = max(99, max_slot)
            if int(target) > cap:
                raise ValidationError(
                    f"Target slot {target} is out of range (1-{cap}): new accounts "
                    f"are numbered from the highest slot, so a large target would "
                    f"inflate future account numbers"
                )

            if num_src == target:
                return num_src, target, False

            if data.get("accounts", {}).get(target):
                # Occupied target: trade places, exactly `swap num_src target`.
                self._swap_accounts_locked(num_src, target)
                return num_src, target, True

            self._relocate_locked(num_src, target)
            return num_src, target, False

    def _relocate_locked(self, num_src: str, target: str) -> None:
        """Move one account from ``num_src`` to the empty slot ``target``.

        The caller holds ``self.lock_file``. The one-way counterpart of
        :meth:`_swap_accounts_locked`: everything keyed by the slot number
        (credential and config backups, session profile, membership in
        ``sequence`` — kept sorted — and ``activeAccountNumber``) follows the
        account to its new number, and ``num_src`` is left empty. The caller
        checks ``target`` is unoccupied; it is re-checked here as an
        invariant. No rollback is needed: the ``sequence.json`` write is the
        commit point — before it the old keys are untouched (strays under
        the target key are cleaned on failure), after it only best-effort
        cleanup of the old keys remains.
        """
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(num_src)
        if not record:
            raise AccountNotFoundError(f"Account-{num_src} does not exist")
        if data.get("accounts", {}).get(target):
            raise ValidationError(
                f"Slot {target} is already occupied — retry the move"
            )
        email = record.get("email", "")

        # Relocating backups/session under a live session-mode claude would
        # pull state out from under a running process.
        self._ensure_no_live_session(num_src, email, "--move-account")

        # Read backup material up front so a read failure aborts before any
        # move. Missing material reads as "" (api-key or never-backed-up
        # slot) — but the plain reader answers that same "" for a backup
        # that EXISTS and simply could not be read right now. See
        # _read_backup_or_abort.
        creds = self._read_backup_or_abort(num_src, email)
        config = self._read_account_config(num_src, email)

        src_dir = self._session_dir(num_src, email)
        dst_dir = self._session_dir(target, email)
        try:
            # Move the session profile to the account's new slot key, best
            # effort: a profile that cannot be moved is pruned below with the
            # old slot's backups, and setup_session re-bootstraps a missing
            # one from the relocated backups — a skipped move costs at most
            # this slot's history.
            if src_dir.exists() and not dst_dir.exists():
                try:
                    os.replace(src_dir, dst_dir)
                except OSError as e:
                    self._logger.warning(
                        f"Session profile move skipped during move: {e}"
                    )

            self._write_or_clear_slot_backup(target, email, creds, config)

            data["accounts"][target] = record
            del data["accounts"][num_src]
            int_src, int_target = int(num_src), int(target)
            # Renumber, then sort: sequence is kept sorted everywhere (add
            # sorts on insert), so rotation and list order follow the new
            # slot number.
            data["sequence"] = [
                int_target if n == int_src else n for n in data.get("sequence", [])
            ]
            data["sequence"].sort()
            if data.get("activeAccountNumber") == int_src:
                data["activeAccountNumber"] = int_target
            data["lastUpdated"] = get_timestamp()
            # The commit point: _write_json's rename publishes the move.
            self._write_json(self.sequence_file, data)
        except BaseException:
            # Pre-commit failure: the records still point at num_src and its
            # keys are untouched — drop any strays written under the target
            # key and put the session profile back, best effort.
            try:
                self._delete_account_credentials(target, email)
                self._delete_config_backup(target, email)
                if dst_dir.exists() and not src_dir.exists():
                    os.replace(dst_dir, src_dir)
            except Exception as e:
                self._logger.error(f"Cleanup after failed move incomplete: {e}")
            raise

        # Post-commit: clear the old keys, best effort — the records now
        # reference the target slot only. _delete_account_files drops the
        # stale (num_src, email) backups and whatever session profile is
        # still under the old key (nothing, unless the move above was
        # skipped). A failure leaks a stale backup under the freed number
        # (logged loudly: it would poison a future same-email account
        # landing on that slot).
        try:
            self._delete_account_files(num_src, email)
        except Exception as e:
            self._logger.error(
                f"Stale backup left under old key {num_src} ({email}): {e}"
            )
        if creds:
            # Any .prev retained while overwriting a stale target key holds
            # that stale material, not this account's history.
            self._store.delete_previous_backup(target, email)

        self._logger.info(f"Moved slot: {num_src} ({email}) -> {target}")

    def slot_for_directory(self, directory: str | Path) -> tuple[str | None, str | None]:
        """Resolve a directory to its mapped account slot (leftover mappings.json).

        Returns (slot, email): (None, None) when no mapping covers the
        directory, (None, email) when a mapping exists but its account was
        removed, and (slot, email) when the mapping resolves.
        """
        from openswap.mappings import MappingStore

        match = MappingStore(self.backup_dir).resolve(directory)
        if match is None:
            return None, None
        _, entry = match
        email = entry.get("email", "")
        seq = self._get_sequence_data_migrated() or {}
        slot = self._find_account_slot(
            seq, email, entry.get("organizationUuid", "") or ""
        )
        return slot, email

    def read_account_credentials(self, account_num: str, email: str) -> str:
        """Public wrapper for session bootstrap. Empty string when missing."""
        return self._read_account_credentials(account_num, email)

    def read_backup_credentials(self, account_num: str, email: str) -> tuple[str, bool]:
        """Backup credentials plus unread flag. Empty+True is locked, not missing."""
        return self._read_account_credentials_ex(account_num, email)

    def write_account_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Public wrapper for session bootstrap.

        Takes NO lock: the caller is expected to hold ``self.lock_file``
        already. Never combine with the locking persist callback in
        list_accounts() — FileLock is not re-entrant across instances in one
        process (see the v0.7.3 deadlock history).
        """
        self._write_account_credentials(account_num, email, credentials)

    def read_account_config(self, account_num: str, email: str) -> str:
        """Public wrapper for session bootstrap. Empty string when missing."""
        return self._read_account_config(account_num, email)

    def switchable_account_numbers(self) -> list[str]:
        """Account numbers in rotation order eligible for automatic selection.

        Excludes slots without usable stored backups and slots the user has
        disabled (``openswap disable``). Disabled slots stay managed and remain
        valid explicit ``openswap switch <num|email>`` targets — they are only
        held out of automatic rotation and the usage-aware strategies.
        """
        data = self._get_sequence_data() or {}
        return [
            str(num)
            for num in data.get("sequence", [])
            if self._account_is_switchable(str(num))
            and not self._disabled_from_data(data, str(num))
        ]

    @staticmethod
    def _disabled_from_data(data: dict, account_num: str) -> bool:
        """Whether a slot is flagged out of rotation in already-loaded data."""
        record = data.get("accounts", {}).get(str(account_num))
        return bool(record and record.get("disabled"))

    def is_account_disabled(self, account_num: str) -> bool:
        """Whether a slot is currently held out of rotation."""
        data = self._get_sequence_data() or {}
        return self._disabled_from_data(data, str(account_num))

    def disabled_account_numbers(self) -> list[str]:
        """Managed slots the user has disabled, in sequence order."""
        data = self._get_sequence_data() or {}
        return [
            str(num)
            for num in data.get("sequence", [])
            if self._disabled_from_data(data, str(num))
        ]

    def set_account_disabled(self, identifier: str, disabled: bool) -> None:
        """Hold an account out of rotation (``disabled=True``) or return it.

        Disabling only affects automatic selection — the auto-switch engine,
        bare ``openswap switch`` rotation, and the ``best`` / ``next-available``
        strategies all skip disabled slots. The account stays managed and is
        still a valid explicit ``openswap switch <num|email>`` target, so you can
        park an account without losing its stored login. Re-enabling restores
        it to rotation in its original sequence position.

        Raises:
            ConfigError: no accounts are managed yet, or the email is ambiguous.
            AccountNotFoundError: identifier doesn't match any account.
        """
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        with FileLock(self.lock_file):
            # resolve_account migrates org fields and hard-errors on ambiguity.
            account_num, email, _ = self.resolve_account(identifier)

            data = self._get_sequence_data() or {}
            record = data.get("accounts", {}).get(account_num)
            if not record:
                raise AccountNotFoundError(f"Account-{account_num} does not exist")

            verb = "disabled" if disabled else "enabled"
            if bool(record.get("disabled")) == disabled:
                print(dimmed(f"Account-{account_num} ({email}) is already {verb}."))
                return

            if disabled:
                record["disabled"] = True
            else:
                record.pop("disabled", None)
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
            self._logger.info(f"{verb.capitalize()} account {account_num}: {email}")

        print(f"{accent(verb.capitalize())} Account-{account_num} ({email}).")

        if disabled:
            active = data.get("activeAccountNumber")
            if str(active) == account_num:
                print(dimmed(
                    "  It is the active account — it stays live until you switch "
                    "away; it just won't be an automatic switch target."
                ))
            if not self.switchable_account_numbers():
                warning(
                    "  No accounts remain in rotation — auto-switch and bare "
                    "switch have nothing to pick. Re-enable one with "
                    "openswap enable <num|email>."
                )
        else:
            print(dimmed("  It is back in the rotation."))

    def account_kind_for(self, account_num: str) -> str:
        """Public wrapper: ``"api_key"`` or ``"oauth"`` (setup-tokens read as oauth)."""
        return self._account_kind(account_num)

    def sequence_data(self) -> dict | None:
        """Public roster read of ``sequence.json``. None only when it does not exist."""
        return self._get_sequence_data()

    def account_email(self, account_num: str) -> str:
        """Stored email for a slot; empty string when unknown."""
        data = self._get_sequence_data() or {}
        return data.get("accounts", {}).get(str(account_num), {}).get("email", "")

    def persist_backup_credentials(
        self, account_num: str, email: str, credentials: str
    ) -> None:
        """Persist rotated credentials to a slot's backup store, under the lock.

        For inactive accounts only — never routes to the active store. Mirrors
        the persist callback ``_fetch_account_usage`` uses. The caller must NOT
        hold ``self.lock_file`` (FileLock is non-reentrant).
        """
        with FileLock(self.lock_file):
            self._write_account_credentials(account_num, email, credentials)

    def account_identity(self, account_num: str) -> dict:
        """Stored identity for a slot: ``{"email", "organizationUuid", "uuid"}``."""
        data = self._get_sequence_data() or {}
        acct = data.get("accounts", {}).get(str(account_num), {})
        return {
            "email": acct.get("email", ""),
            "organizationUuid": acct.get("organizationUuid", "") or "",
            "uuid": (acct.get("uuid") or "").strip(),
        }

    def backfill_account_uuid(
        self,
        account_num: str,
        uuid: str,
        expected_email: str | None = None,
        expected_org: str | None = None,
    ) -> None:
        """Record a resolved account uuid on a slot that lacks one.

        Only ever fills an empty uuid (add-token placeholders) — an existing
        uuid is identity and is never rewritten here. When ``expected_email``
        / ``expected_org`` are given, the fill additionally requires the slot
        to still hold that identity under the lock (a remove/re-add landing
        in the gap — even one keeping the email but changing the org — must
        not get the predecessor's uuid stamped on it). Caller must NOT hold
        ``self.lock_file``.
        """
        if not uuid:
            return
        with FileLock(self.lock_file):
            data = self._get_sequence_data() or {}
            acct = data.get("accounts", {}).get(str(account_num))
            if (
                acct is not None
                and not (acct.get("uuid") or "").strip()
                and (
                    expected_email is None
                    or acct.get("email") == expected_email
                )
                and (
                    expected_org is None
                    or (acct.get("organizationUuid", "") or "") == expected_org
                )
            ):
                acct["uuid"] = uuid
                data["lastUpdated"] = get_timestamp()
                self._write_json(self.sequence_file, data)

    def _read_account_credentials_ex(
        self, account_num: str, email: str
    ) -> tuple[str, bool]:
        return self._store._read_account_credentials_ex(account_num, email)

    def list_unclaimed_credentials(self) -> dict[str, dict]:
        """Internal safety copies preserved at switch time (diagnostics only).

        Write-only storage: entries are created when a switch displaces live
        credential bytes it could not attribute to the outgoing slot, and are
        never consumed automatically — recovery from any such state is the
        documented ``/login`` + ``openswap add [--slot N]``.
        """
        return self._store._list_unclaimed_credentials()

    def _init_sequence_file(self) -> None:
        """Initialize sequence.json if it doesn't exist."""
        if not self.sequence_file.exists():
            init_data = {
                "activeAccountNumber": None,
                "lastUpdated": get_timestamp(),
                "sequence": [],
                "accounts": {},
            }
            self._write_json(self.sequence_file, init_data)

    def _get_sequence_data(self) -> dict | None:
        """Get sequence data. None ONLY when the roster does not exist yet.

        `strict=True` because ~59 call sites read this and 27 of them write
        the result back through `or {}` — so a torn or unreadable
        `sequence.json` read as "no accounts" and the next write rebuilt the
        roster from nothing. Measured on a torn file with a resident slot 1:
        `add_account` collapsed `_get_next_account_number` to 1, overwrote the
        live credential backup at :2934, and THEN died at :2941 with a raw
        TypeError that `cli.py`'s `except ClaudeSwitchError` does not catch —
        so `--json` emitted no envelope at all.

            before  sha256:296e3
            raised  TypeError    is_ClaudeSwitchError=False
            after   sha256:6aabc    DESTROYED
            with strict: ConfigError, backup unchanged

        Guarding each caller was the alternative and it is 27 edits that the
        28th forgets. This is the reader; the distinction belongs here."""
        return self._read_json(self.sequence_file, strict=True)

    def _get_next_account_number(self) -> int:
        """Get next account number."""
        data = self._get_sequence_data()
        if not data or not data.get("accounts"):
            return 1

        account_nums = [int(k) for k in data["accounts"].keys()]
        return max(account_nums, default=0) + 1

    def _account_kind(self, account_num: str | None) -> str:
        """Stored kind for a managed slot: ``"api_key"`` or ``"oauth"`` (default).

        Slots added before this field existed have no ``kind`` and read as
        ``"oauth"`` (back-compat).
        """
        if account_num is None:
            return "oauth"
        data = self._get_sequence_data() or {}
        record = data.get("accounts", {}).get(str(account_num), {})
        return "api_key" if record.get("kind") == "api_key" else "oauth"

    def _find_account_by_alias(self, alias: str) -> str | None:
        """Return the account number whose alias matches (case-insensitive), if any.

        An empty ``alias`` never matches: accounts without one store no
        ``alias`` key, and comparing against an empty string would otherwise
        match the first aliasless account.
        """
        if not alias:
            return None
        data = self._get_sequence_data()
        if not data:
            return None
        alias_key = alias.lower()
        for num, account in data.get("accounts", {}).items():
            if (account.get("alias") or "").lower() == alias_key:
                return num
        return None

    def _alias_in_use(self, alias: str, *, exclude_num: str | None = None) -> str | None:
        """Return the account number already using ``alias`` (other than ``exclude_num``), if any."""
        num = self._find_account_by_alias(alias)
        if num is not None and num == exclude_num:
            return None
        return num

    def _get_sequence_data_migrated(self) -> dict | None:
        """Get sequence data, ensuring org-field migration has run."""
        data = self._get_sequence_data()
        if not data:
            return data
        needs_migration = any(
            "organizationUuid" not in acc
            for acc in data.get("accounts", {}).values()
        )
        if needs_migration:
            self._migrate_org_fields()
            data = self._get_sequence_data()  # Re-read after migration
        return data

    def _migrate_org_fields(self) -> None:
        """Backfill organizationUuid/Name for accounts added before org support.

        For the currently active account, reads org info from the live config
        (which is authoritative). For inactive accounts, falls back to backup
        configs. Writes updated fields back to sequence.json.
        """
        data = self._get_sequence_data()
        if not data:
            return

        # Read live config for the currently active account
        live_email = ""
        live_org_uuid = ""
        live_org_name = ""
        config_path = self._get_claude_config_path()
        if config_path.exists():
            try:
                config_data = self._read_json(config_path)
                if config_data:
                    oauth = config_data.get("oauthAccount", {})
                    live_email = oauth.get("emailAddress", "")
                    live_org_uuid = oauth.get("organizationUuid", "") or ""
                    live_org_name = oauth.get("organizationName", "") or ""
            except Exception:
                pass

        updated = False
        for num, account in data.get("accounts", {}).items():
            if "organizationUuid" in account:
                continue  # Already migrated

            email = account.get("email", "")

            # For the active account, prefer live config (backup may lack org fields)
            if email == live_email and live_email:
                account["organizationUuid"] = live_org_uuid
                account["organizationName"] = live_org_name
                updated = True
                continue

            # For inactive accounts, fall back to backup config
            config_text = self._read_account_config(num, email)
            if config_text:
                try:
                    config_data = json.loads(config_text)
                    oauth = config_data.get("oauthAccount", {})
                    account["organizationUuid"] = oauth.get("organizationUuid", "") or ""
                    account["organizationName"] = oauth.get("organizationName", "") or ""
                except (json.JSONDecodeError, AttributeError):
                    account["organizationUuid"] = ""
                    account["organizationName"] = ""
            else:
                account["organizationUuid"] = ""
                account["organizationName"] = ""
            updated = True

        if updated:
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)

    def remove_account(self, identifier: str, assume_yes: bool = False) -> None:
        """Remove account from managed accounts.

        When ``assume_yes`` is True the confirmation prompt is skipped (used by
        the extra, which collects confirmation before calling).
        """
        self._refuse_session_shell()
        if not self.sequence_file.exists():
            raise ConfigError("No accounts are managed yet")

        # Ensure org fields are migrated before resolving accounts
        self._get_sequence_data_migrated()

        # Resolve identifier
        if not identifier.isdigit():
            is_alias = self._find_account_by_alias(identifier) is not None
            if not is_alias and not self._validate_email(identifier):
                raise ValidationError(f"Invalid account identifier: {identifier}")

            # For email identifiers, handle ambiguous matches interactively.
            # Aliases are unique by construction, so they never hit this.
            if not is_alias:
                data = self._get_sequence_data()
                matches = [
                    num for num, acc in (data or {}).get("accounts", {}).items()
                    if acc.get("email") == identifier
                ]
                if len(matches) > 1:
                    print(f"Multiple accounts found for '{identifier}':")
                    for num in matches:
                        acc = data["accounts"][num]
                        tag = self._get_display_tag(
                            acc.get("email", ""),
                            acc.get("organizationName", ""),
                            acc.get("organizationUuid", ""),
                        )
                        print(f"  {num}: {identifier} {muted(f'[{tag}]')}")
                    choice = input("Enter account number to remove: ").strip()
                    if not choice.isdigit() or choice not in matches:
                        print(dimmed("Cancelled"))
                        return
                    identifier = choice

        account_num = self._resolve_account_identifier(identifier)
        if not account_num:
            raise AccountNotFoundError(
                f"No account found with identifier: {identifier}"
            )

        data = self._get_sequence_data()
        account_info = data.get("accounts", {}).get(account_num)

        if not account_info:
            raise AccountNotFoundError(f"Account-{account_num} does not exist")

        email = account_info.get("email")
        confirmed_org = account_info.get("organizationUuid", "") or ""
        active_account = data.get("activeAccountNumber")

        # Check before the confirmation prompt (better UX); the chokepoint in
        # _delete_account_files re-checks as a safety net for all paths.
        self._ensure_no_live_session(account_num, email, "--remove-account")

        if str(active_account) == account_num:
            warning(f"Warning: Account-{account_num} ({email}) is currently active")

        if not assume_yes:
            confirm = input(
                f"Are you sure you want to permanently remove "
                f"Account-{account_num} ({email})? [y/N] "
            )
            if confirm.lower() != "y":
                print(dimmed("Cancelled"))
                return

        with AccountLeaseStore(self.backup_dir, "claude").mutation_guard() as lease_guard:
            lease_guard.assert_available()
            data = self._get_sequence_data()
            account_info = (data or {}).get("accounts", {}).get(account_num)
            if not account_info:
                raise AccountNotFoundError(f"Account-{account_num} does not exist")
            live_email = account_info.get("email")
            live_org = account_info.get("organizationUuid", "") or ""
            if live_email != email or live_org != confirmed_org:
                raise AccountNotFoundError(
                    f"Account-{account_num} is no longer {email}; "
                    "it moved or was replaced. Nothing was removed."
                )
            email = live_email
            self._delete_account_files(account_num, email)
            del data["accounts"][account_num]
            data["sequence"] = [n for n in data["sequence"] if n != int(account_num)]
            data["lastUpdated"] = get_timestamp()
            self._write_json(self.sequence_file, data)
            self._logger.info(f"Removed account {account_num}: {email}")

        print(f"{accent('Removed')} Account-{account_num} ({email})")

        self._prune_mappings(email, account_info.get("organizationUuid", ""))
